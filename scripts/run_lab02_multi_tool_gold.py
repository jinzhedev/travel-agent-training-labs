from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from openai import OpenAI
from pydantic_settings import BaseSettings, SettingsConfigDict

from training_eval.multi_tool_gold import (
    DATA,
    ROOT,
    candidate_rows,
    dataset_hash,
    load_dataset,
    read_jsonl,
    score,
    write_jsonl,
)


class Settings(BaseSettings):
    glm_api_key: str = ""
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-5.3-flash"
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")


SYSTEM = """你是文旅工具选择器。只返回 JSON，不执行工具。
根据原问题、可信的已有 Observation 和 allowed_tools，决定当前一轮的动作。
已有成功结果不重复查询。独立只读任务可在同轮调用；依赖前序结果的工具必须等待结果，
不能在同一轮猜测参数。只使用 Observation 的景点 ID。不得执行写操作。
用户缺必要参数时澄清；无需工具时回答；无目的批量调用请求应拒绝。
当前参考日期和课堂大风分支约定在输入中。日期、城市等可选参数只在业务需要时填写。
不要把后续轮调用放到当前 calls。
返回结构：{"action":"call|clarify|answer|refuse","calls":[{"name":"工具名","arguments":{}}],
"stop_reason":"continue|done|needs_input|refused","answer":"回答或待执行说明"}。
call 时 stop_reason=continue，answer 时为 done，clarify 时为 needs_input，refuse 时为 refused。
即使候选不充分，也不要调用 allowed_tools 以外的工具。"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Lab 02 多工具金标：候选检索、模型计划和预测评分")
    parser.add_argument("mode", choices=["validate", "candidates", "model", "score"])
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument(
        "--strategy", choices=["full_catalog", "namespace", "retrieval"], default="retrieval"
    )
    parser.add_argument("--max-tools", type=int, default=None)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model", default="")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    manifest, cases = load_dataset(args.data)
    frozen_hash = dataset_hash(args.data)
    if args.mode == "validate":
        print(
            json.dumps(
                {
                    "status": "ok",
                    "case_count": len(cases),
                    "round_count": sum(len(c["rounds"]) for c in cases),
                    "dataset_hash": frozen_hash,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.mode == "score" and not args.predictions:
        parser.error("score 需要 --predictions")
    output = args.output_dir or ROOT / "var/reports/lab02-multi-tool-gold" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    )
    output.mkdir(parents=True, exist_ok=False)
    limit = (
        args.max_tools
        if args.max_tools is not None
        else (5 if args.strategy == "retrieval" else 64)
    )
    model = None
    if args.mode == "score":
        rows = read_jsonl(args.predictions)
    else:
        candidates = candidate_rows(manifest, cases, args.strategy, limit)
        write_jsonl(output / "candidates.jsonl", candidates)
        rows = candidates
        if args.mode == "model":
            settings = Settings()
            if not settings.glm_api_key:
                parser.error("缺少 GLM_API_KEY；可先运行 candidates，或导入 Dify predictions 评分")
            model = args.model or settings.glm_model
            client = OpenAI(
                api_key=settings.glm_api_key,
                base_url=settings.glm_base_url,
                timeout=90,
                max_retries=0,
            )

            def predict(row: dict) -> dict:
                prediction = {k: row[k] for k in ["case_id", "round_id", "candidate_names"]}
                prediction.update(
                    allowed_tools=row["candidate_names"], source="model", run_id=uuid4().hex
                )
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": SYSTEM},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {**row["request"], "allowed_tools": row["selected_tools"]},
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                        response_format={"type": "json_object"},
                    )
                    value = json.loads(response.choices[0].message.content or "")
                    # 模型只能填写计划字段，不能覆盖来源、候选或 run_id。
                    prediction.update(
                        {k: value[k] for k in ["action", "calls", "stop_reason", "answer"]}
                    )
                    from training_eval.multi_tool_gold import Prediction

                    Prediction.model_validate(prediction)
                    prediction["run_id"] = response.id or prediction["run_id"]
                except Exception as exc:
                    prediction.update(
                        action="refuse",
                        calls=[],
                        stop_reason="refused",
                        answer="",
                        error=type(exc).__name__,
                    )
                print(
                    f"{row['case_id']}/{row['round_id']}: {prediction['action']}"
                    + (" (error)" if prediction.get("error") else ""),
                    flush=True,
                )
                return prediction

            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                rows = list(executor.map(predict, candidates))
            write_jsonl(output / "predictions.jsonl", rows)
    report = score(manifest, cases, rows, agent=args.mode in ["model", "score"])
    report["manifest"] = {
        **manifest,
        "dataset_hash": frozen_hash,
        "prompt_hash": hashlib.sha256(SYSTEM.encode()).hexdigest()
        if args.mode == "model"
        else None,
        "sampling": "provider_default" if args.mode == "model" else None,
        "strategy": args.strategy if args.mode != "score" else "imported",
        "max_tools": limit if args.mode != "score" else None,
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ["details", "manifest"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"报告：{output / 'report.json'}")
    # 2 表示评测不达标，与输入损坏/程序错误分开。
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
