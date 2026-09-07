from __future__ import annotations

import argparse
import json
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from openai import OpenAI
from openinference.instrumentation.openai import OpenAIInstrumentor
from phoenix.otel import register
from pydantic_settings import BaseSettings, SettingsConfigDict

from langgraph_lab01.graph import build_graph

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    travel_core_base_url: str = ""
    travel_core_api_key: str = ""
    glm_api_key: str = ""
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-5.3-flash"
    phoenix_endpoint: str = ""
    phoenix_api_key: str = ""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 LangGraph 版 Lab 01 Chatflow")
    parser.add_argument("query")
    parser.add_argument("--model", default="")
    parser.add_argument("--thread-id", default="lab01-demo")
    args = parser.parse_args()
    settings = Settings()
    missing = [
        name
        for name, value in (
            ("TRAVEL_CORE_BASE_URL", settings.travel_core_base_url),
            ("TRAVEL_CORE_API_KEY", settings.travel_core_api_key),
            ("GLM_API_KEY", settings.glm_api_key),
        )
        if not value.strip()
    ]
    if missing:
        raise SystemExit("缺少运行参数：" + ", ".join(missing))

    phoenix_endpoint = settings.phoenix_endpoint.rstrip("/")
    if not phoenix_endpoint.endswith("/v1/traces"):
        phoenix_endpoint += "/v1/traces"

    tracer_provider = register(
        project_name="Lab 01 - LangGraph",
        endpoint=phoenix_endpoint,
        protocol="http/protobuf",
        api_key=settings.phoenix_api_key or None,
    )
    OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)

    client = OpenAI(
        api_key=settings.glm_api_key,
        base_url=settings.glm_base_url,
    )
    tracer = tracer_provider.get_tracer(__name__)
    checkpointer = MemorySaver()
    graph = build_graph(
        llm_client=client,
        model=args.model or settings.glm_model,
        travel_core_base_url=settings.travel_core_base_url,
        travel_core_api_key=settings.travel_core_api_key,
        tracer=tracer,
        checkpointer=checkpointer,
    )
    with tracer.start_as_current_span(
        "lab01.langgraph",
        openinference_span_kind="chain",
    ) as span:
        span.set_attribute("input.value", json.dumps({"query": args.query}, ensure_ascii=False))
        span.set_attribute("input.mime_type", "application/json")
        state = graph.invoke(
            {"messages": [{"role": "user", "content": args.query}]},
            {"configurable": {"thread_id": args.thread_id}},
            context={
                "user_id": "lab01-student",
                "tenant_id": "training",
            },
        )
        span.set_attribute("output.value", json.dumps(state, ensure_ascii=False, default=str))
        span.set_attribute("output.mime_type", "application/json")
    tracer_provider.force_flush()
    print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
