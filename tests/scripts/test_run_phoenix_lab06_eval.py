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
    monkeypatch.setattr(MODULE, "evaluate_experiment", lambda **kw: {"evaluation_runs": []})
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
