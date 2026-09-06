"""Lab 05 的任务预算与推荐保存。只调用课程模拟工具，不连接付费服务。"""
from __future__ import annotations

import hashlib
import json
import secrets
from copy import deepcopy
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Integer, String, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from .config import get_settings
from .database import Base, get_db
from .security import RequestContext, request_context

router = APIRouter(prefix="/v1/lab05", tags=["lab05"])


class RuntimeTask(Base):
    __tablename__ = "lab05_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    call_limit: Mapped[int] = mapped_column(Integer)
    used_calls: Mapped[int] = mapped_column(Integer, default=0)
    blocked_calls: Mapped[int] = mapped_column(Integer, default=0)
    config: Mapped[dict] = mapped_column(JSON)
    record: Mapped[dict] = mapped_column(JSON)


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewTask(StrictInput):
    query: str = Field(min_length=1, max_length=4000)
    profile: Literal["baseline", "limited"] = "limited"
    fail_first_call: bool = False
    lose_commit_response: bool = False


class PaidQuery(StrictInput):
    poi_id: str = Field(min_length=1, max_length=100)


class Recommendation(StrictInput):
    title: str = Field(min_length=1, max_length=160)
    poi_ids: list[str] = Field(min_length=1, max_length=3)
    reason: str = Field(min_length=1, max_length=1000)


def corpus() -> dict:
    return json.loads(Path(get_settings().fixture_path).read_text(encoding="utf-8"))


def snapshot(task: RuntimeTask) -> dict:
    return {
        "task_id": task.id, "status": task.status,
        "call_limit": task.call_limit, "used_calls": task.used_calls,
        "remaining_calls": task.call_limit - task.used_calls,
        "blocked_calls": task.blocked_calls,
        "query": task.config["query"], "context_revision": task.config["context_revision"],
        **deepcopy(task.record),
        "is_simulated": True,
    }


def locked_task(db: Session, *conditions) -> RuntimeTask:
    # UPDATE 在 PostgreSQL/SQLite 上均先取得写锁，避免多实例读改写丢失计数。
    task_id = db.execute(
        update(RuntimeTask).where(*conditions)
        .values(used_calls=RuntimeTask.used_calls).returning(RuntimeTask.id)
    ).scalar_one_or_none()
    if task_id is None:
        raise HTTPException(404, "task_not_found")
    return db.get(RuntimeTask, task_id, populate_existing=True)


def owned(db: Session, ctx: RequestContext, task_id: str) -> RuntimeTask:
    return locked_task(db, RuntimeTask.id == task_id,
                       RuntimeTask.tenant_id == ctx.tenant_id, RuntimeTask.owner_id == ctx.user_id)


def event(task: RuntimeTask, kind: str, **values) -> None:
    record = deepcopy(task.record)
    record["events"] = (record.get("events", []) + [{"kind": kind, **values}])[-200:]
    task.record = record


@router.post("/tasks", status_code=201)
def create_task(payload: NewTask, ctx: RequestContext = Depends(request_context),
                db: Session = Depends(get_db)):
    if ctx.user_id == "anonymous":
        raise HTTPException(401, "operator_identity_required")
    token = secrets.token_urlsafe(32)
    data = corpus()
    task = RuntimeTask(
        id=str(uuid4()), tenant_id=ctx.tenant_id, owner_id=ctx.user_id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        call_limit=30 if payload.profile == "baseline" else 5,
        config={**payload.model_dump(), "context_revision": data["revision"]},
        record={"observations": [], "events": [], "pending_action": None, "result": None},
    )
    db.add(task)
    db.commit()
    return {**snapshot(task), "task_token": token,
            "candidates": [{"poi_id": p["poi_id"], "name": p["name"]} for p in data["pois"]]}


@router.get("/tasks/{task_id}")
def read_task(task_id: str, ctx: RequestContext = Depends(request_context),
              db: Session = Depends(get_db)):
    task = db.scalar(select(RuntimeTask).where(RuntimeTask.id == task_id,
                     RuntimeTask.tenant_id == ctx.tenant_id, RuntimeTask.owner_id == ctx.user_id))
    if task is None:
        raise HTTPException(404, "task_not_found")
    return snapshot(task)


def paid_query(payload: PaidQuery, name: str, token: str, db: Session) -> dict:
    if not token:
        raise HTTPException(401, "task_token_required")
    task = locked_task(db, RuntimeTask.token_hash == hashlib.sha256(token.encode()).hexdigest())
    stop = "" if task.status == "active" else f"task_{task.status}"
    if not stop and task.used_calls >= task.call_limit:
        stop = "tool_budget_exhausted"
    if stop:
        task.blocked_calls += 1
        event(task, "blocked", tool=name, poi_id=payload.poi_id, stop_reason=stop)
        db.commit()
        return {"status": "blocked", "stop_reason": stop, "task_id": task.id,
                "used_calls": task.used_calls, "remaining_calls": task.call_limit-task.used_calls}

    # 先持久化预留，再进入付费边界。进程中断也不能把已预留额度退回。
    task.used_calls += 1
    attempt = task.used_calls
    failure = task.config["fail_first_call"] and attempt == 1
    task_id = task.id
    event(task, "executed", tool=name, poi_id=payload.poi_id,
          attempt=attempt, outcome="admitted", simulated_cost_units=1)
    db.commit()
    data = corpus()
    poi = next((p for p in data["pois"] if p["poi_id"] == payload.poi_id), None)
    if failure or poi is None:
        result = {"status": "upstream_error", "code": "temporary_error" if failure else "poi_not_found"}
    else:
        fields = ("poi_id", "name", "open_time", "close_time", "evidence_id")
        value = {k: poi[k] for k in fields} if name == "paid_poi_hours" else poi
        result = {"status": "ok", "data": value,
                  **{k: data[k] for k in ("source", "observed_at", "valid_until", "revision", "is_simulated")}}
    # 结果可能在取消后晚到；允许记录已准入调用的证据，但不恢复任务状态。
    task = locked_task(db, RuntimeTask.id == task_id)
    if result["status"] == "ok":
        record = deepcopy(task.record)
        record["observations"].append({"tool": name, **result})
        task.record = record
    event(task, "tool_result", tool=name, poi_id=payload.poi_id,
          attempt=attempt, outcome=result["status"])
    db.commit()
    return {**result, "task_id": task.id, "used_calls": task.used_calls,
            "remaining_calls": task.call_limit-task.used_calls, "simulated_cost_units": 1}


@router.post("/tools/paid-poi-detail", operation_id="paid_poi_detail")
def paid_detail(payload: PaidQuery, token: str = Header(default="", alias="X-Lab-Task-Token"),
                db: Session = Depends(get_db)):
    return paid_query(payload, "paid_poi_detail", token, db)


@router.post("/tools/paid-poi-hours", operation_id="paid_poi_hours")
def paid_hours(payload: PaidQuery, token: str = Header(default="", alias="X-Lab-Task-Token"),
               db: Session = Depends(get_db)):
    return paid_query(payload, "paid_poi_hours", token, db)


@router.post("/tasks/{task_id}/prepare")
def prepare(task_id: str, payload: Recommendation, ctx: RequestContext = Depends(request_context),
            db: Session = Depends(get_db)):
    task = owned(db, ctx, task_id)
    candidate = payload.model_dump()
    pending = task.record["pending_action"]
    if pending and pending["candidate"] != candidate:
        raise HTTPException(409, "candidate_changed_requires_new_review")
    if task.status not in ("active", "prepared"):
        raise HTTPException(409, f"task_{task.status}")
    known = {o["data"]["poi_id"] for o in task.record["observations"]}
    if len(set(payload.poi_ids)) != len(payload.poi_ids) or not set(payload.poi_ids).issubset(known):
        raise HTTPException(409, "recommendation_requires_observed_pois")
    if not pending:
        record = deepcopy(task.record)
        record["pending_action"] = {"operation_id": str(uuid4()), "candidate": candidate,
                                    "context_revision": task.config["context_revision"]}
        task.record = record
        event(task, "prepared", operation_id=record["pending_action"]["operation_id"],
              source_run_id=ctx.correlation_id)
    task.status = "prepared"
    db.commit()
    return snapshot(task)


@router.post("/tasks/{task_id}/cancel")
def cancel(task_id: str, ctx: RequestContext = Depends(request_context), db: Session = Depends(get_db)):
    task = owned(db, ctx, task_id)
    if task.status != "completed":
        task.status = "cancelled"
        event(task, "cancelled", source_run_id=ctx.correlation_id)
    db.commit()
    return snapshot(task)


@router.post("/tasks/{task_id}/commit")
def commit(task_id: str, ctx: RequestContext = Depends(request_context), db: Session = Depends(get_db)):
    task = owned(db, ctx, task_id)
    if task.status == "completed":
        result = {**snapshot(task), "replayed": True}
        db.commit()
        return result
    if task.status != "prepared":
        raise HTTPException(409, f"task_{task.status}")
    pending = task.record["pending_action"]
    if pending["context_revision"] != corpus()["revision"]:
        raise HTTPException(409, "context_revision_changed_requires_review")
    record = deepcopy(task.record)
    record["result"] = {"recommendation_id": str(uuid4()), **pending}
    task.record = record
    task.status = "completed"
    event(task, "committed", operation_id=pending["operation_id"], source_run_id=ctx.correlation_id)
    db.commit()
    if task.config["lose_commit_response"]:
        return JSONResponse(status_code=504, content={"status": "result_unknown", "task_id": task.id})
    return {**snapshot(task), "replayed": False}
