from __future__ import annotations

import base64
import json
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TOOL_CATALOG = PROJECT_ROOT / "datasets/tools/catalog-v1/catalog.json"
RESULT_NODE_IDS = {"call_integrated_workflow", "resume_integrated_workflow"}
MISSING_CONCEPT_TERMS = {
    "origin_pier": ("origin_pier", "出发码头"),
    "destination_pier": ("destination_pier", "到达码头"),
}


class ChatflowTurnRunner(Protocol):
    def run_turn(
        self,
        *,
        app_id: str,
        query: str,
        conversation_id: str = "",
    ) -> dict[str, Any]: ...


def console_base_url(value: str) -> str:
    parts = urlsplit(value.strip().rstrip("/"))
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


def iter_sse_events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []

    def decode() -> dict[str, Any] | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        data_lines.clear()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Dify SSE 事件不是有效 JSON：{raw[:300]}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Dify SSE 事件必须是 JSON 对象")
        return value

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            event = decode()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    event = decode()
    if event is not None:
        yield event


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _result_from_outputs(outputs: Any) -> dict[str, Any] | None:
    if not isinstance(outputs, dict):
        return None
    direct = _json_object(outputs.get("result_json"))
    if direct is not None:
        return direct
    for key in ("json", "text", "output"):
        candidate = outputs.get(key)
        parsed = _json_object(candidate)
        if parsed is None:
            continue
        nested = _json_object(parsed.get("result_json"))
        if nested is not None:
            return nested
        if "status" in parsed and "tool_trace" in parsed:
            return parsed
    return None


def parse_turn_events(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    conversation_id = ""
    message_id = ""
    workflow_run_id = ""
    result: dict[str, Any] | None = None
    answer_node_text = ""
    chunks: list[str] = []
    event_counts: Counter[str] = Counter()

    for event in events:
        event_type = str(event.get("event") or "unknown")
        event_counts[event_type] += 1
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        conversation_id = str(event.get("conversation_id") or conversation_id)
        message_id = str(event.get("message_id") or message_id)
        workflow_run_id = str(event.get("workflow_run_id") or workflow_run_id)

        if event_type == "error":
            message = event.get("message") or data.get("error") or "unknown error"
            raise RuntimeError(f"Dify Chatflow 运行失败：{message}")

        if event_type == "node_finished":
            status = str(data.get("status") or "")
            if status in {"failed", "error", "stopped"}:
                raise RuntimeError(
                    f"Dify 节点失败：{data.get('title') or data.get('node_id')}: "
                    f"{data.get('error') or status}"
                )
            node_id = str(data.get("node_id") or "")
            outputs = data.get("outputs")
            if node_id in RESULT_NODE_IDS:
                result = _result_from_outputs(outputs) or result
            if str(data.get("node_type") or "") == "answer" and isinstance(outputs, dict):
                answer_node_text = str(outputs.get("answer") or outputs.get("text") or "")

        if event_type == "workflow_finished":
            status = str(data.get("status") or "")
            if status and status != "succeeded":
                raise RuntimeError(
                    f"Dify Chatflow Workflow {status}：{data.get('error') or 'unknown error'}"
                )

        if event_type in {"message", "agent_message"}:
            chunk = event.get("answer") or data.get("answer") or data.get("text")
            if chunk:
                chunks.append(str(chunk))
        elif event_type == "text_chunk":
            chunk = data.get("text")
            if chunk:
                chunks.append(str(chunk))

    if result is None:
        raise RuntimeError(
            "Dify SSE 未返回综合 Workflow 的 result_json；"
            f"events={dict(event_counts)}"
        )
    if not conversation_id:
        raise RuntimeError("Dify SSE 未返回 conversation_id")

    answer = answer_node_text or "".join(chunks) or str(result.get("answer") or "")
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "workflow_run_id": workflow_run_id,
        "answer": answer,
        "result": result,
        "event_counts": dict(event_counts),
    }


class DifyConsoleChatflow:
    def __init__(self, *, base_url: str, timeout: float = 240.0) -> None:
        self._client = httpx.Client(
            base_url=f"{console_base_url(base_url)}/console/api",
            timeout=timeout,
            follow_redirects=True,
            trust_env=False,
        )

    def __enter__(self) -> DifyConsoleChatflow:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def login(self, *, email: str, password: str) -> None:
        encoded = base64.b64encode(password.encode("utf-8")).decode("ascii")
        response = self._client.post("/login", json={"email": email, "password": encoded})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("result") != "success":
            raise RuntimeError("Dify Console 登录失败")
        csrf = self._client.cookies.get("csrf_token") or self._client.cookies.get(
            "__Host-csrf_token"
        )
        if not csrf:
            raise RuntimeError("Dify Console 登录后未获得 CSRF cookie")
        self._client.headers["X-CSRF-Token"] = csrf

    def run_turn(
        self,
        *,
        app_id: str,
        query: str,
        conversation_id: str = "",
    ) -> dict[str, Any]:
        endpoint = f"/apps/{app_id}/advanced-chat/workflows/draft/run"
        payload = {
            "inputs": {},
            "query": query,
            "files": [],
            "conversation_id": conversation_id,
            "parent_message_id": None,
        }
        with self._client.stream("POST", endpoint, json=payload) as response:
            response.raise_for_status()
            events = list(iter_sse_events(response.iter_lines()))
        return parse_turn_events(events)


def run_multiturn_case(
    runner: ChatflowTurnRunner,
    *,
    app_id: str,
    case_input: Mapping[str, Any],
) -> dict[str, Any]:
    raw_turns = case_input.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ValueError("Dataset example 缺少 turns")

    conversation_id = ""
    outputs: list[dict[str, Any]] = []
    for index, raw_turn in enumerate(raw_turns, start=1):
        if not isinstance(raw_turn, dict):
            raise ValueError(f"第 {index} 轮不是对象")
        query = str(raw_turn.get("query") or "").strip()
        if not query:
            raise ValueError(f"第 {index} 轮缺少 query")
        turn_number = int(raw_turn.get("turn") or index)
        turn_output = runner.run_turn(
            app_id=app_id,
            query=query,
            conversation_id=conversation_id,
        )
        conversation_id = str(turn_output.get("conversation_id") or "")
        outputs.append({"turn": turn_number, "query": query, **turn_output})

    return {
        "case_id": str(case_input.get("case_id") or ""),
        "conversation_id": conversation_id,
        "turns": outputs,
    }


def _turn(output: Mapping[str, Any], number: int) -> dict[str, Any] | None:
    turns = output.get("turns")
    if not isinstance(turns, list):
        return None
    return next(
        (
            item
            for item in turns
            if isinstance(item, dict) and int(item.get("turn") or 0) == number
        ),
        None,
    )


def _tool_trace(turn: Mapping[str, Any]) -> dict[str, Any]:
    result = turn.get("result")
    if not isinstance(result, dict):
        return {}
    trace = result.get("tool_trace")
    return trace if isinstance(trace, dict) else {}


def _calls(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls = _tool_trace(turn).get("calls")
    return [item for item in calls if isinstance(item, dict)] if isinstance(calls, list) else []


def _observations(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    observations = _tool_trace(turn).get("observations")
    if not isinstance(observations, list):
        return []
    flattened: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        results = observation.get("results")
        if isinstance(results, list):
            flattened.extend(item for item in results if isinstance(item, dict))
            continue
        flattened.append(observation)
    return flattened


def _evaluation(errors: list[str]) -> dict[str, Any]:
    return {
        "score": 0.0 if errors else 1.0,
        "label": "fail" if errors else "pass",
        "explanation": "；".join(errors) if errors else "满足回归契约",
    }


def turn_1_partial_execution(output: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    spec = expected.get("turn_1")
    turn = _turn(output, 1)
    if not isinstance(spec, dict) or turn is None:
        return _evaluation(["缺少第 1 轮输出或预期约束"])

    result = turn.get("result") if isinstance(turn.get("result"), dict) else {}
    trace = _tool_trace(turn)
    errors: list[str] = []
    if result.get("status") != spec.get("status"):
        errors.append(f"status={result.get('status')!r}")
    if trace.get("action") != spec.get("action"):
        errors.append(f"action={trace.get('action')!r}")

    call_names = {str(item.get("name") or "") for item in _calls(turn)}
    required_calls = {str(item) for item in spec.get("required_calls") or []}
    missing_calls = sorted(required_calls - call_names)
    if missing_calls:
        errors.append(f"缺少 calls={missing_calls}")

    observation_names = {
        str(item.get("tool_name") or item.get("name") or "")
        for item in _observations(turn)
    }
    required_observations = {str(item) for item in spec.get("required_observations") or []}
    missing_observations = sorted(required_observations - observation_names)
    if missing_observations:
        errors.append(f"缺少 observations={missing_observations}")

    forbidden_actions = {str(item) for item in spec.get("forbidden_actions") or []}
    if str(trace.get("action") or "") in forbidden_actions:
        errors.append(f"出现禁止 action={trace.get('action')!r}")

    answer = str(turn.get("answer") or result.get("answer") or "").strip()
    banned_answers = {str(item).strip() for item in spec.get("answer_must_not_equal") or []}
    if answer in banned_answers:
        errors.append(f"答复命中禁止值={answer!r}")

    missing_information = [
        str(item)
        for item in (
            trace.get("missing_information")
            or result.get("route", {}).get("missing_information")
            or []
        )
    ]
    missing_text = " ".join(missing_information).lower()
    for concept in spec.get("required_missing_concepts") or []:
        terms = MISSING_CONCEPT_TERMS.get(str(concept), (str(concept),))
        if not any(term.lower() in missing_text for term in terms):
            errors.append(f"缺少澄清概念={concept}")

    return _evaluation(errors)


def _is_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _is_subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        unmatched = list(actual)
        for expected_item in expected:
            match_index = next(
                (
                    index
                    for index, actual_item in enumerate(unmatched)
                    if _is_subset(expected_item, actual_item)
                ),
                None,
            )
            if match_index is None:
                return False
            unmatched.pop(match_index)
        return True
    return expected == actual


def turn_2_context_carryover(output: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    spec = expected.get("turn_2")
    turn = _turn(output, 2)
    if not isinstance(spec, dict) or turn is None:
        return _evaluation(["缺少第 2 轮输出或预期约束"])

    result = turn.get("result") if isinstance(turn.get("result"), dict) else {}
    errors: list[str] = []
    if result.get("status") != spec.get("status"):
        errors.append(f"status={result.get('status')!r}")

    required_call = spec.get("required_call")
    if isinstance(required_call, dict):
        required_name = str(required_call.get("name") or "")
        actual_call = next(
            (item for item in _calls(turn) if str(item.get("name") or "") == required_name),
            None,
        )
        if actual_call is None:
            errors.append(f"缺少 call={required_name}")
        elif not _is_subset(
            required_call.get("arguments_subset") or {},
            actual_call.get("arguments") or {},
        ):
            errors.append(f"{required_name} 参数未保留多轮上下文")

    return _evaluation(errors)


def _side_effect_tool_names(path: Path = DEFAULT_TOOL_CATALOG) -> set[str]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(item["name"])
        for item in catalog.get("tools", [])
        if item.get("side_effect") != "none"
    }


def side_effect_safety(output: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    side_effect_tools = _side_effect_tool_names()
    errors: list[str] = []
    for turn_number in (1, 2):
        spec = expected.get(f"turn_{turn_number}")
        turn = _turn(output, turn_number)
        if not isinstance(spec, dict) or turn is None or "max_side_effect_count" not in spec:
            continue
        names = [str(item.get("name") or "") for item in _calls(turn)]
        count = sum(name in side_effect_tools for name in names)
        if count > int(spec["max_side_effect_count"]):
            errors.append(
                f"第 {turn_number} 轮副作用调用数={count}，"
                f"上限={spec['max_side_effect_count']}"
            )
    return _evaluation(errors)


def build_chatflow_evaluators() -> list[Any]:
    return [turn_1_partial_execution, turn_2_context_carryover, side_effect_safety]
