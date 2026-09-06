from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    destination: Mapped[str] = mapped_column(String(200), default="厦门")
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_version_no: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PlanningContextRecord(Base):
    __tablename__ = "planning_contexts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    trip_id: Mapped[str] = mapped_column(String(36), ForeignKey("trips.id"), index=True)
    base_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("trip_id", "version_no", name="uq_trip_version_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    trip_id: Mapped[str] = mapped_column(String(36), ForeignKey("trips.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="proposed", index=True)
    change_request: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    validation_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RevisionRequest(Base):
    __tablename__ = "revision_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    trip_id: Mapped[str] = mapped_column(String(36), ForeignKey("trips.id"), index=True)
    base_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("plan_versions.id"))
    notes: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    requested_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_tenant_idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(300), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(30), default="in_progress")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FailureInjection(Base):
    __tablename__ = "failure_injections"
    __table_args__ = (UniqueConstraint("tenant_id", "failure_key", name="uq_failure_once"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    failure_key: Mapped[str] = mapped_column(String(300), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
