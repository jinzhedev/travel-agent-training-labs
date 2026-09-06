from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import FailureInjection
from .security import RequestContext


def consume_failure_once(db: Session, ctx: RequestContext, stage: str) -> bool:
    """Return True exactly once per tenant/correlation/stage, across API replicas."""

    failure_key = f"{ctx.correlation_id}:{stage}"
    existing = db.scalar(
        select(FailureInjection).where(
            FailureInjection.tenant_id == ctx.tenant_id,
            FailureInjection.failure_key == failure_key,
        )
    )
    if existing:
        return False
    db.add(
        FailureInjection(
            id=str(uuid.uuid4()),
            tenant_id=ctx.tenant_id,
            failure_key=failure_key,
        )
    )
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
