from __future__ import annotations

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/run_phoenix_lab06_eval.py"
SPEC = spec_from_file_location("run_phoenix_lab06_eval", SCRIPT)
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def settings():
    return MODULE.Settings(_env_file=None, dify_base_url="http://test/v1",
                           dify_lab06_api_key="test-key")


def test_task_uses_new_conversation_and_records_raw_usage_and_missing_resources(tmp_path, monkeypatch):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"answer": "答案", "conversation_id": "new-conversation",
                                        "metadata": {"usage": {"total_tokens": 42}}})

    client = SimpleNamespace(spans=SimpleNamespace(get_spans=lambda **kw: []))
    monkeypatch.setattr(MODULE, "original_trace", lambda *a: ("", "not_observed"))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        task = MODULE.make_task(settings(), http, client, "Lab 06", tmp_path / "journal.jsonl")
        output = task({"input": {"case_id": "L06-001", "query": "问题"}})
    assert "conversation_id" not in requests[0]
    assert output["api_reported_usage"]["total_tokens"] == 42
    assert output["retrieved_resources"] is None
    assert json.loads((tmp_path / "journal.jsonl").read_text())["answer"] == "答案"


def test_http_timeout_is_recorded_without_retry_or_sensitive_error(tmp_path):
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive-upstream-error", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as http:
        task = MODULE.make_task(settings(), http, object(), "Lab 06", tmp_path / "log.jsonl")
        result = task({"input": {"case_id": "L06-001", "query": "问题"}})
    assert len(calls) == 1
    assert result["run_error"] == "ReadTimeout"
    assert "sensitive" not in (tmp_path / "log.jsonl").read_text()


def test_preflight_does_not_call_phoenix_or_dify(monkeypatch, tmp_path):
    manifest = {"release_id": "baseline", "app_id": "app", "agent_model": "model",
                "answer_model": "model", "agent_prompt_revision": "v1",
                "answer_prompt_revision": "v0", "query_template_revision": "query-v1", "knowledge_revision": "v1",
                "retrieval": {}, "max_iterations": 4,
                "tool_names": ["weather_forecast", "poi_search"],
                "memory": False, "p95_budget_s": 120}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "run", "--dataset-version", "version",
                                    "--manifest", str(path), "--output", str(tmp_path / "out.json"),
                                    "--dry-run"])
    configured = settings()
    monkeypatch.setattr(MODULE, "Settings", lambda: configured)
    monkeypatch.setattr(MODULE, "phoenix_client", lambda *a: pytest.fail("unexpected network"))
    assert MODULE.main() == 0
    assert not (tmp_path / "out.json").exists()


def test_rejudge_does_not_call_dify_and_keeps_old_report(monkeypatch, tmp_path):
    old = {"experiment_id": "exp", "judge_revision": "v1", "task_runs": []}
    source, destination = tmp_path / "old.json", tmp_path / "new.json"
    source.write_text(json.dumps(old))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "rejudge", "--report", str(source),
                                    "--output", str(destination)])
    monkeypatch.setattr(MODULE, "build_judge", lambda *a: ("judge2", object()))
    client = SimpleNamespace(experiments=SimpleNamespace(get_experiment=lambda **kw: old))
    monkeypatch.setattr(MODULE, "phoenix_client", lambda *a: client)
    monkeypatch.setattr(MODULE, "evaluate_concurrently", lambda *a: {"evaluation_runs": []})
    monkeypatch.setattr(MODULE, "make_task", lambda *a: pytest.fail("Dify rerun"))
    assert MODULE.main() == 0
    assert json.loads(source.read_text())["judge_revision"] == "v1"
    assert json.loads(destination.read_text())["judge_revision"] == "judge2"


def test_phoenix_scores_remain_structured_in_json(tmp_path):
    from dataclasses import make_dataclass

    Score = make_dataclass("Score", [("name", str), ("result", dict)])
    path = tmp_path / "scores.json"
    MODULE.write_json(path, [Score("judge", {"score": None, "label": "review"})])
    assert json.loads(path.read_text()) == [{"name": "judge", "result": {"score": None, "label": "review"}}]


def test_phoenix_task_failure_remains_in_report_and_review_template():
    experiment = {"task_runs": [{"id": "run", "dataset_example_id": "example", "repetition_number": 1,
                                 "output": None, "error": "timeout"}]}
    dataset = SimpleNamespace(examples=[{"id": "example", "input": {"case_id": "L06-001", "query": "q"}}])
    runs = MODULE.report_runs(experiment, dataset)
    assert runs[0]["output"]["case_id"] == "L06-001"
    assert MODULE.response_available(runs[0]["output"])[0] == 0


def test_concurrent_evaluation_uses_async_sdk_and_configured_endpoint(monkeypatch):
    configured = MODULE.Settings(_env_file=None, phoenix_endpoint="http://phoenix.test",
                                 phoenix_api_key="test-only")
    clients = []

    def make_client(*, http_client):
        clients.append(http_client)
        return SimpleNamespace(http=http_client)

    async def evaluate(**kwargs):
        assert kwargs["concurrency"] == 4
        assert kwargs["retries"] == 0
        assert kwargs["timeout"] == 180
        assert kwargs["experiment"]["experiment_id"] == "saved"
        assert str(kwargs["client"].http.base_url) == "http://phoenix.test"
        assert kwargs["client"].http.headers["Authorization"] == "Bearer test-only"
        return {"evaluation_runs": ["scored"]}

    monkeypatch.setattr(MODULE, "AsyncClient", make_client)
    monkeypatch.setattr(MODULE, "async_evaluate_experiment", evaluate)
    monkeypatch.setattr(MODULE, "make_task", lambda *a: pytest.fail("Dify rerun"))
    result = MODULE.evaluate_concurrently(configured, {"experiment_id": "saved"}, {}, 4, 180)
    assert result["evaluation_runs"] == ["scored"]
    assert clients[0].is_closed


@pytest.mark.parametrize("value", ["0", "-1"])
def test_reject_invalid_eval_concurrency_before_network(monkeypatch, value):
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "rejudge", "--report", "unused.json",
                                    "--output", "unused-out.json", "--eval-concurrency", value])
    with pytest.raises(SystemExit) as error:
        MODULE.main()
    assert error.value.code == 2


def test_resume_uses_sdk_incomplete_evaluations_and_refreshes_results(monkeypatch):
    configured = MODULE.Settings(_env_file=None, phoenix_endpoint="http://phoenix.test")
    calls = []

    async def resume(**kwargs):
        calls.append(kwargs)

    async def get(**kwargs):
        assert kwargs == {"experiment_id": "saved"}
        return {"evaluation_runs": ["existing", "resumed"]}

    monkeypatch.setattr(MODULE, "AsyncClient", lambda **kw: SimpleNamespace(
        experiments=SimpleNamespace(resume_evaluation=resume, get_experiment=get)))
    monkeypatch.setattr(MODULE, "async_evaluate_experiment",
                        lambda **kw: pytest.fail("must not rescore all evaluations"))
    result = MODULE.evaluate_concurrently(configured, {"experiment_id": "saved"},
                                          {"quality": "judge"}, 1, 600, resume=True)
    assert calls == [{"experiment_id": "saved", "evaluators": {"quality": "judge"},
                      "concurrency": 1, "retries": 0, "timeout": 600}]
    assert result["evaluation_runs"] == ["existing", "resumed"]


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("changed_judge", [False, True])
def test_resume_preserves_report_and_tracks_pending_including_null_scores(
        monkeypatch, tmp_path, failed, changed_judge):
    import hashlib

    task = {"id": "run1", "output": {"case_id": "L06-001"}, "repetition_number": 1}
    report = {"experiment_id": "saved", "task_runs": [task], "judge_revision": "judge1",
              "evaluator_revision": hashlib.sha256(
                  (MODULE.ROOT / "src/training_eval/lab06.py").read_bytes()).hexdigest()}
    source, output = tmp_path / "original.json", tmp_path / "resumed.json"
    source.write_text(json.dumps(report))
    argv = [str(SCRIPT), "resume", "--report", str(source),
            "--output", str(output), "--eval-timeout", "600"]
    if changed_judge:
        argv.append("--allow-judge-change")
    monkeypatch.setattr("sys.argv", argv)
    judge_name = "judge2" if changed_judge else "judge1"
    monkeypatch.setattr(MODULE, "build_judge", lambda *a: (judge_name, object()))
    client = SimpleNamespace(experiments=SimpleNamespace(get_experiment=lambda **kw: report))
    monkeypatch.setattr(MODULE, "phoenix_client", lambda *a: client)
    monkeypatch.setattr(MODULE, "make_task", lambda *a: pytest.fail("Dify rerun"))

    def evaluate(settings, experiment, evaluators, concurrency, timeout, *, resume):
        assert resume and timeout == 600
        assert judge_name in evaluators
        rows = [{"experiment_run_id": "run1", "name": name, "error": None,
                 "result": {"score": None, "label": "review"}} for name in evaluators]
        if failed:
            rows[-1].update(error="timeout", result=None)
        return {"task_runs": [task], "evaluation_runs": rows}

    monkeypatch.setattr(MODULE, "evaluate_concurrently", evaluate)
    assert MODULE.main() == int(failed)
    assert json.loads(source.read_text()) == report
    result = json.loads(output.read_text())
    assert result["judge_revision"] == judge_name
    assert result["pending_evaluations"] == int(failed)
    assert result["status"] == ("evaluating" if failed else "completed")
    assert len(result["evaluation_runs"]) == 4


def test_resume_rejects_changed_judge_before_network(monkeypatch, tmp_path):
    import hashlib

    source = tmp_path / "report.json"
    source.write_text(json.dumps({"experiment_id": "saved", "task_runs": [{"id": "run1"}],
        "judge_revision": "old", "evaluator_revision": hashlib.sha256(
            (MODULE.ROOT / "src/training_eval/lab06.py").read_bytes()).hexdigest()}))
    monkeypatch.setattr("sys.argv", [str(SCRIPT), "resume", "--report", str(source),
                                    "--output", str(tmp_path / "out.json")])
    monkeypatch.setattr(MODULE, "build_judge", lambda *a: ("changed", object()))
    monkeypatch.setattr(MODULE, "phoenix_client", lambda *a: pytest.fail("unexpected network"))
    with pytest.raises(ValueError, match="Judge 配置与原实验不同"):
        MODULE.main()


def test_phoenix_reads_bypass_environment_proxy_and_use_short_http_timeout(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if "incomplete-evaluations" in self.path or self.path.endswith("/runs"):
                body = {"data": []}
            elif self.path.endswith("/json"):
                body = []
            else:
                body = {"data": {"id": "saved", "dataset_id": "dataset",
                                 "dataset_version_id": "version", "project_name": "evals"}}
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    configured = MODULE.Settings(_env_file=None,
        phoenix_endpoint=f"http://127.0.0.1:{server.server_port}")

    async def resume(**kwargs):
        # 执行阶段不调用 Judge，仅验证完成后的真实 SDK HTTP 读取。
        assert kwargs["timeout"] == 600

    from phoenix.client.resources.experiments import AsyncExperiments
    monkeypatch.setattr(AsyncExperiments, "resume_evaluation", lambda self, **kw: resume(**kw))
    try:
        sync = MODULE.phoenix_client(configured)
        assert sync.experiments.get_experiment(experiment_id="saved")["dataset_id"] == "dataset"
        result = MODULE.evaluate_concurrently(configured, {"experiment_id": "saved"},
                                              {"quality": object()}, 1, 600, resume=True)
        assert result["dataset_id"] == "dataset"
        assert requests.count("/v1/experiments/saved") == 2
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "test-model"])
def test_judge_thinking_mode_is_forwarded_and_versioned(monkeypatch, tmp_path, model):
    import asyncio

    prompt = tmp_path / "judge.txt"
    prompt.write_text("{{input}} {{expected}} {{output}}")
    configured = MODULE.Settings(_env_file=None, judge_model=model,
        judge_api_key="test-key", judge_base_url="http://judge.test/v1")
    name, evaluator = MODULE.build_judge(configured, prompt)
    parameters = ({"extra_body": {"thinking": {"type": "disabled"}}}
                  if model.startswith("deepseek-v4-") else {})

    async def classify(**kwargs):
        assert kwargs.get("extra_body") == parameters.get("extra_body")
        return {"label": "pass", "explanation": "ok"}

    monkeypatch.setattr(evaluator.llm, "async_generate_classification", classify)
    scores = asyncio.run(evaluator.async_evaluate({"input": "q", "expected": "a", "output": "a"}))
    assert scores[0].score == 1
    revision = {"prompt": prompt.read_text(), "model": model, "provider": configured.judge_base_url}
    if parameters:
        revision["invocation_parameters"] = parameters
    assert name == "task_constraints_" + MODULE.digest(revision)[:12]
