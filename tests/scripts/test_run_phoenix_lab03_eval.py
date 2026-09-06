from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/run_phoenix_lab03_eval.py"
SPEC = spec_from_file_location("run_phoenix_lab03_eval", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

answer_requirements = MODULE.answer_requirements
evidence_complete = MODULE.evidence_complete
example_query = MODULE.example_query
extract_object_ids = MODULE.extract_object_ids


@pytest.mark.parametrize("app_id", ["", "test-app"])
def test_main_app_id_is_optional_metadata(monkeypatch, app_id) -> None:
    import sys

    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    settings = MODULE.Settings(
        _env_file=None,
        dify_base_url="http://localhost",
        dify_lab03_api_key="test",
        dify_lab03_app_id=app_id,
        phoenix_endpoint="http://localhost:6006",
    )
    monkeypatch.setattr(MODULE, "Settings", lambda: settings)
    client = SimpleNamespace(datasets=SimpleNamespace(get_dataset=lambda **kwargs: object()))
    monkeypatch.setattr(MODULE, "Client", lambda **kwargs: client)
    captured = {}

    def run_experiment(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(MODULE, "run_experiment", run_experiment)
    assert MODULE.main() == 0
    metadata = captured["experiment_metadata"]
    if app_id:
        assert metadata["app_id"] == app_id
    else:
        assert "app_id" not in metadata


def test_example_query_accepts_trace_dataset_shape() -> None:
    assert example_query({"input": {"query": "周末轮渡末班几点？"}}) == "周末轮渡末班几点？"
    assert example_query({"input": {"input": "周末轮渡末班几点？"}}) == "周末轮渡末班几点？"


def test_judge_template_includes_per_case_acceptance_notes(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "LLM", lambda **kwargs: object())
    monkeypatch.setattr(MODULE, "ClassificationEvaluator", lambda **kwargs: kwargs)
    config = MODULE.build_grounded_answer_judge(api_key="test", base_url="http://localhost", model="test")
    prompt = config["prompt_template"]
    assert "expected.acceptance_notes" in prompt
    assert "{{expected}}" in prompt
    assert "不允许据此推导其他无证据支持的业务规则或办理渠道" in prompt


def test_extract_object_ids_reads_document_name_content_and_metadata() -> None:
    resources = [
        {"document_name": "XM-GUIDE-001-P2-FERRY-TABLE.md"},
        {"content": "object_id: XM-GUIDE-001-P3-WIND-FLOW"},
        {"metadata": {"object_id": "XM-REFUND-2026-P6-TABLE"}},
    ]

    assert extract_object_ids(resources) == [
        "XM-GUIDE-001-P2-FERRY-TABLE",
        "XM-GUIDE-001-P3-WIND-FLOW",
        "XM-REFUND-2026-P6-TABLE",
    ]


def test_answer_requirements_checks_required_and_forbidden_terms() -> None:
    expected = {"must_contain": ["19:30", "20 分钟"], "must_not_contain": ["18:30"]}

    passed = answer_requirements(input={}, output={"answer": "末班 19:30，提前 20 分钟停检。"}, expected=expected)
    failed = answer_requirements(input={}, output={"answer": "末班 18:30。"}, expected=expected)

    assert passed[:2] == (1.0, "pass")
    assert failed[:2] == (0.0, "fail")


def test_evidence_complete_requires_every_gold_object() -> None:
    expected = {"gold_object_ids": ["OBJ-A", "OBJ-B"]}

    passed = evidence_complete(
        input={}, output={"retrieved_object_ids": ["OBJ-A", "OBJ-B"]}, expected=expected
    )
    failed = evidence_complete(
        input={}, output={"retrieved_object_ids": ["OBJ-A"]}, expected=expected
    )

    assert passed[:2] == (1.0, "pass")
    assert failed[:2] == (0.0, "fail")
