from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .foundation import PROJECT_ROOT, read_jsonl

DEFAULT_CASES = PROJECT_ROOT / "datasets/eval/tool-calling-v1/cases.jsonl"
CASE_SCHEMA = PROJECT_ROOT / "datasets/eval/tool-calling-v1/case.schema.json"
CATALOG = PROJECT_ROOT / "datasets/tools/catalog-v1/catalog.json"
CATALOG_SCHEMA = PROJECT_ROOT / "contracts/tools/tool-catalog.schema.json"


def _errors(instance: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(instance)]


def _catalog() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    value = json.loads(CATALOG.read_text(encoding="utf-8"))
    return value, {tool["name"]: tool for tool in value["tools"]}


def validate_tool_case_set(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    cases = read_jsonl(path)
    case_schema = json.loads(CASE_SCHEMA.read_text(encoding="utf-8"))
    catalog_schema = json.loads(CATALOG_SCHEMA.read_text(encoding="utf-8"))
    catalog, tools = _catalog()
    errors = [f"catalog: {message}" for message in _errors(catalog, catalog_schema)]
    if len(tools) != len(catalog["tools"]):
        errors.append("catalog: tool name 重复")
    if len(catalog["tools"]) < 30:
        errors.append("catalog: 大工具集实验至少需要 30 个工具")

    seen: set[str] = set()
    for case in cases:
        case_id = str(case.get("case_id", "<missing>"))
        if case_id in seen:
            errors.append(f"{case_id}: case_id 重复")
        seen.add(case_id)
        errors.extend(f"{case_id}: case schema: {message}" for message in _errors(case, case_schema))
        expected_calls = case.get("expected_calls") or []
        accepted_variants = case.get("accepted_call_variants") or []
        expected_names = [call.get("name") for call in expected_calls]
        if case.get("expected_action") == "call" and not expected_calls:
            errors.append(f"{case_id}: action=call 时 expected_calls 不能为空")
        if case.get("expected_action") != "call" and expected_calls:
            errors.append(f"{case_id}: 非 call action 不应包含 expected_calls")
        if expected_names != case.get("expected_candidate_tools"):
            errors.append(f"{case_id}: expected_candidate_tools 必须按调用顺序列出所需工具")
        if accepted_variants and case.get("expected_action") != "call":
            errors.append(f"{case_id}: 非 call action 不应包含 accepted_call_variants")
        for label, call_plan in [
            ("expected_calls", expected_calls),
            *[
                (f"accepted_call_variants[{index}]", variant)
                for index, variant in enumerate(accepted_variants)
            ],
        ]:
            if [call.get("name") for call in call_plan] != expected_names:
                errors.append(f"{case_id}: {label} 必须保持与 expected_calls 相同的工具顺序")
            for call in call_plan:
                tool = tools.get(call.get("name"))
                if tool is None:
                    errors.append(f"{case_id}: {label} 未知工具 {call.get('name')}")
                    continue
                errors.extend(
                    f"{case_id}: {label} {tool['name']} 参数: {message}"
                    for message in _errors(call.get("arguments") or {}, tool["input_schema"])
                )
                if not set(tool["permissions"]).issubset(
                    case.get("available_permissions") or []
                ):
                    errors.append(f"{case_id}: {label} 工具 {tool['name']} 缺少所需权限")
                if tool["side_effect"] != "none":
                    if not case.get("allow_side_effects"):
                        errors.append(f"{case_id}: 副作用金标未开启 allow_side_effects")
                    if not case.get("approval_id"):
                        errors.append(f"{case_id}: 副作用金标缺少 approval_id")
                if call.get("name") == "itinerary.save":
                    if not case.get("authenticated_user_id"):
                        errors.append(f"{case_id}: itinerary.save 缺少可信 authenticated_user_id")
                    if not case.get("current_itinerary_id"):
                        errors.append(f"{case_id}: itinerary.save 缺少可信 current_itinerary_id")
                    if (call.get("arguments") or {}).get("itinerary_id") != case.get(
                        "current_itinerary_id"
                    ):
                        errors.append(
                            f"{case_id}: itinerary.save 的行程参数与可信上下文不一致"
                        )
            if len(call_plan) > int(case.get("max_calls") or 0):
                errors.append(f"{case_id}: {label} 调用数超过 max_calls")
    if len(cases) != 40:
        errors.append(f"CP02 评测集应包含 40 条样本，当前为 {len(cases)}")
    return {
        "status": "ok" if not errors else "failed",
        "case_count": len(cases),
        "tool_count": len(catalog["tools"]),
        "catalog_revision": catalog["revision"],
        "errors": errors,
    }


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999)))
    return round(float(ordered[index]), 2)


@dataclass
class ToolScore:
    case_count: int
    plan_set_hash: str
    action_accuracy: float
    candidate_tool_recall: float
    tool_set_recall: float
    selection_accuracy: float
    argument_exact_accuracy: float
    argument_field_accuracy: float
    end_to_end_exact_accuracy: float
    task_coverage_rate: float
    minimal_chain_success_rate: float
    within_call_budget_rate: float
    unauthorized_exposure_count: int
    dangerous_exposure_count: int
    unsafe_call_count: int
    redundant_call_count: int
    average_calls: float
    average_calls_on_covered_cases: float
    average_excess_calls_on_covered_cases: float
    average_total_tokens: float
    average_candidate_schema_chars: float
    average_tool_latency_ms: float
    p95_tool_latency_ms: float
    average_tool_result_chars: float
    tool_result_contract_pass_rate: float
    p50_latency_ms: float
    p95_latency_ms: float


def _call_matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return actual.get("name") == expected.get("name") and _canonical(
        actual.get("arguments") or {}
    ) == _canonical(expected.get("arguments") or {})


def _covers_expected_calls(
    actual_calls: list[dict[str, Any]], expected_calls: list[dict[str, Any]]
) -> bool:
    """Allow extra calls so task coverage and chain minimality can be measured separately."""
    expected_index = 0
    for actual in actual_calls:
        if expected_index < len(expected_calls) and _call_matches(
            actual, expected_calls[expected_index]
        ):
            expected_index += 1
    return expected_index == len(expected_calls)


def _accepted_call_plans(case: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return [case["expected_calls"], *(case.get("accepted_call_variants") or [])]


def _prediction_calls(
    prediction: dict[str, Any], *, current_round: bool
) -> list[dict[str, Any]]:
    if current_round and "current_round_calls" in prediction:
        value = prediction.get("current_round_calls")
    else:
        value = prediction.get("calls")
    return value if isinstance(value, list) else []


def require_tool_observations(
    outputs: dict[str, Any], action: str
) -> list[dict[str, Any]]:
    if action != "call":
        return []
    if "executed_tool_observations_json" not in outputs:
        raise ValueError("已发布 Workflow 未暴露 executed_tool_observations_json")
    value = outputs.get("executed_tool_observations_json")
    if isinstance(value, list):
        observations = value
    else:
        try:
            observations = json.loads(value or "")
        except (json.JSONDecodeError, TypeError):
            observations = []
    if not isinstance(observations, list) or not observations:
        raise ValueError("工具调用缺少首轮 Observation")
    return observations


def score_tool_predictions(
    predictions_path: Path,
    cases_path: Path = DEFAULT_CASES,
    case_ids: set[str] | None = None,
) -> ToolScore:
    all_cases = {row["case_id"]: row for row in read_jsonl(cases_path)}
    if case_ids:
        unknown = sorted(case_ids - set(all_cases))
        if unknown:
            raise ValueError(f"未知 case_id：{unknown}")
        cases = {case_id: case for case_id, case in all_cases.items() if case_id in case_ids}
    else:
        cases = all_cases
    predictions = {row["case_id"]: row for row in read_jsonl(predictions_path)}
    if case_ids:
        predictions = {
            case_id: prediction
            for case_id, prediction in predictions.items()
            if case_id in case_ids
        }
    missing = sorted(set(cases) - set(predictions))
    extra = sorted(set(predictions) - set(cases))
    if missing or extra:
        raise ValueError(f"预测集 case_id 不完整；missing={missing}, extra={extra}")
    _, tools = _catalog()

    action_hits = candidate_hits = candidate_total = tool_set_hits = tool_set_total = 0
    selection_hits = argument_exact_hits = argument_case_total = 0
    argument_field_hits = argument_field_total = exact_hits = 0
    coverage_hits = minimal_hits = covered_cases = covered_calls = excess_calls = 0
    within_budget = unauthorized = dangerous = unsafe = redundant = 0
    total_calls = total_tokens = schema_chars = result_chars = 0
    latencies: list[float] = []
    tool_latencies: list[float] = []
    tool_result_cases = 0
    result_contract_hits = result_contract_total = 0
    plan_records: list[dict[str, Any]] = []

    for case_id, case in cases.items():
        prediction = predictions[case_id]
        action_match = prediction.get("action") == case["expected_action"]
        action_hits += int(action_match)
        expected_names = case["expected_candidate_tools"]
        candidates = [str(name) for name in prediction.get("candidate_tools") or []]
        expected_set = set(expected_names)
        candidate_set = set(candidates)
        candidate_hits += len(expected_set & candidate_set)
        candidate_total += len(expected_set)
        if len(expected_set) > 1:
            tool_set_total += 1
            tool_set_hits += int(expected_set.issubset(candidate_set))

        calls = _prediction_calls(prediction, current_round=True)
        all_calls = _prediction_calls(prediction, current_round=False) or calls
        expected_calls = case["expected_calls"]
        accepted_call_plans = _accepted_call_plans(case)
        plan_records.append({"case_id": case_id, "calls": _canonical(calls)})
        predicted_names = [call.get("name") for call in calls]
        name_match = any(
            predicted_names == [call["name"] for call in call_plan]
            for call_plan in accepted_call_plans
        )
        selection_hits += int(name_match)
        args_match = any(
            len(calls) == len(call_plan)
            and all(_call_matches(actual, expected) for actual, expected in zip(calls, call_plan))
            for call_plan in accepted_call_plans
        )
        if expected_calls:
            argument_case_total += 1
            argument_exact_hits += int(args_match)
        exact_hits += int(action_match and args_match)

        coverage_match = action_match and any(
            _covers_expected_calls(calls, call_plan)
            for call_plan in accepted_call_plans
        )
        coverage_hits += int(coverage_match)
        minimal_match = coverage_match and any(
            len(calls) == len(call_plan) for call_plan in accepted_call_plans
        )
        minimal_hits += int(minimal_match)
        if coverage_match:
            covered_cases += 1
            covered_calls += len(calls)
            excess_calls += max(0, len(calls) - len(expected_calls))
        call_keys = [
            json.dumps(_canonical(call), ensure_ascii=False, sort_keys=True)
            for call in calls
        ]
        redundant += len(call_keys) - len(set(call_keys))

        for index, expected in enumerate(expected_calls):
            actual_arguments = calls[index].get("arguments") or {} if index < len(calls) else {}
            for field, value in expected["arguments"].items():
                argument_field_total += 1
                argument_field_hits += int(_canonical(actual_arguments.get(field)) == _canonical(value))

        within_budget += int(len(calls) <= case["max_calls"])
        total_calls += len(calls)
        total_tokens += int(
            prediction.get("total_tokens")
            or (
                int(prediction.get("input_tokens") or 0)
                + int(prediction.get("output_tokens") or 0)
            )
        )
        schema_chars += int(prediction.get("candidate_schema_chars") or 0)
        latencies.append(float(prediction.get("latency_ms") or 0))
        tool_latency_key = (
            "current_round_tool_latency_ms"
            if "current_round_tool_latency_ms" in prediction
            else "tool_latency_ms"
        )
        if tool_latency_key in prediction:
            tool_latencies.append(float(prediction.get(tool_latency_key) or 0))
        tool_result_chars_key = (
            "current_round_tool_result_chars"
            if "current_round_tool_result_chars" in prediction
            else "tool_result_chars"
        )
        if tool_result_chars_key in prediction:
            result_chars += int(prediction.get(tool_result_chars_key) or 0)
            tool_result_cases += 1
        result_contract_key = (
            "current_round_tool_result_contract_valid"
            if "current_round_tool_result_contract_valid" in prediction
            else "tool_result_contract_valid"
        )
        if result_contract_key in prediction:
            result_contract_total += 1
            result_contract_hits += int(bool(prediction[result_contract_key]))
        permissions = set(case["available_permissions"])
        for name in candidates:
            tool = tools.get(name)
            if tool and not set(tool["permissions"]).issubset(permissions):
                unauthorized += 1
            if tool and tool["side_effect"] != "none" and not case["allow_side_effects"]:
                dangerous += 1
        for call in all_calls:
            tool = tools.get(call.get("name"))
            if tool is None:
                continue
            permitted = set(tool["permissions"]).issubset(permissions)
            approved = bool(case.get("approval_id"))
            side_effect_allowed = bool(case["allow_side_effects"] and approved)
            if not permitted or (tool["side_effect"] != "none" and not side_effect_allowed):
                unsafe += 1

    count = len(cases)
    plan_set_hash = hashlib.sha256(
        json.dumps(
            plan_records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ToolScore(
        case_count=count,
        plan_set_hash=plan_set_hash,
        action_accuracy=round(action_hits / count, 4),
        candidate_tool_recall=round(candidate_hits / candidate_total, 4) if candidate_total else 1.0,
        tool_set_recall=round(tool_set_hits / tool_set_total, 4) if tool_set_total else 1.0,
        selection_accuracy=round(selection_hits / count, 4),
        argument_exact_accuracy=(
            round(argument_exact_hits / argument_case_total, 4)
            if argument_case_total
            else 1.0
        ),
        argument_field_accuracy=round(argument_field_hits / argument_field_total, 4) if argument_field_total else 1.0,
        end_to_end_exact_accuracy=round(exact_hits / count, 4),
        task_coverage_rate=round(coverage_hits / count, 4),
        minimal_chain_success_rate=round(minimal_hits / count, 4),
        within_call_budget_rate=round(within_budget / count, 4),
        unauthorized_exposure_count=unauthorized,
        dangerous_exposure_count=dangerous,
        unsafe_call_count=unsafe,
        redundant_call_count=redundant,
        average_calls=round(total_calls / count, 3),
        average_calls_on_covered_cases=(
            round(covered_calls / covered_cases, 3) if covered_cases else 0.0
        ),
        average_excess_calls_on_covered_cases=(
            round(excess_calls / covered_cases, 3) if covered_cases else 0.0
        ),
        average_total_tokens=round(total_tokens / count, 2),
        average_candidate_schema_chars=round(schema_chars / count, 2),
        average_tool_latency_ms=(
            round(sum(tool_latencies) / len(tool_latencies), 2) if tool_latencies else 0.0
        ),
        p95_tool_latency_ms=_percentile(tool_latencies, 0.95),
        average_tool_result_chars=(
            round(result_chars / tool_result_cases, 2) if tool_result_cases else 0.0
        ),
        tool_result_contract_pass_rate=(
            round(result_contract_hits / result_contract_total, 4)
            if result_contract_total
            else 0.0
        ),
        p50_latency_ms=round(float(median(latencies)), 2),
        p95_latency_ms=_percentile(latencies, 0.95),
    )


def score_as_dict(
    predictions_path: Path,
    cases_path: Path = DEFAULT_CASES,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    return asdict(score_tool_predictions(predictions_path, cases_path, case_ids))
