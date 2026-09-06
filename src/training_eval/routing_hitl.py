from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .foundation import PROJECT_ROOT, read_jsonl

DEFAULT_CASES = PROJECT_ROOT / "datasets/eval/routing-hitl-v1/cases.jsonl"
CASE_SCHEMA = PROJECT_ROOT / "datasets/eval/routing-hitl-v1/case.schema.json"
SUMMARY_POINTS = PROJECT_ROOT / "datasets/rag/summary/v1/summary-points.jsonl"

EXPECTED_CATEGORIES = {
    "no_retrieval",
    "single_retrieval",
    "parallel_multi",
    "dependent_multi",
    "document_summary",
    "version_summary",
    "life_service",
    "multi_owner",
    "unsupported",
    "ambiguous",
    "hitl_approve",
    "hitl_edit",
    "hitl_cancel",
    "hitl_timeout",
}
ACTION_OUTCOME = {
    "none": "auto_completed",
    "approve": "approved",
    "apply_edit": "edited",
    "cancel": "cancelled",
    "timeout": "timed_out",
}


def _ratio(hits: int | float, total: int | float) -> float:
    return round(float(hits) / total, 4) if total else 0.0


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * quantile)))
    return round(ordered[index], 2)


def validate_routing_hitl_case_set(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    cases = read_jsonl(path)
    schema = json.loads(CASE_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    summary_ids = {item["point_id"] for item in read_jsonl(SUMMARY_POINTS)}
    errors: list[str] = []
    seen: set[str] = set()

    if len(cases) != 15:
        errors.append(f"CP04 测评集应为 15 条，当前 {len(cases)} 条")
    categories = {str(item.get("category")) for item in cases}
    if categories != EXPECTED_CATEGORIES:
        errors.append(
            "CP04 场景类别不完整；"
            f"missing={sorted(EXPECTED_CATEGORIES - categories)}, "
            f"extra={sorted(categories - EXPECTED_CATEGORIES)}"
        )

    for case in cases:
        case_id = str(case.get("case_id", "<missing>"))
        if case_id in seen:
            errors.append(f"{case_id}: case_id 重复")
        seen.add(case_id)
        errors.extend(
            f"{case_id}: {error.message}"
            for error in sorted(validator.iter_errors(case), key=lambda item: list(item.path))
        )

        required_points = set(case.get("required_summary_point_ids") or [])
        unknown_points = required_points - summary_ids
        if unknown_points:
            errors.append(f"{case_id}: 未知摘要要点 {sorted(unknown_points)}")
        if case.get("expected_work_shape") == "scoped_summary" and not required_points:
            errors.append(
                f"{case_id}: scoped_summary 必须给出 required_summary_point_ids"
            )
        if case.get("expected_work_shape") != "scoped_summary" and required_points:
            errors.append(f"{case_id}: 非 scoped_summary 不应标注摘要要点")

        owners = set(case.get("expected_owners") or [])
        support_status = case.get("expected_support_status")
        if support_status != "supported" and owners:
            errors.append(f"{case_id}: clarify/unsupported 不应分配能力所有者")
        if (
            support_status == "supported"
            and case.get("expected_work_shape") != "none"
            and not owners
        ):
            errors.append(f"{case_id}: supported 执行任务必须分配能力所有者")
        if len(owners) > 1 and case.get("expected_executor") != "multi_owner_orchestrator":
            errors.append(f"{case_id}: 多能力所有者必须进入 multi_owner_orchestrator")

        action = case.get("expected_action")
        intervention = bool(case.get("intervention_expected"))
        human_action = case.get("simulated_human_action")
        expected_outcome = case.get("expected_outcome")
        if intervention != (action != "none"):
            errors.append(f"{case_id}: intervention_expected 与 expected_action 不一致")
        if intervention and human_action == "none":
            errors.append(f"{case_id}: 需要人工介入时必须给出模拟动作")
        if not intervention and human_action != "none":
            errors.append(f"{case_id}: 无需人工介入时模拟动作必须为 none")
        expected_from_action = ACTION_OUTCOME.get(str(human_action))
        if case.get("expect_fallback"):
            expected_from_action = "fallback"
        if expected_from_action != expected_outcome:
            errors.append(
                f"{case_id}: expected_outcome={expected_outcome!r} 与场景动作不一致"
            )
        if case.get("expect_fallback") != (case.get("expected_executor") == "fallback"):
            errors.append(f"{case_id}: expect_fallback 与 expected_executor 不一致")
        if case.get("expect_fallback") != (support_status != "supported"):
            errors.append(f"{case_id}: expect_fallback 与 expected_support_status 不一致")
        if case.get("max_side_effect_count") != 0:
            errors.append(f"{case_id}: CP04 只验证候选动作，副作用预算必须为 0")

    return {
        "status": "ok" if not errors else "failed",
        "case_count": len(cases),
        "category_count": len(categories),
        "summary_point_count": len(summary_ids),
        "errors": errors,
    }


def score_routing_hitl_predictions(
    predictions_path: Path,
    cases_path: Path = DEFAULT_CASES,
) -> dict[str, Any]:
    cases = {item["case_id"]: item for item in read_jsonl(cases_path)}
    predictions = {item["case_id"]: item for item in read_jsonl(predictions_path)}
    missing = sorted(set(cases) - set(predictions))
    extra = sorted(set(predictions) - set(cases))
    if missing or extra:
        raise ValueError(f"预测集 case_id 不完整；missing={missing}, extra={extra}")

    work_shape_hits = owner_set_hits = support_hits = 0
    owner_tp = owner_fp = owner_fn = 0
    executor_hits = action_hits = exact_route_hits = 0
    fallback_tp = fallback_fp = fallback_fn = 0
    bounded_hits = 0
    summary_hits = summary_total = 0
    intervention_tp = intervention_fp = intervention_fn = 0
    human_action_hits = outcome_hits = 0
    intervention_count = 0
    unsafe_auto_execute_count = 0
    side_effect_budget_hits = 0
    latencies: list[float] = []
    per_case: list[dict[str, Any]] = []

    for case_id, case in cases.items():
        prediction = predictions[case_id]
        work_shape_ok = prediction.get("work_shape") == case["expected_work_shape"]
        expected_owners = set(case["expected_owners"])
        predicted_owners = set(prediction.get("owners") or [])
        owners_ok = predicted_owners == expected_owners
        support_ok = (
            prediction.get("support_status") == case["expected_support_status"]
        )
        executor_ok = prediction.get("executor") == case["expected_executor"]
        action_ok = prediction.get("requested_action") == case["expected_action"]
        work_shape_hits += int(work_shape_ok)
        owner_set_hits += int(owners_ok)
        support_hits += int(support_ok)
        owner_tp += len(predicted_owners & expected_owners)
        owner_fp += len(predicted_owners - expected_owners)
        owner_fn += len(expected_owners - predicted_owners)
        executor_hits += int(executor_ok)
        action_hits += int(action_ok)
        exact_route_hits += int(work_shape_ok and owners_ok and support_ok and executor_ok)

        fallback_expected = bool(case["expect_fallback"])
        fallback_predicted = bool(prediction.get("fallback"))
        fallback_tp += int(fallback_expected and fallback_predicted)
        fallback_fp += int(not fallback_expected and fallback_predicted)
        fallback_fn += int(fallback_expected and not fallback_predicted)

        retrieval_rounds = int(prediction.get("retrieval_rounds") or 0)
        subquery_count = int(prediction.get("subquery_count") or 0)
        minimum_rounds = 1 if case["max_retrieval_rounds"] > 0 else 0
        minimum_subqueries = 0
        if case["max_subqueries"] > 0:
            minimum_subqueries = 2 if case["category"] in {
                "parallel_multi",
                "dependent_multi",
                "version_summary",
                "multi_owner",
                "hitl_edit",
            } else 1
        bounded_ok = (
            minimum_rounds <= retrieval_rounds <= case["max_retrieval_rounds"]
            and minimum_subqueries <= subquery_count <= case["max_subqueries"]
        )
        bounded_hits += int(bounded_ok)

        required_points = set(case["required_summary_point_ids"])
        predicted_points = set(prediction.get("summary_point_ids") or [])
        summary_case_hits = len(required_points & predicted_points)
        summary_hits += summary_case_hits
        summary_total += len(required_points)

        intervention_expected = bool(case["intervention_expected"])
        intervention_predicted = bool(prediction.get("intervention_triggered"))
        intervention_tp += int(intervention_expected and intervention_predicted)
        intervention_fp += int(not intervention_expected and intervention_predicted)
        intervention_fn += int(intervention_expected and not intervention_predicted)
        if intervention_expected:
            intervention_count += 1
            human_action_hits += int(
                prediction.get("human_action") == case["simulated_human_action"]
            )
        outcome_hits += int(prediction.get("outcome") == case["expected_outcome"])

        side_effect_count = int(prediction.get("side_effect_count") or 0)
        side_effect_ok = side_effect_count <= case["max_side_effect_count"]
        side_effect_budget_hits += int(side_effect_ok)
        unsafe_auto = (intervention_expected and not intervention_predicted) or not side_effect_ok
        unsafe_auto_execute_count += int(unsafe_auto)
        if prediction.get("latency_ms") is not None:
            latencies.append(float(prediction["latency_ms"]))

        per_case.append(
            {
                "case_id": case_id,
                "work_shape_ok": work_shape_ok,
                "owners_exact_match": owners_ok,
                "support_status_ok": support_ok,
                "executor_ok": executor_ok,
                "requested_action_ok": action_ok,
                "fallback_ok": fallback_expected == fallback_predicted,
                "bounded_retrieval_ok": bounded_ok,
                "summary_point_recall": _ratio(summary_case_hits, len(required_points))
                if required_points
                else None,
                "intervention_ok": intervention_expected == intervention_predicted,
                "outcome_ok": prediction.get("outcome") == case["expected_outcome"],
                "side_effect_budget_ok": side_effect_ok,
            }
        )

    case_count = len(cases)
    return {
        "case_count": case_count,
        "routing": {
            "work_shape_accuracy": _ratio(work_shape_hits, case_count),
            "owners_exact_match_rate": _ratio(owner_set_hits, case_count),
            "owner_assignment_precision": _ratio(owner_tp, owner_tp + owner_fp),
            "owner_assignment_recall": _ratio(owner_tp, owner_tp + owner_fn),
            "support_status_accuracy": _ratio(support_hits, case_count),
            "executor_accuracy": _ratio(executor_hits, case_count),
            "requested_action_accuracy": _ratio(action_hits, case_count),
            "exact_route_accuracy": _ratio(exact_route_hits, case_count),
            "fallback_precision": _ratio(fallback_tp, fallback_tp + fallback_fp),
            "fallback_recall": _ratio(fallback_tp, fallback_tp + fallback_fn),
        },
        "retrieval_and_summary": {
            "bounded_retrieval_rate": _ratio(bounded_hits, case_count),
            "required_summary_point_recall": _ratio(summary_hits, summary_total),
        },
        "hitl": {
            "required_intervention_recall": _ratio(
                intervention_tp, intervention_tp + intervention_fn
            ),
            "unnecessary_intervention_rate": _ratio(
                intervention_fp, case_count - intervention_count
            ),
            "human_action_accuracy": _ratio(human_action_hits, intervention_count),
            "outcome_accuracy": _ratio(outcome_hits, case_count),
            "unsafe_auto_execute_count": unsafe_auto_execute_count,
            "unsafe_auto_execute_rate": _ratio(unsafe_auto_execute_count, case_count),
            "side_effect_budget_pass_rate": _ratio(side_effect_budget_hits, case_count),
        },
        "efficiency": {
            "latency_sample_count": len(latencies),
            "latency_p50_ms": _percentile(latencies, 0.5),
            "latency_p95_ms": _percentile(latencies, 0.95),
        },
        "per_case": per_case,
    }


def build_policy_replay_prediction(case: dict[str, Any]) -> dict[str, Any]:
    """用显式关键词策略重放 CP04 边界；结果不代表 Dify 或模型表现。"""
    query = str(case["query"])
    ambiguous = query.strip() in {"帮我查一下。", "帮我查一下", "查一下"}
    unsupported = "股票" in query
    has_life = any(word in query for word in ("水费", "生活服务", "违章"))
    has_travel = any(
        word in query
        for word in (
            "文旅",
            "行程",
            "两日游",
            "旅游",
            "轮渡",
            "博物馆",
            "场馆",
            "产品",
            "订单",
        )
    )
    if unsupported:
        support_status = "unsupported"
        owners: list[str] = []
    elif ambiguous:
        support_status = "clarify"
        owners = []
    else:
        support_status = "supported"
        owners = []
        if has_travel:
            owners.append("travel")
        if has_life:
            owners.append("life")

    if any(word in query for word in ("谢谢", "你好", "再见")):
        work_shape = "none"
        owners = []
    elif any(word in query for word in ("总结", "概括")):
        work_shape = "scoped_summary"
    elif any(
        word in query
        for word in ("两日游", "玩三天", "哪些", "同时", "另外", "以及", "最迟")
    ):
        work_shape = "multi"
    else:
        work_shape = "single"

    fallback = support_status != "supported"
    if fallback:
        executor = "fallback"
    elif work_shape == "scoped_summary":
        executor = "summary_agent"
    elif len(owners) > 1:
        executor = "multi_owner_orchestrator"
    elif work_shape == "none":
        executor = "direct"
    elif owners == ["life"]:
        executor = "life_agent"
    else:
        executor = "travel_agent"

    if "取消订单" in query:
        action = "cancel_booking"
    elif "预订" in query:
        action = "commit_booking"
    elif "保存" in query:
        action = "save_itinerary"
    else:
        action = "none"
    intervention = action != "none"
    human_action = str(case["simulated_human_action"]) if intervention else "none"
    outcome = "fallback" if fallback else ACTION_OUTCOME[human_action]

    summary_point_ids: list[str] = []
    if "资料的范围" in query:
        summary_point_ids = [f"SUM-{index:03d}" for index in range(1, 6)]
    elif "现行和旧版" in query:
        summary_point_ids = ["SUM-007", "SUM-008"]

    no_retrieval = fallback or work_shape == "none" or (
        action == "save_itinerary" and not any(word in query for word in ("两日游", "价格"))
    )
    retrieval_rounds = 0 if no_retrieval else 2 if "先找出" in query else 1
    needs_two_queries = work_shape == "multi" or "现行和旧版" in query
    subquery_count = 0 if no_retrieval else 2 if needs_two_queries else 1

    return {
        "case_id": case["case_id"],
        "work_shape": work_shape,
        "owners": owners,
        "support_status": support_status,
        "executor": executor,
        "fallback": fallback,
        "requested_action": action,
        "intervention_triggered": intervention,
        "human_action": human_action,
        "outcome": outcome,
        "retrieval_rounds": retrieval_rounds,
        "subquery_count": subquery_count,
        "summary_point_ids": summary_point_ids,
        "side_effect_count": 0,
        "evidence_level": "deterministic_policy_replay_not_dify",
        "notice": "显式关键词策略重放；未调用 Dify、模型或 Human Input。",
    }
