from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median
from typing import Any

from jsonschema import Draft202012Validator

from .foundation import PROJECT_ROOT, read_jsonl

DEFAULT_CASES = PROJECT_ROOT / "datasets/eval/rag-v1/cases.jsonl"
CASE_SCHEMA = PROJECT_ROOT / "datasets/eval/rag-v1/case.schema.json"
OBJECT_CORPUS = PROJECT_ROOT / "datasets/rag/document-ai/v1/parsed/object-aware.jsonl"
TEXT_CORPUS = PROJECT_ROOT / "datasets/rag/document-ai/v1/parsed/text-only.jsonl"
PLANS = PROJECT_ROOT / "datasets/rag/multihop/v1/plans.jsonl"


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999)))
    return round(float(ordered[index]), 2)


def _ratio(hits: int | float, total: int | float) -> float:
    return round(float(hits) / total, 4) if total else 0.0


def validate_rag_case_set(path: Path = DEFAULT_CASES) -> dict[str, Any]:
    cases = read_jsonl(path)
    schema = json.loads(CASE_SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    objects = read_jsonl(OBJECT_CORPUS)
    text_chunks = read_jsonl(TEXT_CORPUS)
    plans = {item["plan_id"]: item for item in read_jsonl(PLANS)}
    object_by_id = {item["object_id"]: item for item in objects}
    fine_ids = {fine_id for item in objects for fine_id in item.get("fine_grained_ids") or []}
    document_pages = {(item["document_id"], item["page"]) for item in objects}
    errors: list[str] = []
    seen: set[str] = set()

    if len(objects) != 14:
        errors.append(f"对象语料应为 14 条，当前 {len(objects)} 条")
    if len(text_chunks) != 7:
        errors.append(f"文本基线应为 7 页，当前 {len(text_chunks)} 页")
    if len(cases) != 12:
        errors.append(f"CP03 测评集应为 12 条，当前 {len(cases)} 条")

    for case in cases:
        case_id = str(case.get("case_id", "<missing>"))
        if case_id in seen:
            errors.append(f"{case_id}: case_id 重复")
        seen.add(case_id)
        errors.extend(
            f"{case_id}: {error.message}"
            for error in sorted(validator.iter_errors(case), key=lambda item: list(item.path))
        )
        if case.get("answerable") and not case.get("gold_object_ids"):
            errors.append(f"{case_id}: 可回答题必须标注 gold_object_ids")
        if not case.get("answerable") and any(
            case.get(field)
            for field in ("gold_document_ids", "gold_pages", "gold_object_ids", "gold_fine_ids")
        ):
            errors.append(f"{case_id}: 拒答题不应含金标证据")
        for object_id in case.get("gold_object_ids") or []:
            if object_id not in object_by_id:
                errors.append(f"{case_id}: 未知 gold_object_id {object_id}")
        for fine_id in case.get("gold_fine_ids") or []:
            if fine_id not in fine_ids:
                errors.append(f"{case_id}: 未知 gold_fine_id {fine_id}")
        gold_docs = set(case.get("gold_document_ids") or [])
        for page in case.get("gold_pages") or []:
            if gold_docs and not any(
                (document_id, page) in document_pages for document_id in gold_docs
            ):
                errors.append(f"{case_id}: page={page} 不属于任一 gold_document_id")
        plan_id = case.get("evidence_plan_id")
        if case.get("mode") == "multihop" and plan_id not in plans:
            errors.append(f"{case_id}: multihop 缺少有效 evidence_plan_id")
        if case.get("mode") == "single" and plan_id:
            errors.append(f"{case_id}: single 模式不应引用 evidence_plan_id")

    dependent = plans.get("PLAN-ACCESS-OPENING", {})
    dependent_slots = dependent.get("slots") or []
    if not any(
        slot.get("depends_on") and "{entities}" in (slot.get("query_template") or "")
        for slot in dependent_slots
    ):
        errors.append("PLAN-ACCESS-OPENING 必须含使用上一跳实体的依赖查询")

    return {
        "status": "ok" if not errors else "failed",
        "case_count": len(cases),
        "object_count": len(objects),
        "text_page_count": len(text_chunks),
        "plan_count": len(plans),
        "errors": errors,
    }


def score_rag_predictions(
    predictions_path: Path,
    cases_path: Path = DEFAULT_CASES,
) -> dict[str, Any]:
    cases = {item["case_id"]: item for item in read_jsonl(cases_path)}
    predictions = {item["case_id"]: item for item in read_jsonl(predictions_path)}
    canonical_objects = {item["object_id"]: item for item in read_jsonl(OBJECT_CORPUS)}
    missing = sorted(set(cases) - set(predictions))
    extra = sorted(set(predictions) - set(cases))
    if missing or extra:
        raise ValueError(f"预测集 case_id 不完整；missing={missing}, extra={extra}")

    page_hits = page_total = object_hits = object_total = fine_hits = fine_total = 0
    complete_evidence_hits = answerable_count = 0
    support_hits = unanswerable_count = unanswerable_hits = 0
    dependent_total = dependent_formed = slot_total = slot_completed = 0
    context_chars: list[float] = []
    pre_generation_ms: list[float] = []
    end_to_end_ms: list[float] = []
    ttft_ms: list[float] = []
    generated_count = answer_check_hits = 0
    citation_hits = citation_total = citation_gold_total = 0
    reciprocal_ranks: list[float] = []
    ndcg_values: list[float] = []

    per_case: list[dict[str, Any]] = []
    for case_id, case in cases.items():
        prediction = predictions[case_id]
        evidence = prediction.get("retrieved_evidence") or []
        canonical_evidence = [
            canonical_objects.get(str(item.get("object_id") or item.get("evidence_id") or ""))
            for item in evidence
        ]
        pages = {
            int(item.get("page") if item.get("page") is not None else canonical.get("page"))
            for item, canonical in zip(evidence, canonical_evidence, strict=True)
            if item.get("page") is not None
            or (canonical is not None and canonical.get("page") is not None)
        }
        objects = {
            str(item.get("object_id") or item.get("evidence_id"))
            for item in evidence
            if item.get("object_id") or item.get("evidence_id") in canonical_objects
        }
        fine = {
            str(fine_id)
            for item, canonical in zip(evidence, canonical_evidence, strict=True)
            for fine_id in (
                item.get("fine_grained_ids") or ((canonical or {}).get("fine_grained_ids") or [])
            )
        }
        gold_pages = set(case["gold_pages"])
        gold_objects = set(case["gold_object_ids"])
        gold_fine = set(case["gold_fine_ids"])
        page_case_hits = len(pages & gold_pages)
        object_case_hits = len(objects & gold_objects)
        fine_case_hits = len(fine & gold_fine)
        page_hits += page_case_hits
        page_total += len(gold_pages)
        object_hits += object_case_hits
        object_total += len(gold_objects)
        fine_hits += fine_case_hits
        fine_total += len(gold_fine)

        if case["answerable"]:
            answerable_count += 1
            complete_evidence_hits += int(bool(gold_objects) and gold_objects.issubset(objects))
            ordered_objects = [
                item.get("object_id") or item.get("evidence_id") for item in evidence
            ]
            first_relevant = next(
                (
                    index
                    for index, object_id in enumerate(ordered_objects, start=1)
                    if object_id in gold_objects
                ),
                None,
            )
            reciprocal_ranks.append(1.0 / first_relevant if first_relevant else 0.0)
            dcg = sum(
                1.0 / math.log2(index + 1)
                for index, object_id in enumerate(ordered_objects, start=1)
                if object_id in gold_objects
            )
            ideal_count = min(len(gold_objects), len(ordered_objects))
            ideal_dcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
            ndcg_values.append(dcg / ideal_dcg if ideal_dcg else 0.0)
        else:
            unanswerable_count += 1
            unanswerable_hits += int(prediction.get("support_status") == "insufficient")
        support_hits += int(prediction.get("support_status") == case["expected_support_status"])

        trace = prediction.get("trace") or []
        dependent_items = [item for item in trace if item.get("depends_on")]
        dependent_total += len(dependent_items)
        dependent_formed += sum(
            bool(item.get("dependent_query_formed")) for item in dependent_items
        )
        slot_total += len(trace)
        slot_completed += sum(item.get("status") in {"complete", "completed"} for item in trace)
        metrics = prediction.get("metrics") or {}
        context_char_count = metrics.get("context_chars")
        if context_char_count is None:
            context_char_count = sum(len(str(item.get("content") or "")) for item in evidence)
        context_chars.append(float(context_char_count))
        pre_generation_ms.append(float(metrics.get("simulated_total_pre_generation_ms") or 0))
        if prediction.get("end_to_end_latency_ms") is not None:
            end_to_end_ms.append(float(prediction["end_to_end_latency_ms"]))
        if prediction.get("ttft_ms") is not None:
            ttft_ms.append(float(prediction["ttft_ms"]))

        if prediction.get("generation_status") == "completed":
            generated_count += 1
            answer = str(prediction.get("answer") or "")
            must_hit = all(item in answer for item in case["must_contain"])
            must_not_hit = all(item not in answer for item in case["must_not_contain"])
            answer_check_hits += int(must_hit and must_not_hit)
            citations = set(str(item) for item in prediction.get("citations") or [])
            citation_hits += len(citations & gold_objects)
            citation_total += len(citations)
            citation_gold_total += len(gold_objects)

        per_case.append(
            {
                "case_id": case_id,
                "page_recall": _ratio(page_case_hits, len(gold_pages)),
                "object_recall": _ratio(object_case_hits, len(gold_objects)),
                "fine_grained_recall": _ratio(fine_case_hits, len(gold_fine)),
                "support_status_match": prediction.get("support_status")
                == case["expected_support_status"],
                "evidence_ids": [
                    item.get("evidence_id") or item.get("object_id") for item in evidence
                ],
            }
        )

    answer_metrics = (
        {
            "status": "completed",
            "generated_case_count": generated_count,
            "answer_checklist_accuracy": _ratio(answer_check_hits, generated_count),
            "citation_precision": _ratio(citation_hits, citation_total),
            "citation_recall": _ratio(citation_hits, citation_gold_total),
            "groundedness": "human_or_judge_required",
        }
        if generated_count
        else {
            "status": "not_run",
            "generated_case_count": 0,
            "reason": "离线检索模拟没有调用生成模型，不能伪造答案、引用或 groundedness 分数。",
        }
    )
    return {
        "case_count": len(cases),
        "retrieval": {
            "page_recall_at_k": _ratio(page_hits, page_total),
            "object_recall_at_k": _ratio(object_hits, object_total),
            "fine_grained_recall_at_k": _ratio(fine_hits, fine_total),
            "complete_evidence_rate": _ratio(complete_evidence_hits, answerable_count),
            "mrr_at_k": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4),
            "ndcg_at_k": round(sum(ndcg_values) / len(ndcg_values), 4),
            "support_status_accuracy": _ratio(support_hits, len(cases)),
            "unanswerable_accuracy": _ratio(unanswerable_hits, unanswerable_count),
            "evidence_slot_completion_rate": _ratio(slot_completed, slot_total),
            "dependent_query_formed_rate": _ratio(dependent_formed, dependent_total),
        },
        "efficiency": {
            "average_context_chars": round(sum(context_chars) / len(context_chars), 2),
            "median_context_chars": round(median(context_chars), 2),
            "simulated_pre_generation_p50_ms": _percentile(pre_generation_ms, 0.5),
            "simulated_pre_generation_p95_ms": _percentile(pre_generation_ms, 0.95),
            "latency_label": "deterministic_training_proxy_not_wall_clock_or_ttft",
            "live_end_to_end_p50_ms": _percentile(end_to_end_ms, 0.5) if end_to_end_ms else None,
            "live_end_to_end_p95_ms": _percentile(end_to_end_ms, 0.95) if end_to_end_ms else None,
            "live_ttft_p50_ms": _percentile(ttft_ms, 0.5) if ttft_ms else None,
            "live_ttft_p95_ms": _percentile(ttft_ms, 0.95) if ttft_ms else None,
            "ttft_status": "observed" if ttft_ms else "not_observed",
        },
        "generation": answer_metrics,
        "per_case": per_case,
    }
