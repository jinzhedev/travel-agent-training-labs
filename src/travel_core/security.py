from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from .config import get_settings


@dataclass(frozen=True)
class RequestContext:
    tenant_id: str
    user_id: str
    correlation_id: str
    idempotency_key: str | None
    permissions: frozenset[str]


def request_context(
    x_api_key: str = Header(default="", alias="X-API-Key"),
    x_tenant_id: str = Header(default="training", alias="X-Tenant-ID"),
    x_user_id: str = Header(default="anonymous", alias="X-User-ID"),
    x_correlation_id: str = Header(default="unknown", alias="X-Correlation-ID"),
    x_permissions: str = Header(default="travel.read", alias="X-Permissions"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RequestContext:
    settings = get_settings()
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
    return RequestContext(
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        correlation_id=x_correlation_id,
        idempotency_key=idempotency_key,
        permissions=frozenset(item.strip() for item in x_permissions.split(",") if item.strip()),
    )
