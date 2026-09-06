from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .foundation import PROJECT_ROOT, read_jsonl

DATASET_ROOT = PROJECT_ROOT / "datasets/eval/continuous-improvement-v1"
DEFAULT_TRACES = DATASET_ROOT / "production-traces.jsonl"
DEFAULT_CASES = DATASET_ROOT / "cases.jsonl"
CASE_SCHEMA = DATASET_ROOT / "case.schema.json"
PREDICTION_SCHEMA = DATASET_ROOT / "prediction.schema.json"
DEFAULT_CALIBRATION = DATASET_ROOT / "grader-calibration.jsonl"
DEFAULT_CANDIDATES = DATASET_ROOT / "release-candidates.jsonl"
RELEASE_DECISION_SCHEMA = DATASET_ROOT / "release-decision.schema.json"

EXPECTED_TRACE_COUNT = 13
EXPECTED_CALIBRATION_COUNT = 10
EXPECTED_CANDIDATE_ACTIONS = {
    "release-current": "retain_current",
    "release-candidate-prompt": "block_offline",
    "release-candidate-model": "advance_canary",
    "release-candidate-mixed": "rerun_controlled",
}


def _ratio(hits: int | float, total: int | float) -> float:
    return round(float(hits) / total, 4) if total else 0.0


def _schema_validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))


def analyze_grader_calibration(
    path: Path = DEFAULT_CALIBRATION,
) -> dict[str, Any]:
    rows = read_jsonl(path)
    agreement = false_positive = false_negative = 0
    per_slice: dict[str, dict[str, int]] = {}
    for row in rows:
        human = row.get("human_label")
        judge = row.get("judge_label")
        slice_name = str(row.get("slice"))
        item = per_slice.setdefault(slice_name, {"count": 0, "agreement": 0})
        item["count"] += 1
        if human == judge:
            agreement += 1
            item["agreement"] += 1
        if human == "pass" and judge == "fail":
            false_positive += 1
        if human == "fail" and judge == "pass":
            false_negative += 1
    return {
        "case_count": len(rows),
        "agreement_rate": _ratio(agreement, len(rows)),
        "judge_false_positive_count": false_positive,
        "judge_false_negative_count": false_negative,
        "per_slice": {
            key: {
                **value,
                "agreement_rate": _ratio(value["agreement"], value["count"]),
            }
            for key, value in sorted(per_slice.items())
        },
    }


def validate_continuous_improvement_dataset(
    traces_path: Path = DEFAULT_TRACES,
    cases_path: Path = DEFAULT_CASES,
    calibration_path: Path = DEFAULT_CALIBRATION,
    candidates_path: Path = DEFAULT_CANDIDATES,
) -> dict[str, Any]:
    traces = read_jsonl(traces_path)
    cases = read_jsonl(cases_path)
    calibration = read_jsonl(calibration_path)
    candidates = read_jsonl(candidates_path)
    case_validator = _schema_validator(CASE_SCHEMA)
    errors: list[str] = []

    if len(traces) != EXPECTED_TRACE_COUNT:
        errors.append(
            f"课程 Trace 应为 {EXPECTED_TRACE_COUNT} 条，当前 {len(traces)} 条"
        )
    if len(cases) != EXPECTED_TRACE_COUNT:
        errors.append(
            f"课程策展 case 应为 {EXPECTED_TRACE_COUNT} 条，当前 {len(cases)} 条"
        )
    if len(calibration) != EXPECTED_CALIBRATION_COUNT:
        errors.append(
            "grader calibration 应为 "
            f"{EXPECTED_CALIBRATION_COUNT} 条，当前 {len(calibration)} 条"
        )

    trace_ids: set[str] = set()
    for trace in traces:
        trace_id = str(trace.get("trace_id", "<missing>"))
        if trace_id in trace_ids:
            errors.append(f"{trace_id}: trace_id 重复")
        trace_ids.add(trace_id)
        for field in (
            "release_manifest_id",
            "cohort",
            "task_type",
            "trajectory",
            "business_outcome",
            "user_feedback",
            "operational",
            "privacy",
            "evaluator_signal",
        ):
            if field not in trace:
                errors.append(f"{trace_id}: 缺少 {field}")
        if trace.get("evidence_level") != "frozen_trace":
            errors.append(f"{trace_id}: evidence_level 必须为 frozen_trace")
        if trace.get("is_simulated") is not True:
            errors.append(f"{trace_id}: is_simulated 必须为 true")

    case_ids: set[str] = set()
    referenced_trace_ids: set[str] = set()
    for case in cases:
        case_id = str(case.get("case_id", "<missing>"))
        if case_id in case_ids:
            errors.append(f"{case_id}: case_id 重复")
        case_ids.add(case_id)
        referenced_trace_ids.add(str(case.get("trace_id")))
        errors.extend(
            f"{case_id}: {error.message}"
            for error in sorted(
                case_validator.iter_errors(case), key=lambda item: list(item.path)
            )
        )
    if trace_ids != referenced_trace_ids:
        errors.append(
            "Trace 与 case 不是一一对应；"
            f"unreferenced={sorted(trace_ids - referenced_trace_ids)}, "
            f"unknown={sorted(referenced_trace_ids - trace_ids)}"
        )

    calibration_ids: set[str] = set()
    calibration_sources: set[str] = set()
    for row in calibration:
        calibration_id = str(row.get("calibration_id", "<missing>"))
        if calibration_id in calibration_ids:
            errors.append(f"{calibration_id}: calibration_id 重复")
        calibration_ids.add(calibration_id)
        calibration_sources.add(str(row.get("source")))
        if row.get("human_label") not in {"pass", "fail"}:
            errors.append(f"{calibration_id}: human_label 无效")
        if row.get("judge_label") not in {"pass", "fail"}:
            errors.append(f"{calibration_id}: judge_label 无效")
    unknown_sources = calibration_sources - trace_ids
    if unknown_sources:
        errors.append(f"grader calibration 引用了未知 Trace：{sorted(unknown_sources)}")

    candidate_ids: set[str] = set()
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id", "<missing>"))
        if candidate_id in candidate_ids:
            errors.append(f"{candidate_id}: candidate_id 重复")
        candidate_ids.add(candidate_id)
        if candidate.get("evidence_level") != "offline_only":
            errors.append(f"{candidate_id}: evidence_level 必须为 offline_only")
        if candidate.get("is_simulated") is not True:
            errors.append(f"{candidate_id}: is_simulated 必须为 true")
        expected_action = EXPECTED_CANDIDATE_ACTIONS.get(candidate_id)
        if candidate.get("expected_release_action") != expected_action:
            errors.append(f"{candidate_id}: expected_release_action 与课程契约不一致")
    if candidate_ids != set(EXPECTED_CANDIDATE_ACTIONS):
        errors.append(
            "候选集不完整；"
            f"expected={sorted(EXPECTED_CANDIDATE_ACTIONS)}, "
            f"actual={sorted(candidate_ids)}"
        )

    return {
        "status": "ok" if not errors else "error",
        "trace_count": len(traces),
        "case_count": len(cases),
        "calibration": analyze_grader_calibration(calibration_path),
        "candidate_count": len(candidates),
        "errors": errors,
    }


def _validate_prediction_rows(
    path: Path,
    schema_path: Path,
    id_field: str,
) -> list[str]:
    validator = _schema_validator(schema_path)
    errors: list[str] = []
    seen: set[str] = set()
    for row in read_jsonl(path):
        item_id = str(row.get(id_field, "<missing>"))
        if item_id in seen:
            errors.append(f"{item_id}: {id_field} 重复")
        seen.add(item_id)
        errors.extend(
            f"{item_id}: {error.message}"
            for error in sorted(
                validator.iter_errors(row), key=lambda item: list(item.path)
            )
        )
    return errors


def score_curation_predictions(
    predictions_path: Path,
    cases_path: Path = DEFAULT_CASES,
) -> dict[str, Any]:
    errors = _validate_prediction_rows(
        predictions_path, PREDICTION_SCHEMA, "case_id"
    )
    if errors:
        raise ValueError("策展预测格式无效：" + "; ".join(errors))
    cases = {row["case_id"]: row for row in read_jsonl(cases_path)}
    predictions = {
        row["case_id"]: row for row in read_jsonl(predictions_path)
    }
    missing = sorted(set(cases) - set(predictions))
    extra = sorted(set(predictions) - set(cases))
    if missing or extra:
        raise ValueError(f"策展预测 case_id 不完整；missing={missing}, extra={extra}")

    fields = {
        "review_queue": "expected_review_queue",
        "first_deviation_layer": "expected_first_deviation_layer",
        "failure_family": "expected_failure_family",
        "privacy_action": "expected_privacy_action",
        "dataset_action": "expected_dataset_action",
        "target_suite": "expected_target_suite",
        "review_mode": "expected_review_mode",
    }
    hits = Counter()
    case_hits = 0
    privacy_mishandled_count = duplicate_overweight_count = unsafe_drop_count = 0
    per_case: list[dict[str, Any]] = []
    for case_id, case in cases.items():
        prediction = predictions[case_id]
        checks = {
            field: prediction[field] == case[gold_field]
            for field, gold_field in fields.items()
        }
        hits.update(field for field, passed in checks.items() if passed)
        case_pass = all(checks.values()) and prediction["evidence_level"] == "offline_only"
        case_hits += int(case_pass)
        if (
            case["expected_privacy_action"] == "redact_then_review"
            and prediction["privacy_action"] != "redact_then_review"
        ):
            privacy_mishandled_count += 1
        if (
            case["expected_dataset_action"] == "link_duplicate"
            and prediction["dataset_action"]
            in {"promote_regression", "promote_rolling", "promote_safety"}
        ):
            duplicate_overweight_count += 1
        if (
            case["expected_target_suite"] == "safety"
            and case["expected_dataset_action"]
            in {"promote_safety", "retain_control"}
            and prediction["dataset_action"] in {"do_not_promote", "link_duplicate"}
        ):
            unsafe_drop_count += 1
        per_case.append({"case_id": case_id, **checks, "case_pass": case_pass})

    count = len(cases)
    return {
        "case_count": count,
        "case_pass_rate": _ratio(case_hits, count),
        "field_accuracy": {
            field: _ratio(hits[field], count) for field in fields
        },
        "safety": {
            "privacy_mishandled_count": privacy_mishandled_count,
            "duplicate_overweight_count": duplicate_overweight_count,
            "unsafe_drop_count": unsafe_drop_count,
        },
        "per_case": per_case,
    }


def score_release_decisions(
    decisions_path: Path,
    candidates_path: Path = DEFAULT_CANDIDATES,
) -> dict[str, Any]:
    errors = _validate_prediction_rows(
        decisions_path, RELEASE_DECISION_SCHEMA, "candidate_id"
    )
    if errors:
        raise ValueError("发布判断格式无效：" + "; ".join(errors))
    candidates = {
        row["candidate_id"]: row for row in read_jsonl(candidates_path)
    }
    decisions = {
        row["candidate_id"]: row for row in read_jsonl(decisions_path)
    }
    missing = sorted(set(candidates) - set(decisions))
    extra = sorted(set(decisions) - set(candidates))
    if missing or extra:
        raise ValueError(f"发布判断 candidate_id 不完整；missing={missing}, extra={extra}")

    action_hits = reason_hits = 0
    hard_gate_bypass_count = unattributable_advance_count = 0
    per_candidate: list[dict[str, Any]] = []
    for candidate_id, candidate in candidates.items():
        decision = decisions[candidate_id]
        action_ok = decision["release_action"] == candidate["expected_release_action"]
        reasons_ok = set(decision["blocking_reasons"]) == set(
            candidate["expected_blocking_reasons"]
        )
        action_hits += int(action_ok)
        reason_hits += int(reasons_ok)
        if (
            any(candidate["hard_gates"].values())
            and decision["release_action"] == "advance_canary"
        ):
            hard_gate_bypass_count += 1
        if (
            len(candidate["change_scope"]["variables"]) > 1
            and decision["release_action"] == "advance_canary"
        ):
            unattributable_advance_count += 1
        per_candidate.append(
            {
                "candidate_id": candidate_id,
                "release_action_ok": action_ok,
                "blocking_reasons_exact_match": reasons_ok,
                "candidate_pass": action_ok and reasons_ok,
            }
        )
    count = len(candidates)
    return {
        "candidate_count": count,
        "release_action_accuracy": _ratio(action_hits, count),
        "blocking_reasons_exact_match_rate": _ratio(reason_hits, count),
        "hard_gate_bypass_count": hard_gate_bypass_count,
        "unattributable_advance_count": unattributable_advance_count,
        "per_candidate": per_candidate,
    }
