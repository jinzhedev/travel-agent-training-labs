from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from phoenix.client import Client
from phoenix.client.experiments import run_experiment
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = "lab01-weather-tool"


class Settings(BaseSettings):
    dify_base_url: str = ""
    dify_lab01_api_key: str = ""
    dify_lab01_app_id: str = ""
    phoenix_endpoint: str = ""
    phoenix_api_key: str = ""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")


def chat_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/chat-messages" if base.endswith("/v1") else f"{base}/v1/chat-messages"


def example_query(example: Any) -> str:
    value = getattr(example, "input", None)
    if value is None and isinstance(example, Mapping):
        value = example.get("input")
    if isinstance(value, Mapping):
        value = value.get("input") or value.get("query")
    query = str(value or "").strip()
    if not query:
        raise ValueError("dataset example 缺少 input.input/query")
    return query


def example_output(example: Any) -> Mapping[str, Any]:
    value = getattr(example, "output", None)
    if value is None and isinstance(example, Mapping):
        value = example.get("output")
    return value if isinstance(value, Mapping) else {}


def answer_quality(*, input: Mapping[str, Any], output: Mapping[str, Any], expected: Mapping[str, Any]) -> tuple[float, str, str]:
    del input
    actual = str(output.get("answer") or "")
    reference = str(expected.get("answer") or "")
    if not actual:
        return 0.0, "fail", "实际输出缺少 answer"

    required_terms: list[str]
    if "请告诉" in reference or "具体出行日期" in reference:
        required_terms = ["具体", "日期"]
    elif "亲子" in reference:
        required_terms = ["亲子", "植物园"]
    else:
        required_terms = ["多云"]
    missing = [term for term in required_terms if term not in actual]
    if missing:
        return 0.0, "fail", f"回答缺少关键信息：{', '.join(missing)}"
    return 1.0, "pass", "回答包含该案例的关键预期信息"


def main() -> int:
    parser = argparse.ArgumentParser(description="使用 Dify Chatflow API 运行 Phoenix Lab 01 Experiment")
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET)
    parser.add_argument("--app-id", default="")
    parser.add_argument("--experiment-name", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    base_url = settings.dify_base_url.strip()
    api_key = settings.dify_lab01_api_key.strip()
    app_id = (args.app_id or settings.dify_lab01_app_id).strip()
    phoenix_endpoint = settings.phoenix_endpoint.strip()
    missing = [
        name
        for name, value in (
            ("DIFY_BASE_URL", base_url),
            ("DIFY_LAB01_API_KEY", api_key),
            ("PHOENIX_ENDPOINT", phoenix_endpoint),
        )
        if not value
    ]
    if missing:
        raise SystemExit("缺少运行参数：" + ", ".join(missing))

    phoenix_kwargs: dict[str, Any] = {"base_url": phoenix_endpoint}
    if settings.phoenix_api_key:
        phoenix_kwargs["api_key"] = settings.phoenix_api_key
    client = Client(**phoenix_kwargs)
    dataset = client.datasets.get_dataset(dataset=args.dataset_name)
    endpoint = chat_endpoint(base_url)

    with httpx.Client(timeout=args.timeout, trust_env=False) as http_client:
        def task(example: Any) -> dict[str, Any]:
            query = example_query(example)
            response = http_client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "inputs": {},
                    "query": query,
                    "response_mode": "blocking",
                    "user": "lab01-phoenix-eval",
                },
            )
            response.raise_for_status()
            body = response.json()
            return {
                "answer": body.get("answer") or "",
                "conversation_id": body.get("conversation_id") or "",
                "message_id": body.get("message_id") or "",
                "metadata": body.get("metadata") or {},
            }

        def evaluator(input: Mapping[str, Any], output: Mapping[str, Any], expected: Mapping[str, Any]):
            return answer_quality(input=input, output=output, expected=expected)

        experiment = run_experiment(
            client=client,
            dataset=dataset,
            task=task,
            evaluators={"answer_quality": evaluator},
            experiment_name=args.experiment_name
            or datetime.now().astimezone().strftime("lab01-%Y%m%d-%H%M%S"),
            experiment_description="Lab 01：Dify Chatflow API 的最小回答评测",
            experiment_metadata={
                "suite": "lab01-chatflow",
                **({"app_id": app_id} if app_id else {}),
                "dify_api_mode": "chat-messages",
            },
            dry_run=args.dry_run,
            timeout=int(args.timeout),
            repetitions=args.repetitions,
            retries=0,
        )

    print(json.dumps({
        "status": "completed",
        "dataset_name": args.dataset_name,
        "experiment_id": experiment.get("experiment_id", ""),
        "task_run_count": len(experiment.get("task_runs", [])),
        "evaluation_run_count": len(experiment.get("evaluation_runs", [])),
        "dry_run": args.dry_run,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
