from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import httpx
from phoenix.client import Client
from phoenix.client.experiments import run_experiment
from phoenix.client.resources.experiments.types import EvalsEvaluator
from phoenix.evals import LLM, ClassificationEvaluator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = "lab01-weather-tool"


class Settings(BaseSettings):
    dify_base_url: str = ""
    dify_lab01_api_key: str = ""
    dify_lab01_app_id: str = ""
    phoenix_endpoint: str = ""
    phoenix_api_key: str = ""
    judge_api_key: str = ""
    judge_base_url: str = ""
    judge_model: str = ""

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


def build_llm_judge(
    *, api_key: str, base_url: str, model: str
) -> ClassificationEvaluator:
    return ClassificationEvaluator(
        name="llm_answer_quality",
        llm=LLM(
            provider="openai",
            model=model,
            sync_client_kwargs={"api_key": api_key, "base_url": base_url},
            async_client_kwargs={"api_key": api_key, "base_url": base_url},
        ),
        prompt_template="""
你是一个严格但务实的 Chatflow 评测员。请比较用户输入、参考答案和实际回答，
判断实际回答是否完成了用户请求。

判定规则：
1. 对天气问题，日期必须与参考答案一致，天气事实也必须与参考答案一致。
2. 对景点问题，必须回答用户要求的城市、日期和主题，并保留参考答案中的关键结果。
3. 如果用户没有提供出行日期，正确行为是询问具体日期，不能擅自使用今天或其他日期查询。
4. 允许 Markdown、措辞差异以及模型输出中的 <think> 内容；只评价最终回答的事实和任务完成度。
5. 如果实际回答只是部分结果、工具调用失败、达到迭代上限，判定为 fail。

用户输入：
<input>{{input}}</input>

参考答案（expected）：
<expected>{{expected}}</expected>

实际回答（output）：
<output>{{output}}</output>

只输出一个分类：pass 或 fail。
""",
        choices={
            "pass": (1.0, "实际回答正确满足用户请求，且没有编造缺失信息。"),
            "fail": (0.0, "实际回答错误、不完整，或在日期缺失时擅自猜测并调用工具。"),
        },
        direction="maximize",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="使用 Phoenix LLM-as-a-judge 评测 Dify Lab 01")
    parser.add_argument("--dataset-name", default=DEFAULT_DATASET)
    parser.add_argument("--app-id", default="")
    parser.add_argument("--experiment-name", default="")
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--judge-base-url", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    base_url = settings.dify_base_url.strip()
    dify_api_key = settings.dify_lab01_api_key.strip()
    app_id = (args.app_id or settings.dify_lab01_app_id).strip()
    phoenix_endpoint = settings.phoenix_endpoint.strip()
    judge_api_key = settings.judge_api_key.strip()
    judge_base_url = (args.judge_base_url or settings.judge_base_url).strip()
    judge_model = (args.judge_model or settings.judge_model).strip()
    missing = [
        name
        for name, value in (
            ("DIFY_BASE_URL", base_url),
            ("DIFY_LAB01_API_KEY", dify_api_key),
            ("PHOENIX_ENDPOINT", phoenix_endpoint),
            ("JUDGE_API_KEY", judge_api_key),
            ("JUDGE_BASE_URL/--judge-base-url", judge_base_url),
            ("JUDGE_MODEL/--judge-model", judge_model),
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
    judge = build_llm_judge(api_key=judge_api_key, base_url=judge_base_url, model=judge_model)

    with httpx.Client(timeout=args.timeout, trust_env=False) as http_client:
        def task(example: Any) -> dict[str, Any]:
            query = example_query(example)
            response = http_client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {dify_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "inputs": {},
                    "query": query,
                    "response_mode": "blocking",
                    "user": "lab01-phoenix-llm-judge",
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

        experiment = run_experiment(
            client=client,
            dataset=dataset,
            task=task,
            evaluators={"llm_answer_quality": cast(EvalsEvaluator, judge)},
            experiment_name=args.experiment_name
            or datetime.now().astimezone().strftime("lab01-llm-judge-%Y%m%d-%H%M%S"),
            experiment_description="Lab 01：使用 Phoenix LLM-as-a-judge 评测 Dify Chatflow 最终回答",
            experiment_metadata={
                "suite": "lab01-chatflow",
                **({"app_id": app_id} if app_id else {}),
                "dify_api_mode": "chat-messages",
                "evaluator": "llm_answer_quality",
                "judge_model": judge_model,
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
