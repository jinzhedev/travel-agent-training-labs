"""Lab 06 的案例引用、诊断评分与课堂版本检查。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "datasets/eval/tool-rag-improvement-v1/cases.jsonl"
OBJECTS = ROOT / "datasets/rag/document-ai/v1/parsed/object-aware.jsonl"
OBJECT_PATTERN = re.compile(r"XM-[A-Z0-9-]+-P\d+-[A-Z0-9-]+")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_versions() -> dict[str, str]:
    paths = [
        OBJECTS, ROOT / "datasets/scenario/xiamen/v1/pois.json",
        ROOT / "datasets/tools/catalog-v1/catalog.json",
        ROOT / "src/travel_core/services/tools.py",
    ]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def build_examples(path: Path = CASES) -> list[dict[str, Any]]:
    # 直接引用权威对象和工具实现，不复制另一份文旅事实 fixture。
    from travel_core.security import RequestContext
    from travel_core.services.tools import _mock_result

    objects = {item["object_id"]: item for item in read_jsonl(OBJECTS)}
    ctx = RequestContext("training", "lab06-gold", "offline", None, frozenset({"travel.read"}))
    examples = []
    seen: set[str] = set()
    for case in read_jsonl(path):
        for key in ("case_id", "query", "acceptance", "tool_check"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"案例缺少 {key}")
        if case["case_id"] in seen:
            raise ValueError(f"重复 case_id: {case['case_id']}")
        seen.add(case["case_id"])
        if not case.get("slices") or not all(isinstance(s, str) for s in case["slices"]):
            raise ValueError("slices 必须是非空字符串数组")
        if not isinstance(case.get("document_ids"), list):
            raise ValueError("document_ids 必须是数组")
        documents = [objects[key] for key in case["document_ids"]]
        weather = None
        if case.get("weather_date"):
            from datetime import date

            date.fromisoformat(case["weather_date"])
            weather = _mock_result("weather.forecast", {
                "city": "厦门", "start_date": case["weather_date"],
                "end_date": case["weather_date"],
            }, ctx)
        examples.append({
            "id": case["case_id"],
            "input": {"case_id": case["case_id"], "query": case["query"]},
            "output": {
                "acceptance": case["acceptance"], "tool_check": case["tool_check"],
                "document_ids": case["document_ids"], "reference_documents": documents,
                "weather_reference": weather,
            },
            "metadata": {
                "slices": case["slices"], "is_simulated": True,
                "source_trace_id": case.get("source_trace_id", "course_authored"),
                "source_versions": source_versions(),
            },
        })
    if not examples:
        raise ValueError("评测集不能为空")
    return examples


def object_ids(resources: Any) -> list[str]:
    if not isinstance(resources, list):
        return []
    return sorted(set(OBJECT_PATTERN.findall(json.dumps(resources, ensure_ascii=False))))


def response_available(output: dict) -> tuple:
    if output.get("run_error") or not str(output.get("answer") or "").strip():
        return 0.0, "fail", "API 失败或没有最终回答；必须计入失败分母"
    if re.search(r"\{\{#[^\n]+?#\}\}", output["answer"]):
        return 0.0, "fail", "最终回答包含未解析的 Dify 变量；先修复绑定"
    return 1.0, "pass", "存在最终回答；不代表业务任务成功"


def evidence_coverage(output: dict, expected: dict) -> tuple:
    required = set(expected["document_ids"])
    if not required:
        return None, "not_applicable", "本例不要求指定文档；仍需人工检查拒答或澄清"
    if output.get("retrieved_resources") is None:
        return None, "review", "API 未提供检索资源；需从原始检索节点核对，不能判已命中"
    missing = required - set(object_ids(output["retrieved_resources"]))
    if missing:
        return 0.0, "fail", "API 暴露的资源未覆盖：" + ", ".join(sorted(missing))
    return 1.0, "pass", "所需对象出现在 API 资源中；仍需检查内容是否完整传给最终模型"


def citation_sources(output: dict, expected: dict) -> tuple:
    cited = set(OBJECT_PATTERN.findall(output.get("answer") or ""))
    if not cited:
        if expected["document_ids"]:
            return 0.0, "fail", "本例需要文档依据，但没有输出对象 ID"
        return None, "not_applicable", "本例无需文档引用"
    if output.get("retrieved_resources") is None:
        return None, "review", "无法通过 API 核对引用来源"
    invented = cited - set(object_ids(output["retrieved_resources"]))
    if invented:
        return 0.0, "fail", "引用不在 API 资源中，需核对原始 Context：" + ", ".join(sorted(invented))
    return 1.0, "pass", "引用 ID 来自 API 资源；不证明引用内容支持结论"


def percentile95(values: list[float]) -> float | None:
    return sorted(values)[math.ceil(len(values) * .95) - 1] if values else None


def review_template(report: dict) -> list[dict]:
    return [{
        "run_id": run["id"], "case_id": run["output"]["case_id"],
        "repetition": run["repetition_number"],
        "task_result": "pending", "tool_result": "pending",
        "critical_violation": None, "reason": "", "trace_id": "",
    } for run in report["task_runs"]]


def release_check(baseline: dict, candidate: dict, reviews: list[dict]) -> dict:
    """仅检查课堂只读应用；缺失或不可比的证据不会返回 pass。"""
    blocks: list[str] = []
    pending: list[str] = []
    for key in ("dataset_id", "dataset_version_id", "repetitions", "source_versions",
                "evaluator_revision", "judge_revision", "expected_case_ids"):
        if baseline.get(key) != candidate.get(key) or key not in candidate:
            pending.append(f"比较条件不同或缺失：{key}")
    left, right = baseline.get("manifest", {}), candidate.get("manifest", {})
    changed = [k for k in set(left) | set(right)
               if k not in {"release_id", "app_id"} and left.get(k) != right.get(k)]
    if len(changed) != 1 or changed[0] not in {"answer_model", "answer_prompt_revision"}:
        pending.append("本练习要求只改变 answer_model 或 answer_prompt_revision 中的一项")
    if candidate.get("status") != "completed" or baseline.get("status") != "completed":
        pending.append("实验未完整结束")
    for label, report in (("baseline", baseline), ("candidate", candidate)):
        keys = [(r.get("output", {}).get("case_id"), r.get("repetition_number"))
                for r in report.get("task_runs", [])]
        expected = {(cid, i) for cid in report.get("expected_case_ids", [])
                    for i in range(1, report.get("repetitions", 0) + 1)}
        if not expected or set(keys) != expected or len(keys) != len(expected):
            pending.append(f"{label} 缺少 trial 或存在重复 trial")
    review_map = {r.get("run_id"): r for r in reviews}
    if len(review_map) != len(reviews):
        pending.append("人工复核包含重复 run_id")
    actual_ids = {r["id"] for r in candidate.get("task_runs", [])}
    if set(review_map) != actual_ids:
        pending.append("人工复核必须与候选的全部 run_id 一一对应")
    for run in candidate.get("task_runs", []):
        output = run.get("output") or {}
        if response_available(output)[0] == 0:
            blocks.append(f"{run['id']}：运行失败")
        review = review_map.get(run["id"], {})
        if (review.get("case_id") != output.get("case_id")
                or review.get("repetition") != run.get("repetition_number")):
            pending.append(f"{run['id']}：复核对象不匹配")
        if review.get("critical_violation") is True:
            blocks.append(f"{run['id']}：关键约束违反")
        elif review.get("critical_violation") is not False:
            pending.append(f"{run['id']}：关键约束尚未复核")
        for key in ("task_result", "tool_result"):
            if review.get(key) == "fail":
                blocks.append(f"{run['id']}：{key} 未通过")
            elif review.get(key) != "pass":
                pending.append(f"{run['id']}：{key} 尚未复核")
        if not review.get("reason") or not review.get("trace_id"):
            pending.append(f"{run['id']}：缺少复核依据或 trace ID")
    durations = [r["output"].get("elapsed_s") for r in candidate.get("task_runs", [])]
    if not durations or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
                            for v in durations):
        pending.append("整次请求耗时缺失或无效")
        p95 = None
    else:
        p95 = percentile95(durations)
    budget = right.get("p95_budget_s")
    if not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
        pending.append("缺少预先约定的 p95 预算")
    elif p95 is not None and p95 > budget:
        blocks.append("实测样本 p95 超过课堂预算")
    return {
        "status": "block" if blocks else "review" if pending else "pass",
        "scope": "read_only_classroom_candidate", "changed_variables": changed,
        "blocking_reasons": blocks, "review_reasons": pending,
        "sample_p95_s": p95,
        "release_action": "待人工填写影子、灰度、观察窗口和回退条件；本命令不发布应用",
    }
