"""关联 Dify 原始 trace，并将自定义用量转换为 Phoenix 标准字段。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from phoenix.client.experiments import evaluate_experiment


def usage_spans(spans):
    """生成可幂等导入的统计 span；不是新增模型调用，不复制价格。"""
    result = []
    existing = {s["context"]["span_id"] for s in spans}
    for span in spans:
        if span["span_kind"] != "LLM":
            continue
        attrs = span.get("attributes", {})
        if any(k.startswith("llm.token_count.") for k in attrs):
            continue
        usage = {
            key: attrs.get(f"metadata.{key}_tokens")
            for key in ("prompt", "completion", "total")
        }
        if usage["prompt"] is None or usage["completion"] is None:
            raise ValueError("LLM span 缺少可验证的 token 用量")
        usage = {k: int(v) for k, v in usage.items() if v is not None}
        if any(v < 0 for v in usage.values()):
            raise ValueError("token 用量不能为负")
        source = span["context"]
        span_id = hashlib.sha256(
            f"{source['trace_id']}:{source['span_id']}:usage-v1".encode()
        ).hexdigest()[:16]
        if span_id in existing:
            continue
        result.append({
            "name": "Dify usage normalization (no model call)",
            "context": {"trace_id": source["trace_id"], "span_id": span_id},
            "parent_id": source["span_id"],
            "span_kind": "LLM",
            "start_time": span["end_time"],
            "end_time": span["end_time"],
            "status_code": "OK",
            "attributes": {
                "openinference.span.kind": "LLM",
                "llm.model_name": attrs["metadata.model_name"],
                "metadata.usage_source_span_id": source["span_id"],
                "metadata.usage_normalization": "dify-custom-to-openinference-v1",
                "metadata.is_model_call": False,
                **{f"llm.token_count.{k}": v for k, v in usage.items()},
            },
        })
    return result


def find_trace(client, project, output, timeout=60):
    conversation = output.get("conversation_id")
    if not conversation:
        raise ValueError("API 输出缺少 conversation_id，不能可靠关联 trace")
    deadline = time.monotonic() + timeout
    while True:
        spans = client.spans.get_spans(project_identifier=project, limit=1000)
        roots = [s for s in spans if s["name"].startswith("chatflow_")
                 and s.get("attributes", {}).get("session.id") == conversation]
        if len(roots) > 1:
            raise ValueError("同一新会话存在多条 chatflow trace，停止自动匹配")
        if roots:
            root = roots[0]
            trace_id = root["context"]["trace_id"]
            full = client.spans.get_spans(
                project_identifier=project, trace_ids=[trace_id], limit=1000
            )
            if any(s["span_kind"] == "LLM" for s in full):
                return root, full
        if time.monotonic() >= deadline:
            raise TimeoutError("等待 Dify trace 超时；API 输出已保存，请勿盲目重跑")
        time.sleep(2)


def run_linked_experiment(*, client, dataset, task, evaluators, project,
                          experiment_name, experiment_description,
                          experiment_metadata, repetitions=1, timeout=120,
                          resume_experiment_id="", phoenix_endpoint=""):
    if resume_experiment_id:
        previous = client.experiments.get(experiment_id=resume_experiment_id)
        if (previous["dataset_id"] != dataset.id
                or previous["dataset_version_id"] != dataset.version_id
                or previous["repetitions"] != repetitions):
            raise ValueError("恢复实验必须使用相同 dataset、版本与 repetitions")
    experiment = {"id": resume_experiment_id} if resume_experiment_id else client.experiments.create(
        dataset_id=dataset.id, dataset_version_id=dataset.version_id,
        experiment_name=experiment_name,
        experiment_description=experiment_description,
        experiment_metadata={**experiment_metadata, "trace_link": "original_dify",
                             "usage_normalization": "token_only_no_model_call"},
        repetitions=repetitions,
    )
    experiment_id = experiment["id"]
    print(f"Linked experiment: {experiment_id}", flush=True)
    backup = Path("reports/local/phoenix-backups")
    backup.mkdir(parents=True, exist_ok=True)
    existing_runs = client.experiments.get_experiment(experiment_id=experiment_id)["task_runs"]
    for example in dataset.examples:
        for repetition in range(1, repetitions + 1):
            if any(r["dataset_example_id"] == example["id"]
                   and r["repetition_number"] == repetition for r in existing_runs):
                continue
            start = datetime.now(timezone.utc)
            output = task(example)
            end = datetime.now(timezone.utc)
            record = {"experiment_id": experiment_id, "example_id": example["id"],
                      "repetition": repetition, "output": output,
                      "start_time": start.isoformat(), "end_time": end.isoformat()}
            path = backup / f"linked-{experiment_id}-{example['id']}-{repetition}.json"
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            root, spans = find_trace(client, project, output)
            expected_usage = {
                k: sum(int(s.get("attributes", {}).get(f"metadata.{k}_tokens", 0))
                       for s in spans if s["span_kind"] == "LLM"
                       and not s.get("attributes", {}).get("metadata.usage_normalization"))
                for k in ("prompt", "completion")
            }
            normalized = usage_spans(spans)
            if normalized:
                client.spans.log_spans(project_identifier=project, spans=normalized)
            trace_id = root["context"]["trace_id"]
            output["dify_trace_id"] = trace_id
            record["trace_id"] = trace_id
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            run = client.experiments.log_run(
                experiment_id=experiment_id, dataset_example_id=example["id"],
                repetition_number=repetition, output=output,
                start_time=start, end_time=end, trace_id=trace_id,
            )
            record["run_id"] = run["id"]
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            print(json.dumps({"example": example["id"], "trace_id": trace_id,
                              "normalized_spans": len(normalized)}, ensure_ascii=False),
                  flush=True)
            deadline = time.monotonic() + 30
            while True:
                query = ('{node(id:' + json.dumps(run["id"]) +
                         '){... on ExperimentRun {costSummary{prompt{tokens cost}'
                         ' completion{tokens cost} total{tokens cost}}}}}')
                response = subprocess.run(
                    ["px", "api", "graphql", query, "--endpoint", phoenix_endpoint],
                    check=True, capture_output=True, text=True, timeout=15,
                )
                metrics = json.loads(response.stdout)["data"]["node"]["costSummary"]
                if metrics["prompt"]["tokens"] is not None:
                    if any(metrics[k]["tokens"] != expected_usage[k] for k in expected_usage):
                        raise RuntimeError("Phoenix 用量与 Dify 原始用量不一致，停止后续调用")
                    print(json.dumps({"verified_usage": metrics}), flush=True)
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("关联已保存，但 Phoenix 未汇总 token；停止后续模型调用")
                time.sleep(2)
    return evaluate_experiment(
        client=client, experiment=client.experiments.get_experiment(experiment_id=experiment_id),
        evaluators=evaluators, retries=0, timeout=timeout,
    )
