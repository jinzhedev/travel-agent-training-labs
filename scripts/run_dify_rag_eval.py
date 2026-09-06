from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from training_eval.foundation import read_jsonl
from training_eval.rag import (
    DEFAULT_CASES,
    PLANS,
    score_rag_predictions,
    validate_rag_case_set,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports/local/dify-workflow-rag"


class DifySettings(BaseSettings):
    dify_base_url: str = ""
    dify_workflow_api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}/workflows/run" if base.endswith("/v1") else f"{base}/v1/workflows/run"


def _as_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * quantile + 0.999999)))
    return round(float(ordered[index]), 2)


def _report_path(prediction_path: Path) -> Path:
    suffix = ".predictions.jsonl"
    if prediction_path.name.endswith(suffix):
        return prediction_path.with_name(f"{prediction_path.name[: -len(suffix)]}.report.json")
    return prediction_path.with_suffix(".report.json")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 CP03 Dify Workflow 文档 RAG 评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument(
        "--response-mode",
        choices=("blocking", "streaming"),
        default="blocking",
        help="Dify Workflow API 响应模式",
    )
    parser.add_argument(
        "--plan-source",
        choices=("auto", "fixed"),
        default="auto",
        help="auto 留空 Plan 交给 Planner；fixed 使用 plans.jsonl 调试 Plan",
    )
    parser.add_argument("--run-name", default="")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser.parse_args()


def _iter_sse_events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    data_lines: list[str] = []

    def decode_event() -> dict[str, Any] | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        data_lines.clear()
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"无法解析 Dify SSE data：{raw[:200]}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"Dify SSE event 应为对象，当前为 {type(event).__name__}")
        return event

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            event = decode_event()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field != "data" or not separator:
            continue
        data_lines.append(value[1:] if value.startswith(" ") else value)

    event = decode_event()
    if event is not None:
        yield event


def _workflow_inputs(
    case: dict[str, Any],
    plan: dict[str, Any],
    plan_source: str,
) -> dict[str, Any]:
    plan_json = ""
    if plan_source == "fixed" and case["mode"] == "multihop":
        plan_json = json.dumps(plan.get("slots") or [], ensure_ascii=False)
    return {
        "user_request": case["query"],
        "trip_id": "",
        "base_version_id": "",
        "revision_notes": "",
        "classroom_stage": "document_rag",
        "quality_profile": "balanced",
        "failure_mode": "none",
        "tool_strategy": "retrieval",
        "max_tools": 5,
        "tool_permissions": "travel.read",
        "allow_side_effects": "false",
        "approval_id": "",
        "tool_execution_mode": "sequential",
        "tool_response_mode": "concise",
        "rag_mode": case["mode"],
        "rag_evidence_plan_json": plan_json,
        "rag_backend_dify_loop": True,
    }


def _request_blocking(
    client: httpx.Client,
    *,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    response = client.post(endpoint, headers=headers, json=payload)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
    return response.json(), {
        "end_to_end_latency_ms": elapsed_ms,
        "first_event_ms": None,
        "ttft_ms": None,
        "ttft_status": "not_observed_by_blocking_workflow_api",
        "stream_event_counts": {},
    }


def _request_streaming(
    client: httpx.Client,
    *,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    first_event_ms: float | None = None
    ttft_ms: float | None = None
    finished_ms: float | None = None
    final_event: dict[str, Any] | None = None
    event_counts: Counter[str] = Counter()

    with client.stream("POST", endpoint, headers=headers, json=payload) as response:
        if response.status_code >= 400:
            detail = response.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {response.status_code}: {detail[:300]}")
        for event in _iter_sse_events(response.iter_lines()):
            event_type = str(event.get("event") or "unknown")
            event_counts[event_type] += 1
            now_ms = round((time.perf_counter() - started) * 1000, 2)
            if first_event_ms is None and event_type != "ping":
                first_event_ms = now_ms
            data = event.get("data") or {}
            if (
                ttft_ms is None
                and event_type == "text_chunk"
                and isinstance(data, dict)
                and str(data.get("text") or "")
            ):
                ttft_ms = now_ms
            if event_type == "workflow_finished":
                final_event = event
                finished_ms = now_ms
                break

    if final_event is None:
        raise RuntimeError(f"SSE 未收到 workflow_finished；events={dict(event_counts)}")
    data = final_event.get("data") or {}
    body = {
        "workflow_run_id": final_event.get("workflow_run_id") or data.get("id") or "",
        "task_id": final_event.get("task_id") or "",
        "data": data,
    }
    return body, {
        "end_to_end_latency_ms": finished_ms,
        "first_event_ms": first_event_ms,
        "ttft_ms": ttft_ms,
        "ttft_status": "observed" if ttft_ms is not None else "not_observed_no_text_chunk",
        "stream_event_counts": dict(event_counts),
    }


def _transport_summary(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    end_to_end = [float(item["end_to_end_latency_ms"]) for item in predictions]
    first_events = [
        float(item["first_event_ms"])
        for item in predictions
        if item.get("first_event_ms") is not None
    ]
    ttft = [float(item["ttft_ms"]) for item in predictions if item.get("ttft_ms") is not None]
    return {
        "case_count": len(predictions),
        "end_to_end_p50_ms": _percentile(end_to_end, 0.50),
        "end_to_end_p95_ms": _percentile(end_to_end, 0.95),
        "first_event_p50_ms": _percentile(first_events, 0.50),
        "first_event_p95_ms": _percentile(first_events, 0.95),
        "ttft_observed_count": len(ttft),
        "ttft_p50_ms": _percentile(ttft, 0.50),
        "ttft_p95_ms": _percentile(ttft, 0.95),
    }


def main() -> int:
    args = _parse_args()
    settings = DifySettings()
    base_url = settings.dify_base_url.strip()
    api_key = settings.dify_workflow_api_key.strip()
    if not base_url or not api_key:
        raise SystemExit("请先设置 DIFY_BASE_URL 和 DIFY_WORKFLOW_API_KEY")

    all_cases = read_jsonl(args.cases)
    requested = set(args.case_id)
    cases = [item for item in all_cases if not requested or item["case_id"] in requested]
    missing_requested = sorted(requested - {item["case_id"] for item in cases})
    if missing_requested:
        raise SystemExit(f"未知 case_id：{missing_requested}")
    plans = {item["plan_id"]: item for item in read_jsonl(PLANS)}
    predictions: list[dict[str, Any]] = []
    errors: list[str] = []
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    with httpx.Client(timeout=args.timeout) as client:
        for case in cases:
            plan = plans.get(case.get("evidence_plan_id")) or {}
            payload = {
                "inputs": _workflow_inputs(case, plan, args.plan_source),
                "response_mode": args.response_mode,
                "user": f"cp03-document-rag-eval-{args.response_mode}",
            }
            try:
                if args.response_mode == "streaming":
                    body, timing = _request_streaming(
                        client,
                        endpoint=_endpoint(base_url),
                        headers=headers,
                        payload=payload,
                    )
                else:
                    body, timing = _request_blocking(
                        client,
                        endpoint=_endpoint(base_url),
                        headers=headers,
                        payload=payload,
                    )
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                errors.append(f"{case['case_id']}: {exc}")
                continue

            data = body.get("data") or {}
            if data.get("status") != "succeeded":
                errors.append(
                    f"{case['case_id']}: workflow {data.get('status')}: "
                    f"{data.get('error') or 'unknown error'}"
                )
                continue
            outputs = data.get("outputs") or {}
            predictions.append(
                {
                    "case_id": case["case_id"],
                    "generation_status": "completed",
                    "answer": outputs.get("rag_answer") or "",
                    "citations": _as_json(outputs.get("rag_citations_json"), []),
                    "support_status": outputs.get("rag_support_status") or "insufficient",
                    "retrieved_evidence": _as_json(outputs.get("rag_retrieved_evidence_json"), []),
                    "trace": _as_json(outputs.get("rag_trace_json"), []),
                    "metrics": _as_json(outputs.get("rag_metrics_json"), {}),
                    **timing,
                    "total_tokens": int(data.get("total_tokens") or 0),
                    "workflow_run_id": body.get("workflow_run_id")
                    or data.get("workflow_run_id")
                    or data.get("id", ""),
                    "error": outputs.get("rag_error") or "",
                }
            )
            print(
                f"{case['case_id']}: succeeded, "
                f"e2e={timing['end_to_end_latency_ms']} ms, "
                f"first_event={timing['first_event_ms']}, ttft={timing['ttft_ms']}"
            )

    run_name = args.run_name or f"cp03-{args.plan_source}-{args.response_mode}"
    prediction_path = args.output or args.output_dir / f"{run_name}.predictions.jsonl"
    report_path = _report_path(prediction_path)
    prediction_path.parent.mkdir(parents=True, exist_ok=True)
    prediction_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in predictions) + "\n",
        encoding="utf-8",
    )

    selected_all_cases = not requested and len(cases) == len(all_cases)
    score = None
    if not errors and selected_all_cases:
        score = score_rag_predictions(prediction_path, args.cases)
    report = {
        "run_name": run_name,
        "backend": "dify_published_workflow_api",
        "response_mode": args.response_mode,
        "plan_source": args.plan_source,
        "dataset": validate_rag_case_set(args.cases),
        "requested_case_count": len(cases),
        "completed_case_count": len(predictions),
        "errors": errors,
        "transport": _transport_summary(predictions),
        "score": score,
        "prediction_path": str(prediction_path),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(report_path)
    if errors:
        raise SystemExit("Dify 运行未完成：\n" + "\n".join(errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
