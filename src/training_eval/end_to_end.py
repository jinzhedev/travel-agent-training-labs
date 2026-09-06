from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .foundation import PROJECT_ROOT, read_jsonl

DEFAULT_CASES = PROJECT_ROOT / "datasets/eval/end-to-end/v1/cases.jsonl"
CASE_SCHEMA = PROJECT_ROOT / "datasets/eval/end-to-end/v1/case.schema.json"
NATURAL_MULTI_OWNER_QUERY = (
    "我下周去厦门玩三天，帮我规划下行程。另外提醒我出发前把水费交了，"
    "能直接办的话就帮我办掉。"
)


def validate_end_to_end_case_set(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    cases = read_jsonl(path)
    schema = json.loads(CASE_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    seen: set[str] = set()

    if len(cases) != 12:
        errors.append(f"综合端到端金标应为 12 条，当前 {len(cases)} 条")

    for case in cases:
        case_id = str(case.get("case_id", "<missing>"))
        if case_id in seen:
            errors.append(f"{case_id}: case_id 重复")
        seen.add(case_id)
        errors.extend(
            f"{case_id}: {error.message}"
            for error in sorted(
                validator.iter_errors(case), key=lambda item: list(item.path)
            )
        )

        capabilities = set(case.get("expected_capabilities") or [])
        stages = set(case.get("required_subworkflow_stages") or [])
        if "document_rag" in stages and "document_rag" not in capabilities:
            errors.append(f"{case_id}: 调用 document_rag 前必须声明对应 capability")
        if "tool_calling" in stages and "tool_agent" not in capabilities:
            errors.append(f"{case_id}: 调用 tool_calling 前必须声明对应 capability")
        if case.get("expected_initial_status") == "pending_human":
            if case.get("simulated_human_action") == "none":
                errors.append(f"{case_id}: pending_human 必须给出模拟人工动作")
        elif case.get("simulated_human_action") != "none":
            errors.append(f"{case_id}: 非 pending_human 不应给出人工动作")

        if set(case.get("unsupported_requests") or []) and case.get(
            "max_side_effect_count"
        ):
            errors.append(f"{case_id}: 不支持的请求不得产生副作用")

    multi_owner = next(
        (case for case in cases if case.get("case_id") == "E2E-005"), {}
    )
    if multi_owner.get("query") != NATURAL_MULTI_OWNER_QUERY:
        errors.append("E2E-005: 文旅与生活双任务必须使用已确认的自然 query")
    if set(multi_owner.get("expected_owners") or []) != {"travel", "life"}:
        errors.append("E2E-005: 必须同时分配 travel 与 life")
    if set(multi_owner.get("unsupported_requests") or []) != {"创建提醒", "代缴水费"}:
        errors.append("E2E-005: 必须显式标记提醒与代缴能力边界")

    return {
        "status": "ok" if not errors else "failed",
        "case_count": len(cases),
        "errors": errors,
    }
