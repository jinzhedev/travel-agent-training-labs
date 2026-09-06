from __future__ import annotations

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/run_dify_rag_eval.py"
SPEC = spec_from_file_location("run_dify_rag_eval", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

_iter_sse_events = MODULE._iter_sse_events
_report_path = MODULE._report_path
_transport_summary = MODULE._transport_summary
_workflow_inputs = MODULE._workflow_inputs


def test_iter_sse_events_ignores_comments_and_decodes_data() -> None:
    lines = [
        ": keep-alive",
        "",
        'data: {"event":"workflow_started","data":{"id":"run-1"}}',
        "",
        'data: {"event":"text_chunk","data":{"text":"答"}}',
        "",
        'data: {"event":"workflow_finished",',
        'data: "data":{"status":"succeeded"}}',
    ]

    events = list(_iter_sse_events(lines))

    assert [item["event"] for item in events] == [
        "workflow_started",
        "text_chunk",
        "workflow_finished",
    ]
    assert events[-1]["data"]["status"] == "succeeded"


def test_workflow_inputs_switch_between_auto_and_fixed_plan() -> None:
    case = {"query": "问题", "mode": "multihop"}
    plan = {"slots": [{"slot_id": "slot-1", "query": "检索词"}]}

    automatic = _workflow_inputs(case, plan, "auto")
    fixed = _workflow_inputs(case, plan, "fixed")

    assert automatic["rag_evidence_plan_json"] == ""
    assert json.loads(fixed["rag_evidence_plan_json"]) == plan["slots"]
    assert automatic["rag_backend_dify_loop"] is True


def test_transport_summary_keeps_first_event_separate_from_ttft() -> None:
    predictions = [
        {"end_to_end_latency_ms": 1000, "first_event_ms": 50, "ttft_ms": None},
        {"end_to_end_latency_ms": 2000, "first_event_ms": 80, "ttft_ms": 1500},
    ]

    result = _transport_summary(predictions)

    assert result["end_to_end_p95_ms"] == 2000
    assert result["first_event_p50_ms"] == 80
    assert result["ttft_observed_count"] == 1
    assert result["ttft_p95_ms"] == 1500


def test_report_path_never_overwrites_predictions() -> None:
    assert _report_path(Path("run.predictions.jsonl")) == Path("run.report.json")
    assert _report_path(Path("custom.jsonl")) == Path("custom.report.json")
