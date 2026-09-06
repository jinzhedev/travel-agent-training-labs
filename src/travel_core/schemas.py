from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TripRequest(ApiModel):
    destination: str = "厦门"
    start_date: str | None = None
    end_date: str | None = None
    travelers: list[dict[str, Any]] = Field(default_factory=list)
    budget_cny: float | None = None
    preferences: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    must_visit: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    free_text: str = ""


class PlanningContextIn(ApiModel):
    request: TripRequest
    trip_id: str | None = None
    base_version_id: str | None = None
    revision_notes: str | None = None
    quality_profile: Literal["fast", "balanced", "accurate"] = "balanced"
    failure_mode: str = "none"
    source_run_id: str | None = None


class Evidence(ApiModel):
    evidence_id: str
    source_type: str
    title: str
    snippet: str
    observed_at: str
    valid_until: str | None = None
    confidence: float = 1.0


class CandidatePoi(ApiModel):
    poi_id: str
    name: str
    district: str
    open_time: str
    close_time: str
    suggested_duration_min: int
    cost_cny: float
    indoor: bool
    weather_sensitive: bool
    tags: list[str]
    evidence_id: str


class PlanningContextOut(ApiModel):
    context_id: str
    trip_id: str
    base_version_id: str | None
    request: dict[str, Any]
    candidate_pois: list[dict[str, Any]]
    route_matrix_minutes: dict[str, dict[str, int]]
    evidence: list[Evidence]
    policy: dict[str, Any]
    weather: list[dict[str, Any]]
    fixture_notice: str
    generated_at: datetime


class ValidatePlanIn(ApiModel):
    context_id: str
    plan: dict[str, Any] | None = None
    plan_json_text: str | None = None
    failure_mode: str = "none"

    @model_validator(mode="after")
    def require_plan(self):
        if self.plan is None and not self.plan_json_text:
            raise ValueError("plan or plan_json_text is required")
        return self


class Violation(ApiModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    path: str | None = None
    suggested_fix: str | None = None


class ValidatePlanOut(ApiModel):
    valid: bool
    score: float
    violations: list[Violation]
    metrics: dict[str, Any]
    normalized_plan: dict[str, Any] | None = None


class CreateVersionIn(ApiModel):
    context_id: str
    base_version_id: str | None = None
    plan: dict[str, Any] | None = None
    plan_json_text: str | None = None
    change_request: str | None = None
    source_run_id: str | None = None
    failure_mode: str = "none"

    @model_validator(mode="after")
    def require_plan(self):
        if self.plan is None and not self.plan_json_text:
            raise ValueError("plan or plan_json_text is required")
        return self


class PlanVersionOut(ApiModel):
    trip_id: str
    version_id: str
    version_no: int
    parent_version_id: str | None
    status: str
    plan: dict[str, Any]
    validation: dict[str, Any]
    rendered_plan: str
    created_at: datetime


class ApprovalIn(ApiModel):
    approval_note: str | None = None


class RevisionRequestIn(ApiModel):
    base_version_id: str
    notes: str = Field(min_length=1)


class RevisionRequestOut(ApiModel):
    revision_request_id: str
    trip_id: str
    base_version_id: str
    status: str
    notes: str


class ToolSelectIn(ApiModel):
    task: str = Field(min_length=1, max_length=4000)
    strategy: Literal["full_catalog", "namespace", "retrieval"] = "retrieval"
    max_tools: int = Field(default=5, ge=1, le=64)
    allow_side_effects: bool = False
    namespaces: list[str] = Field(default_factory=list)


class ToolCall(ApiModel):
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolBatchExecuteIn(ApiModel):
    calls: list[ToolCall] = Field(min_length=1, max_length=8)
    approval_id: str | None = None
    current_itinerary_id: str | None = None
    execution_mode: Literal["sequential", "parallel"] = "sequential"
    response_mode: Literal["detailed", "concise"] = "detailed"


class EvidencePlanSlot(ApiModel):
    slot_id: str = Field(min_length=1, max_length=100)
    query: str = ""
    depends_on: list[str] = Field(default_factory=list)
    query_template: str | None = None
    entity_source: str | None = None
    required: bool = True
    expected_object_types: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_query_or_template(self):
        if not self.query.strip() and not self.query_template:
            raise ValueError("evidence slot requires query or query_template")
        if self.depends_on and not self.query_template:
            raise ValueError("dependent evidence slot requires query_template")
        return self


class RagRunIn(ApiModel):
    query: str = Field(min_length=1, max_length=4000)
    mode: Literal["single", "multihop"] = "single"
    corpus_mode: Literal["text_only", "object_aware"] = "object_aware"
    retrieval_strategy: Literal["keyword", "hybrid_simulated"] = "hybrid_simulated"
    n_retrieve: int = Field(default=12, ge=1, le=50)
    rerank_mode: Literal["off", "simulated"] = "simulated"
    n_rerank: int = Field(default=8, ge=1, le=50)
    k_context: int = Field(default=4, ge=1, le=20)
    rerank_budget_ms: int = Field(default=100, ge=0, le=10_000)
    evidence_plan: list[EvidencePlanSlot] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_candidate_funnel(self):
        if self.k_context > self.n_retrieve:
            raise ValueError("k_context must be less than or equal to n_retrieve")
        if self.rerank_mode != "off":
            if self.n_rerank > self.n_retrieve:
                raise ValueError("n_rerank must be less than or equal to n_retrieve")
            if self.k_context > self.n_rerank:
                raise ValueError("k_context must be less than or equal to n_rerank")
        if self.mode == "multihop" and not self.evidence_plan:
            raise ValueError("multihop mode requires evidence_plan")
        return self
