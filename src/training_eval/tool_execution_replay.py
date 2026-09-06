from __future__ import annotations

import hashlib
import json
import time
import uuid
from statistics import median
from typing import Any

import httpx


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return (
        f"{base}/tools:execute-batch"
        if base.endswith("/v1")
        else f"{base}/v1/tools:execute-batch"
    )


def _execution_mode(requested: str, category: str) -> str:
    if requested != "auto":
        return requested
    return "parallel" if category == "parallel" else "sequential"


def _plan_calls(plan: dict[str, Any]) -> list[dict[str, Any]]:
    value = (
        plan.get("current_round_calls")
        if "current_round_calls" in plan
        else plan.get("calls")
    )
    if not isinstance(value, list) or not value:
        raise ValueError(f"{plan.get('case_id')}: 固定回放计划缺少当前轮 calls")
    return value


def _plan_hash(calls: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(
            calls,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _median(values: list[float]) -> float:
    return round(float(median(values)), 2)


def replay_predictions(
    cases: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    *,
    client: httpx.Client,
    base_url: str,
    api_key: str,
    tenant_id: str,
    execution_mode: str,
    response_mode: str,
    repeats: int,
) -> list[dict[str, Any]]:
    if repeats < 1:
        raise ValueError("repeats 必须大于等于 1")
    plan_by_case = {str(plan.get("case_id")): plan for plan in plans}
    missing = [case["case_id"] for case in cases if case["case_id"] not in plan_by_case]
    if missing:
        raise ValueError(f"固定回放计划缺少 case_id：{missing}")

    predictions: list[dict[str, Any]] = []
    for case in cases:
        case_id = case["case_id"]
        if case.get("allow_side_effects"):
            raise ValueError(f"{case_id}: 固定性能回放只允许无副作用样本")
        plan = plan_by_case[case_id]
        calls = _plan_calls(plan)
        actual_execution_mode = _execution_mode(execution_mode, case["category"])
        samples: list[dict[str, Any]] = []

        for repeat_index in range(1, repeats + 1):
            nonce = uuid.uuid4().hex
            correlation_id = f"cp02-replay-{case_id.lower()}-{repeat_index}-{nonce[:8]}"
            started = time.perf_counter()
            response = client.post(
                _endpoint(base_url),
                headers={
                    "X-API-Key": api_key,
                    "X-Tenant-ID": tenant_id,
                    "X-User-ID": case.get("authenticated_user_id") or "cp02-tool-replay",
                    "X-Correlation-ID": correlation_id,
                    "X-Permissions": ",".join(case["available_permissions"]),
                    "Idempotency-Key": f"{correlation_id}-{nonce[8:20]}",
                    "Content-Type": "application/json",
                },
                json={
                    "calls": calls,
                    "approval_id": case.get("approval_id"),
                    "current_itinerary_id": case.get("current_itinerary_id"),
                    "execution_mode": actual_execution_mode,
                    "response_mode": response_mode,
                },
            )
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"{case_id}: Travel Core HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )
            body = response.json()
            if body.get("status") != "ok":
                raise RuntimeError(f"{case_id}: Travel Core 执行失败：{body}")
            samples.append(
                {
                    "repeat": repeat_index,
                    "latency_ms": latency_ms,
                    "tool_latency_ms": float(body.get("elapsed_ms") or 0),
                    "tool_result_chars": int(body.get("result_chars") or 0),
                    "tool_result_contract_valid": bool(
                        body.get("result_contract_valid")
                    ),
                    "correlation_id": body.get("correlation_id") or correlation_id,
                }
            )

        prediction = {
            "case_id": case_id,
            "action": plan.get("action") or "call",
            "candidate_tools": plan.get("candidate_tools") or [],
            "calls": calls,
            "current_round_calls": calls,
            "latency_ms": _median([sample["latency_ms"] for sample in samples]),
            "total_tokens": 0,
            "source_total_tokens": int(plan.get("total_tokens") or 0),
            "candidate_schema_chars": int(plan.get("candidate_schema_chars") or 0),
            "source_workflow_run_id": plan.get("workflow_run_id") or "",
            "evidence_level": "travel_core_execution_replay",
            "plan_hash": _plan_hash(calls),
            "tool_execution_mode": actual_execution_mode,
            "tool_response_mode": response_mode,
            "tool_latency_ms": _median(
                [sample["tool_latency_ms"] for sample in samples]
            ),
            "tool_result_chars": _median(
                [sample["tool_result_chars"] for sample in samples]
            ),
            "tool_result_contract_valid": all(
                sample["tool_result_contract_valid"] for sample in samples
            ),
            "benchmark_repeats": samples,
        }
        predictions.append(prediction)

    return predictions
