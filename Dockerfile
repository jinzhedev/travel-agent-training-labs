FROM ghcr.io/astral-sh/uv:0.11.29 AS uv

FROM python:3.12-slim-bookworm AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src

RUN uv sync --frozen --no-editable

FROM builder AS production-builder

RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    FIXTURE_PATH=/app/datasets/scenario/xiamen/v1/pois.json \
    TOOL_CATALOG_PATH=/app/datasets/tools/catalog-v1/catalog.json \
    RAG_TEXT_CORPUS_PATH=/app/datasets/rag/document-ai/v1/parsed/text-only.jsonl \
    RAG_OBJECT_CORPUS_PATH=/app/datasets/rag/document-ai/v1/parsed/object-aware.jsonl \
    RAG_PLAN_PATH=/app/datasets/rag/multihop/v1/plans.jsonl

RUN groupadd --gid 10001 travel-core \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin travel-core

WORKDIR /app

COPY --chown=10001:10001 datasets /app/datasets

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

STOPSIGNAL SIGTERM

CMD ["uvicorn", "travel_core.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "5"]

FROM runtime-base AS development

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

FROM runtime-base AS runtime

COPY --from=production-builder --chown=10001:10001 /app/.venv /app/.venv
