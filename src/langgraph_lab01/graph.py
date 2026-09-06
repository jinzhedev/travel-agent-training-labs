from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import date
from typing import Annotated, Any, Literal, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from openai import OpenAI


class Lab01State(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], add_messages]
    current_date: str
    tool_rounds: int


class Lab01Context(TypedDict, total=False):
    user_id: str
    tenant_id: str
    reference_date: str


SYSTEM_PROMPT = """你是厦门文旅助手。

你只能使用以下两个工具：
- weather_forecast：查询指定日期的天气、是否下雨、风速和温度
- poi_search：查询指定日期的厦门景点

规则：
1. 询问天气、晴雨、温度或风速时使用 weather_forecast；询问景点时使用 poi_search。
2. 只调用回答当前问题所需的工具，不重复调用，不为景点问题额外查询天气。
3. 工具参数中的日期必须使用 YYYY-MM-DD。
4. 用户只提供月日时，结合当前日期确定年份。
5. 用户没有提供天气查询日期时，不要调用工具，直接询问具体出行日期；不要默认查询今天。
6. 轮渡、船票、酒店、交通、餐厅等不在工具列表中的请求，不要猜测，也不要用相近工具替代。说明当前只支持天气和景点查询。
7. 工具调用完成后必须输出中文最终回答，不要输出 <think>、内部推理或工具选择过程。

当前日期：{current_date}
"""


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "weather_forecast",
            "description": "查询厦门指定日期的天气、是否下雨、温度和风力。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市，例如厦门"},
                    "start_date": {"type": "string", "format": "date"},
                    "end_date": {"type": "string", "format": "date"},
                },
                "required": ["city", "start_date", "end_date"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "poi_search",
            "description": "查询厦门指定日期、指定主题的景点。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市，例如厦门"},
                    "date": {"type": "string", "format": "date"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["city", "date", "tags"],
                "additionalProperties": False,
            },
        },
    },
]


def _messages_for_model(state: Lab01State) -> list[dict[str, Any]]:
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(current_date=state["current_date"])}]
    for message in state.get("messages", []):
        if isinstance(message, dict):
            messages.append(message)
            continue
        role = {
            "human": "user",
            "ai": "assistant",
            "tool": "tool",
            "system": "system",
        }.get(getattr(message, "type", ""), "user")
        converted: dict[str, Any] = {
            "role": role,
            "content": message.content or "",
        }
        if role == "assistant" and getattr(message, "tool_calls", None):
            converted["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["args"], ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        if role == "tool":
            converted["tool_call_id"] = message.tool_call_id
        messages.append(converted)
    return messages


def _tool_calls(message: Any) -> list[dict[str, Any]]:
    if isinstance(message, dict):
        return message.get("tool_calls", [])
    return getattr(message, "tool_calls", []) or []


def build_graph(
    *,
    llm_client: OpenAI,
    model: str,
    travel_core_base_url: str,
    travel_core_api_key: str,
    timeout: float = 30.0,
    tracer: Any | None = None,
    checkpointer: Any | None = None,
):
    """Build the Lab 01 graph with injectable LLM and HTTP configuration."""

    def set_current_date(state: Lab01State, *, runtime: Runtime[Lab01Context]) -> dict[str, str]:
        _ = state
        with _span(tracer, "current_date", "chain") as span:
            value = (runtime.context or {}).get("reference_date") or date.today().isoformat()
            _set_io(span, {}, {"current_date": value})
            return {"current_date": value}

    def agent(state: Lab01State) -> dict[str, Any]:
        with _span(tracer, "agent", "agent") as span:
            model_messages = _messages_for_model(state)
            _set_io(span, model_messages, None)
            response = llm_client.chat.completions.create(
                model=model,
                messages=model_messages,
                tools=TOOL_SCHEMAS,
                temperature=0,
            )
            message = response.choices[0].message
            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": message.content or "",
            }
            if message.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in message.tool_calls
                ]
            _set_io(span, model_messages, assistant)
            return {"messages": [assistant]}

    def tools(state: Lab01State) -> dict[str, Any]:
        messages = state.get("messages")
        if not messages:
            raise RuntimeError("state.messages 不能为空")
        last = messages[-1]
        results: list[dict[str, Any]] = []
        for call in _tool_calls(last):
            if "function" in call:
                name = call["function"]["name"]
                arguments = json.loads(call["function"]["arguments"])
            else:
                name = call["name"]
                arguments = call["args"]
            if name == "weather_forecast":
                path = "/v1/tools/weather/forecast"
            elif name == "poi_search":
                path = "/v1/tools/poi/search"
            else:
                results.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps({"error": f"unsupported tool: {name}"}, ensure_ascii=False),
                })
                continue
            with _span(tracer, name, "tool") as span:
                _set_io(span, arguments, None)
                response = httpx.post(
                    f"{travel_core_base_url.rstrip('/')}{path}",
                    headers={"X-API-KEY": travel_core_api_key},
                    json=arguments,
                    timeout=timeout,
                    trust_env=False,
                )
                response.raise_for_status()
                _set_io(span, arguments, response.text)
                results.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": response.text,
                })
        return {"messages": results, "tool_rounds": state.get("tool_rounds", 0) + 1}

    def route(state: Lab01State) -> Literal["tools", "end"]:
        messages = state.get("messages")
        if not messages:
            return 'end'
        last = messages[-1]
        return "tools" if _tool_calls(last) and state.get("tool_rounds", 0) < 2 else "end"

    builder = StateGraph(Lab01State, context_schema=Lab01Context)
    builder.add_node("current_date", set_current_date)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_edge(START, "current_date")
    builder.add_edge("current_date", "agent")
    builder.add_conditional_edges("agent", route, {"tools": "tools", "end": END})
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)


def _span(tracer: Any | None, name: str, kind: str):
    if tracer is None:
        return nullcontext()
    return tracer.start_as_current_span(name, openinference_span_kind=kind)


def _set_io(span: Any, input_value: Any, output_value: Any) -> None:
    if span is None:
        return
    span.set_attribute("input.value", json.dumps(input_value, ensure_ascii=False, default=str))
    span.set_attribute("input.mime_type", "application/json")
    if output_value is not None:
        span.set_attribute("output.value", json.dumps(output_value, ensure_ascii=False, default=str))
        span.set_attribute("output.mime_type", "application/json")


def export_graph_png(graph: Any, output_path: str) -> None:
    """Export a compiled LangGraph graph as a PNG image."""
    graph.get_graph().draw_mermaid_png(output_file_path=output_path)
