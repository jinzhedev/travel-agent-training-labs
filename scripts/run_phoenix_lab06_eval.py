"""Lab 06：准备评测集、运行真实 Chatflow、对已保存回答重新评分。"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from phoenix.client import AsyncClient, Client
from phoenix.client.experiments import async_evaluate_experiment, run_experiment
from phoenix.evals import LLM, ClassificationEvaluator
from pydantic_settings import BaseSettings, SettingsConfigDict

from training_eval.lab06 import (
    CASES,
    ROOT,
    build_examples,
    citation_sources,
    digest,
    evidence_coverage,
    read_jsonl,
    release_check,
    response_available,
    review_template,
    source_versions,
)

DEFAULT_DATASET = "lab06-tool-rag-v1"
DEFAULT_JUDGE = ROOT / "labs/06-evals-release/prompts/judge-v1.txt"
PHOENIX_HTTP_TIMEOUT = 30


class Settings(BaseSettings):
    dify_base_url: str = ""
    dify_lab06_api_key: str = ""
    phoenix_endpoint: str = ""
    phoenix_api_key: str = ""
    judge_api_key: str = ""
    judge_base_url: str = ""
    judge_model: str = ""
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")


def write_json(path: Path, value: Any) -> None:
    def encode(item):
        if dataclasses.is_dataclass(item):
            return dataclasses.asdict(item)
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        raise TypeError(f"无法序列化 {type(item).__name__}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=encode) + "\n")


def read_manifest(path: Path) -> dict:
    value = json.loads(path.read_text())
    for key in ("release_id", "app_id", "agent_model", "answer_model", "agent_prompt_revision",
                "answer_prompt_revision", "query_template_revision", "knowledge_revision", "retrieval", "max_iterations",
                "tool_names", "memory", "p95_budget_s"):
        if key not in value or value[key] is None or "<" in str(value[key]):
            raise ValueError(f"发布记录未填写：{key}")
    if value["memory"] is not False:
        raise ValueError("Lab 06 固定关闭 Memory")
    if set(value["tool_names"]) != {"weather_forecast", "poi_search"}:
        raise ValueError("Lab 06 只允许两个只读工具")
    return value


def build_judge(settings: Settings, prompt_path: Path) -> tuple[str, ClassificationEvaluator]:
    for key in ("judge_api_key", "judge_base_url", "judge_model"):
        if not getattr(settings, key).strip():
            raise ValueError(f"缺少 {key.upper()}")
    prompt = prompt_path.read_text()
    invocation_parameters = {}
    if settings.judge_model.startswith("deepseek-v4-"):
        # Phoenix 分类评分强制调用指定函数；DeepSeek V4 的思考模式拒绝该 tool_choice。
        invocation_parameters = {"extra_body": {"thinking": {"type": "disabled"}}}
    revision_data = {"prompt": prompt, "model": settings.judge_model,
                     "provider": settings.judge_base_url}
    if invocation_parameters:
        revision_data["invocation_parameters"] = invocation_parameters
    revision = digest(revision_data)[:12]
    name = f"task_constraints_{revision}"
    evaluator = ClassificationEvaluator(
        name=name,
        llm=LLM(provider="openai", model=settings.judge_model,
                sync_client_kwargs={"api_key": settings.judge_api_key,
                                    "base_url": settings.judge_base_url},
                async_client_kwargs={"api_key": settings.judge_api_key,
                                     "base_url": settings.judge_base_url}),
        prompt_template=prompt,
        **invocation_parameters,
        choices={"pass": (1.0, "最终建议满足本例约束"),
                 "fail": (0.0, "最终建议违反本例约束"),
                 "review": (None, "证据不足，需要人工复核")},
    )
    return name, evaluator


def original_trace(client: Client, project: str, conversation: str,
                   wait_seconds: float = 8) -> tuple[str, str]:
    if not conversation:
        return "", "API 未返回 conversation_id"
    try:
        deadline = time.monotonic() + wait_seconds
        while True:
            spans = client.spans.get_spans(
                project_identifier=project, attributes={"session.id": conversation}, limit=100,
            )
            roots = [s for s in spans if s["name"].startswith("chatflow_")
                     and s.get("attributes", {}).get("session.id") == conversation]
            if len(roots) == 1:
                return roots[0]["context"]["trace_id"], "matched_by_new_conversation"
            if len(roots) > 1 or time.monotonic() >= deadline:
                return "", "原始 trace 尚未到达或匹配不唯一；按 conversation_id 人工核对"
            time.sleep(1)
    except (httpx.HTTPError, KeyError, ValueError):
        return "", "Phoenix trace 查询不可用；保留 API 记录供人工核对"


def make_task(settings: Settings, http_client: httpx.Client, client: Client,
              project: str, journal: Path):
    base = settings.dify_base_url.rstrip("/")
    endpoint = base + ("/chat-messages" if base.endswith("/v1") else "/v1/chat-messages")

    def task(example: Any) -> dict:
        data = example["input"] if isinstance(example, dict) else example.input
        start = time.monotonic()
        output: dict[str, Any] = {"case_id": data["case_id"], "query": data["query"]}
        try:
            response = http_client.post(endpoint,
                headers={"Authorization": f"Bearer {settings.dify_lab06_api_key}"},
                json={"inputs": {}, "query": data["query"], "response_mode": "blocking",
                      "user": "lab06-eval"})
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Dify response must be an object")
            metadata = body.get("metadata") or {}
            output.update({
                "answer": body.get("answer") or "", "run_error": None,
                "message_id": body.get("message_id") or "",
                "conversation_id": body.get("conversation_id") or "",
                "retrieved_resources": metadata.get("retriever_resources"),
                "api_reported_usage": metadata.get("usage"),
            })
        except (httpx.HTTPError, ValueError) as exc:
            # 不输出服务响应正文，避免认证信息或供应商内部错误进入课堂报告。
            output.update({"answer": "", "run_error": type(exc).__name__,
                           "retrieved_resources": None, "api_reported_usage": None})
        output["elapsed_s"] = round(time.monotonic() - start, 3)
        # 先落盘，再做 trace 查询或 Phoenix 写入；日志保存到本地，不进入 Git。
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a") as file:
            file.write(json.dumps(output, ensure_ascii=False) + "\n")
        trace_id, note = original_trace(client, project, output.get("conversation_id", ""))
        output.update({"dify_trace_id": trace_id, "trace_note": note})
        return output

    return task


def phoenix_client(settings: Settings) -> Client:
    if not settings.phoenix_endpoint:
        raise ValueError("缺少 PHOENIX_ENDPOINT")
    headers = ({"Authorization": f"Bearer {settings.phoenix_api_key}"}
               if settings.phoenix_api_key else {})
    return Client(http_client=httpx.Client(
        base_url=settings.phoenix_endpoint, headers=headers,
        trust_env=False, timeout=PHOENIX_HTTP_TIMEOUT))


def evaluate_concurrently(settings: Settings, experiment: dict, evaluators: dict,
                          concurrency: int, timeout: int, *, resume: bool = False) -> dict:
    """只并发评分；使用已完成的 task runs，不重新调用 Dify。"""
    async def evaluate() -> dict:
        headers = ({"Authorization": f"Bearer {settings.phoenix_api_key}"}
                   if settings.phoenix_api_key else {})
        async with httpx.AsyncClient(base_url=settings.phoenix_endpoint,
                                     headers=headers, trust_env=False,
                                     timeout=PHOENIX_HTTP_TIMEOUT) as http_client:
            client = AsyncClient(http_client=http_client)
            if resume:
                await client.experiments.resume_evaluation(
                    experiment_id=experiment["experiment_id"], evaluators=evaluators,
                    concurrency=concurrency, retries=0, timeout=timeout)
                print("评分执行结束，正在从 Phoenix 读取结果并保存报告……", flush=True)
                return await client.experiments.get_experiment(
                    experiment_id=experiment["experiment_id"])
            return await async_evaluate_experiment(
                client=client, experiment=experiment, evaluators=evaluators,
                concurrency=concurrency, retries=0, timeout=timeout)

    return asyncio.run(evaluate())


def report_runs(experiment: dict, dataset) -> list[dict]:
    inputs = {e["id"]: e["input"] for e in dataset.examples}
    return [{"id": run["id"], "dataset_example_id": run["dataset_example_id"],
             "repetition_number": run["repetition_number"],
             "output": run.get("output") or {
                 **inputs.get(run["dataset_example_id"], {"case_id": "unknown"}),
                 "answer": "", "run_error": "experiment_task_error", "elapsed_s": None,
             },
             "error": run.get("error")}
            for run in experiment["task_runs"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="检查金标；--apply 才写入 Phoenix")
    prepare.add_argument("--cases", type=Path, default=CASES)
    prepare.add_argument("--dataset-name", default=DEFAULT_DATASET)
    prepare.add_argument("--apply", action="store_true")
    run = sub.add_parser("run", help="运行真实应用；--dry-run 只做本地预检")
    run.add_argument("--dataset-name", default=DEFAULT_DATASET)
    run.add_argument("--dataset-version", required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--repetitions", type=int, default=3)
    run.add_argument("--timeout", type=float, default=180)
    run.add_argument("--trace-project", default="Lab 06")
    run.add_argument("--with-llm-judge", action="store_true")
    run.add_argument("--judge-prompt", type=Path, default=DEFAULT_JUDGE)
    run.add_argument("--dry-run", action="store_true")
    rejudge = sub.add_parser("rejudge", help="对已保存的回答重新评分，不调用 Dify")
    rejudge.add_argument("--report", type=Path, required=True)
    rejudge.add_argument("--judge-prompt", type=Path, default=DEFAULT_JUDGE)
    rejudge.add_argument("--output", type=Path, required=True)
    rejudge.add_argument("--eval-timeout", type=int, default=180,
                         help="单项评分超时秒数，默认 180")
    resume = sub.add_parser("resume", help="只补缺失或执行失败的评分，不调用 Dify")
    resume.add_argument("--report", type=Path, required=True)
    resume.add_argument("--judge-prompt", type=Path, default=DEFAULT_JUDGE)
    resume.add_argument("--output", type=Path, required=True)
    resume.add_argument("--allow-judge-change", action="store_true",
                        help="使用当前 Judge 版本；补齐该版本全部评分，保留旧版本评分")
    resume.add_argument("--eval-timeout", type=int, default=600,
                        help="单项评分超时秒数，默认 600")
    gate = sub.add_parser("check-release", help="对比固定条件并检查人工复核，退出码 0/1/2")
    gate.add_argument("--baseline", type=Path, required=True)
    gate.add_argument("--candidate", type=Path, required=True)
    gate.add_argument("--reviews", type=Path, required=True)
    gate.add_argument("--output", type=Path, required=True)
    for command in (run, rejudge, resume):
        command.add_argument("--eval-concurrency", type=int, default=4,
                             help="评分并发数，默认 4；不改变 Dify 请求并发")
    args = parser.parse_args()
    if args.command in {"run", "rejudge", "resume"} and args.eval_concurrency < 1:
        parser.error("--eval-concurrency 必须为正整数")
    if args.command in {"rejudge", "resume"} and args.eval_timeout < 1:
        parser.error("--eval-timeout 必须为正整数")
    settings = Settings()
    if args.command == "check-release":
        result = release_check(json.loads(args.baseline.read_text()),
                               json.loads(args.candidate.read_text()), read_jsonl(args.reviews))
        write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return {"pass": 0, "block": 1, "review": 2}[result["status"]]
    if args.command == "prepare":
        examples = build_examples(args.cases)
        result = {"status": "validated", "count": len(examples),
                  "case_ids": [e["input"]["case_id"] for e in examples],
                  "dataset_name": args.dataset_name}
        if args.apply:
            client = phoenix_client(settings)
            try:
                old = client.datasets.get_dataset(dataset=args.dataset_name)
            except ValueError as exc:
                if not str(exc).startswith("Dataset not found:"):
                    raise
                old = None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                old = None
            if old is not None:
                old_values = {e["input"]["case_id"]: (e["input"], e["output"], e["metadata"])
                              for e in old.examples}
                new_values = {e["input"]["case_id"]: (e["input"], e["output"], e["metadata"])
                              for e in examples}
                if old_values != new_values:
                    raise ValueError("同名 Dataset 已有不同内容；请使用新版本名称，保留旧基线")
                dataset = old
            else:
                dataset = client.datasets.create_dataset(name=args.dataset_name, examples=examples,
                    dataset_description="Lab 06 只读 Tool + RAG；课程冻结事实，非真实旅游信息")
            result.update({"status": "ready", "dataset_id": dataset.id,
                           "dataset_version": dataset.version_id})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "resume":
        if args.output.exists():
            raise ValueError("报告已存在；请用新路径，保留原始记录")
        report = json.loads(args.report.read_text())
        if not report.get("experiment_id") or not report.get("task_runs"):
            raise ValueError("报告尚未保存应用运行结果；resume 只能续跑评分")
        revision = hashlib.sha256((ROOT / "src/training_eval/lab06.py").read_bytes()).hexdigest()
        if report.get("evaluator_revision") != revision:
            raise ValueError("代码评分器已变化；请恢复原版本后续跑")
        evaluators = {"response_available": response_available,
                      "evidence_coverage": evidence_coverage, "citation_sources": citation_sources}
        if report["judge_revision"] != "not_used":
            name, judge = build_judge(settings, args.judge_prompt)
            if name != report["judge_revision"] and not args.allow_judge_change:
                raise ValueError("Judge 配置与原实验不同；请恢复原配置，或使用 rejudge 重评")
            evaluators[name] = judge
            report["judge_revision"] = name
        client = phoenix_client(settings)
        experiment = client.experiments.get_experiment(experiment_id=report["experiment_id"])
        report.update({"status": "evaluating", "eval_concurrency": args.eval_concurrency,
                       "eval_timeout": args.eval_timeout})
        write_json(args.output, report)
        evaluated = evaluate_concurrently(settings, experiment, evaluators,
                                          args.eval_concurrency, args.eval_timeout, resume=True)
        # 保留所有评分，包括没有数值分数的 review / not_applicable。
        rows = [dataclasses.asdict(r) if dataclasses.is_dataclass(r) else r
                for r in evaluated["evaluation_runs"]]
        completed = {(r["experiment_run_id"], r["name"]) for r in rows
                     if not r.get("error") and r.get("result") is not None}
        required = {(r["id"], name) for r in evaluated["task_runs"] for name in evaluators}
        pending = len(required - completed)
        report.update({"status": "evaluating" if pending else "completed",
                       "evaluation_runs": rows, "pending_evaluations": pending})
        write_json(args.output, report)
        review_path = args.output.with_suffix(".reviews.template.jsonl")
        review_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                       for r in review_template(report)))
        print(json.dumps({"experiment_id": report["experiment_id"], "dify_calls": 0,
                          "pending_evaluations": pending, "report": str(args.output)},
                         ensure_ascii=False))
        return 1 if pending else 0
    if args.command == "rejudge":
        if args.output.exists():
            raise ValueError("重评报告已存在；请用新路径，保留原始评分")
        report = json.loads(args.report.read_text())
        name, judge = build_judge(settings, args.judge_prompt)
        client = phoenix_client(settings)
        experiment = client.experiments.get_experiment(experiment_id=report["experiment_id"])
        evaluated = evaluate_concurrently(settings, experiment, {name: judge},
                                          args.eval_concurrency, args.eval_timeout)
        report.update({"judge_revision": name, "evaluation_runs": evaluated["evaluation_runs"],
                       "eval_concurrency": args.eval_concurrency})
        write_json(args.output, report)
        print(json.dumps({"experiment_id": report["experiment_id"], "judge_revision": name,
                          "dify_calls": 0, "report": str(args.output)}, ensure_ascii=False))
        return 0
    if args.repetitions < 1 or args.timeout <= 0:
        parser.error("repetitions 和 timeout 必须为正数")
    manifest = read_manifest(args.manifest)
    if args.dry_run:
        print(json.dumps({"status": "preflight", "dify_calls": 0, "phoenix_writes": 0,
                          "release_id": manifest["release_id"],
                          "dataset_version": args.dataset_version}, ensure_ascii=False))
        return 0
    if args.output.exists():
        raise ValueError("报告已存在；请用新文件名，避免覆盖基线")
    if not settings.dify_base_url or not settings.dify_lab06_api_key:
        raise ValueError("缺少 DIFY_BASE_URL 或 DIFY_LAB06_API_KEY")
    client = phoenix_client(settings)
    dataset = client.datasets.get_dataset(dataset=args.dataset_name, version_id=args.dataset_version)
    sources = source_versions()
    if any(e["metadata"].get("source_versions") != sources for e in dataset.examples):
        raise ValueError("本地课程事实与冻结 Dataset 不一致，请先核对数据修订")
    evaluators: dict[str, Any] = {
        "response_available": response_available,
        "evidence_coverage": evidence_coverage,
        "citation_sources": citation_sources,
    }
    judge_name = "not_used"
    if args.with_llm_judge:
        judge_name, judge = build_judge(settings, args.judge_prompt)
        evaluators[judge_name] = judge
    report = {
        "status": "running", "dataset_id": dataset.id,
        "dataset_version_id": dataset.version_id,
        "expected_case_ids": [e["input"]["case_id"] for e in dataset.examples],
        "repetitions": args.repetitions, "manifest": manifest, "source_versions": sources,
        "evaluator_revision": hashlib.sha256((ROOT / "src/training_eval/lab06.py").read_bytes()).hexdigest(),
        "judge_revision": judge_name, "eval_concurrency": args.eval_concurrency,
    }
    write_json(args.output, report)
    with httpx.Client(timeout=args.timeout, trust_env=False) as http_client:
        experiment = run_experiment(client=client, dataset=dataset,
            task=make_task(settings, http_client, client, args.trace_project,
                           args.output.with_suffix(".requests.jsonl")),
            experiment_name=manifest["release_id"],
            experiment_description="Lab 06：工具、文档和决策的持续改进评测",
            experiment_metadata={"manifest": manifest, "source_versions": sources,
                                 "judge_revision": judge_name},
            repetitions=args.repetitions, retries=0, timeout=int(args.timeout + 30))
    # 先保存已完成的回答；评分中断时仍可追溯原实验，不必重跑 Dify。
    report.update({"status": "evaluating", "experiment_id": experiment["experiment_id"],
                   "task_runs": report_runs(experiment, dataset), "evaluation_runs": []})
    write_json(args.output, report)
    experiment = evaluate_concurrently(settings, experiment, evaluators,
                                       args.eval_concurrency, int(args.timeout + 30))
    report.update({"status": "completed", "experiment_id": experiment["experiment_id"],
                   "task_runs": report_runs(experiment, dataset),
                   "evaluation_runs": experiment["evaluation_runs"]})
    write_json(args.output, report)
    review_path = args.output.with_suffix(".reviews.template.jsonl")
    review_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                   for r in review_template(report)))
    print(json.dumps({"experiment_id": report["experiment_id"],
                      "task_runs": len(report["task_runs"]), "report": str(args.output),
                      "reviews": str(review_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
