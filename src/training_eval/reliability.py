from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .foundation import PROJECT_ROOT, read_jsonl

DEFAULT_CASES = PROJECT_ROOT / "datasets/eval/reliability-v1/cases.jsonl"
CASE_SCHEMA = PROJECT_ROOT / "datasets/eval/reliability-v1/case.schema.json"
PREDICTION_SCHEMA = PROJECT_ROOT / "datasets/eval/reliability-v1/prediction.schema.json"
DIAGNOSIS_TRACES = PROJECT_ROOT / "datasets/eval/reliability-v1/diagnosis-traces.jsonl"

EXPECTED_CATEGORIES = {
    "liveness",
    "readiness",
    "transient_read_retry",
    "transient_validation_retry",
    "idempotent_replay",
    "idempotency_key_conflict",
    "timeout_after_commit",
    "optimistic_version_conflict",
    "missing_idempotency_key",
    "idempotent_approval",
    "pod_replacement",
    "rolling_update",
    "cross_system_trace",
}


def _ratio(hits: int | float, total: int | float) -> float:
    return round(float(hits) / total, 4) if total else 0.0


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * quantile)))
    return round(ordered[index], 2)


def _evidence_satisfies(actual: str, required: str) -> bool:
    if required == "live_service":
        return actual in {"live_service", "live_compose", "live_k8s", "live_dify"}
    return actual == required


def validate_reliability_case_set(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    cases = read_jsonl(path)
    diagnosis_traces = read_jsonl(DIAGNOSIS_TRACES)
    schema = json.loads(CASE_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    seen: set[str] = set()

    if len(cases) != 13:
        errors.append(f"CP05 测评集应为 13 条，当前 {len(cases)} 条")
    expected_diagnosis = {
        "DIAG-MODEL-001": "model",
        "DIAG-TOOL-001": "tool",
        "DIAG-RUNTIME-001": "runtime",
    }
    diagnosis = {
        str(item.get("trace_id")): str(item.get("expected_first_deviation_layer"))
        for item in diagnosis_traces
    }
    if diagnosis != expected_diagnosis:
        errors.append(
            "CP05 冻结诊断 trace 不完整；"
            f"expected={expected_diagnosis}, actual={diagnosis}"
        )
    for item in diagnosis_traces:
        trace_id = str(item.get("trace_id", "<missing>"))
        if item.get("evidence_level") != "frozen_trace":
            errors.append(f"{trace_id}: evidence_level 必须是 frozen_trace")
        if not item.get("events") or not item.get("expected_action") or not item.get("not_proven"):
            errors.append(f"{trace_id}: events、expected_action 和 not_proven 必填")
    categories = {str(item.get("category")) for item in cases}
    if categories != EXPECTED_CATEGORIES:
        errors.append(
            "CP05 场景类别不完整；"
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
        statuses = case.get("expected_http_sequence") or []
        if bool(case.get("conflict_expected")) != (409 in statuses):
            errors.append(f"{case_id}: conflict_expected 必须与 HTTP 409 一致")
        if case.get("expected_replay_count", 0) > 0 and not case.get("retry_allowed"):
            errors.append(f"{case_id}: 需要 replay 的案例必须允许使用原请求重试")
        if case.get("category") in {"pod_replacement", "rolling_update"} and case.get(
            "required_evidence_level"
        ) != "live_k8s":
            errors.append(f"{case_id}: Kubernetes 案例必须要求 live_k8s")

    return {
        "status": "ok" if not errors else "failed",
        "case_count": len(cases),
        "category_count": len(categories),
        "live_only_count": sum(bool(item.get("live_only")) for item in cases),
        "diagnosis_trace_count": len(diagnosis_traces),
        "errors": errors,
    }


def validate_reliability_predictions(path: Path) -> list[str]:
    schema = json.loads(PREDICTION_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    seen: set[str] = set()
    for prediction in read_jsonl(path):
        case_id = str(prediction.get("case_id", "<missing>"))
        if case_id in seen:
            errors.append(f"{case_id}: prediction case_id 重复")
        seen.add(case_id)
        errors.extend(
            f"{case_id}: {error.message}"
            for error in sorted(validator.iter_errors(prediction), key=lambda item: list(item.path))
        )
    return errors


def score_reliability_predictions(
    predictions_path: Path,
    cases_path: Path = DEFAULT_CASES,
) -> dict[str, Any]:
    prediction_errors = validate_reliability_predictions(predictions_path)
    if prediction_errors:
        raise ValueError("预测格式无效：" + "; ".join(prediction_errors))

    cases = {item["case_id"]: item for item in read_jsonl(cases_path)}
    predictions = {item["case_id"]: item for item in read_jsonl(predictions_path)}
    missing = sorted(set(cases) - set(predictions))
    extra = sorted(set(predictions) - set(cases))
    if missing or extra:
        raise ValueError(f"预测集 case_id 不完整；missing={missing}, extra={extra}")

    http_hits = business_hits = replay_hits = final_hits = owner_hits = 0
    recovery_hits = recovery_total = conflict_hits = conflict_total = 0
    evidence_hits = trace_hits = trace_total = case_hits = 0
    unsafe_retry_count = 0
    latencies: list[float] = []
    per_case: list[dict[str, Any]] = []

    for case_id, case in cases.items():
        prediction = predictions[case_id]
        http_ok = prediction["http_sequence"] == case["expected_http_sequence"]
        business_ok = (
            prediction["plan_version_count"] == case["expected_plan_version_count"]
        )
        replay_ok = prediction["replay_count"] == case["expected_replay_count"]
        final_ok = prediction["final_state"] == case["expected_final_state"]
        owner_ok = prediction["state_owner"] == case["expected_state_owner"]
        recovery_ok = prediction["recovery_verified"] == case["recovery_expected"]
        conflict_ok = prediction["conflict_detected"] == case["conflict_expected"]
        evidence_ok = _evidence_satisfies(
            prediction["evidence_level"], case["required_evidence_level"]
        )
        required_trace = set(case["required_trace_fields"])
        predicted_trace = set(prediction["trace_fields"])
        trace_case_hits = len(required_trace & predicted_trace)
        trace_ok = trace_case_hits == len(required_trace)
        safe_retry_ok = prediction["unsafe_retry_count"] == 0

        http_hits += int(http_ok)
        business_hits += int(business_ok)
        replay_hits += int(replay_ok)
        final_hits += int(final_ok)
        owner_hits += int(owner_ok)
        evidence_hits += int(evidence_ok)
        trace_hits += trace_case_hits
        trace_total += len(required_trace)
        unsafe_retry_count += prediction["unsafe_retry_count"]
        if case["recovery_expected"]:
            recovery_total += 1
            recovery_hits += int(recovery_ok)
        if case["conflict_expected"]:
            conflict_total += 1
            conflict_hits += int(conflict_ok)
        if prediction["latency_ms"] is not None:
            latencies.append(float(prediction["latency_ms"]))

        case_ok = all(
            (
                http_ok,
                business_ok,
                replay_ok,
                final_ok,
                owner_ok,
                recovery_ok,
                conflict_ok,
                evidence_ok,
                trace_ok,
                safe_retry_ok,
            )
        )
        case_hits += int(case_ok)
        per_case.append(
            {
                "case_id": case_id,
                "http_sequence_ok": http_ok,
                "business_effect_ok": business_ok,
                "replay_ok": replay_ok,
                "final_state_ok": final_ok,
                "state_owner_ok": owner_ok,
                "recovery_ok": recovery_ok,
                "conflict_ok": conflict_ok,
                "evidence_ok": evidence_ok,
                "trace_complete": trace_ok,
                "safe_retry_ok": safe_retry_ok,
                "case_pass": case_ok,
            }
        )

    case_count = len(cases)
    return {
        "case_count": case_count,
        "evidence_coverage_rate": _ratio(evidence_hits, case_count),
        "case_pass_rate": _ratio(case_hits, case_count),
        "transport_and_state": {
            "http_sequence_accuracy": _ratio(http_hits, case_count),
            "exact_business_effect_rate": _ratio(business_hits, case_count),
            "idempotent_replay_accuracy": _ratio(replay_hits, case_count),
            "final_state_accuracy": _ratio(final_hits, case_count),
            "state_owner_accuracy": _ratio(owner_hits, case_count),
            "recovery_success_rate": _ratio(recovery_hits, recovery_total),
            "conflict_detection_rate": _ratio(conflict_hits, conflict_total),
            "unsafe_retry_count": unsafe_retry_count,
        },
        "observability": {
            "trace_field_completeness_rate": _ratio(trace_hits, trace_total),
        },
        "efficiency": {
            "latency_sample_count": len(latencies),
            "latency_p50_ms": _percentile(latencies, 0.5),
            "latency_p95_ms": _percentile(latencies, 0.95),
        },
        "per_case": per_case,
    }


def prediction_template(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "http_sequence": [],
        "plan_version_count": 0,
        "replay_count": 0,
        "final_state": "not_run",
        "state_owner": "not_run",
        "recovery_verified": False,
        "conflict_detected": False,
        "trace_fields": [],
        "unsafe_retry_count": 0,
        "latency_ms": None,
        "evidence_level": "not_run",
        "run_ids": [],
        "resource_ids": {},
        "notes": "",
    }
