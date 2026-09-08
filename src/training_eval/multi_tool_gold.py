"""Lab 02 多工具金标：独立轮次候选评测和计划评分，不执行工具。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field

from travel_core.config import get_settings
from travel_core.schemas import ToolSelectIn
from travel_core.security import RequestContext
from travel_core.services.tools import catalog, select_tools

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "datasets/eval/multi-tool-gold-v1"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Call(Record):
    name: str
    arguments: dict[str, Any]


class Observation(Record):
    name: str
    status: Literal["success"]
    data: dict[str, Any]


class Dependency(Record):
    from_: str = Field(alias="from")
    to: str
    argument: str | None = None
    observation_path: Literal["matches[].poi_id"] | None = None
    expected_value: list[str] | None = None
    condition: dict[str, Any] | None = None


class Round(Record):
    round_id: str
    observations: list[Observation]
    round_required_tools: list[str]
    acceptable_tools: list[str] = Field(max_length=0)
    forbidden_tools: list[str]
    expected_action: Literal["call", "clarify", "answer", "refuse"]
    expected_calls: list[Call]
    max_calls: int = Field(ge=0)
    dependencies: list[Dependency]
    expected_stop_reason: Literal["continue", "done", "needs_input", "refused"]


class Case(Record):
    case_id: str
    input: str
    relation: Literal["single", "parallel", "ordered", "conditional"]
    required_tools: list[str]
    risk_tags: list[str]
    source_case_id: str | None
    rounds: list[Round] = Field(min_length=1)


class Prediction(Record):
    case_id: str
    round_id: str
    candidate_names: list[str]
    allowed_tools: list[str]
    action: Literal["call", "clarify", "answer", "refuse"]
    calls: list[Call]
    stop_reason: Literal["continue", "done", "needs_input", "refused"]
    answer: str
    source: Literal["model", "dify", "manual"]
    run_id: str = Field(min_length=1)
    error: str | None = None


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def dependency_valid(dependency: dict, observations: list[dict], calls: list[dict]) -> bool:
    source = next((o["data"] for o in observations if o["name"] == dependency["from"]), None)
    if source is None:
        return False
    condition = dependency.get("condition")
    if condition and not any(
        source.get(rule["field"], float("-inf")) >= rule["gte"] for rule in condition["any"]
    ):
        return False
    argument = dependency.get("argument")
    if argument:
        values = [item["poi_id"] for item in source["matches"]]
        return values == dependency["expected_value"] and all(
            c["arguments"].get(argument) == values for c in calls if c["name"] == dependency["to"]
        )
    return True


def load_dataset(directory: Path = DATA) -> tuple[dict, list[dict]]:
    manifest = json.loads((directory / "manifest.json").read_text())
    cases = read_jsonl(directory / "cases.jsonl")
    if len(cases) != 12 or len({c["case_id"] for c in cases}) != 12:
        raise ValueError("需要 12 条不重复的金标")
    tools = {t["name"]: t for t in catalog()["tools"]}
    pois = {
        p["poi_id"]: p for p in json.loads(Path(get_settings().fixture_path).read_text())["pois"]
    }
    if manifest["catalog_revision"] != catalog()["revision"]:
        raise ValueError("工具目录 revision 与金标不一致")
    if manifest["environment"] != get_settings().environment or manifest["allow_side_effects"]:
        raise ValueError("实验要求固定环境并禁用副作用")
    for case in cases:
        Case.model_validate(case)
        if len({r["round_id"] for r in case["rounds"]}) != len(case["rounds"]):
            raise ValueError(f"{case['case_id']}: round_id 重复")
        required = set(case["required_tools"])
        if not required <= tools.keys():
            raise ValueError("全任务集合含未知工具")
        for r in case["rounds"]:
            names = [c["name"] for c in r["expected_calls"]]
            current = set(r["round_required_tools"])
            if set(names) != current or len(names) != len(current) or not current <= required:
                raise ValueError("轮次工具与期望调用不一致")
            if current & set(r["forbidden_tools"]) or not set(r["forbidden_tools"]) <= tools.keys():
                raise ValueError("禁止工具标注冲突或未知")
            if (r["expected_action"] == "call") != bool(names) or len(names) > r["max_calls"]:
                raise ValueError("动作、调用数或预算不一致")
            for call in r["expected_calls"]:
                tool = tools[call["name"]]
                if tool["side_effect"] != "none" or not set(tool["permissions"]) <= set(
                    manifest["available_permissions"]
                ):
                    raise ValueError("金标要求越权或副作用调用")
                Draft202012Validator(tool["input_schema"], format_checker=FormatChecker()).validate(
                    call["arguments"]
                )
            for o in r["observations"]:
                if o["name"] not in required:
                    raise ValueError("Observation 来源不属于全任务工具集合")
                if o["name"] == "poi.search":
                    for match in o["data"]["matches"]:
                        poi = pois.get(match["poi_id"])
                        if poi is None or poi["name"] != match["name"]:
                            raise ValueError("Observation 景点 ID 或名称与课程数据不一致")
                        if not set(match.get("tags", [])) <= set(poi["tags"]):
                            raise ValueError("Observation 景点标签与课程数据不一致")
            for d in r["dependencies"]:
                if d["to"] not in current or not dependency_valid(
                    d, r["observations"], r["expected_calls"]
                ):
                    raise ValueError("依赖与冻结 Observation 不一致")
    return manifest, cases


def model_input(case: dict, r: dict, manifest: dict) -> dict:
    """只发送原问题和已发生的 Observation，不泄露金标或后续答案。"""
    return {
        "input": case["input"],
        "observations": r["observations"],
        "reference_date": manifest["reference_date"],
        "wind_rule": manifest["wind_rule"],
    }


def candidate_rows(manifest: dict, cases: list[dict], strategy: str, max_tools: int) -> list[dict]:
    ctx = RequestContext(
        manifest["tenant_id"],
        manifest["user_id"],
        "multi-tool-gold",
        None,
        frozenset(manifest["available_permissions"]),
    )
    rows = []
    for case in cases:
        for r in case["rounds"]:
            request = model_input(case, r, manifest)
            # 后续轮以相同原问题和当前结果检索；没有从 gold 提取 namespace 或工具名。
            task = case["input"]
            if r["observations"]:
                task += "\n已有结果：" + json.dumps(r["observations"], ensure_ascii=False)
            result = select_tools(
                ToolSelectIn(
                    task=task, strategy=strategy, max_tools=max_tools, allow_side_effects=False
                ),
                ctx,
            )
            rows.append(
                {
                    "case_id": case["case_id"],
                    "round_id": r["round_id"],
                    "request": request,
                    **result,
                }
            )
    return rows


def score(manifest: dict, cases: list[dict], rows: list[dict], *, agent: bool = False) -> dict:
    gold = {(c["case_id"], r["round_id"]): (c, r) for c in cases for r in c["rounds"]}
    actual = {}
    tools = {t["name"]: t for t in catalog()["tools"]}
    for row in rows:
        if agent:
            Prediction.model_validate(row)
        key = row["case_id"], row["round_id"]
        if key in actual:
            raise ValueError(f"预测轮次重复：{key}")
        actual[key] = row
    if actual.keys() != gold.keys():
        raise ValueError(
            f"预测轮次不完整：missing={sorted(gold.keys() - actual.keys())}, extra={sorted(actual.keys() - gold.keys())}"
        )
    details = []
    hits = total = exposed = 0
    for key, (case, r) in gold.items():
        p = actual[key]
        candidate = set(p["candidate_names"])
        required = set(r["round_required_tools"])
        forbidden = set(r["forbidden_tools"])
        missing = sorted(required - candidate)
        exposure = sorted(candidate & forbidden)
        unknown = sorted(candidate - tools.keys())
        unauthorized = sorted(
            name
            for name in candidate & tools.keys()
            if not set(tools[name]["permissions"]) <= set(manifest["available_permissions"])
            or manifest["environment"] not in tools[name]["environments"]
        )
        hits += len(required & candidate)
        total += len(required)
        exposed += bool(exposure)
        checks = {
            "candidate": not missing and not unknown,
            "exposure": not exposure and not unauthorized,
        }
        detail = {
            "case_id": key[0],
            "round_id": key[1],
            "round_recall": len(required & candidate) / len(required) if required else None,
            "candidate_precision": len(set(case["required_tools"]) & candidate) / len(candidate)
            if candidate
            else None,
            "candidate_inflation": len(candidate - set(case["required_tools"])),
            "missing_tools": missing,
            "forbidden_exposure": exposure,
            "unknown_tools": unknown,
            "unauthorized_exposure": unauthorized,
        }
        if agent:
            calls = p["calls"]
            names = [c["name"] for c in calls]
            allowed = set(p["allowed_tools"])
            expected = {c["name"]: c["arguments"] for c in r["expected_calls"]}
            # 每轮均为当前可执行集合；同轮独立调用顺序不影响得分。
            checks["action"] = p["action"] == r["expected_action"] and not p.get("error")
            checks["selection"] = (
                Counter(names) == Counter(expected.keys())
                and allowed <= candidate
                and set(names) <= allowed
            )
            checks["dependency"] = not (set(names) & forbidden) and all(
                dependency_valid(d, r["observations"], calls) for d in r["dependencies"]
            )

            def argument_matches(call: dict) -> bool:
                args = dict(call["arguments"])
                expected_args = expected.get(call["name"])
                if call["name"] == "poi.get_details" and args.get("city") == "厦门":
                    args.pop("city")
                return args == expected_args

            checks["parameters"] = checks["selection"] and all(argument_matches(c) for c in calls)
            repeated = len(names) != len(set(names))
            checks["stop"] = (
                len(calls) <= r["max_calls"]
                and not repeated
                and p["stop_reason"] == r["expected_stop_reason"]
            )
            detail.update(
                action=p["action"],
                calls=calls,
                stop_reason=p["stop_reason"],
                answer=p["answer"],
                run_id=p["run_id"],
                source=p["source"],
                error=p.get("error"),
            )
        order = ["action", "candidate", "exposure", "dependency", "selection", "parameters", "stop"]
        detail.update(
            checks=checks,
            passed=all(checks.values()),
            first_deviation=next((k for k in order if k in checks and not checks[k]), None),
        )
        details.append(detail)
    recall = hits / total if total else None
    result = {
        "mode": "agent_plan" if agent else "candidate_only",
        "case_count": len(cases),
        "round_count": len(details),
        "required_tool_hits": hits,
        "required_tool_total": total,
        "round_recall": recall,
        "forbidden_exposure_rounds": exposed,
        "unauthorized_exposure_rounds": sum(bool(d["unauthorized_exposure"]) for d in details),
        "side_effect_exposure_rounds": sum(
            any(
                tools[n]["side_effect"] != "none"
                for n in actual[key]["candidate_names"]
                if n in tools
            )
            for key in gold
        ),
        "average_candidate_inflation": sum(d["candidate_inflation"] for d in details)
        / len(details),
        "passed_rounds": sum(d["passed"] for d in details),
        "passed": all(d["passed"] for d in details)
        and (recall is None or recall >= manifest["minimum_round_recall"]),
        "final_task_result": "not_evaluated",
        "details": details,
    }
    if agent:
        result["action_accuracy"] = sum(d["checks"]["action"] for d in details) / len(details)
        result["dependency_pass_rate"] = sum(d["checks"]["dependency"] for d in details) / len(
            details
        )
    return result


def dataset_hash(directory: Path = DATA) -> str:
    return hashlib.sha256(
        (directory / "manifest.json").read_bytes() + (directory / "cases.jsonl").read_bytes()
    ).hexdigest()
