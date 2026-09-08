from copy import deepcopy

import pytest

from training_eval.multi_tool_gold import load_dataset, model_input, score
from training_eval.tool_calling import validate_tool_case_set


def fixture_predictions(cases):
    """只供评分器单元测试，不作为模型基线，也不写入运行报告。"""
    return [
        dict(
            case_id=c["case_id"],
            round_id=r["round_id"],
            candidate_names=r["round_required_tools"][:],
            allowed_tools=r["round_required_tools"][:],
            action=r["expected_action"],
            calls=deepcopy(r["expected_calls"]),
            stop_reason=r["expected_stop_reason"],
            answer="测试回答",
            source="manual",
            run_id="unit-test",
        )
        for c in cases
        for r in c["rounds"]
    ]


def get_row(rows, case_id, round_id="initial"):
    return next(r for r in rows if r["case_id"] == case_id and r["round_id"] == round_id)


def test_dataset_and_existing_catalog_contract():
    manifest, cases = load_dataset()
    assert len(cases) == 12
    assert sum(len(c["rounds"]) for c in cases) == 21
    assert validate_tool_case_set()["status"] == "ok"
    for c in cases:
        for r in c["rounds"]:
            request = model_input(c, r, manifest)
            assert set(request) == {"input", "observations", "reference_date", "wind_rule"}


def test_parallel_order_does_not_matter_and_empty_recall_is_null():
    manifest, cases = load_dataset()
    rows = fixture_predictions(cases)
    get_row(rows, "MT-001")["calls"].reverse()
    report = score(manifest, cases, rows, agent=True)
    assert report["passed"]
    assert get_row(report["details"], "MT-011")["round_recall"] is None
    assert report["final_task_result"] == "not_evaluated"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_incomplete_or_duplicate_predictions_rejected(mutation):
    manifest, cases = load_dataset()
    rows = fixture_predictions(cases)
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(rows[0])
    else:
        rows[0]["round_id"] = "nonexistent"
    with pytest.raises(ValueError):
        score(manifest, cases, rows, agent=True)


def test_candidate_recall_does_not_hide_exposure():
    manifest, cases = load_dataset()
    rows = fixture_predictions(cases)
    get_row(rows, "MT-001")["candidate_names"].append("booking.cancel")
    get_row(rows, "MT-002")["candidate_names"].append("account.get_profile")
    report = score(manifest, cases, rows, agent=True)
    assert report["round_recall"] == 1
    assert report["side_effect_exposure_rounds"] == 1
    assert report["unauthorized_exposure_rounds"] == 2
    assert not report["passed"]


def test_early_call_and_fabricated_observation_ids_fail_dependency():
    manifest, cases = load_dataset()
    rows = fixture_predictions(cases)
    early = get_row(rows, "MT-004")
    early["calls"].append(
        {"name": "poi.search", "arguments": {"city": "厦门", "date": "2026-09-07"}}
    )
    fabricated = get_row(rows, "MT-006", "found")
    fabricated["calls"][0]["arguments"]["poi_ids"] = ["fabricated-a", "fabricated-b"]
    report = score(manifest, cases, rows, agent=True)
    assert get_row(report["details"], "MT-004")["first_deviation"] == "dependency"
    assert get_row(report["details"], "MT-006", "found")["first_deviation"] == "dependency"


def test_false_branch_and_duplicate_calls_fail():
    manifest, cases = load_dataset()
    rows = fixture_predictions(cases)
    false_branch = get_row(rows, "MT-004", "wind_false")
    false_branch["action"] = "call"
    false_branch["calls"] = [{"name": "poi.search", "arguments": {}}]
    repeated = get_row(rows, "MT-001")
    repeated["calls"].append(deepcopy(repeated["calls"][0]))
    report = score(manifest, cases, rows, agent=True)
    assert get_row(report["details"], "MT-004", "wind_false")["first_deviation"] == "action"
    assert not get_row(report["details"], "MT-001")["checks"]["stop"]


def test_gold_rejects_wrong_dependency(tmp_path):
    import json

    from training_eval.multi_tool_gold import DATA, write_jsonl

    _, cases = load_dataset()
    cases[5]["rounds"][1]["dependencies"][0]["expected_value"] = ["fake"]
    (tmp_path / "manifest.json").write_text((DATA / "manifest.json").read_text())
    write_jsonl(tmp_path / "cases.jsonl", cases)
    with pytest.raises(ValueError, match="依赖"):
        load_dataset(tmp_path)
    assert json.loads((tmp_path / "manifest.json").read_text())["allow_side_effects"] is False
