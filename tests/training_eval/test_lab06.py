from __future__ import annotations

import copy
import json

import pytest

from training_eval.lab06 import (
    CASES,
    build_examples,
    citation_sources,
    evidence_coverage,
    release_check,
    response_available,
)


def reports():
    base = {
        "status": "completed", "dataset_id": "ds", "dataset_version_id": "v1",
        "repetitions": 1, "source_versions": {"facts": "sha"},
        "evaluator_revision": "rules1", "judge_revision": "judge1",
        "expected_case_ids": ["L06-001"],
        "manifest": {"answer_prompt_revision": "v0", "p95_budget_s": 120},
        "task_runs": [{"id": "base-run", "repetition_number": 1,
                       "output": {"case_id": "L06-001", "answer": "回答", "elapsed_s": 5}}],
    }
    candidate = copy.deepcopy(base)
    candidate["manifest"]["answer_prompt_revision"] = "v1"
    candidate["task_runs"][0]["id"] = "candidate-run"
    reviews = [{"run_id": "candidate-run", "case_id": "L06-001", "repetition": 1,
                "task_result": "pass", "tool_result": "pass", "critical_violation": False,
                "reason": "原始 Context 与工具参数已经复核", "trace_id": "trace1"}]
    return base, candidate, reviews


def test_gold_resolves_real_sources_and_retains_success_controls():
    examples = build_examples()
    assert len(examples) == 12
    assert examples[0]["output"]["weather_reference"]["condition"] == "雷阵雨"
    assert "不能据此推断" in examples[0]["output"]["reference_documents"][0]["content"]
    assert sum("success_control" in e["metadata"]["slices"] for e in examples) == 4
    assert examples[5]["output"]["weather_reference"] is None


def test_duplicate_case_does_not_increase_weight(tmp_path):
    first = CASES.read_text().splitlines()[0]
    path = tmp_path / "cases.jsonl"
    path.write_text(first + "\n" + first)
    with pytest.raises(ValueError, match="重复"):
        build_examples(path)


def test_missing_document_reference_fails_before_network(tmp_path):
    case = json.loads(CASES.read_text().splitlines()[0])
    case["document_ids"] = ["missing"]
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case))
    with pytest.raises(KeyError):
        build_examples(path)


def test_missing_telemetry_is_review_not_pass_or_zero():
    expected = {"document_ids": ["XM-ACCESS-001-P5-VENUES"]}
    assert evidence_coverage({"retrieved_resources": None}, expected)[:2] == (None, "review")
    assert evidence_coverage({"retrieved_resources": []}, expected)[:2] == (0.0, "fail")
    assert evidence_coverage({}, {"document_ids": []})[1] == "not_applicable"


def test_citation_not_in_observed_resources_is_flagged():
    output = {"answer": "依据 XM-ACCESS-001-P5-VENUES", "retrieved_resources": []}
    assert citation_sources(output, {"document_ids": []})[:2] == (0.0, "fail")


def test_timeout_remains_failure_even_if_partial_answer_exists():
    assert response_available({"answer": "部分答案", "run_error": "ReadTimeout"})[0] == 0


def test_unresolved_dify_binding_is_not_a_valid_answer():
    assert response_available({"answer": "{{#node.text#}}"})[0] == 0


def test_fully_reviewed_single_variable_candidate_can_pass():
    assert release_check(*reports())["status"] == "pass"


@pytest.mark.parametrize("field", ["dataset_version_id", "source_versions", "judge_revision"])
def test_incomparable_runs_cannot_pass(field):
    base, candidate, reviews = reports()
    candidate[field] = "changed"
    assert release_check(base, candidate, reviews)["status"] == "review"


def test_missing_failed_trial_cannot_be_hidden():
    base, candidate, reviews = reports()
    base["repetitions"] = candidate["repetitions"] = 2
    assert release_check(base, candidate, reviews)["status"] == "review"


def test_multi_variable_change_cannot_pass():
    base, candidate, reviews = reports()
    candidate["manifest"]["answer_model"] = "another-model"
    assert release_check(base, candidate, reviews)["status"] == "review"


@pytest.mark.parametrize("mutation", ["critical", "tool", "task", "timeout", "budget"])
def test_explicit_failures_block(mutation):
    base, candidate, reviews = reports()
    if mutation == "critical":
        reviews[0]["critical_violation"] = True
    elif mutation in {"tool", "task"}:
        reviews[0][f"{mutation}_result"] = "fail"
    elif mutation == "timeout":
        candidate["task_runs"][0]["output"]["run_error"] = "ReadTimeout"
    else:
        candidate["task_runs"][0]["output"]["elapsed_s"] = 121
    assert release_check(base, candidate, reviews)["status"] == "block"


def test_foreign_run_or_unreviewed_constraint_never_passes():
    base, candidate, reviews = reports()
    reviews[0]["run_id"] = "other-run"
    assert release_check(base, candidate, reviews)["status"] == "review"
    reviews[0]["run_id"] = "candidate-run"
    reviews[0]["critical_violation"] = None
    assert release_check(base, candidate, reviews)["status"] == "review"
