from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as datetime_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "datasets/rag/document-ai/v1"
TEXT_JSONL = DATA_ROOT / "parsed/text-only.jsonl"
OBJECT_JSONL = DATA_ROOT / "parsed/object-aware.jsonl"
TEXT_IMPORT_DIR = DATA_ROOT / "dify-import/text-only"
OBJECT_IMPORT_DIR = DATA_ROOT / "dify-import/object-aware"

COMMON_METADATA_TYPES = {
    "revision": "string",
    "is_simulated": "string",
    "valid_until": "time",
    "document_id": "string",
    "page": "number",
}
OBJECT_METADATA_TYPES = {**COMMON_METADATA_TYPES, "object_type": "string"}


class DifySettings(BaseSettings):
    dify_base_url: str = ""
    dify_kb_api_key: str = ""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExpectedDocument:
    path: Path
    metadata: dict[str, str | int | float]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _date_to_timestamp(value: str) -> int:
    parsed = date.fromisoformat(value)
    local_midnight = datetime.combine(
        parsed,
        datetime_time.min,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    return int(local_midnight.timestamp())


def _metadata_values(item: dict[str, Any], *, include_object_type: bool) -> dict[str, str | int]:
    valid_until = item.get("valid_until")
    if not valid_until:
        raise SyncError(f"{item.get('document_id')}: valid_until 不能为空")
    is_simulated = item.get("is_simulated")
    if not isinstance(is_simulated, bool):
        raise SyncError(f"{item.get('document_id')}: is_simulated 必须是布尔值")
    values: dict[str, str | int] = {
        "revision": str(item["revision"]),
        "is_simulated": str(is_simulated).lower(),
        "valid_until": _date_to_timestamp(str(valid_until)),
        "document_id": str(item["document_id"]),
        "page": int(item["page"]),
    }
    if include_object_type:
        values["object_type"] = str(item["object_type"])
    return values


def _expected_text_documents() -> list[ExpectedDocument]:
    documents = [
        ExpectedDocument(
            path=TEXT_IMPORT_DIR / f"{item['chunk_id']}.md",
            metadata=_metadata_values(item, include_object_type=False),
        )
        for item in _read_jsonl(TEXT_JSONL)
    ]
    return _validate_expected_documents(documents, expected_count=7, label="整页文本")


def _expected_object_documents() -> list[ExpectedDocument]:
    documents = [
        ExpectedDocument(
            path=OBJECT_IMPORT_DIR / f"{item['object_id']}.md",
            metadata=_metadata_values(item, include_object_type=True),
        )
        for item in _read_jsonl(OBJECT_JSONL)
    ]
    return _validate_expected_documents(documents, expected_count=14, label="对象级")


def _validate_expected_documents(
    documents: list[ExpectedDocument], *, expected_count: int, label: str
) -> list[ExpectedDocument]:
    if len(documents) != expected_count:
        raise SyncError(f"{label}权威 JSONL 应有 {expected_count} 条，实际为 {len(documents)} 条")
    names = [document.path.name for document in documents]
    if len(names) != len(set(names)):
        raise SyncError(f"{label}导入文件名不唯一")
    missing = [str(document.path) for document in documents if not document.path.is_file()]
    if missing:
        raise SyncError(f"{label}导入文件缺失：{missing}")
    return documents


def _api_base_url(value: str) -> str:
    base = value.strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/v1") else f"{base}/v1"


class DifyKnowledgeClient:
    def __init__(self, *, base_url: str, api_key: str, timeout: float) -> None:
        self.base_url = _api_base_url(base_url)
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def __enter__(self) -> DifyKnowledgeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.client.request(method, f"{self.base_url}/{path.lstrip('/')}", **kwargs)
        except httpx.HTTPError as exc:
            raise SyncError(f"Dify 请求失败：{method} {path}: {exc}") from exc
        if response.status_code >= 400:
            try:
                body = response.json()
                detail = (
                    body.get("message") or body.get("code") or json.dumps(body, ensure_ascii=False)
                )
            except (ValueError, AttributeError):
                detail = response.text[:500]
            raise SyncError(f"Dify API HTTP {response.status_code}: {method} {path}: {detail}")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise SyncError(f"Dify API 返回了非 JSON 响应：{method} {path}") from exc

    def find_dataset(self, name: str) -> dict[str, Any] | None:
        page = 1
        exact_matches: list[dict[str, Any]] = []
        while True:
            body = self.request(
                "GET",
                "datasets",
                params={"keyword": name, "page": page, "limit": 100},
            )
            exact_matches.extend(item for item in body.get("data", []) if item.get("name") == name)
            if not body.get("has_more"):
                break
            page += 1
        if len(exact_matches) > 1:
            raise SyncError(f"发现多个同名知识库 {name}，无法安全选择")
        return exact_matches[0] if exact_matches else None

    def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        return self.request("GET", f"datasets/{dataset_id}")

    def create_dataset(
        self,
        *,
        name: str,
        embedding_model: str,
        embedding_provider: str,
        permission: str,
        retrieval_model: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "indexing_technique": "high_quality",
            "permission": permission,
            "provider": "vendor",
            "embedding_model": embedding_model,
            "embedding_model_provider": embedding_provider,
        }
        if retrieval_model:
            payload["retrieval_model"] = retrieval_model
        return self.request("POST", "datasets", json=payload)

    def resolve_embedding_model(
        self,
        requested_model: str,
        *,
        preferred_provider: str | None,
    ) -> tuple[str, str]:
        body = self.request("GET", "workspaces/current/models/model-types/text-embedding")
        matches: list[tuple[str, str, str]] = []
        for provider in body.get("data", []):
            provider_id = str(provider.get("provider") or "")
            status = str(provider.get("status") or "")
            for model in provider.get("models") or []:
                model_id = str(model.get("model") or "")
                if model_id.casefold() == requested_model.casefold():
                    matches.append((model_id, provider_id, status))
        if preferred_provider:
            preferred = [item for item in matches if item[1] == preferred_provider]
            if len(preferred) == 1:
                return preferred[0][0], preferred[0][1]
        active = [item for item in matches if item[2] == "active"]
        candidates = active or matches
        if not candidates:
            raise SyncError(f"Dify 当前没有可用的 embedding 模型 {requested_model}")
        if len(candidates) > 1:
            providers = sorted({item[1] for item in candidates})
            raise SyncError(f"模型 {requested_model} 对应多个 provider，请明确选择：{providers}")
        return candidates[0][0], candidates[0][1]

    def list_documents(self, dataset_id: str) -> list[dict[str, Any]]:
        page = 1
        documents: list[dict[str, Any]] = []
        while True:
            body = self.request(
                "GET",
                f"datasets/{dataset_id}/documents",
                params={"page": page, "limit": 100},
            )
            documents.extend(body.get("data", []))
            if not body.get("has_more"):
                break
            page += 1
        return documents

    def ensure_metadata_fields(
        self,
        dataset_id: str,
        expected_types: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        body = self.request("GET", f"datasets/{dataset_id}/metadata")
        fields = {str(item["name"]): item for item in body.get("doc_metadata", [])}
        for name, metadata_type in expected_types.items():
            existing = fields.get(name)
            if existing:
                if existing.get("type") != metadata_type:
                    raise SyncError(
                        f"metadata {name} 类型应为 {metadata_type}，实际为 {existing.get('type')}"
                    )
                continue
            created = self.request(
                "POST",
                f"datasets/{dataset_id}/metadata",
                json={"name": name, "type": metadata_type},
            )
            fields[name] = created
            print(f"  已创建 metadata 字段：{name} ({metadata_type})", flush=True)
        return {name: fields[name] for name in expected_types}

    def upload_document(
        self,
        dataset_id: str,
        document: ExpectedDocument,
        *,
        embedding_model: str,
        embedding_provider: str,
        retrieval_model: dict[str, Any] | None,
    ) -> str:
        process_rule = {
            "mode": "hierarchical",
            "rules": {
                "pre_processing_rules": [
                    {"id": "remove_extra_spaces", "enabled": True},
                    {"id": "remove_urls_emails", "enabled": False},
                ],
                "segmentation": {"separator": "\n\n", "max_tokens": 1024},
                "parent_mode": "full-doc",
                "subchunk_segmentation": {"separator": "\n", "max_tokens": 256},
            },
        }
        config: dict[str, Any] = {
            "indexing_technique": "high_quality",
            "doc_form": "hierarchical_model",
            "doc_language": "Chinese",
            "process_rule": process_rule,
            "embedding_model": embedding_model,
            "embedding_model_provider": embedding_provider,
        }
        if retrieval_model:
            config["retrieval_model"] = retrieval_model
        with document.path.open("rb") as file_handle:
            body = self.request(
                "POST",
                f"datasets/{dataset_id}/document/create-by-file",
                files={
                    "file": (document.path.name, file_handle, "text/markdown"),
                    "data": (None, json.dumps(config, ensure_ascii=False), "text/plain"),
                },
            )
        batch = str(body.get("batch") or "")
        if not batch:
            raise SyncError(f"上传 {document.path.name} 后未取得 batch ID")
        return batch

    def wait_for_batches(
        self,
        dataset_id: str,
        batches: dict[str, str],
        *,
        poll_timeout: float,
    ) -> None:
        pending = dict(batches)
        deadline = time.monotonic() + poll_timeout
        while pending:
            if time.monotonic() >= deadline:
                raise SyncError(f"等待 Dify 索引超时，仍未完成：{sorted(pending)}")
            for document_name, batch in list(pending.items()):
                body = self.request(
                    "GET",
                    f"datasets/{dataset_id}/documents/{batch}/indexing-status",
                )
                entries = body.get("data") or []
                statuses = {
                    str(item.get("indexing_status") or item.get("status") or "") for item in entries
                }
                errors = [item.get("error") for item in entries if item.get("error")]
                if errors or statuses.intersection({"error", "failed"}):
                    raise SyncError(f"{document_name} 索引失败：{errors or sorted(statuses)}")
                if entries and statuses.issubset({"completed"}):
                    pending.pop(document_name)
                    print(f"  索引完成：{document_name}", flush=True)
            if pending:
                time.sleep(2)

    def update_document_metadata(
        self,
        dataset_id: str,
        documents: list[ExpectedDocument],
        dify_documents: dict[str, dict[str, Any]],
        fields: dict[str, dict[str, Any]],
    ) -> None:
        operation_data = []
        for expected in documents:
            dify_document = dify_documents[expected.path.name]
            metadata_list = [
                {
                    "id": fields[name]["id"],
                    "name": name,
                    "value": value,
                }
                for name, value in expected.metadata.items()
            ]
            operation_data.append(
                {
                    "document_id": dify_document["id"],
                    "metadata_list": metadata_list,
                    "partial_update": True,
                }
            )
        self.request(
            "POST",
            f"datasets/{dataset_id}/documents/metadata",
            json={"operation_data": operation_data},
        )

    def verify_hierarchical_documents(
        self,
        dataset_id: str,
        documents: dict[str, dict[str, Any]],
    ) -> None:
        for name, document in sorted(documents.items()):
            if document.get("doc_form") != "hierarchical_model":
                raise SyncError(f"{name} 未使用 Parent-child：doc_form={document.get('doc_form')}")
            segments = (
                self.request(
                    "GET",
                    f"datasets/{dataset_id}/documents/{document['id']}/segments",
                    params={"page": 1, "limit": 100},
                ).get("data")
                or []
            )
            if not segments:
                raise SyncError(f"{name} 没有生成父块")
            child_count = 0
            for segment in segments:
                body = self.request(
                    "GET",
                    f"datasets/{dataset_id}/documents/{document['id']}/segments/{segment['id']}/child_chunks",
                    params={"page": 1, "limit": 100},
                )
                child_count += int(body.get("total") or len(body.get("data") or []))
            if child_count < 1:
                raise SyncError(f"{name} 没有生成子块")


def _dataset_documents_by_name(
    documents: list[dict[str, Any]],
    expected: list[ExpectedDocument],
    *,
    dataset_name: str,
    allow_missing: bool,
) -> dict[str, dict[str, Any]]:
    expected_names = {document.path.name for document in expected}
    actual = {str(document["name"]): document for document in documents}
    unexpected = sorted(set(actual) - expected_names)
    if unexpected:
        raise SyncError(f"知识库 {dataset_name} 含有非预期文档：{unexpected}")
    missing = sorted(expected_names - set(actual))
    if missing and not allow_missing:
        raise SyncError(f"知识库 {dataset_name} 缺少文档：{missing}")
    return actual


def _assert_embedding(dataset: dict[str, Any], model: str, provider: str) -> None:
    actual_model = str(dataset.get("embedding_model") or "")
    actual_provider = str(dataset.get("embedding_model_provider") or "")
    if actual_model.casefold() != model.casefold() or actual_provider != provider:
        raise SyncError(
            f"知识库 {dataset.get('name')} 的 embedding 为 {actual_model} / {actual_provider}，"
            f"预期为 {model} / {provider}"
        )


def _metadata_map(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("doc_metadata") or []
    if isinstance(metadata, dict):
        return metadata
    return {str(item.get("name")): item.get("value") for item in metadata}


def _metadata_equal(expected: str | int | float, actual: Any) -> bool:
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return float(expected) == float(actual)
    return expected == actual


def _verify_metadata(
    actual_documents: list[dict[str, Any]],
    expected_documents: list[ExpectedDocument],
    *,
    dataset_name: str,
) -> None:
    actual_by_name = {str(item["name"]): item for item in actual_documents}
    failures: list[str] = []
    for expected in expected_documents:
        actual = _metadata_map(actual_by_name[expected.path.name])
        for name, value in expected.metadata.items():
            if not _metadata_equal(value, actual.get(name)):
                failures.append(
                    f"{expected.path.name}.{name}: expected={value!r}, actual={actual.get(name)!r}"
                )
    if failures:
        raise SyncError(f"知识库 {dataset_name} metadata 验证失败：\n" + "\n".join(failures))


def _sync_text_dataset(
    client: DifyKnowledgeClient,
    dataset: dict[str, Any],
    documents: list[ExpectedDocument],
    *,
    embedding_model: str,
    embedding_provider: str,
) -> None:
    dataset_id = str(dataset["id"])
    _assert_embedding(dataset, embedding_model, embedding_provider)
    actual = client.list_documents(dataset_id)
    by_name = _dataset_documents_by_name(
        actual,
        documents,
        dataset_name=str(dataset["name"]),
        allow_missing=False,
    )
    fields = client.ensure_metadata_fields(dataset_id, COMMON_METADATA_TYPES)
    client.update_document_metadata(dataset_id, documents, by_name, fields)
    _verify_metadata(
        client.list_documents(dataset_id),
        documents,
        dataset_name=str(dataset["name"]),
    )
    print(f"已更新并验证 {dataset['name']}：{len(documents)} 份文档", flush=True)


def _sync_object_dataset(
    client: DifyKnowledgeClient,
    text_dataset: dict[str, Any],
    object_dataset: dict[str, Any] | None,
    documents: list[ExpectedDocument],
    *,
    dataset_name: str,
    embedding_model: str,
    embedding_provider: str,
    poll_timeout: float,
) -> dict[str, Any]:
    retrieval_model = text_dataset.get("retrieval_model_dict")
    if object_dataset is None:
        object_dataset = client.create_dataset(
            name=dataset_name,
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            permission=str(text_dataset.get("permission") or "only_me"),
            retrieval_model=retrieval_model if isinstance(retrieval_model, dict) else None,
        )
        print(f"已创建知识库 {dataset_name}：{object_dataset['id']}", flush=True)
    dataset_id = str(object_dataset["id"])
    object_dataset = client.get_dataset(dataset_id)
    _assert_embedding(object_dataset, embedding_model, embedding_provider)

    existing = client.list_documents(dataset_id)
    by_name = _dataset_documents_by_name(
        existing,
        documents,
        dataset_name=dataset_name,
        allow_missing=True,
    )
    batches: dict[str, str] = {}
    for document in documents:
        if document.path.name in by_name:
            print(f"  已存在，跳过上传：{document.path.name}", flush=True)
            continue
        batches[document.path.name] = client.upload_document(
            dataset_id,
            document,
            embedding_model=embedding_model,
            embedding_provider=embedding_provider,
            retrieval_model=retrieval_model if isinstance(retrieval_model, dict) else None,
        )
        print(f"  已上传：{document.path.name}", flush=True)
    if batches:
        client.wait_for_batches(dataset_id, batches, poll_timeout=poll_timeout)

    refreshed = client.list_documents(dataset_id)
    by_name = _dataset_documents_by_name(
        refreshed,
        documents,
        dataset_name=dataset_name,
        allow_missing=False,
    )
    client.verify_hierarchical_documents(dataset_id, by_name)
    fields = client.ensure_metadata_fields(dataset_id, OBJECT_METADATA_TYPES)
    client.update_document_metadata(dataset_id, documents, by_name, fields)
    _verify_metadata(client.list_documents(dataset_id), documents, dataset_name=dataset_name)
    print(f"已更新并验证 {dataset_name}：{len(documents)} 份 Parent-child 文档", flush=True)
    return client.get_dataset(dataset_id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="同步 CP03 整页文本与对象级 Dify 知识库，并批量写入 metadata"
    )
    parser.add_argument("--text-dataset", default="XM-Guide-Text")
    parser.add_argument("--object-dataset", default="XM-Guide-Object")
    parser.add_argument("--embedding-model", default="baai/bge-m3")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--poll-timeout", type=float, default=600)
    args = parser.parse_args()

    settings = DifySettings()
    base_url = settings.dify_base_url.strip()
    api_key = settings.dify_kb_api_key.strip()
    if not base_url or not api_key:
        raise SystemExit("请先在 .env 设置 DIFY_BASE_URL 和 DIFY_KB_API_KEY")

    text_documents = _expected_text_documents()
    object_documents = _expected_object_documents()
    try:
        with DifyKnowledgeClient(
            base_url=base_url, api_key=api_key, timeout=args.timeout
        ) as client:
            text_dataset = client.find_dataset(args.text_dataset)
            if text_dataset is None:
                raise SyncError(f"未找到已有知识库 {args.text_dataset}")
            text_dataset = client.get_dataset(str(text_dataset["id"]))
            requested_model = args.embedding_model.strip()
            current_model = str(text_dataset.get("embedding_model") or "")
            if current_model.casefold() != requested_model.casefold():
                raise SyncError(
                    f"{args.text_dataset} 使用 {current_model}，与要求的 {requested_model} 不一致"
                )
            embedding_model, embedding_provider = client.resolve_embedding_model(
                requested_model,
                preferred_provider=str(text_dataset.get("embedding_model_provider") or ""),
            )
            print(
                f"使用 embedding：{embedding_model} / {embedding_provider}",
                flush=True,
            )
            _sync_text_dataset(
                client,
                text_dataset,
                text_documents,
                embedding_model=embedding_model,
                embedding_provider=embedding_provider,
            )
            object_dataset = client.find_dataset(args.object_dataset)
            result = _sync_object_dataset(
                client,
                text_dataset,
                object_dataset,
                object_documents,
                dataset_name=args.object_dataset,
                embedding_model=embedding_model,
                embedding_provider=embedding_provider,
                poll_timeout=args.poll_timeout,
            )
            print(
                f"同步完成：text_dataset_id={text_dataset['id']}, object_dataset_id={result['id']}",
                flush=True,
            )
    except SyncError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
