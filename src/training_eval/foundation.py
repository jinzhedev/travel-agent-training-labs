from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = PROJECT_ROOT / "datasets/eval/foundation-v1/cases.jsonl"
CASE_SCHEMA = PROJECT_ROOT / "datasets/eval/foundation-v1/case.schema.json"
REQUEST_SCHEMA = PROJECT_ROOT / "contracts/request/travel-request.schema.json"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: JSON 无效：{exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: 每行必须是 JSON 对象")
        rows.append(value)
    return rows


def _errors(instance: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(instance)]


def validate_case_set(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    cases = read_jsonl(path)
    case_schema = json.loads(CASE_SCHEMA.read_text(encoding="utf-8"))
    request_schema = json.loads(REQUEST_SCHEMA.read_text(encoding="utf-8"))
    errors: list[str] = []
    seen: set[str] = set()
    for case in cases:
        case_id = str(case.get("case_id", "<missing>"))
        if case_id in seen:
            errors.append(f"{case_id}: case_id 重复")
        seen.add(case_id)
        errors.extend(f"{case_id}: case schema: {item}" for item in _errors(case, case_schema))
        expected = case.get("expected_request")
        if isinstance(expected, dict):
            errors.extend(
                f"{case_id}: request schema: {item}"
                for item in _errors(expected, request_schema)
            )
        if expected and expected.get("free_text") != case.get("input"):
            errors.append(f"{case_id}: expected_request.free_text 必须保留原始输入")
    if not 10 <= len(cases) <= 15:
        errors.append(f"基础集应包含 10–15 条样本，当前为 {len(cases)}")
    return {
        "status": "ok" if not errors else "failed",
        "case_count": len(cases),
        "categories": dict(sorted(Counter(case["category"] for case in cases).items())),
        "errors": errors,
    }


def _list_recall(expected: list[Any], actual: list[Any]) -> tuple[int, int]:
    expected_set = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in expected}
    actual_set = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in actual}
    return len(expected_set & actual_set), len(expected_set)


@dataclass
class Score:
    case_count: int
    schema_valid_rate: float
    scalar_accuracy: float
    list_recall: float
    list_precision: float
    decision_accuracy: float
    clarification_recall: float
    unsupported_inference_count: int


def score_predictions(predictions_path: Path, cases_path: Path = DEFAULT_CASES) -> Score:
    cases = {row["case_id"]: row for row in read_jsonl(cases_path)}
    predictions = {row["case_id"]: row for row in read_jsonl(predictions_path)}
    request_schema = json.loads(REQUEST_SCHEMA.read_text(encoding="utf-8"))
    missing = sorted(set(cases) - set(predictions))
    extra = sorted(set(predictions) - set(cases))
    if missing or extra:
        raise ValueError(f"预测集 case_id 不完整；missing={missing}, extra={extra}")

    schema_valid = scalar_hits = scalar_total = list_hits = list_total = 0
    list_predicted = decision_hits = clarification_hits = clarification_total = 0
    unsupported_inferences = 0
    scalar_fields = ["destination", "start_date", "end_date", "budget_cny"]
    list_fields = [
        "travelers",
        "preferences",
        "hard_constraints",
        "must_visit",
        "avoid",
    ]
    for case_id, case in cases.items():
        prediction = predictions[case_id]
        actual = prediction.get("request") or {}
        expected = case["expected_request"]
        schema_valid += int(not _errors(actual, request_schema))
        for field in scalar_fields:
            scalar_total += 1
            scalar_hits += int(actual.get(field) == expected.get(field))
            if expected.get(field) is None and actual.get(field) is not None:
                unsupported_inferences += 1
        for field in list_fields:
            hits, total = _list_recall(expected.get(field, []), actual.get(field, []))
            list_hits += hits
            list_total += total
            list_predicted += len(
                {
                    json.dumps(item, ensure_ascii=False, sort_keys=True)
                    for item in actual.get(field, [])
                }
            )
        decision_hits += int(prediction.get("decision") == case["expected_decision"])
        hits, total = _list_recall(
            case["required_clarifications"], prediction.get("clarifications") or []
        )
        clarification_hits += hits
        clarification_total += total
    count = len(cases)
    return Score(
        case_count=count,
        schema_valid_rate=round(schema_valid / count, 4),
        scalar_accuracy=round(scalar_hits / scalar_total, 4),
        list_recall=round(list_hits / list_total, 4) if list_total else 1.0,
        list_precision=round(list_hits / list_predicted, 4) if list_predicted else 1.0,
        decision_accuracy=round(decision_hits / count, 4),
        clarification_recall=(
            round(clarification_hits / clarification_total, 4)
            if clarification_total
            else 1.0
        ),
        unsupported_inference_count=unsupported_inferences,
    )


def score_as_dict(predictions_path: Path, cases_path: Path = DEFAULT_CASES) -> dict[str, Any]:
    return asdict(score_predictions(predictions_path, cases_path))
