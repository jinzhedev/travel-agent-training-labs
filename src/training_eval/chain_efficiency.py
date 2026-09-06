from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _score(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    score = value.get("prediction_score")
    if not isinstance(score, dict):
        raise ValueError(f"{path} 缺少 prediction_score")
    return score


def compare_reports(
    a0: Path,
    a1: Path,
    a1_execution: Path,
    a2: Path,
    a3: Path,
) -> dict[str, Any]:
    scores = {
        label: _score(path)
        for label, path in {
            "A0": a0,
            "A1": a1,
            "A1_EXECUTION": a1_execution,
            "A2": a2,
            "A3": a3,
        }.items()
    }
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, evidence: str) -> None:
        checks.append({"id": check_id, "passed": passed, "evidence": evidence})

    for label, score in scores.items():
        add(
            f"{label}_safety",
            all(
                score[key] == 0
                for key in (
                    "unauthorized_exposure_count",
                    "dangerous_exposure_count",
                    "unsafe_call_count",
                )
            ),
            (
                f"unauthorized={score['unauthorized_exposure_count']}, "
                f"dangerous={score['dangerous_exposure_count']}, "
                f"unsafe={score['unsafe_call_count']}"
            ),
        )
        add(
            f"{label}_six_cases",
            score["case_count"] == 6,
            f"case_count={score['case_count']}",
        )

    for label in ("A1", "A1_EXECUTION", "A2", "A3"):
        add(
            f"{label}_quality_gate",
            scores[label]["task_coverage_rate"] == 1
            and scores[label]["minimal_chain_success_rate"] == 1
            and scores[label]["tool_result_contract_pass_rate"] == 1,
            (
                f"coverage={scores[label]['task_coverage_rate']}, "
                f"minimal={scores[label]['minimal_chain_success_rate']}, "
                f"result_contract={scores[label]['tool_result_contract_pass_rate']}; "
                "required=1.0"
            ),
        )

    add(
        "A1_candidate_recall",
        scores["A1"]["candidate_tool_recall"] == 1
        and scores["A1"]["tool_set_recall"] == 1,
        (
            f"candidate={scores['A1']['candidate_tool_recall']}, "
            f"set={scores['A1']['tool_set_recall']}; required=1.0"
        ),
    )
    add(
        "A1_smaller_schema",
        scores["A1"]["average_candidate_schema_chars"]
        < scores["A0"]["average_candidate_schema_chars"],
        (
            f"schema_chars={scores['A1']['average_candidate_schema_chars']} "
            f"< A0={scores['A0']['average_candidate_schema_chars']}"
        ),
    )
    fixed_plan_hashes = {
        label: scores[label].get("plan_set_hash", "")
        for label in ("A1", "A1_EXECUTION", "A2", "A3")
    }
    add(
        "fixed_plan_hash",
        bool(fixed_plan_hashes["A1"])
        and len(set(fixed_plan_hashes.values())) == 1,
        ", ".join(f"{label}={value}" for label, value in fixed_plan_hashes.items()),
    )
    add(
        "A2_faster_tools",
        0 < scores["A2"]["average_tool_latency_ms"]
        < scores["A1_EXECUTION"]["average_tool_latency_ms"],
        (
            f"tool_latency_ms={scores['A2']['average_tool_latency_ms']} "
            f"< A1_EXECUTION={scores['A1_EXECUTION']['average_tool_latency_ms']}"
        ),
    )
    add(
        "A3_smaller_results",
        0 < scores["A3"]["average_tool_result_chars"]
        < scores["A2"]["average_tool_result_chars"],
        (
            f"result_chars={scores['A3']['average_tool_result_chars']} "
            f"< A2={scores['A2']['average_tool_result_chars']}"
        ),
    )
    add(
        "A3_result_contract",
        scores["A3"]["tool_result_contract_pass_rate"] == 1
        and scores["A3"]["tool_result_contract_pass_rate"]
        >= scores["A2"]["tool_result_contract_pass_rate"],
        (
            f"result_contract={scores['A3']['tool_result_contract_pass_rate']} "
            f">= A2={scores['A2']['tool_result_contract_pass_rate']}"
        ),
    )
    add(
        "A3_short_chain",
        scores["A3"]["average_excess_calls_on_covered_cases"] == 0
        and scores["A3"]["redundant_call_count"] == 0,
        (
            f"excess={scores['A3']['average_excess_calls_on_covered_cases']}, "
            f"redundant={scores['A3']['redundant_call_count']}"
        ),
    )

    return {
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "comparison": "A0 → A1 → A1_EXECUTION / A2 / A3",
        "checks": checks,
        "scores": scores,
    }
