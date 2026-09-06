from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "datasets/rag/document-ai/v1"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in value)


def _render_object(item: dict[str, Any]) -> str:
    details: list[str] = []
    if item.get("cells"):
        details.append("### 表格单元格\n\n" + "\n".join(
            f"- `{cell['cell_id']}`：{cell['value']}" for cell in item["cells"]
        ))
    if item.get("edges"):
        nodes = {node["id"]: node["label"] for node in item.get("nodes", [])}
        for edge in item["edges"]:
            for endpoint in (edge["from"], edge["to"]):
                if not nodes.get(endpoint):
                    raise ValueError(
                        f"{item['object_id']}: 流程边 {edge['edge_id']} 缺少节点 {endpoint} 的名称"
                    )
        details.append("### 流程边\n\n" + "\n".join(
            f"- `{edge['edge_id']}`：{edge['from']}（{nodes[edge['from']]}）"
            f" → {edge['to']}（{nodes[edge['to']]}）；条件：{edge.get('condition') or '无'}"
            for edge in item["edges"]
        ))
    if item.get("events"):
        details.append("### 时序事件\n\n" + "\n".join(
            f"- `{event['event_id']}`：{event['sender']} → {event['receiver']}；{event['message']}；条件：{event.get('condition') or '无'}"
            for event in item["events"]
        ))
    metadata = (
        f"- object_id: `{item['object_id']}`\n"
        f"- document_id: `{item['document_id']}`\n"
        f"- page: `{item['page']}`\n"
        f"- object_type: `{item['object_type']}`\n"
        f"- bbox: `{json.dumps(item.get('bbox'), ensure_ascii=False)}`\n"
        f"- fine_grained_ids: `{json.dumps(item.get('fine_grained_ids') or [], ensure_ascii=False)}`\n"
        f"- observed_at: `{item['observed_at']}`\n"
        f"- valid_until: `{item.get('valid_until')}`\n"
        f"- revision: `{item['revision']}`\n"
        f"- is_simulated: `{str(item.get('is_simulated', True)).lower()}`"
    )
    suffix = "\n\n" + "\n\n".join(details) if details else ""
    return f"# {item['title']}\n\n{metadata}\n\n## 对象内容\n\n{item['content']}{suffix}\n"


def export(dataset: Path = DATASET) -> None:
    target = dataset / "dify-import"
    object_dir = target / "object-aware"
    text_dir = target / "text-only"
    object_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)
    for item in _read_jsonl(dataset / "parsed/object-aware.jsonl"):
        (object_dir / f"{_safe_name(item['object_id'])}.md").write_text(
            _render_object(item), encoding="utf-8"
        )
    for item in _read_jsonl(dataset / "parsed/text-only.jsonl"):
        rendered = (
            f"# {item['title']}\n\n"
            f"- chunk_id: `{item['chunk_id']}`\n"
            f"- document_id: `{item['document_id']}`\n"
            f"- page: `{item['page']}`\n"
            f"- source: `{item['source']}`\n"
            f"- observed_at: `{item['observed_at']}`\n"
            f"- valid_until: `{item['valid_until']}`\n"
            f"- revision: `{item['revision']}`\n"
            f"- is_simulated: `true`\n\n"
            f"## 整页文本\n\n{item['content']}\n"
        )
        (text_dir / f"{_safe_name(item['chunk_id'])}.md").write_text(rendered, encoding="utf-8")
    print(f"exported {len(list(object_dir.glob('*.md')))} object files and {len(list(text_dir.glob('*.md')))} page files to {target}")


def main() -> int:
    export()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
