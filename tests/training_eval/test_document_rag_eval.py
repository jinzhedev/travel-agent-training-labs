from __future__ import annotations

import json

from training_eval.rag import score_rag_predictions, validate_rag_case_set


def test_rag_gold_set_is_cross_referenced():
    result = validate_rag_case_set()
    assert result["status"] == "ok", result["errors"]
    assert result["case_count"] == 12
    assert result["object_count"] == 14


def test_workflow_evidence_id_is_resolved_to_canonical_object(tmp_path):
    cases_path = tmp_path / "cases.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    cases_path.write_text(
        json.dumps(
            {
                "case_id": "RAG-001",
                "answerable": True,
                "gold_pages": [1],
                "gold_object_ids": ["XM-GUIDE-001-P1-VENUE-TABLE"],
                "gold_fine_ids": ["P1-T1-R1-C4"],
                "expected_support_status": "supported",
                "must_contain": ["周一闭馆"],
                "must_not_contain": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    predictions_path.write_text(
        json.dumps(
            {
                "case_id": "RAG-001",
                "generation_status": "completed",
                "answer": "周一闭馆",
                "citations": ["XM-GUIDE-001-P1-VENUE-TABLE"],
                "support_status": "supported",
                "retrieved_evidence": [
                    {
                        "evidence_id": "XM-GUIDE-001-P1-VENUE-TABLE",
                        "content": "对象正文",
                    }
                ],
                "trace": [{"status": "complete", "depends_on": []}],
                "metrics": {},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    score = score_rag_predictions(predictions_path, cases_path)

    assert score["retrieval"]["page_recall_at_k"] == 1.0
    assert score["retrieval"]["object_recall_at_k"] == 1.0
    assert score["retrieval"]["fine_grained_recall_at_k"] == 1.0
    assert score["retrieval"]["evidence_slot_completion_rate"] == 1.0
    assert score["efficiency"]["average_context_chars"] == 4.0


def test_explicit_zero_context_chars_is_not_recomputed(tmp_path):
    cases_path = tmp_path / "cases.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    cases_path.write_text(
        json.dumps(
            {
                "case_id": "RAG-001",
                "answerable": True,
                "gold_pages": [1],
                "gold_object_ids": ["XM-GUIDE-001-P1-VENUE-TABLE"],
                "gold_fine_ids": [],
                "expected_support_status": "supported",
                "must_contain": [],
                "must_not_contain": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    predictions_path.write_text(
        json.dumps(
            {
                "case_id": "RAG-001",
                "generation_status": "completed",
                "answer": "答案",
                "citations": ["XM-GUIDE-001-P1-VENUE-TABLE"],
                "support_status": "supported",
                "retrieved_evidence": [
                    {
                        "evidence_id": "XM-GUIDE-001-P1-VENUE-TABLE",
                        "content": "不应计入的正文",
                    }
                ],
                "trace": [],
                "metrics": {"context_chars": 0},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    score = score_rag_predictions(predictions_path, cases_path)

    assert score["efficiency"]["average_context_chars"] == 0.0
