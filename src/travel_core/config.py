from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration.

    SQLite is intentionally the zero-dependency default for local training.
    The Compose/Kubernetes examples switch DATABASE_URL to PostgreSQL.
    """

    app_name: str = "travel-core"
    environment: str = "training"
    database_url: str = "sqlite:///./var/travel_core.db"
    api_key: str = "change-me"
    idempotency_ttl_seconds: int = 86_400
    fixture_city: str = "厦门"
    fixture_path: str = str(PROJECT_ROOT / "datasets/scenario/xiamen/v1/pois.json")
    tool_catalog_path: str = str(PROJECT_ROOT / "datasets/tools/catalog-v1/catalog.json")
    rag_text_corpus_path: str = str(
        PROJECT_ROOT / "datasets/rag/document-ai/v1/parsed/text-only.jsonl"
    )
    rag_object_corpus_path: str = str(
        PROJECT_ROOT / "datasets/rag/document-ai/v1/parsed/object-aware.jsonl"
    )
    rag_plan_path: str = str(PROJECT_ROOT / "datasets/rag/multihop/v1/plans.jsonl")
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
