from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..schemas import EvidencePlanSlot, RagRunIn

_LATIN_OR_NUMBER = re.compile(r"[a-zA-Z0-9_.:%-]+")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_STOP_BIGRAMS = {
    "什么",
    "哪些",
    "怎么",
    "是否",
    "需要",
    "资料",
    "同时",
    "当前",
    "多少",
    "几点",
    "进行",
    "每天",
    "如果",
}


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


@lru_cache
def _corpus(path: str) -> tuple[dict[str, Any], ...]:
    return tuple(_read_jsonl(path))


def _tokens(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    latin = {item.lower() for item in _LATIN_OR_NUMBER.findall(normalized)}
    han = "".join(_HAN.findall(normalized))
    unigrams = {char for char in han}
    bigrams = {han[index : index + 2] for index in range(max(0, len(han) - 1))}
    return latin | unigrams | (bigrams - _STOP_BIGRAMS)


def _query_hints(query: str) -> list[str]:
    hints = [
        "厦门市博物馆",
        "华侨博物院",
        "厦门园林植物园游客中心",
        "厦门园林植物园",
        "轮椅旅客",
        "周末及节假日",
        "周一开放",
        "周一闭馆",
        "停止入馆",
        "停止检票",
        "大风橙色",
        "橙色或红色",
        "已有票务占位",
        "已有占位",
        "查询最终状态",
        "退票规则",
        "现行模拟规则",
        "已失效",
        "60 分钟",
        "30 分钟",
        "40 分钟",
        "19:30",
        "17:00",
        "approval_id",
    ]
    compact = re.sub(r"\s+", "", query.lower())
    return [hint for hint in hints if re.sub(r"\s+", "", hint.lower()) in compact]


def _record_text(record: dict[str, Any]) -> str:
    values = [
        str(record.get("title") or ""),
        str(record.get("content") or ""),
        str(record.get("retrieval_text") or ""),
        " ".join(str(item) for item in record.get("entities") or []),
    ]
    return " ".join(values)


def _base_score(query: str, record: dict[str, Any], strategy: str) -> float:
    query_tokens = _tokens(query)
    record_tokens = _tokens(_record_text(record))
    overlap = query_tokens & record_tokens
    score = sum(2.0 if len(token) > 1 else 0.25 for token in overlap)
    compact_record = re.sub(r"\s+", "", _record_text(record).lower())
    for hint in _query_hints(query):
        if re.sub(r"\s+", "", hint.lower()) in compact_record:
            score += 8.0
    if strategy == "hybrid_simulated":
        query_chars = set("".join(_HAN.findall(query)))
        record_chars = set("".join(_HAN.findall(compact_record)))
        if query_chars:
            score += 3.0 * len(query_chars & record_chars) / len(query_chars)
        if any(word in query for word in ("表", "几点", "多久", "多少", "分钟")):
            score += 1.5 if record.get("object_type") == "table" else 0.0
        if any(word in query for word in ("处置链", "流程", "下一步")):
            score += 3.0 if record.get("object_type") == "flowchart" else 0.0
        if any(word in query for word in ("谁向谁", "确认步骤", "提交前")):
            score += 3.0 if record.get("object_type") == "sequence_diagram" else 0.0
    return round(score, 6)


def _rerank_score(query: str, record: dict[str, Any], base_score: float) -> float:
    text = _record_text(record)
    score = base_score
    for hint in _query_hints(query):
        if hint.replace(" ", "") in text.replace(" ", ""):
            score += 4.0
    if "现行" in query and "现行" in text:
        score += 8.0
    if any(word in query for word in ("当前", "应采用哪一版")):
        score += 4.0 if "现行" in text else -2.0 if "已失效" in text else 0.0
    expected_type = None
    if any(word in query for word in ("处置链", "流程", "下一步")):
        expected_type = "flowchart"
    elif any(word in query for word in ("谁向谁", "确认步骤", "提交前")):
        expected_type = "sequence_diagram"
    elif any(word in query for word in ("几点", "多久", "多少", "分钟", "开放")):
        expected_type = "table"
    if expected_type and record.get("object_type") == expected_type:
        score += 5.0
    return round(score, 6)


def _as_evidence(record: dict[str, Any], score: float, rank: int) -> dict[str, Any]:
    evidence_id = record.get("object_id") or record.get("chunk_id")
    return {
        "evidence_id": evidence_id,
        "document_id": record["document_id"],
        "page": record["page"],
        "object_id": record.get("object_id"),
        "supports_object_ids": record.get("supports_object_ids") or [],
        "object_type": record.get("object_type") or "page_text",
        "fine_grained_ids": record.get("fine_grained_ids") or [],
        "title": record["title"],
        "content": record["content"],
        "bbox": record.get("bbox"),
        "entities": record.get("entities") or [],
        "source": record["source"],
        "observed_at": record["observed_at"],
        "valid_until": record.get("valid_until"),
        "is_simulated": record.get("is_simulated", True),
        "revision": record["revision"],
        "score": round(score, 4),
        "rank": rank,
    }


def _search(query: str, payload: RagRunIn, *, k_context: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    path = (
        settings.rag_text_corpus_path
        if payload.corpus_mode == "text_only"
        else settings.rag_object_corpus_path
    )
    records = list(_corpus(path))
    ranked = sorted(
        ((_base_score(query, record, payload.retrieval_strategy), record) for record in records),
        key=lambda item: (-item[0], item[1].get("object_id") or item[1].get("chunk_id")),
    )
    # A frozen top-score abstention threshold is part of the fixture contract.
    # Once the query is admitted, the candidate pool may contain low-score items
    # so the N_retrieve → N_rerank funnel remains observable.
    positive = [(score, record) for score, record in ranked if score > 0]
    candidates = positive[: payload.n_retrieve] if positive and positive[0][0] >= 6.0 else []
    rerank_applied = False
    rerank_fallback = None
    effective = candidates
    simulated_rerank_ms = 0.0
    if payload.rerank_mode == "simulated":
        rerank_input = candidates[: payload.n_rerank]
        simulated_rerank_ms = round(3.5 * len(rerank_input), 2)
        if simulated_rerank_ms > payload.rerank_budget_ms:
            rerank_fallback = "budget_exceeded_use_retrieval_order"
        else:
            effective = sorted(
                ((_rerank_score(query, record, score), record) for score, record in rerank_input),
                key=lambda item: (-item[0], item[1].get("object_id") or item[1].get("chunk_id")),
            )
            rerank_applied = True
    limit = k_context or payload.k_context
    context = [_as_evidence(record, score, index + 1) for index, (score, record) in enumerate(effective[:limit])]
    context_chars = sum(len(item["content"]) for item in context)
    retrieval_ms = round(
        4.0
        + 0.6 * len(records)
        + 0.25 * min(payload.n_retrieve, len(records))
        + (3.0 if payload.retrieval_strategy == "hybrid_simulated" else 0.0),
        2,
    )
    context_prepare_ms = round(context_chars / 80.0, 2)
    return {
        "evidence": context,
        "metrics": {
            "corpus_records": len(records),
            "candidate_count": len(candidates),
            "n_retrieve": payload.n_retrieve,
            "n_rerank_requested": payload.n_rerank,
            "n_rerank_actual": min(len(candidates), payload.n_rerank) if payload.rerank_mode != "off" else 0,
            "k_context_requested": limit,
            "k_context_actual": len(context),
            "context_chars": context_chars,
            "simulated_retrieval_ms": retrieval_ms,
            "simulated_rerank_ms": simulated_rerank_ms if payload.rerank_mode != "off" else 0.0,
            "simulated_context_prepare_ms": context_prepare_ms,
            "simulated_total_pre_generation_ms": round(retrieval_ms + simulated_rerank_ms + context_prepare_ms, 2),
            "rerank_applied": rerank_applied,
            "rerank_fallback": rerank_fallback,
            "latency_label": "deterministic_training_proxy_not_wall_clock",
        },
    }


def _entities_from_evidence(evidence: list[dict[str, Any]]) -> list[str]:
    entities: list[str] = []
    for item in evidence:
        for entity in item.get("entities") or []:
            if entity not in entities:
                entities.append(entity)
    return entities


def _slot_query(slot: EvidencePlanSlot, completed: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    if not slot.depends_on:
        return slot.query, []
    source_ids = [slot.entity_source] if slot.entity_source else slot.depends_on
    source_evidence = [
        item
        for source_id in source_ids
        for item in completed.get(source_id, {}).get("evidence", [])
    ]
    entities = _entities_from_evidence(source_evidence)
    if not entities:
        return "", []
    rendered = (slot.query_template or "").replace("{entities}", " ".join(entities))
    return rendered, entities


def _multihop(payload: RagRunIn) -> dict[str, Any]:
    completed: dict[str, dict[str, Any]] = {}
    trace: list[dict[str, Any]] = []
    combined: list[dict[str, Any]] = []
    missing_required: list[str] = []
    stage_metrics: list[dict[str, Any]] = []

    for slot in payload.evidence_plan:
        query, derived_entities = _slot_query(slot, completed)
        missing_dependencies = [item for item in slot.depends_on if item not in completed]
        if missing_dependencies or not query.strip():
            if slot.required:
                missing_required.append(slot.slot_id)
            trace.append(
                {
                    "slot_id": slot.slot_id,
                    "status": "dependency_missing",
                    "depends_on": slot.depends_on,
                    "query": query,
                    "derived_entities": derived_entities,
                    "dependent_query_formed": False,
                    "evidence_ids": [],
                }
            )
            continue
        result = _search(query, payload, k_context=min(2, payload.k_context))
        completed[slot.slot_id] = result
        evidence = result["evidence"]
        if slot.required and not evidence:
            missing_required.append(slot.slot_id)
        for item in evidence:
            if item["evidence_id"] not in {existing["evidence_id"] for existing in combined}:
                combined.append(item)
        stage_metrics.append(result["metrics"])
        trace.append(
            {
                "slot_id": slot.slot_id,
                "status": "completed" if evidence else "empty",
                "depends_on": slot.depends_on,
                "query": query,
                "derived_entities": derived_entities,
                "dependent_query_formed": bool(slot.depends_on and derived_entities),
                "evidence_ids": [item["evidence_id"] for item in evidence],
            }
        )

    evidence = combined[: payload.k_context]
    context_chars = sum(len(item["content"]) for item in evidence)
    metrics = {
        "slot_count": len(payload.evidence_plan),
        "completed_slot_count": sum(item["status"] == "completed" for item in trace),
        "dependent_slot_count": sum(bool(slot.depends_on) for slot in payload.evidence_plan),
        "dependent_query_formed_count": sum(item["dependent_query_formed"] for item in trace),
        "missing_required_slots": missing_required,
        "k_context_actual": len(evidence),
        "context_chars": context_chars,
        "simulated_retrieval_ms": round(sum(item["simulated_retrieval_ms"] for item in stage_metrics), 2),
        "simulated_rerank_ms": round(sum(item["simulated_rerank_ms"] for item in stage_metrics), 2),
        "simulated_context_prepare_ms": round(sum(item["simulated_context_prepare_ms"] for item in stage_metrics), 2),
        "simulated_total_pre_generation_ms": round(sum(item["simulated_total_pre_generation_ms"] for item in stage_metrics), 2),
        "latency_label": "deterministic_training_proxy_not_wall_clock",
    }
    return {"evidence": evidence, "trace": trace, "metrics": metrics, "missing_required": missing_required}


def run_rag(payload: RagRunIn) -> dict[str, Any]:
    if payload.mode == "single":
        result = _search(payload.query, payload)
        evidence = result["evidence"]
        trace = [
            {
                "slot_id": "single_query",
                "status": "completed" if evidence else "empty",
                "depends_on": [],
                "query": payload.query,
                "derived_entities": [],
                "dependent_query_formed": False,
                "evidence_ids": [item["evidence_id"] for item in evidence],
            }
        ]
        metrics = result["metrics"]
        missing_required: list[str] = [] if evidence else ["single_query"]
    else:
        result = _multihop(payload)
        evidence = result["evidence"]
        trace = result["trace"]
        metrics = result["metrics"]
        missing_required = result["missing_required"]

    support_status = "supported" if evidence and not missing_required else "insufficient"
    return {
        "mode": payload.mode,
        "corpus_mode": payload.corpus_mode,
        "retrieval_strategy": payload.retrieval_strategy,
        "rerank_mode": payload.rerank_mode,
        "support_status": support_status,
        "evidence": evidence,
        "evidence_ids": [item["evidence_id"] for item in evidence],
        "trace": trace,
        "metrics": metrics,
        "notice": (
            "课程离线检索与重排代理：用于比较链路结构，不代表真实 embedding、"
            "reranker、TTFT 或线上质量。真实指标必须由 Dify 运行记录产生。"
        ),
    }
