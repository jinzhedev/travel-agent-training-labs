"""Lab 04：读取 Phoenix Dataset，运行最小 Chatflow 并检查执行器及 HITL。

--dry-run 只做本地检查，不调用 Dify，也不写 Phoenix。
自动回归的人工决定由 --simulate-human 模拟；真实超时由 Web App 单独验证。
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from phoenix.client import Client
from phoenix.client.experiments import run_experiment
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "datasets/eval/routing-hitl-v2/cases.jsonl"
DATASET = "lab04-routing-hitl-v2"
WORKERS = {"travel": "旅游执行器", "life": "生活执行器"}


class Settings(BaseSettings):
    dify_base_url: str = ""
    dify_lab04_api_key: str = ""
    phoenix_endpoint: str = ""
    phoenix_api_key: str = ""
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")


def load_cases(path: Path = CASES) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if [c["case_id"] for c in cases] != [f"L04-{i:03}" for i in range(1, 16)]:
        raise ValueError("题集必须包含 L04-001～015，按编号排列且不重复")
    outcomes = {
        "none": {"auto_completed", "clarify", "unsupported"},
        "approve": {"approved"},
        "apply_edit": {"edited"},
        "cancel": {"cancelled"},
        "timeout": {"timed_out"},
    }
    for case in cases:
        action = case["human_action"]
        expected = case["expected"]
        if action not in outcomes or expected["outcome"] not in outcomes[action]:
            raise ValueError(f"{case['case_id']}: 人工决定与终态不一致")
        if expected["action"] != ("none" if action == "none" else "save_itinerary"):
            raise ValueError(f"{case['case_id']}: 候选动作与人工介入不一致")
        if (case["mode"] == "manual_timeout") != (action == "timeout"):
            raise ValueError("超时必须单独记录")
        if set(case["input"]) != {"query"} or not case["input"]["query"].strip():
            raise ValueError("应用输入只接收 query")
    return cases


def examples_for(cases: list[dict]) -> list[dict]:
    return [
        {
            "id": c["case_id"],
            "input": c["input"],
            "output": c["expected"],
            "metadata": {
                "case_id": c["case_id"],
                "human_action": c["human_action"],
                "suite": DATASET,
            },
        }
        for c in cases
        if c["mode"] == "auto"
    ]


def check_dataset(dataset: Any, cases: list[dict]) -> None:
    """防止 UI 编辑后丢题、重复题或把模型输出保留为金标。"""
    actual = list(dataset.examples)
    gold = {c["case_id"]: c for c in cases if c["mode"] == "auto"}
    seen = set()
    for example in actual:
        meta = example["metadata"]
        case_id = meta.get("case_id")
        if case_id not in gold or case_id in seen:
            raise ValueError("Phoenix Dataset 中存在未知或重复的 case_id")
        case = gold[case_id]
        if (
            example["input"] != case["input"]
            or example["output"] != case["expected"]
            or meta.get("human_action") != case["human_action"]
        ):
            raise ValueError(
                f"{case_id}: Dataset 与权威题集不一致，请核对 input / output / metadata"
            )
        seen.add(case_id)
    if seen != set(gold):
        raise ValueError(f"Phoenix Dataset 缺少题目：{sorted(set(gold) - seen)}")


def sse_events(lines: Iterable[str]) -> Iterable[dict]:
    buffer = []
    for line in lines:
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
        elif not line.strip() and buffer:
            event = json.loads("\n".join(buffer))
            buffer.clear()
            if not isinstance(event, dict):
                raise ValueError("SSE data 必须是 JSON 对象")
            yield event
    if buffer:
        event = json.loads("\n".join(buffer))
        if not isinstance(event, dict):
            raise ValueError("SSE data 必须是 JSON 对象")
        yield event


def run_chatflow(
    client: httpx.Client,
    *,
    base_url: str,
    api_key: str,
    query: str,
    human_action: str,
    edited_answer: str = "",
    timeout: float = 180,
) -> dict:
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    headers = {"Authorization": f"Bearer {api_key}"}
    user = f"lab04-{uuid4()}"
    run_id = ""
    conversation_id = ""
    token = ""  # 临时凭据仅留在内存，不返回或输出到日志。
    paused = False
    submitted = False
    finished = None
    workers: dict[str, dict] = {}
    started = time.monotonic()

    def consume(response: httpx.Response) -> None:
        nonlocal run_id, conversation_id, token, paused, finished
        if response.status_code >= 400:
            raise RuntimeError(f"Dify stream HTTP {response.status_code}")

        def bounded_lines():
            for line in response.iter_lines():
                if time.monotonic() - started > timeout:
                    raise RuntimeError(f"等待超过 {timeout} 秒，run={run_id}；不创建新 Run 重试")
                yield line

        for event in sse_events(bounded_lines()):
            kind, data = event.get("event"), event.get("data") or {}
            event_conversation = event.get("conversation_id")
            if event_conversation:
                if conversation_id and event_conversation != conversation_id:
                    raise RuntimeError("恢复事件的 conversation_id 发生变化")
                conversation_id = event_conversation
            event_run = event.get("workflow_run_id")
            if not event_run and kind in (
                "workflow_started",
                "workflow_finished",
                "workflow_paused",
            ):
                event_run = data.get("id") or data.get("workflow_run_id")
            if event_run:
                if run_id and event_run != run_id:
                    raise RuntimeError("恢复事件的 workflow_run_id 发生变化")
                run_id = event_run
            if kind == "error":
                raise RuntimeError(f"Dify 运行失败，run={run_id}；请查看应用日志")
            if kind == "node_finished" and data.get("title") in WORKERS.values():
                execution_id = data.get("id")
                if not execution_id:
                    raise ValueError("执行器事件缺少 execution ID")
                workers[execution_id] = {
                    "title": data["title"],
                    "status": data.get("status"),
                    "text": (data.get("outputs") or {}).get("text", ""),
                }
            if kind == "human_input_required":
                token = data.get("form_token") or ""
            if kind == "workflow_paused":
                paused = True
                break
            if kind == "workflow_finished":
                finished = data
                break

    try:
        with client.stream(
            "POST",
            f"{base}/chat-messages",
            headers=headers,
            json={
                "inputs": {},
                "query": query,
                "user": user,
                "response_mode": "streaming",
                "auto_generate_name": False,
            },  # 不传 conversation_id：每题新建会话，不继承前题上下文。
        ) as response:
            consume(response)
        if paused:
            if human_action not in ("approve", "apply_edit", "cancel"):
                raise RuntimeError(f"出现预期外的人工暂停，run={run_id}；请在 Dify 中查看")
            if not token or not run_id:
                raise RuntimeError("暂停事件缺少 run ID 或 webapp form token")
            form_url = f"{base}/form/human_input/{token}"
            form_response = client.get(form_url, headers=headers, params={"user": user})
            if form_response.status_code != 200:
                raise RuntimeError(f"读取人工表单 HTTP {form_response.status_code}")
            form = form_response.json()
            if human_action not in {a["id"] for a in form.get("user_actions", [])}:
                raise ValueError("表单没有指定的人工 action")
            if f"{run_id}:save_itinerary" not in form.get("form_content", ""):
                raise ValueError("表单没有展示绑定当前 Run 的 action_id")
            submitted_response = client.post(
                form_url,
                headers=headers,
                json={
                    "user": user,
                    "action": human_action,
                    "inputs": {
                        "reviewed_answer": edited_answer if human_action == "apply_edit" else ""
                    },
                },
            )
            if submitted_response.status_code >= 400:
                raise RuntimeError(f"提交人工决定 HTTP {submitted_response.status_code}")
            submitted = True
            with client.stream(
                "GET", f"{base}/workflow/{run_id}/events", headers=headers, params={"user": user}
            ) as response:
                consume(response)
    except httpx.HTTPError:
        # httpx 异常可能包含 form URL，不能把 token 写入 Phoenix task error。
        raise RuntimeError(f"Dify 网络请求失败，run={run_id}；请检查对应 Run") from None
    if not finished or finished.get("status") != "succeeded":
        raise RuntimeError(f"未观察到成功终态，run={run_id}；暂停或断流不等于完成")
    # 已完成的 Run 重连可能只返回 workflow_finished；不依赖 message 分片或节点重放。
    answer = (finished.get("outputs") or {}).get("answer")
    if not isinstance(answer, str):
        raise ValueError("Chatflow 终态缺少 outputs.answer；检查 Answer 节点配置")
    try:
        result = json.loads(answer)
    except json.JSONDecodeError:
        raise ValueError("Answer 必须只展示最终状态 JSON，不添加前缀或代码围栏") from None
    if not isinstance(result, dict):
        raise ValueError("Answer 中的最终状态必须为 JSON 对象")
    return {
        "result": result,
        "workflow_run_id": run_id,
        "conversation_id": conversation_id,
        "workers": list(workers.values()),
        "paused": paused,
        "submitted": submitted,
        "human_action": human_action if submitted else "none",
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def score(ok: bool, good: str, bad: str) -> tuple[float, str, str]:
    return (1.0, "pass", good) if ok else (0.0, "fail", bad)


def route_and_tasks(output: Mapping, expected: Mapping) -> tuple:
    result = output.get("result") or {}
    ok = result.get("route") == expected["route"] and result.get("action") == expected["action"]
    tasks = result.get("tasks")
    if not isinstance(tasks, list) or not all(isinstance(t, dict) for t in tasks):
        return score(False, "", "tasks 缺失或类型错误")
    ids = [t.get("id") for t in tasks]
    ok = ok and all(isinstance(i, str) and i for i in ids) and len(ids) == len(set(ids))
    if expected["route"] not in ("clarify", "unsupported"):
        keys = ("kind", "city", "date", "utility_type", "depends_on")
        actual_tasks = [{key: t.get(key) for key in keys} for t in tasks]
        ok = ok and sorted(actual_tasks, key=lambda t: str(t["kind"])) == sorted(
            expected["tasks"], key=lambda t: t["kind"]
        )
    if expected.get("missing_contains"):
        ok = ok and expected["missing_contains"] in " ".join(
            result.get("missing_information") or []
        )
    return score(bool(ok), "路由、任务参数与候选动作符合预期", "检查路由、任务遗漏、参数或缺失信息")


def worker_completion(output: Mapping, expected: Mapping) -> tuple:
    owners = {"travel": ["travel"], "life": ["life"], "both": ["travel", "life"]}.get(
        expected["route"], []
    )
    workers = output.get("workers") or []
    answers = (output.get("result") or {}).get("worker_answers")
    ok = isinstance(answers, dict) and set(answers) == set(owners) and len(workers) == len(owners)
    for owner in owners:
        matching = [
            w
            for w in workers
            if w.get("title") == WORKERS[owner] and w.get("status") == "succeeded"
        ]
        ok = (
            ok
            and len(matching) == 1
            and bool(matching[0].get("text", "").strip())
            and matching[0]["text"].strip() == (answers or {}).get(owner)
        )
    return score(
        bool(ok), "实际执行器事件与合并结果逐项对应", "执行器缺失、重复、失败或合并结果不对应"
    )


def hitl_lifecycle(output: Mapping, expected: Mapping) -> tuple:
    result = output.get("result") or {}
    needs = expected["action"] != "none"
    run_id = output.get("workflow_run_id")
    ok = (
        bool(run_id)
        and result.get("workflow_run_id") == run_id
        and result.get("outcome") == expected["outcome"]
    )
    ok = ok and output.get("paused") is needs and output.get("submitted") is needs
    if needs:
        ok = (
            ok
            and result.get("action_id") == f"{run_id}:save_itinerary"
            and result.get("human_decision") == output.get("human_action")
        )
    else:
        ok = ok and result.get("action_id") == ""
    if expected["outcome"] == "edited":
        ok = ok and result.get("answer") == expected["edited_answer"]
    return score(
        bool(ok),
        "暂停、决定、原 Run 与终态对应",
        "人工分支、动作绑定、修改结果或 run ID 不符合预期",
    )


def result_contract(output: Mapping) -> tuple:
    result = output.get("result") or {}
    ok = (
        isinstance(result.get("answer"), str)
        and bool(result["answer"].strip())
        and type(result.get("side_effect_count")) is int
        and result["side_effect_count"] == 0
    )
    return score(
        ok, "结果非空，记录为零业务写入；实际工具仍需检查 trace", "缺少最终结果或零写入记录"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-name", default=DATASET)
    parser.add_argument("--experiment-name", default="lab04-baseline")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--init-dataset", action="store_true", help="将 14 条自动题写入指定 Phoenix Dataset"
    )
    parser.add_argument(
        "--simulate-human", action="store_true", help="为四条人工决定样本模拟提交表单"
    )
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--repetitions", type=int, default=1)
    args = parser.parse_args()
    cases = load_cases()
    if args.timeout <= 0 or args.repetitions < 1:
        parser.error("timeout 和 repetitions 必须为正数")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "local_check_passed",
                    "auto_cases": 14,
                    "manual_timeout_cases": ["L04-015"],
                    "network_calls": 0,
                },
                ensure_ascii=False,
            )
        )
        return 0
    settings = Settings()
    if not settings.phoenix_endpoint:
        parser.error("缺少 PHOENIX_ENDPOINT")
    phoenix = Client(base_url=settings.phoenix_endpoint, api_key=settings.phoenix_api_key or None)
    if args.init_dataset:
        dataset = phoenix.datasets.create_dataset(
            name=args.dataset_name, examples=examples_for(cases)
        )
        print(
            json.dumps(
                {
                    "status": "dataset_written",
                    "dataset": args.dataset_name,
                    "example_count": len(dataset.examples),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not settings.dify_base_url or not settings.dify_lab04_api_key:
        parser.error("缺少 DIFY_BASE_URL / DIFY_LAB04_API_KEY")
    if not args.simulate_human:
        parser.error("全集含人工决定样本；模拟回归需传 --simulate-human，手工操作见 LAB.md")
    dataset = phoenix.datasets.get_dataset(dataset=args.dataset_name)
    check_dataset(dataset, cases)
    with httpx.Client(timeout=args.timeout, trust_env=False) as client:

        def task(example: Any) -> dict:
            return run_chatflow(
                client,
                base_url=settings.dify_base_url,
                api_key=settings.dify_lab04_api_key,
                query=example["input"]["query"],
                human_action=example["metadata"]["human_action"],
                edited_answer=example["output"].get("edited_answer", ""),
                timeout=args.timeout,
            )

        experiment = run_experiment(
            client=phoenix,
            dataset=dataset,
            task=task,
            evaluators={
                "route_and_tasks": route_and_tasks,
                "worker_completion": worker_completion,
                "hitl_lifecycle": hitl_lifecycle,
                "result_contract": result_contract,
            },
            experiment_name=args.experiment_name,
            experiment_metadata={
                "suite": DATASET,
                "app_mode": "chatflow",
                "human_input": "simulated",
                "manual_timeout_case": "L04-015",
            },
            timeout=int(args.timeout) + 30,
            repetitions=args.repetitions,
            retries=0,
        )
    failures = sum(bool(r.get("error")) for r in experiment.get("task_runs", []))
    for evaluation in experiment.get("evaluation_runs", []):
        value = getattr(evaluation, "result", None)
        values = value if isinstance(value, list) else [value]
        failures += int(bool(getattr(evaluation, "error", None)))
        failures += sum(
            isinstance(v, dict) and v.get("score") is not None and v["score"] < 1 for v in values
        )
    if len(experiment.get("task_runs", [])) != 14 * args.repetitions:
        failures += 1
    if len(experiment.get("evaluation_runs", [])) != 56 * args.repetitions:
        failures += 1
    print(
        json.dumps(
            {
                "status": "failed" if failures else "passed",
                "experiment_id": experiment.get("experiment_id"),
                "task_runs": len(experiment.get("task_runs", [])),
                "failures": failures,
                "timeout_case": "manual_not_scored",
            },
            ensure_ascii=False,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
