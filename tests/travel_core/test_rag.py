from __future__ import annotations


def test_object_aware_rag_returns_fine_grained_table_evidence(client, headers):
    response = client.post(
        "/v1/rag:run",
        headers=headers,
        json={
            "query": "周末轮渡末班几点？开航前多久停止检票？",
            "corpus_mode": "object_aware",
            "n_retrieve": 12,
            "n_rerank": 6,
            "k_context": 4,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["support_status"] == "supported"
    table = next(item for item in body["evidence"] if item["object_id"] == "XM-GUIDE-001-P2-FERRY-TABLE")
    assert "P2-T1-R2-C3" in table["fine_grained_ids"]


def test_multihop_uses_entities_from_first_slot(client, headers):
    response = client.post(
        "/v1/rag:run",
        headers=headers,
        json={
            "query": "室内无障碍场馆里，周一开放的场馆最晚几点停止入馆？",
            "mode": "multihop",
            "n_retrieve": 12,
            "n_rerank": 6,
            "k_context": 4,
            "evidence_plan": [
                {"slot_id": "venues", "query": "室内无障碍场馆有哪些"},
                {
                    "slot_id": "opening",
                    "depends_on": ["venues"],
                    "entity_source": "venues",
                    "query_template": "{entities} 周一开放 停止入馆",
                },
            ],
        },
    )
    assert response.status_code == 200
    trace = response.json()["trace"]
    assert trace[1]["dependent_query_formed"] is True
    assert "华侨博物院" in trace[1]["query"]


def test_unknown_question_abstains(client, headers):
    response = client.post(
        "/v1/rag:run",
        headers=headers,
        json={"query": "厦门水族馆每天几点进行企鹅喂食？"},
    )
    assert response.status_code == 200
    assert response.json()["support_status"] == "insufficient"


def test_invalid_candidate_funnel_is_rejected(client, headers):
    response = client.post(
        "/v1/rag:run",
        headers=headers,
        json={"query": "轮渡", "n_retrieve": 4, "n_rerank": 8, "k_context": 3},
    )
    assert response.status_code == 422
