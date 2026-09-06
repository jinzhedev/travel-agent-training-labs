from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from statistics import median
from typing import Any

from scripts.sync_dify_rag_knowledge import DifyKnowledgeClient, DifySettings, SyncError
from training_eval.foundation import read_jsonl
from training_eval.rag import (
    DEFAULT_CASES,
    OBJECT_CORPUS,
    PLANS,
    TEXT_CORPUS,
    score_rag_predictions,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports/local/dify-native-rag"

PRESETS: dict[str, dict[str, Any]] = {
    "text-semantic-k6": {
        "dataset_name": "XM-Guide-Text",
        "corpus_mode": "text_only",
        "search_method": "semantic_search",
        "reranking_enable": False,
        "top_k": 6,
        "score_threshold": 0.50,
    },
    "object-semantic-k6": {
        "dataset_name": "XM-Guide-Object",
        "corpus_mode": "object_aware",
        "search_method": "semantic_search",
        "reranking_enable": False,
        "top_k": 6,
        "score_threshold": 0.50,
    },
    "object-hybrid-k6": {
        "dataset_name": "XM-Guide-Object",
        "corpus_mode": "object_aware",
        "search_method": "hybrid_search",
        "reranking_enable": False,
        "top_k": 6,
        "score_threshold": 0.50,
    },
    "object-hybrid-rerank-k6": {
        "dataset_name": "XM-Guide-Object",
        "corpus_mode": "object_aware",
        "search_method": "hybrid_search",
        "reranking_enable": True,
        "top_k": 6,
        "score_threshold": 0.50,
    },
    "object-hybrid-rerank-k4": {
        "dataset_name": "XM-Guide-Object",
        "corpus_mode": "object_aware",
        "search_method": "hybrid_search",
        "reranking_enable": True,
        "top_k": 4,
        "score_threshold": 0.50,
    },
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Dify 原生知识库检索运行 CP03 评测")
    parser.add_argument("--preset", choices=sorted(PRESETS), required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--run-name", default="")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args()


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999)))
    return round(ordered[index], 2)


def _document_name(record: dict[str, Any]) -> str:
    segment = record.get("segment") or {}
    document = segment.get("document") or {}
    return str(document.get("name") or "")


def _segment_content(record: dict[str, Any]) -> str:
    return str((record.get("segment") or {}).get("content") or "")


def _canonical_maps() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    objects = {f"{item['object_id']}.md": item for item in read_jsonl(OBJECT_CORPUS)}
    pages = {f"{item['chunk_id']}.md": item for item in read_jsonl(TEXT_CORPUS)}
    return objects, pages


def _as_evidence(
    record: dict[str, Any],
    *,
    corpus_mode: str,
    rank: int,
    object_by_name: dict[str, dict[str, Any]],
    page_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    name = _document_name(record)
    canonical = (
        object_by_name.get(name) if corpus_mode == "object_aware" else page_by_name.get(name)
    )
    if canonical is None:
        raise SyncError(f"Dify 返回未知文档，无法回指冻结语料：{name or '<missing name>'}")
    object_id = canonical.get("object_id")
    chunk_id = canonical.get("chunk_id")
    return {
        "evidence_id": object_id or chunk_id,
        "document_id": canonical["document_id"],
        "page": canonical["page"],
        "object_id": object_id,
        "supports_object_ids": canonical.get("supports_object_ids") or [],
        "object_type": canonical.get("object_type") or "page_text",
        "fine_grained_ids": canonical.get("fine_grained_ids") or [],
        "title": canonical["title"],
        "content": _segment_content(record),
        "bbox": canonical.get("bbox"),
        "entities": canonical.get("entities") or [],
        "source": canonical["source"],
        "observed_at": canonical["observed_at"],
        "valid_until": canonical.get("valid_until"),
        "is_simulated": canonical.get("is_simulated", True),
        "revision": canonical["revision"],
        "score": round(float(record.get("score") or 0), 6),
        "rank": rank,
        "dify_document_name": name,
        "dify_segment_id": str((record.get("segment") or {}).get("id") or ""),
    }


def _entities(evidence: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for item in evidence:
        for entity in item.get("entities") or []:
            if entity not in result:
                result.append(entity)
    return result


def _retrieval_model(dataset: dict[str, Any], preset: dict[str, Any]) -> dict[str, Any]:
    model = {
        "search_method": preset["search_method"],
        "reranking_enable": preset["reranking_enable"],
        "top_k": preset["top_k"],
        "score_threshold_enabled": True,
        "score_threshold": preset["score_threshold"],
    }
    if preset["reranking_enable"]:
        stored = dataset.get("retrieval_model_dict") or {}
        reranker = stored.get("reranking_model") or {}
        if not reranker.get("reranking_provider_name") or not reranker.get("reranking_model_name"):
            raise SyncError("知识库没有已配置的 reranker，无法运行真实重排 preset")
        model["reranking_mode"] = "reranking_model"
        model["reranking_model"] = reranker
    return model


def _retrieve(
    client: DifyKnowledgeClient,
    *,
    dataset_id: str,
    query: str,
    retrieval_model: dict[str, Any],
    corpus_mode: str,
    object_by_name: dict[str, dict[str, Any]],
    page_by_name: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], float]:
    started = time.perf_counter()
    body = client.request(
        "POST",
        f"datasets/{dataset_id}/retrieve",
        json={"query": query, "retrieval_model": retrieval_model},
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    evidence = [
        _as_evidence(
            record,
            corpus_mode=corpus_mode,
            rank=rank,
            object_by_name=object_by_name,
            page_by_name=page_by_name,
        )
        for rank, record in enumerate(body.get("records") or [], start=1)
    ]
    return evidence, latency_ms


def _run_case(
    client: DifyKnowledgeClient,
    *,
    case: dict[str, Any],
    plan: dict[str, Any],
    dataset_id: str,
    retrieval_model: dict[str, Any],
    corpus_mode: str,
    object_by_name: dict[str, dict[str, Any]],
    page_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    slots = (plan.get("slots") or []) if case["mode"] == "multihop" else []
    if not slots:
        slots = [
            {"slot_id": "single_query", "query": case["query"], "depends_on": [], "required": True}
        ]

    completed: dict[str, list[dict[str, Any]]] = {}
    combined: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    missing_required: list[str] = []
    total_latency_ms = 0.0

    for slot in slots:
        depends_on = slot.get("depends_on") or []
        source_ids = [slot.get("entity_source")] if slot.get("entity_source") else depends_on
        derived_entities = _entities(
            [item for source_id in source_ids for item in completed.get(str(source_id), [])]
        )
        query = str(slot.get("query") or "")
        if depends_on:
            query = str(slot.get("query_template") or "").replace(
                "{entities}", " ".join(derived_entities)
            )
        missing_dependencies = [source_id for source_id in depends_on if source_id not in completed]
        if missing_dependencies or not query.strip():
            if slot.get("required", True):
                missing_required.append(str(slot["slot_id"]))
            trace.append(
                {
                    "slot_id": slot["slot_id"],
                    "status": "dependency_missing",
                    "depends_on": depends_on,
                    "query": query,
                    "derived_entities": derived_entities,
                    "dependent_query_formed": False,
                    "evidence_ids": [],
                    "retrieval_latency_ms": 0.0,
                }
            )
            continue

        evidence, latency_ms = _retrieve(
            client,
            dataset_id=dataset_id,
            query=query,
            retrieval_model=retrieval_model,
            corpus_mode=corpus_mode,
            object_by_name=object_by_name,
            page_by_name=page_by_name,
        )
        total_latency_ms += latency_ms
        completed[str(slot["slot_id"])] = evidence
        if slot.get("required", True) and not evidence:
            missing_required.append(str(slot["slot_id"]))
        existing_ids = {item["evidence_id"] for item in combined}
        for item in evidence:
            if item["evidence_id"] not in existing_ids:
                combined.append(item)
                existing_ids.add(item["evidence_id"])
        trace.append(
            {
                "slot_id": slot["slot_id"],
                "status": "completed" if evidence else "empty",
                "depends_on": depends_on,
                "query": query,
                "derived_entities": derived_entities,
                "dependent_query_formed": bool(depends_on and derived_entities),
                "evidence_ids": [item["evidence_id"] for item in evidence],
                "retrieval_latency_ms": latency_ms,
            }
        )

    for rank, item in enumerate(combined, start=1):
        item["rank"] = rank
    support_status = "supported" if combined and not missing_required else "insufficient"
    return {
        "case_id": case["case_id"],
        "generation_status": "not_run",
        "answer": "",
        "citations": [],
        "support_status": support_status,
        "retrieved_evidence": combined,
        "trace": trace,
        "metrics": {
            "backend": "dify_native_knowledge_api",
            "slot_count": len(slots),
            "completed_slot_count": sum(item["status"] == "completed" for item in trace),
            "dependent_slot_count": sum(bool(item.get("depends_on")) for item in trace),
            "dependent_query_formed_count": sum(
                bool(item.get("dependent_query_formed")) for item in trace
            ),
            "missing_required_slots": missing_required,
            "k_context_actual": len(combined),
            "context_chars": sum(len(item["content"]) for item in combined),
            "retrieval_wall_clock_ms": round(total_latency_ms, 2),
        },
        "notice": "真实 Dify Knowledge API 检索；未调用生成模型，延迟只覆盖知识库检索请求。",
    }


def main() -> int:
    args = _parse_args()
    preset = PRESETS[args.preset]
    selected = set(args.case_id)
    cases = [item for item in read_jsonl(args.cases) if not selected or item["case_id"] in selected]
    if selected - {item["case_id"] for item in cases}:
        raise SystemExit(f"未知 case_id：{sorted(selected - {item['case_id'] for item in cases})}")
    if not cases:
        raise SystemExit("没有可运行的 case")

    settings = DifySettings()
    if not settings.dify_base_url or not settings.dify_kb_api_key:
        raise SystemExit("请在 .env 中设置 DIFY_BASE_URL 和 DIFY_KB_API_KEY")
    plans = {item["plan_id"]: item for item in read_jsonl(PLANS)}
    object_by_name, page_by_name = _canonical_maps()

    with DifyKnowledgeClient(
        base_url=settings.dify_base_url,
        api_key=settings.dify_kb_api_key,
        timeout=args.timeout,
    ) as client:
        match = client.find_dataset(str(preset["dataset_name"]))
        if not match:
            raise SyncError(f"找不到知识库：{preset['dataset_name']}")
        dataset = client.get_dataset(str(match["id"]))
        if dataset.get("indexing_technique") != "high_quality":
            raise SyncError("真实语义检索实验要求 high_quality 索引")
        retrieval_model = _retrieval_model(dataset, preset)
        predictions = [
            _run_case(
                client,
                case=case,
                plan=plans.get(case.get("evidence_plan_id")) or {},
                dataset_id=str(dataset["id"]),
                retrieval_model=retrieval_model,
                corpus_mode=str(preset["corpus_mode"]),
                object_by_name=object_by_name,
                page_by_name=page_by_name,
            )
            for case in cases
        ]

    run_name = args.run_name.strip() or args.preset
    args.output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = args.output_dir / f"{run_name}.predictions.jsonl"
    report_path = args.output_dir / f"{run_name}.report.json"
    predictions_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in predictions) + "\n",
        encoding="utf-8",
    )
    score = score_rag_predictions(
        predictions_path,
        args.cases if not selected else _write_case_view(args.output_dir, run_name, cases),
    )
    score["generation"] = {
        "status": "not_run",
        "generated_case_count": 0,
        "reason": "本脚本真实调用 Dify Knowledge API，但不调用 Workflow 或生成模型。",
    }
    score["efficiency"]["latency_label"] = (
        "scorer_proxy_fields_unused_for_native_run; see live_retrieval"
    )
    latencies = [float(item["metrics"]["retrieval_wall_clock_ms"]) for item in predictions]
    report = {
        "run_name": run_name,
        "preset": args.preset,
        "backend": "dify_native_knowledge_api",
        "dataset": {
            "name": dataset.get("name"),
            "id": dataset.get("id"),
            "revision": "xiamen-document-ai-v1.0.0",
            "indexing_technique": dataset.get("indexing_technique"),
            "embedding_model": dataset.get("embedding_model"),
            "embedding_model_provider": dataset.get("embedding_model_provider"),
        },
        "config": retrieval_model,
        "case_ids": [item["case_id"] for item in cases],
        "score": score,
        "live_retrieval": {
            "request_count": sum(len(item["trace"]) for item in predictions),
            "average_ms_per_case": round(sum(latencies) / len(latencies), 2),
            "median_ms_per_case": round(median(latencies), 2),
            "p95_ms_per_case": _percentile(latencies, 0.95),
            "scope": "client_wall_clock_for_dify_knowledge_retrieve_only",
        },
        "prediction_path": str(predictions_path.relative_to(PROJECT_ROOT)),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(report_path.relative_to(PROJECT_ROOT))
    return 0


def _write_case_view(output_dir: Path, run_name: str, cases: list[dict[str, Any]]) -> Path:
    path = output_dir / f"{run_name}.cases.jsonl"
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in cases) + "\n",
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    raise SystemExit(main())
