from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db, init_db
from .failure_injection import consume_failure_once
from .idempotency import IdempotentResult, execute_idempotent
from .lab05 import router as lab05_router
from .observability import install_http_observability
from .schemas import (
    ApprovalIn,
    CreateVersionIn,
    PlanningContextIn,
    RagRunIn,
    RevisionRequestIn,
    ToolBatchExecuteIn,
    ToolSelectIn,
    ValidatePlanIn,
)
from .security import RequestContext, request_context
from .services import planning, rag, tools


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


settings = get_settings()
app = FastAPI(
    title="Travel Core API",
    version="2.0.0-cp05",
    description=(
        "文旅 Agent 课程的业务核心服务。Dify 负责智能体行为；Travel Core 负责事实、"
        "确定性规则、业务状态、工具目录、权限过滤、幂等和版本。"
    ),
    lifespan=lifespan,
)
install_http_observability(app)
app.include_router(lab05_router)


def _response(result: IdempotentResult) -> JSONResponse:
    return JSONResponse(
        status_code=result.status_code,
        content=jsonable_encoder(result.body),
        headers={"X-Idempotent-Replay": "true" if result.replayed else "false"},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "type": "about:blank",
            "title": "Request failed",
            "status": exc.status_code,
            "detail": jsonable_encoder(exc.detail),
            "instance": str(request.url.path),
        },
        media_type="application/problem+json",
        headers=exc.headers,
    )


@app.get("/healthz", tags=["operations"], operation_id="healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.get("/readyz", tags=["operations"], operation_id="readyz")
def readyz(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.post("/v1/planning-contexts", tags=["planning"], status_code=201, operation_id="createPlanningContext")
def create_planning_context(
    payload: PlanningContextIn,
    ctx: RequestContext = Depends(request_context),
    db: Session = Depends(get_db),
):
    if payload.failure_mode == "context_503_once" and consume_failure_once(db, ctx, "planning-context"):
        raise HTTPException(status_code=503, detail="injected transient context failure; retry is safe")

    def operation() -> tuple[dict[str, Any], int]:
        body = planning.create_planning_context(db, ctx, payload)
        return jsonable_encoder(body), 201

    result = execute_idempotent(
        db,
        ctx=ctx,
        method="POST",
        path="/v1/planning-contexts",
        payload=payload.model_dump(mode="json"),
        operation=operation,
    )
    return _response(result)


@app.post("/v1/plans:validate", tags=["planning"], operation_id="validatePlan")
def validate_plan(
    payload: ValidatePlanIn,
    ctx: RequestContext = Depends(request_context),
    db: Session = Depends(get_db),
):
    if payload.failure_mode == "validation_503_once" and consume_failure_once(db, ctx, "validation"):
        raise HTTPException(status_code=503, detail="injected transient validation failure; retry is safe")
    plan = planning.parse_plan(payload.plan, payload.plan_json_text)
    result = planning.validate_plan(db, ctx.tenant_id, payload.context_id, plan)
    return result.model_dump(mode="json")


@app.post("/v1/trips/{trip_id}/versions", tags=["trips"], status_code=201, operation_id="createPlanVersion")
def create_version(
    trip_id: str,
    payload: CreateVersionIn,
    ctx: RequestContext = Depends(request_context),
    db: Session = Depends(get_db),
):
    plan = planning.parse_plan(payload.plan, payload.plan_json_text)

    def operation() -> tuple[dict[str, Any], int]:
        body = planning.create_plan_version(
            db,
            ctx,
            trip_id=trip_id,
            context_id=payload.context_id,
            base_version_id=payload.base_version_id,
            plan=plan,
            change_request=payload.change_request,
            source_run_id=payload.source_run_id,
        )
        return jsonable_encoder(body), 201

    result = execute_idempotent(
        db,
        ctx=ctx,
        method="POST",
        path=f"/v1/trips/{trip_id}/versions",
        payload=payload.model_dump(mode="json"),
        operation=operation,
    )
    if payload.failure_mode == "persist_504_after_commit" and not result.replayed:
        # The business transaction and idempotency response are already committed.
        # A Dify retry with the same key will receive the stored 201 response.
        return JSONResponse(
            status_code=504,
            content={
                "type": "about:blank",
                "title": "Injected gateway timeout after commit",
                "status": 504,
                "detail": "The version may already exist. Retry with the same Idempotency-Key.",
                "trip_id": trip_id,
            },
            headers={"X-Demo-Commit-Completed": "true"},
        )
    return _response(result)


@app.post("/v1/trips/{trip_id}/versions/{version_id}:approve", tags=["trips"], operation_id="approvePlanVersion")
def approve_version(
    trip_id: str,
    version_id: str,
    payload: ApprovalIn,
    ctx: RequestContext = Depends(request_context),
    db: Session = Depends(get_db),
):
    def operation() -> tuple[dict[str, Any], int]:
        return planning.approve_version(db, ctx, trip_id, version_id), 200

    result = execute_idempotent(
        db,
        ctx=ctx,
        method="POST",
        path=f"/v1/trips/{trip_id}/versions/{version_id}:approve",
        payload=payload.model_dump(mode="json"),
        operation=operation,
    )
    return _response(result)


@app.post("/v1/trips/{trip_id}/revision-requests", tags=["trips"], status_code=202, operation_id="createRevisionRequest")
def request_revision(
    trip_id: str,
    payload: RevisionRequestIn,
    ctx: RequestContext = Depends(request_context),
    db: Session = Depends(get_db),
):
    def operation() -> tuple[dict[str, Any], int]:
        return planning.create_revision_request(
            db, ctx, trip_id, payload.base_version_id, payload.notes
        ), 202

    result = execute_idempotent(
        db,
        ctx=ctx,
        method="POST",
        path=f"/v1/trips/{trip_id}/revision-requests",
        payload=payload.model_dump(mode="json"),
        operation=operation,
    )
    return _response(result)


@app.post("/v1/trips/{trip_id}:abandon", tags=["trips"], operation_id="abandonTrip")
def abandon_trip(
    trip_id: str,
    ctx: RequestContext = Depends(request_context),
    db: Session = Depends(get_db),
):
    def operation() -> tuple[dict[str, Any], int]:
        return planning.abandon_trip(db, ctx, trip_id), 200

    result = execute_idempotent(
        db,
        ctx=ctx,
        method="POST",
        path=f"/v1/trips/{trip_id}:abandon",
        payload={"trip_id": trip_id},
        operation=operation,
    )
    return _response(result)


@app.get("/v1/trips/{trip_id}", tags=["trips"], operation_id="getTrip")
def get_trip(
    trip_id: str,
    ctx: RequestContext = Depends(request_context),
    db: Session = Depends(get_db),
):
    return planning.get_trip(db, ctx, trip_id)


@app.get("/v1/tools", tags=["tools"], operation_id="listTravelTools")
def list_tools(
    allow_side_effects: bool = False,
    ctx: RequestContext = Depends(request_context),
):
    return tools.list_tools(ctx, allow_side_effects=allow_side_effects)


@app.post("/v1/tools:select", tags=["tools"], operation_id="selectTravelTools")
def select_tools(
    payload: ToolSelectIn,
    ctx: RequestContext = Depends(request_context),
):
    return tools.select_tools(payload, ctx)


@app.post("/v1/rag:run", tags=["rag"], operation_id="runDocumentRag")
def run_document_rag(
    payload: RagRunIn,
    _: RequestContext = Depends(request_context),
):
    """Run the transparent CP03 retrieval fixture.

    This endpoint deliberately exposes every funnel stage so learners can compare
    parse quality, retrieval breadth, rerank breadth and final context size.
    """
    return rag.run_rag(payload)


@app.post("/v1/tools:execute-batch", tags=["tools"], operation_id="executeTravelTools")
def execute_tools(
    payload: ToolBatchExecuteIn,
    ctx: RequestContext = Depends(request_context),
    db: Session = Depends(get_db),
):
    def operation() -> tuple[dict[str, Any], int]:
        return tools.execute_batch(payload, ctx), 200

    result = execute_idempotent(
        db,
        ctx=ctx,
        method="POST",
        path="/v1/tools:execute-batch",
        payload=payload.model_dump(mode="json"),
        operation=operation,
    )
    return _response(result)


def _register_catalog_tool_routes() -> None:
    """Expose every catalog tool as a separately addressable, read/write API.

    The catalog remains the source of truth for the request schemas and runtime
    authorization.  The endpoint body is intentionally a plain mapping here:
    the shared executor performs the JSON Schema validation, while ``openapi_extra``
    publishes each catalog tool's precise schema for OpenAPI consumers such as Dify.
    """
    def make_endpoint(tool_name: str):
        def endpoint(
            payload: dict[str, Any] = Body(...),
            ctx: RequestContext = Depends(request_context),
        ) -> dict[str, Any]:
            result = tools.execute_batch(
                ToolBatchExecuteIn(
                    calls=[{"name": tool_name, "arguments": payload}],
                    response_mode="concise",
                ),
                ctx,
            )
            return result["results"][0]["data"]

        return endpoint

    for tool in tools.catalog()["tools"]:
        tool_name = tool["name"]
        namespace, operation = tool_name.split(".", maxsplit=1)
        operation_id = tool_name.replace(".", "_")
        endpoint = make_endpoint(tool_name)
        endpoint.__name__ = operation_id
        endpoint.__doc__ = tool["summary"]
        app.add_api_route(
            f"/v1/tools/{namespace}/{operation}",
            endpoint,
            methods=["POST"],
            tags=["tools"],
            operation_id=operation_id,
            summary=tool["summary"],
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": tool["input_schema"]},
                    },
                },
                "x-travel-core-tool-name": tool_name,
            },
        )


_register_catalog_tool_routes()


@app.get("/v1/tools/openapi.json", include_in_schema=False)
def tools_openapi(request: Request) -> dict[str, Any]:
    """Return an OpenAPI document containing only catalog-backed tool APIs."""
    source = app.openapi()
    selected_paths: dict[str, Any] = {}
    for tool in tools.catalog()["tools"]:
        namespace, operation = tool["name"].split(".", maxsplit=1)
        path = f"/v1/tools/{namespace}/{operation}"
        if path in source["paths"]:
            selected_paths[path] = source["paths"][path]

    for path_item in selected_paths.values():
        for operation in path_item.values():
            if isinstance(operation, dict) and "operationId" in operation:
                operation["security"] = [{"ApiKeyAuth": []}]

    return {
        "openapi": source["openapi"],
        "info": {
            "title": "Travel Core Tools",
            "version": source["info"]["version"],
            "description": "Catalog-backed Travel Core tool APIs for Dify Agent tools.",
        },
        "servers": [{"url": str(request.base_url).rstrip("/")}],
        "paths": selected_paths,
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "Travel Core API key",
                }
            }
        },
    }
