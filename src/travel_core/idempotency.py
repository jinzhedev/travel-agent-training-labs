from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import IdempotencyRecord
from .security import RequestContext


@dataclass
class IdempotentResult:
    body: dict[str, Any]
    status_code: int
    replayed: bool


def canonical_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def execute_idempotent(
    db: Session,
    *,
    ctx: RequestContext,
    method: str,
    path: str,
    payload: Any,
    operation: Callable[[], tuple[dict[str, Any], int]],
) -> IdempotentResult:
    """Execute a database-only write atomically with its idempotency record.

    Same key + same payload replays the stored response. Same key + different
    payload is rejected. External provider side effects still require provider-side
    idempotency and an outbox/Saga; this helper deliberately does not pretend that a
    local transaction can cover a remote system.
    """

    key = ctx.idempotency_key
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    request_hash = canonical_hash(payload)
    existing = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == ctx.tenant_id,
            IdempotencyRecord.idempotency_key == key,
        )
    )
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(status_code=409, detail="idempotency key reused with a different request")
        if existing.state == "completed" and existing.response_json is not None:
            return IdempotentResult(existing.response_json, existing.status_code or 200, True)
        raise HTTPException(status_code=409, detail="request with this idempotency key is still in progress")

    record = IdempotencyRecord(
        id=str(uuid.uuid4()),
        tenant_id=ctx.tenant_id,
        idempotency_key=key,
        request_hash=request_hash,
        method=method.upper(),
        path=path,
        state="in_progress",
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == ctx.tenant_id,
                IdempotencyRecord.idempotency_key == key,
            )
        )
        if winner and winner.request_hash == request_hash and winner.state == "completed":
            return IdempotentResult(winner.response_json or {}, winner.status_code or 200, True)
        raise HTTPException(status_code=409, detail="concurrent request is already using this idempotency key")

    try:
        body, status_code = operation()
        record.state = "completed"
        record.status_code = status_code
        record.response_json = body
        db.commit()
        return IdempotentResult(body, status_code, False)
    except Exception:
        db.rollback()
        raise
