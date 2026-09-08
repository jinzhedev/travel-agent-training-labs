from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/run_phoenix_lab04_eval.py"
SPEC = spec_from_file_location("lab04_eval", SCRIPT)
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_call_evidence_ignores_round_duplicates_and_credentials():
    call = {
        "tool_call_id": "call-1",
        "tool_call_name": "poi_search",
        "tool_call_input": {
            "city": "厦门",
            "tags": ["亲子"],
            "X-API-Key": "private-key",
            "unexpected": "secret",
        },
        "tool_response": "private response",
    }
    step = {"label": "CALL poi_search", "status": "success", "data": {"output": call}}
    evidence = MODULE.tool_observations(
        {
            "json": [
                {"label": "ROUND 1", "data": {"output": call}},
                step,
                deepcopy(step),
                {"data": []},
            ]
        }
    )
    assert evidence == [
        {"name": "poi_search", "status": "success", "arguments": {"city": "厦门", "tags": ["亲子"]}}
    ]


def poi_example():
    case = MODULE.load_cases()[2]
    call = {
        "name": "poi_search",
        "status": "success",
        "arguments": {"city": "厦门", "date": "2026-09-07", "tags": ["亲子"]},
    }
    return case, {"workers": [{"node_type": "agent", "tool_calls": [call]}]}


def test_successful_answer_without_preference_is_not_a_pass():
    case, output = poi_example()
    assert MODULE.tool_execution(output, case["expected"], case["input"])[0] == 1
    del output["workers"][0]["tool_calls"][0]["arguments"]["tags"]
    output["result"] = {"answer": "已调用 poi_search，推荐亲子景点。"}
    assert MODULE.tool_execution(output, case["expected"], case["input"])[0] == 0


@pytest.mark.parametrize("mutation", ["date", "missing", "duplicate", "wrong_tool", "failed"])
def test_wrong_actual_call_fails(mutation):
    case, output = poi_example()
    calls = output["workers"][0]["tool_calls"]
    if mutation == "date":
        calls[0]["arguments"]["date"] = "2026-09-08"
    elif mutation == "missing":
        calls.clear()
    elif mutation == "duplicate":
        calls.append(deepcopy(calls[0]))
    elif mutation == "wrong_tool":
        calls[0]["name"] = "save_itinerary"
    else:
        calls[0]["status"] = "failed"
    assert MODULE.tool_execution(output, case["expected"], case["input"])[0] == 0


def test_blocked_request_must_not_call_tools():
    case = MODULE.load_cases()[3]
    assert MODULE.tool_execution({"workers": []}, case["expected"], case["input"])[0] == 1
    _, output = poi_example()
    assert MODULE.tool_execution(output, case["expected"], case["input"])[0] == 0


def test_http_fallback_is_explicitly_unscored():
    case = MODULE.load_cases()[6]
    score, label, _ = MODULE.tool_execution(
        {"workers": [{"node_type": "llm"}]}, case["expected"], case["input"]
    )
    assert score is None and label == "not_scored"


def test_final_answer_excludes_tool_round_preface():
    rounds = {
        "json": [
            {
                "label": "ROUND 1",
                "parent_id": None,
                "status": "success",
                "data": {"output": {"llm_response": "我先查询。", "tool_responses": [{}]}},
            },
            {"label": "CALL poi_search", "parent_id": "round-1", "status": "success"},
            {
                "label": "ROUND 2",
                "parent_id": None,
                "status": "success",
                "data": {"output": {"llm_response": "  已查到植物园。  ", "tool_responses": []}},
            },
            {"data": []},
        ]
    }
    assert MODULE.final_worker_text(rounds) == "已查到植物园。"


@pytest.mark.parametrize(
    "output",
    [
        {"tool_responses": [{}], "llm_response": "准备查询"},
        {"tool_responses": [], "llm_response": " "},
        {"llm_response": "没有轮次证据"},
    ],
)
def test_incomplete_agent_round_is_not_final_answer(output):
    with pytest.raises(ValueError):
        MODULE.final_worker_text(
            {
                "json": [
                    {
                        "label": "ROUND 1",
                        "parent_id": None,
                        "status": "success",
                        "data": {"output": output},
                    },
                ]
            }
        )
