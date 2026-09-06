from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response

LOGGER = logging.getLogger("travel_core.access")
LOGGER.setLevel(logging.INFO)


def _route(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", None) or request.url.path)


def _record(
    request: Request,
    *,
    request_id: str,
    correlation_id: str,
    status_code: int,
    duration_ms: float,
    error_type: str | None = None,
) -> dict[str, object]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "http_request_completed",
        "service": "travel-core",
        "request_id": request_id,
        "correlation_id": correlation_id,
        "tenant_id": request.headers.get("X-Tenant-ID", ""),
        "user_id": request.headers.get("X-User-ID", ""),
        "http_method": request.method,
        "http_route": _route(request),
        "http_status_code": status_code,
        "duration_ms": round(duration_ms, 2),
        "error_type": error_type,
    }


def install_http_observability(app: FastAPI) -> None:
    @app.middleware("http")
    async def observe_http(request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        correlation_id = request.headers.get("X-Correlation-ID") or request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            LOGGER.info(
                json.dumps(
                    _record(
                        request,
                        request_id=request_id,
                        correlation_id=correlation_id,
                        status_code=500,
                        duration_ms=duration_ms,
                        error_type=type(exc).__name__,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Request-Duration-Ms"] = f"{duration_ms:.2f}"
        LOGGER.info(
            json.dumps(
                _record(
                    request,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return response

