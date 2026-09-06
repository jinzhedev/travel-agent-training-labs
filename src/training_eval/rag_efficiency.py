from __future__ import annotations

import json
from pathlib import Path
from typing import Any

QUALITY_KEYS = [
    "page_recall_at_k",
    "object_recall_at_k",
    "fine_grained_recall_at_k",
    "complete_evidence_rate",
    "ndcg_at_k",
    "support_status_accuracy",
    "unanswerable_accuracy",
    "dependent_query_formed_rate",
]


def _score(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if "score" in value:
        return value["score"]
    if "prediction_score" in value:
        return value["prediction_score"]
    raise ValueError(f"{path} 中没有 score 或 prediction_score")


def _no_quality_drop(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, list[str]]:
    failures = [
        key
        for key in QUALITY_KEYS
        if float(after["retrieval"].get(key) or 0) < float(before["retrieval"].get(key) or 0)
    ]
    before_generation = before.get("generation") or {}
    after_generation = after.get("generation") or {}
    if before_generation.get("status") == after_generation.get("status") == "completed":
        for key in ("answer_checklist_accuracy", "citation_precision", "citation_recall"):
            if float(after_generation.get(key) or 0) < float(before_generation.get(key) or 0):
                failures.append(f"generation.{key}")
    return not failures, failures


def compare_rag_efficiency(paths: list[Path]) -> dict[str, Any]:
    if len(paths) != 5:
        raise ValueError("RAG 效率实验需要 R0–R4 五份报告")
    scores = [_score(path) for path in paths]
    checks: list[dict[str, Any]] = []
    for index in range(1, 5):
        quality_ok, quality_failures = _no_quality_drop(scores[index - 1], scores[index])
        checks.append(
            {
                "comparison": f"R{index - 1}→R{index}",
                "quality_gate": quality_ok,
                "quality_regressions": quality_failures,
            }
        )
    checks[0]["ranking_gain"] = (
        scores[1]["retrieval"]["ndcg_at_k"] > scores[0]["retrieval"]["ndcg_at_k"]
    )
    checks[1]["rerank_latency_reduced"] = (
        scores[2]["efficiency"]["simulated_pre_generation_p95_ms"]
        < scores[1]["efficiency"]["simulated_pre_generation_p95_ms"]
    )
    checks[2]["context_reduced"] = (
        scores[3]["efficiency"]["average_context_chars"]
        < scores[2]["efficiency"]["average_context_chars"]
    )
    checks[3]["retrieval_proxy_non_increasing"] = (
        scores[4]["efficiency"]["simulated_pre_generation_p95_ms"]
        <= scores[3]["efficiency"]["simulated_pre_generation_p95_ms"]
    )
    passed = all(
        check["quality_gate"]
        and all(value for key, value in check.items() if key not in {"comparison", "quality_gate", "quality_regressions"})
        for check in checks
    )
    return {"status": "passed" if passed else "failed", "checks": checks}
