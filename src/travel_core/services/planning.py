from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import PlanningContextRecord, PlanVersion, RevisionRequest, Trip
from ..schemas import PlanningContextIn, ValidatePlanOut, Violation
from ..security import RequestContext


def _fixture() -> dict[str, Any]:
    fixture_path = Path(get_settings().fixture_path)
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _readiness(payload: PlanningContextIn, fixture: dict[str, Any]) -> dict[str, Any]:
    request = payload.request
    clarifications: list[str] = []
    if not request.start_date or not request.end_date:
        clarifications.append("出行日期")
    if not request.travelers:
        clarifications.append("出行人数")
    if request.budget_cny is None:
        clarifications.append("预算范围")

    known_names = {poi["name"] for poi in fixture["pois"]}
    unknown_must_visit = [name for name in request.must_visit if name not in known_names]
    if unknown_must_visit:
        clarifications.append(f"当前冻结候选池不含{'、'.join(unknown_must_visit)}")

    combined_constraints = " ".join(request.hard_constraints)
    if "鼓浪屿" in request.must_visit and "不能坐船" in combined_constraints:
        clarifications.append("鼓浪屿必须乘船与不能坐船存在冲突")
    return {
        "decision": "clarify" if clarifications else "proceed",
        "clarifications": clarifications,
    }


def _route_minutes(a: dict[str, Any], b: dict[str, Any]) -> int:
    # Haversine distance converted to an intentionally conservative urban travel
    # estimate. Production replaces this fixture with a route-matrix provider.
    lat1, lon1, lat2, lon2 = map(math.radians, [a["lat"], a["lng"], b["lat"], b["lng"]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    distance_km = 6371 * 2 * math.asin(math.sqrt(h))
    return max(10, int(round(distance_km / 22 * 60 + 8)))


def _weather_for_request(payload: PlanningContextIn) -> list[dict[str, Any]]:
    start_raw = payload.request.start_date
    try:
        start = date.fromisoformat(start_raw) if start_raw else date(2026, 9, 10)
    except ValueError:
        start = date(2026, 9, 10)
    text = " ".join(
        [
            payload.request.free_text,
            *payload.request.preferences,
            *payload.request.hard_constraints,
            payload.revision_notes or "",
        ]
    )
    rainy = "雨" in text or "室内" in text
    return [
        {
            "date": start.isoformat(),
            "condition": "晴",
            "high_c": 30,
            "low_c": 24,
            "source": "training_fixture",
        },
        {
            "date": (start + timedelta(days=1)).isoformat(),
            "condition": "雨" if rainy else "多云",
            "high_c": 28,
            "low_c": 23,
            "source": "training_fixture",
        },
    ]


def create_planning_context(db: Session, ctx: RequestContext, payload: PlanningContextIn) -> dict[str, Any]:
    fixture = _fixture()
    destination = payload.request.destination or fixture["city"]
    if destination != fixture["city"]:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsupported_destination",
                "message": f"CP01 冻结数据只支持{fixture['city']}，收到：{destination}",
                "supported_destinations": [fixture["city"]],
            },
        )
    trip: Trip | None = None
    if payload.trip_id:
        trip = db.scalar(
            select(Trip).where(Trip.id == payload.trip_id, Trip.tenant_id == ctx.tenant_id)
        )
        if trip is None:
            raise HTTPException(status_code=404, detail="trip not found")
    else:
        trip = Trip(
            id=str(uuid.uuid4()),
            tenant_id=ctx.tenant_id,
            owner_id=ctx.user_id,
            destination=destination,
            status="draft",
        )
        db.add(trip)
        db.flush()

    if payload.base_version_id and payload.base_version_id != trip.current_version_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_conflict",
                "message": "base_version_id is not the current trip version",
                "current_version_id": trip.current_version_id,
            },
        )

    pois = fixture["pois"]
    route_matrix: dict[str, dict[str, int]] = {}
    for a in pois:
        route_matrix[a["poi_id"]] = {}
        for b in pois:
            route_matrix[a["poi_id"]][b["poi_id"]] = 0 if a is b else _route_minutes(a, b)

    evidence = [
        {
            "evidence_id": poi["evidence_id"],
            "source_type": "training_fixture",
            "title": f"{poi['name']}教学数据",
            "snippet": (
                f"开放时间 {poi['open_time']}-{poi['close_time']}；"
                f"建议游览 {poi['suggested_duration_min']} 分钟；标签：{'、'.join(poi['tags'])}。"
            ),
            "observed_at": fixture["observed_at"],
            "valid_until": fixture.get("valid_until"),
            "confidence": 0.8,
        }
        for poi in pois
    ]

    budget_by_profile = {
        "fast": {"max_candidates": 6, "max_plan_tokens": 1800, "rerank": False},
        "balanced": {"max_candidates": 9, "max_plan_tokens": 3000, "rerank": True},
        "accurate": {"max_candidates": 12, "max_plan_tokens": 5000, "rerank": True},
    }
    context_id = str(uuid.uuid4())
    response = {
        "context_id": context_id,
        "trip_id": trip.id,
        "base_version_id": trip.current_version_id,
        "request": payload.request.model_dump(mode="json"),
        "readiness": _readiness(payload, fixture),
        "revision_notes": payload.revision_notes,
        "candidate_pois": pois,
        "route_matrix_minutes": route_matrix,
        "evidence": evidence,
        "policy": {
            **budget_by_profile[payload.quality_profile],
            "quality_profile": payload.quality_profile,
            "hard_rules": [
                "All POIs must come from candidate_pois",
                "No time overlap",
                "Respect opening windows and route time",
                "Preserve explicit must_visit and locked items",
            ],
        },
        "weather": _weather_for_request(payload),
        "fixture_notice": fixture["fixture_notice"],
        "generated_at": datetime.now(timezone.utc),
    }
    db.add(
        PlanningContextRecord(
            id=context_id,
            tenant_id=ctx.tenant_id,
            trip_id=trip.id,
            base_version_id=trip.current_version_id,
            request_json=payload.model_dump(mode="json"),
            context_json=json.loads(json.dumps(response, ensure_ascii=False, default=str)),
        )
    )
    return response


def get_context(db: Session, tenant_id: str, context_id: str) -> PlanningContextRecord:
    record = db.scalar(
        select(PlanningContextRecord).where(
            PlanningContextRecord.id == context_id,
            PlanningContextRecord.tenant_id == tenant_id,
        )
    )
    if record is None:
        raise HTTPException(status_code=404, detail="planning context not found")
    return record


def parse_plan(plan: dict[str, Any] | None, plan_json_text: str | None) -> dict[str, Any]:
    if plan is not None:
        return plan
    assert plan_json_text is not None
    text = plan_json_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:].lstrip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"plan_json_text is invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="plan must be a JSON object")
    return parsed


def validate_plan(db: Session, tenant_id: str, context_id: str, plan: dict[str, Any]) -> ValidatePlanOut:
    record = get_context(db, tenant_id, context_id)
    context = record.context_json
    request = context.get("request") or {}
    pois = {poi["poi_id"]: poi for poi in context.get("candidate_pois", [])}
    route_matrix = context.get("route_matrix_minutes") or {}
    weather = {item["date"]: item for item in context.get("weather", [])}
    violations: list[Violation] = []
    days = plan.get("days")
    if not isinstance(days, list) or not days:
        violations.append(
            Violation(
                code="missing_days",
                severity="error",
                message="plan.days must be a non-empty array",
                path="days",
                suggested_fix="Generate at least one day with time-bounded items",
            )
        )
        days = []

    seen: set[str] = set()
    total_cost = 0.0
    evidence_refs = 0
    item_count = 0
    for day_index, day in enumerate(days):
        day_date = str(day.get("date") or "")
        try:
            parsed_date = date.fromisoformat(day_date)
        except ValueError:
            parsed_date = None
            violations.append(
                Violation(
                    code="invalid_date",
                    severity="error",
                    message=f"Invalid date: {day_date}",
                    path=f"days[{day_index}].date",
                )
            )
        items = day.get("items")
        if not isinstance(items, list):
            violations.append(
                Violation(
                    code="invalid_items",
                    severity="error",
                    message="day.items must be an array",
                    path=f"days[{day_index}].items",
                )
            )
            continue
        previous_end: int | None = None
        previous_poi_id: str | None = None
        for item_index, item in enumerate(items):
            item_count += 1
            path = f"days[{day_index}].items[{item_index}]"
            poi_id = str(item.get("poi_id") or "")
            poi = pois.get(poi_id)
            if poi is None:
                violations.append(
                    Violation(
                        code="unknown_poi",
                        severity="error",
                        message=f"POI {poi_id!r} is not in the grounded candidate pool",
                        path=f"{path}.poi_id",
                        suggested_fix="Choose a candidate_pois.poi_id",
                    )
                )
                continue
            if poi_id in seen:
                violations.append(
                    Violation(
                        code="duplicate_poi",
                        severity="warning",
                        message=f"{poi['name']} appears more than once",
                        path=path,
                    )
                )
            seen.add(poi_id)
            start_raw, end_raw = str(item.get("start") or ""), str(item.get("end") or "")
            try:
                start, end = _minutes(start_raw), _minutes(end_raw)
                if start >= end:
                    raise ValueError
            except (ValueError, AttributeError):
                violations.append(
                    Violation(
                        code="invalid_time",
                        severity="error",
                        message=f"Invalid time range {start_raw}-{end_raw}",
                        path=path,
                    )
                )
                continue

            open_min, close_min = _minutes(poi["open_time"]), _minutes(poi["close_time"])
            if start < open_min or end > close_min:
                violations.append(
                    Violation(
                        code="outside_opening_window",
                        severity="error",
                        message=(
                            f"{poi['name']} is scheduled {start_raw}-{end_raw}, outside fixture opening "
                            f"window {poi['open_time']}-{poi['close_time']}"
                        ),
                        path=path,
                        suggested_fix="Move or replace this item",
                    )
                )
            if parsed_date and parsed_date.isoweekday() in poi.get("closed_weekdays", []):
                violations.append(
                    Violation(
                        code="closed_weekday",
                        severity="error",
                        message=f"{poi['name']} is closed on this weekday in the fixture",
                        path=path,
                        suggested_fix="Choose another day or POI",
                    )
                )

            if previous_end is not None:
                expected_route = int(route_matrix.get(previous_poi_id, {}).get(poi_id, 0))
                declared_route = int(item.get("transport_from_previous_min") or expected_route)
                if start < previous_end + max(expected_route, declared_route):
                    violations.append(
                        Violation(
                            code="route_time_conflict",
                            severity="error",
                            message=(
                                f"Insufficient travel time before {poi['name']}; need at least "
                                f"{max(expected_route, declared_route)} minutes"
                            ),
                            path=path,
                            suggested_fix="Delay the start time or remove an item",
                        )
                    )
            previous_end, previous_poi_id = end, poi_id

            total_cost += float(item.get("cost_cny", poi.get("cost_cny", 0)) or 0)
            evidence_refs += len(item.get("evidence_ids") or [])
            day_weather = weather.get(day_date, {})
            if day_weather.get("condition") == "雨" and poi.get("weather_sensitive") and not poi.get("indoor"):
                violations.append(
                    Violation(
                        code="weather_mismatch",
                        severity="warning",
                        message=f"{poi['name']} is weather-sensitive on a rainy fixture day",
                        path=path,
                        suggested_fix="Prefer an indoor alternative or explicitly accept the risk",
                    )
                )

    must_visit = set(request.get("must_visit") or [])
    names_seen = {pois[poi_id]["name"] for poi_id in seen if poi_id in pois}
    for requirement in must_visit:
        if requirement not in seen and requirement not in names_seen:
            violations.append(
                Violation(
                    code="missing_must_visit",
                    severity="error",
                    message=f"Required place {requirement!r} is missing",
                    path="days",
                    suggested_fix="Add the required place and re-optimize the schedule",
                )
            )

    budget = request.get("budget_cny")
    if budget is not None and total_cost > float(budget):
        violations.append(
            Violation(
                code="budget_exceeded",
                severity="error",
                message=f"Estimated item cost {total_cost:.0f} exceeds budget {float(budget):.0f}",
                path="estimated_total_cost_cny",
                suggested_fix="Replace paid items or reduce paid activities",
            )
        )

    error_count = sum(v.severity == "error" for v in violations)
    warning_count = sum(v.severity == "warning" for v in violations)
    score = max(0.0, round(1.0 - error_count * 0.18 - warning_count * 0.04, 3))
    return ValidatePlanOut(
        valid=error_count == 0,
        score=score,
        violations=violations,
        metrics={
            "item_count": item_count,
            "grounded_item_count": len(seen),
            "total_cost_cny": round(total_cost, 2),
            "evidence_reference_count": evidence_refs,
            "error_count": error_count,
            "warning_count": warning_count,
        },
        normalized_plan=plan,
    )


def render_plan_markdown(plan: dict[str, Any], validation: dict[str, Any]) -> str:
    lines = [f"## {plan.get('title') or '旅行计划'}", ""]
    for day in plan.get("days") or []:
        lines.append(f"### {day.get('date', '待定日期')} · {day.get('theme', '行程')}" )
        for item in day.get("items") or []:
            lines.append(
                f"- **{item.get('start', '--:--')}–{item.get('end', '--:--')}** "
                f"{item.get('name') or item.get('poi_id')}"
                f"（预计 ¥{item.get('cost_cny', 0)}，交通 {item.get('transport_from_previous_min', 0)} 分钟）"
            )
        lines.append("")
    lines.append(
        f"校验：{'通过' if validation.get('valid') else '未通过'}；"
        f"评分 {validation.get('score', 0)}；"
        f"错误 {validation.get('metrics', {}).get('error_count', 0)}，"
        f"警告 {validation.get('metrics', {}).get('warning_count', 0)}。"
    )
    lines.append("")
    lines.append("> 课堂冻结数据，仅用于工程演示；真实上线必须查询实时开放时间、价格、天气和库存。")
    return "\n".join(lines)


def create_plan_version(
    db: Session,
    ctx: RequestContext,
    *,
    trip_id: str,
    context_id: str,
    base_version_id: str | None,
    plan: dict[str, Any],
    change_request: str | None,
    source_run_id: str | None,
) -> dict[str, Any]:
    trip = db.scalar(
        select(Trip)
        .where(Trip.id == trip_id, Trip.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if trip is None:
        raise HTTPException(status_code=404, detail="trip not found")
    if trip.current_version_id != base_version_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_conflict",
                "message": "The trip changed after this workflow started",
                "current_version_id": trip.current_version_id,
                "base_version_id": base_version_id,
            },
        )

    validation = validate_plan(db, ctx.tenant_id, context_id, plan).model_dump(mode="json")
    if not validation["valid"]:
        raise HTTPException(status_code=422, detail={"code": "plan_invalid", "validation": validation})

    version_id = str(uuid.uuid4())
    version_no = trip.current_version_no + 1
    context = get_context(db, ctx.tenant_id, context_id).context_json
    version = PlanVersion(
        id=version_id,
        tenant_id=ctx.tenant_id,
        trip_id=trip.id,
        version_no=version_no,
        parent_version_id=trip.current_version_id,
        status="proposed",
        change_request=change_request,
        source_run_id=source_run_id,
        plan_json=plan,
        validation_json=validation,
        evidence_json=context.get("evidence") or [],
        created_by=ctx.user_id,
    )
    db.add(version)
    trip.current_version_id = version_id
    trip.current_version_no = version_no
    trip.status = "draft"
    db.flush()
    return {
        "trip_id": trip.id,
        "version_id": version.id,
        "version_no": version.version_no,
        "parent_version_id": version.parent_version_id,
        "status": version.status,
        "plan": version.plan_json,
        "validation": version.validation_json,
        "rendered_plan": render_plan_markdown(version.plan_json, version.validation_json),
        "created_at": version.created_at.isoformat() if version.created_at else _iso_now(),
    }


def approve_version(db: Session, ctx: RequestContext, trip_id: str, version_id: str) -> dict[str, Any]:
    trip = db.scalar(select(Trip).where(Trip.id == trip_id, Trip.tenant_id == ctx.tenant_id))
    version = db.scalar(
        select(PlanVersion).where(
            PlanVersion.id == version_id,
            PlanVersion.trip_id == trip_id,
            PlanVersion.tenant_id == ctx.tenant_id,
        )
    )
    if trip is None or version is None:
        raise HTTPException(status_code=404, detail="trip or version not found")
    if trip.current_version_id != version_id:
        raise HTTPException(status_code=409, detail="only the current version can be approved")
    version.status = "approved"
    version.approved_at = datetime.now(timezone.utc)
    trip.status = "approved"
    db.flush()
    return {
        "trip_id": trip.id,
        "version_id": version.id,
        "status": "approved",
        "approved_at": version.approved_at.isoformat(),
    }


def create_revision_request(
    db: Session,
    ctx: RequestContext,
    trip_id: str,
    base_version_id: str,
    notes: str,
) -> dict[str, Any]:
    trip = db.scalar(select(Trip).where(Trip.id == trip_id, Trip.tenant_id == ctx.tenant_id))
    if trip is None:
        raise HTTPException(status_code=404, detail="trip not found")
    if trip.current_version_id != base_version_id:
        raise HTTPException(status_code=409, detail="revision base is stale")
    item = RevisionRequest(
        id=str(uuid.uuid4()),
        tenant_id=ctx.tenant_id,
        trip_id=trip_id,
        base_version_id=base_version_id,
        notes=notes,
        requested_by=ctx.user_id,
        status="queued",
    )
    db.add(item)
    db.flush()
    return {
        "revision_request_id": item.id,
        "trip_id": trip_id,
        "base_version_id": base_version_id,
        "status": item.status,
        "notes": notes,
    }


def abandon_trip(db: Session, ctx: RequestContext, trip_id: str) -> dict[str, Any]:
    trip = db.scalar(select(Trip).where(Trip.id == trip_id, Trip.tenant_id == ctx.tenant_id))
    if trip is None:
        raise HTTPException(status_code=404, detail="trip not found")
    trip.status = "abandoned"
    db.flush()
    return {"trip_id": trip.id, "status": trip.status}


def get_trip(db: Session, ctx: RequestContext, trip_id: str) -> dict[str, Any]:
    trip = db.scalar(select(Trip).where(Trip.id == trip_id, Trip.tenant_id == ctx.tenant_id))
    if trip is None:
        raise HTTPException(status_code=404, detail="trip not found")
    versions = db.scalars(
        select(PlanVersion)
        .where(PlanVersion.trip_id == trip_id, PlanVersion.tenant_id == ctx.tenant_id)
        .order_by(PlanVersion.version_no)
    ).all()
    return {
        "trip_id": trip.id,
        "destination": trip.destination,
        "status": trip.status,
        "current_version_id": trip.current_version_id,
        "current_version_no": trip.current_version_no,
        "versions": [
            {
                "version_id": item.id,
                "version_no": item.version_no,
                "parent_version_id": item.parent_version_id,
                "status": item.status,
                "change_request": item.change_request,
                "created_at": item.created_at.isoformat(),
            }
            for item in versions
        ],
    }
