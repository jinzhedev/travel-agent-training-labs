from __future__ import annotations

import json
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from jsonschema import Draft202012Validator, FormatChecker

from ..config import get_settings
from ..schemas import ToolBatchExecuteIn, ToolSelectIn
from ..security import RequestContext


@lru_cache
def catalog() -> dict[str, Any]:
    path = Path(get_settings().tool_catalog_path)
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_map() -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in catalog()["tools"]}


def _allowed(tool: dict[str, Any], ctx: RequestContext, allow_side_effects: bool) -> bool:
    if get_settings().environment not in tool["environments"]:
        return False
    if not set(tool["permissions"]).issubset(ctx.permissions):
        return False
    return allow_side_effects or tool["side_effect"] == "none"


def _model_card(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "namespace": tool["namespace"],
        "summary": tool["summary"],
        "use_when": tool["use_when"],
        "avoid_when": tool["avoid_when"],
        "risk_level": tool["risk_level"],
        "side_effect": tool["side_effect"],
        "requires_confirmation": tool["requires_confirmation"],
        "input_schema": tool["input_schema"],
    }


def list_tools(ctx: RequestContext, *, allow_side_effects: bool = False) -> dict[str, Any]:
    items = [tool for tool in catalog()["tools"] if _allowed(tool, ctx, allow_side_effects)]
    return {
        "catalog_revision": catalog()["revision"],
        "tools": [_model_card(tool) for tool in items],
        "count": len(items),
        "notice": catalog()["notice"],
    }


def _infer_namespaces(task: str) -> set[str]:
    hints = {
        "poi": ["景点", "开放", "票价", "预约", "亲子", "无障碍"],
        "weather": ["天气", "下雨", "降雨", "晴雨", "预警", "大风", "暴雨", "温度", "空气质量"],
        "mobility": [
            "交通", "高铁", "火车", "航班", "飞机", "怎么走", "公交", "地铁",
            "轮渡", "渡轮", "船", "船票", "余票", "班次",
        ],
        "stay": ["酒店", "住宿", "入住", "离店", "房价"],
        "itinerary": ["行程", "一日游", "两日游", "路线", "预算"],
        "booking": ["预订", "订单", "退改", "退款", "取消", "占位"],
        "life": ["水费", "电费", "生活服务", "交通违法", "违章"],
        "account": ["账户", "手机号", "联系人", "我的偏好"],
        "ops": ["服务状态", "健康检查", "幂等", "超时核验"],
    }
    return {
        namespace
        for namespace, words in hints.items()
        if any(word.lower() in task.lower() for word in words)
    }


def _score(task: str, tool: dict[str, Any]) -> int:
    query = task.lower()
    score = 0
    for keyword in tool["keywords"]:
        if keyword.lower() in query:
            score += 8
    if tool["namespace"] in _infer_namespaces(task):
        score += 2
    for token in tool["name"].replace("_", " ").replace(".", " ").split():
        if len(token) > 2 and token in query:
            score += 2
    return score


def select_tools(payload: ToolSelectIn, ctx: RequestContext) -> dict[str, Any]:
    all_tools = catalog()["tools"]
    eligible = [tool for tool in all_tools if _allowed(tool, ctx, payload.allow_side_effects)]
    requested_namespaces = set(payload.namespaces)
    inferred_namespaces = _infer_namespaces(payload.task)

    if payload.strategy == "full_catalog":
        ranked = [(0, tool) for tool in eligible]
    elif payload.strategy == "namespace":
        active = requested_namespaces or inferred_namespaces
        ranked = [(0, tool) for tool in eligible if tool["namespace"] in active]
    else:
        ranked = [(_score(payload.task, tool), tool) for tool in eligible]
        ranked = [item for item in ranked if item[0] > 0]
        ranked.sort(key=lambda item: (-item[0], item[1]["name"]))

    selected = [tool for _, tool in ranked[: payload.max_tools]]
    cards = [_model_card(tool) for tool in selected]
    return {
        "catalog_revision": catalog()["revision"],
        "strategy": payload.strategy,
        "selected_tools": cards,
        "candidate_names": [tool["name"] for tool in selected],
        "counts": {
            "catalog": len(all_tools),
            "eligible_after_hard_filter": len(eligible),
            "selected": len(selected),
        },
        "selection_trace": {
            "requested_namespaces": sorted(requested_namespaces),
            "inferred_namespaces": sorted(inferred_namespaces),
            "allow_side_effects": payload.allow_side_effects,
            "permission_count": len(ctx.permissions),
            "estimated_schema_chars": len(json.dumps(cards, ensure_ascii=False)),
        },
        "notice": catalog()["notice"],
    }


def _validate_arguments(tool: dict[str, Any], arguments: dict[str, Any]) -> None:
    validator = Draft202012Validator(tool["input_schema"], format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(arguments), key=lambda error: list(error.path))
    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_tool_arguments",
                "tool_name": tool["name"],
                "errors": [error.message for error in errors],
            },
        )


def _normalize_entity_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return "".join(normalized.split()).casefold()


def _normalize_pier_name(value: str) -> str:
    aliases = {
        "厦门邮轮中心码头": "邮轮中心",
        "邮轮中心码头": "邮轮中心",
        "三丘田码头": "三丘田",
    }
    normalized = _normalize_entity_name(value)
    return _normalize_entity_name(aliases.get(value, normalized))


@lru_cache
def _hotel_fixture() -> dict[str, Any]:
    path = Path(get_settings().fixture_path).parent / "hotels.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _hotel_by_id(hotel_id: str) -> dict[str, Any]:
    hotel = next(
        (item for item in _hotel_fixture()["hotels"] if item["hotel_id"] == hotel_id),
        None,
    )
    if hotel is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "hotel_not_found", "hotel_id": hotel_id},
        )
    return hotel


def _hotel_dates(arguments: dict[str, Any]) -> tuple[str, str, int]:
    check_in = arguments["check_in"]
    check_out = arguments["check_out"]
    nights = (date.fromisoformat(check_out) - date.fromisoformat(check_in)).days
    if nights <= 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_stay_dates",
                "check_in": check_in,
                "check_out": check_out,
            },
        )
    return check_in, check_out, nights


def _resolve_poi(fixture: dict[str, Any], arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    poi_id = arguments.get("poi_id")
    if poi_id:
        poi = next((item for item in fixture["pois"] if item["poi_id"] == poi_id), None)
        if poi is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "poi_not_found", "poi_id": poi_id},
            )
        return poi, "poi_id"

    city = arguments.get("city")
    if city and _normalize_entity_name(city) != _normalize_entity_name(fixture["city"]):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "poi_not_found",
                "city": city,
                "poi_name": arguments.get("poi_name"),
                "reason": "city_out_of_scope",
            },
        )

    poi_name = arguments["poi_name"]
    target = _normalize_entity_name(poi_name)
    matches = []
    for item in fixture["pois"]:
        candidate_names = [item["name"], *(item.get("aliases") or [])]
        if target in {_normalize_entity_name(name) for name in candidate_names}:
            matches.append(item)

    if not matches:
        raise HTTPException(
            status_code=404,
            detail={"code": "poi_not_found", "city": city, "poi_name": poi_name},
        )
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ambiguous_poi_name",
                "city": city,
                "poi_name": poi_name,
                "candidates": [
                    {"poi_id": item["poi_id"], "name": item["name"]}
                    for item in matches
                ],
            },
        )
    return matches[0], "poi_name"


def _build_itinerary_draft(
    fixture: dict[str, Any], arguments: dict[str, Any]
) -> dict[str, Any]:
    if _normalize_entity_name(arguments["city"]) != _normalize_entity_name(
        fixture["city"]
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "itinerary_city_out_of_scope",
                "city": arguments["city"],
            },
        )

    poi_by_id = {poi["poi_id"]: poi for poi in fixture["pois"]}
    provided_candidate_ids = arguments.get("candidate_poi_ids")
    candidate_ids = list(provided_candidate_ids or poi_by_id)
    must_visit_ids = list(arguments.get("must_visit_poi_ids") or [])
    must_visit_set = set(must_visit_ids)
    exclude_ids = set(arguments.get("exclude_poi_ids") or [])
    referenced_ids = set(candidate_ids) | set(must_visit_ids) | exclude_ids
    unknown_ids = sorted(referenced_ids - set(poi_by_id))
    if unknown_ids:
        raise HTTPException(
            status_code=422,
            detail={"code": "unknown_candidate_poi", "poi_ids": unknown_ids},
        )

    outside_candidate_pool = sorted(must_visit_set - set(candidate_ids))
    if outside_candidate_pool:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "must_visit_outside_candidate_pool",
                "poi_ids": outside_candidate_pool,
            },
        )

    conflicting_ids = sorted(must_visit_set & exclude_ids)
    if conflicting_ids:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "conflicting_poi_constraints",
                "poi_ids": conflicting_ids,
            },
        )

    ordered_ids = [
        *must_visit_ids,
        *(poi_id for poi_id in candidate_ids if poi_id not in must_visit_set),
    ]
    selected_ids = [poi_id for poi_id in ordered_ids if poi_id not in exclude_ids]
    if not selected_ids:
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_itinerary_candidate_pool"},
        )

    day_count = int(arguments["days"])
    start_date_raw = arguments.get("start_date")
    parsed_start_date = date.fromisoformat(start_date_raw) if start_date_raw else None
    draft_days = [
        {
            "day": day_index + 1,
            "date": (
                (parsed_start_date + timedelta(days=day_index)).isoformat()
                if parsed_start_date
                else None
            ),
            "items": [],
        }
        for day_index in range(day_count)
    ]

    for index, poi_id in enumerate(selected_ids[: day_count * 3]):
        poi = poi_by_id[poi_id]
        day_index = index % day_count
        draft_days[day_index]["items"].append(
            {
                "poi_id": poi["poi_id"],
                "name": poi["name"],
                "open_time": poi["open_time"],
                "close_time": poi["close_time"],
                "suggested_duration_min": poi["suggested_duration_min"],
                "cost_cny": poi["cost_cny"],
                "evidence_id": poi["evidence_id"],
            }
        )

    return {
        "draft": {
            "city": fixture["city"],
            "start_date": start_date_raw,
            "days": draft_days,
            "preferences": arguments.get("preferences") or [],
            "budget": arguments.get("budget"),
            "unresolved_questions": (
                ["候选景点不足以覆盖全部行程天数"]
                if any(not day["items"] for day in draft_days)
                else []
            ),
        },
        "candidate_source": (
            "provided_poi_ids" if provided_candidate_ids else "catalog_default"
        ),
        "candidate_poi_ids": candidate_ids,
        "revision": fixture["revision"],
        "source": fixture["source"],
    }


def _mock_result(
    name: str, arguments: dict[str, Any], ctx: RequestContext
) -> dict[str, Any]:
    if name == "poi.search":
        fixture = json.loads(Path(get_settings().fixture_path).read_text(encoding="utf-8"))
        tags = set(arguments.get("tags") or [])
        indoor_only = arguments.get("indoor_only")
        matches = []
        for poi in fixture["pois"]:
            if tags and not tags.intersection(poi.get("tags") or []):
                continue
            if indoor_only is True and not poi.get("indoor"):
                continue
            matches.append({"poi_id": poi["poi_id"], "name": poi["name"], "tags": poi["tags"]})
        return {"matches": matches[:5], "source": fixture["source"], "revision": fixture["revision"]}
    if name == "poi.get_details":
        fixture = json.loads(Path(get_settings().fixture_path).read_text(encoding="utf-8"))
        poi, resolved_by = _resolve_poi(fixture, arguments)
        return {
            "poi": poi,
            "resolution": {
                "resolved_by": resolved_by,
                "input_value": arguments.get("poi_id") or arguments.get("poi_name"),
                "poi_id": poi["poi_id"],
                "canonical_name": poi["name"],
            },
            "revision": fixture["revision"],
        }
    if name == "weather.alerts":
        return {"city": arguments["city"], "date": arguments["date"], "alerts": [], "source": "training_fixture"}
    if name == "weather.forecast":
        is_rainy_fixture_date = arguments["start_date"] == "2026-09-08"
        return {
            "city": arguments["city"],
            "start_date": arguments["start_date"],
            "end_date": arguments["end_date"],
            "condition": "雷阵雨" if is_rainy_fixture_date else "多云",
            "high_c": 31 if is_rainy_fixture_date else 28,
            "low_c": 27 if is_rainy_fixture_date else 23,
            "wind_speed_kph": 24,
            "wind_gust_kph": 42,
            "wind_scale": 6,
            "source": "training_fixture",
        }
    if name == "mobility.search_ferry":
        fixture_path = Path(get_settings().tool_catalog_path).parent / "ferry-status.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        route = next(
            (
                item
                for item in fixture["routes"]
                if _normalize_pier_name(item["origin_pier"])
                == _normalize_pier_name(arguments["origin_pier"])
                and _normalize_pier_name(item["destination_pier"])
                == _normalize_pier_name(arguments["destination_pier"])
                and item["date"] == arguments["date"]
            ),
            None,
        )
        if route is None:
            return {
                "origin_pier": arguments["origin_pier"],
                "destination_pier": arguments["destination_pier"],
                "date": arguments["date"],
                "sailings": [],
                "source": fixture["source"],
                "revision": fixture["revision"],
            }

        sailings = route["sailings"]
        requested_time = arguments.get("departure_time")
        if requested_time:
            sailings = [item for item in sailings if item["departure_time"] == requested_time]
        requested_types = {
            item["type"] for item in (arguments.get("passenger_types") or [])
        }
        if requested_types:
            sailings = [
                {
                    **item,
                    "passenger_inventory": {
                        key: value
                        for key, value in item["passenger_inventory"].items()
                        if key in requested_types
                    },
                }
                for item in sailings
            ]
        return {
            "origin_pier": arguments["origin_pier"],
            "destination_pier": arguments["destination_pier"],
            "date": arguments["date"],
            "requested_departure_time": requested_time,
            "requested_passenger_types": sorted(requested_types),
            "sailings": sailings,
            "source": fixture["source"],
            "revision": fixture["revision"],
        }
    if name.startswith("mobility."):
        return {"options": [{"mode": arguments.get("mode", "any"), "duration_min": 35}], "source": "training_fixture"}
    if name == "stay.search_hotels":
        fixture = _hotel_fixture()
        if _normalize_entity_name(arguments["city"]) != _normalize_entity_name(fixture["city"]):
            return {
                "hotels": [],
                "city": arguments["city"],
                "check_in": arguments["check_in"],
                "check_out": arguments["check_out"],
                "guests": arguments["guests"],
                "source": fixture["source"],
                "revision": fixture["revision"],
            }
        check_in, check_out, nights = _hotel_dates(arguments)
        max_price = arguments.get("max_price")
        near_poi = _normalize_entity_name(arguments.get("near_poi", ""))
        hotels = []
        for hotel in fixture["hotels"]:
            if max_price is not None and hotel["nightly_cny"] > max_price:
                continue
            if near_poi and not any(
                near_poi in _normalize_entity_name(poi) for poi in hotel["near_poi"]
            ):
                continue
            if hotel["rooms_available"] <= 0:
                continue
            hotels.append(
                {
                    **hotel,
                    "check_in": check_in,
                    "check_out": check_out,
                    "nights": nights,
                    "guests": arguments["guests"],
                    "total_cny": hotel["nightly_cny"] * nights,
                }
            )
        return {
            "city": fixture["city"],
            "check_in": check_in,
            "check_out": check_out,
            "guests": arguments["guests"],
            "hotels": hotels,
            "source": fixture["source"],
            "revision": fixture["revision"],
        }
    if name == "stay.get_room_inventory":
        hotel = _hotel_by_id(arguments["hotel_id"])
        check_in, check_out, nights = _hotel_dates(arguments)
        return {
            "hotel_id": hotel["hotel_id"],
            "hotel_name": hotel["name"],
            "check_in": check_in,
            "check_out": check_out,
            "nights": nights,
            "room_type": arguments.get("room_type") or hotel["room_type"],
            "rooms_available": hotel["rooms_available"],
            "available": hotel["rooms_available"] > 0,
            "source": _hotel_fixture()["source"],
            "revision": _hotel_fixture()["revision"],
        }
    if name == "stay.calculate_member_price":
        hotel = _hotel_by_id(arguments["hotel_id"])
        check_in, check_out, nights = _hotel_dates(arguments)
        discounts = {"none": 0.0, "silver": 0.05, "gold": 0.1}
        member_level = arguments["member_level"]
        discount_rate = discounts[member_level]
        list_total = hotel["nightly_cny"] * nights
        return {
            "hotel_id": hotel["hotel_id"],
            "hotel_name": hotel["name"],
            "check_in": check_in,
            "check_out": check_out,
            "nights": nights,
            "guests": arguments["guests"],
            "member_level": member_level,
            "nightly_cny": hotel["nightly_cny"],
            "discount_rate": discount_rate,
            "list_total_cny": list_total,
            "total_cny": round(list_total * (1 - discount_rate), 2),
            "source": _hotel_fixture()["source"],
            "revision": _hotel_fixture()["revision"],
        }
    if name == "stay.search_hotel_policy":
        hotel = _hotel_by_id(arguments["hotel_id"])
        return {
            "hotel_id": hotel["hotel_id"],
            "hotel_name": hotel["name"],
            "check_in_policy": "入住时间 15:00 后，退房时间 12:00 前",
            "cancellation_policy": hotel["cancellation_policy"],
            "booking_policy": "预订结果以模拟库存和确认响应为准",
            "source": _hotel_fixture()["source"],
            "revision": _hotel_fixture()["revision"],
        }
    if name == "stay.create_booking":
        hotel = _hotel_by_id(arguments["hotel_id"])
        check_in, check_out, nights = _hotel_dates(arguments)
        if hotel["rooms_available"] <= 0:
            raise HTTPException(
                status_code=409,
                detail={"code": "room_inventory_unavailable", "hotel_id": hotel["hotel_id"]},
            )
        return {
            "accepted": True,
            "reservation_id": f"res-{hotel['hotel_id']}-{check_in}",
            "status": "confirmed",
            "hotel_id": hotel["hotel_id"],
            "hotel_name": hotel["name"],
            "room_type": arguments.get("room_type") or hotel["room_type"],
            "check_in": check_in,
            "check_out": check_out,
            "nights": nights,
            "guests": arguments["guests"],
            "owner_id": ctx.user_id,
            "source": _hotel_fixture()["source"],
            "revision": _hotel_fixture()["revision"],
            "simulated": True,
        }
    if name == "itinerary.build_draft":
        fixture = json.loads(Path(get_settings().fixture_path).read_text(encoding="utf-8"))
        return _build_itinerary_draft(fixture, arguments)
    if name == "itinerary.save":
        return {
            "accepted": True,
            "itinerary_id": arguments["itinerary_id"],
            "owner_id": ctx.user_id,
            "source": "training_fixture",
            "simulated": True,
        }
    if name == "booking.commit":
        product_id = arguments["product_id"]
        if product_id.startswith("xm-hotel-"):
            hotel = _hotel_by_id(product_id)
            return {
                "accepted": True,
                "reservation_id": f"res-{product_id}",
                "status": "confirmed",
                "product_id": product_id,
                "product_name": hotel["name"],
                "travelers": arguments["travelers"],
                "owner_id": ctx.user_id,
                "verification_required": True,
                "source": _hotel_fixture()["source"],
                "revision": _hotel_fixture()["revision"],
                "simulated": True,
            }
        return {
            "accepted": True,
            "reservation_id": f"res-{product_id}",
            "status": "confirmed",
            "product_id": product_id,
            "travelers": arguments["travelers"],
            "owner_id": ctx.user_id,
            "verification_required": True,
            "source": "training_fixture",
            "simulated": True,
        }
    if name.startswith("booking."):
        return {"accepted": True, "verification_required": tool_requires_verification(name), "source": "training_fixture"}
    return {"accepted": True, "arguments": arguments, "source": "training_fixture", "simulated": True}


def _simulated_latency_seconds(tool: dict[str, Any]) -> float:
    return {"fast": 0.04, "medium": 0.08, "slow": 0.12}[tool["latency_class"]]


def _without_transport_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_transport_metadata(item)
            for key, item in value.items()
            if key not in {"arguments", "simulated"}
        }
    if isinstance(value, list):
        return [_without_transport_metadata(item) for item in value]
    return value


def _concise_result(name: str, value: dict[str, Any]) -> dict[str, Any]:
    """Project verbose fixture responses into task fields plus evidence references."""
    result = _without_transport_metadata(value)
    source = result.pop("source", None)
    revision = result.pop("revision", None)
    if source and revision:
        result["evidence_ref"] = f"{source}@{revision}"
    elif source:
        result["source"] = source
    elif revision:
        result["revision"] = revision
    if name == "poi.search":
        result["matches"] = [
            {key: item[key] for key in ("poi_id", "name") if key in item}
            for item in value.get("matches") or []
        ]
    elif name == "poi.get_details" and isinstance(value.get("poi"), dict):
        result["poi"] = {
            key: value["poi"][key]
            for key in (
                "poi_id",
                "name",
                "open_time",
                "close_time",
                "indoor",
                "weather_sensitive",
                "tags",
                "evidence_id",
            )
            if key in value["poi"]
        }
    return result


def _result_contract_valid(name: str, data: dict[str, Any]) -> bool:
    required_by_name = {
        "poi.search": {"matches"},
        "poi.get_details": {"poi"},
        "weather.alerts": {"city", "date", "alerts"},
        "weather.forecast": {
            "city",
            "start_date",
            "end_date",
            "condition",
            "high_c",
            "low_c",
            "wind_speed_kph",
            "wind_gust_kph",
            "wind_scale",
        },
        "stay.search_hotels": {"hotels"},
        "stay.get_room_inventory": {"hotel_id", "rooms_available", "available"},
        "stay.calculate_member_price": {"hotel_id", "total_cny", "member_level"},
        "stay.search_hotel_policy": {"hotel_id", "cancellation_policy"},
        "stay.create_booking": {"accepted", "reservation_id", "status"},
        "itinerary.build_draft": {"draft", "candidate_source"},
    }
    if name == "poi.search":
        return "matches" in data and all(
            {"poi_id", "name"}.issubset(item) for item in data["matches"]
        )
    if name == "poi.get_details":
        return "poi" in data and {
            "poi_id",
            "name",
            "open_time",
            "close_time",
            "evidence_id",
        }.issubset(data["poi"])
    if name == "itinerary.build_draft":
        draft = data.get("draft") or {}
        days = draft.get("days") or []
        items = [item for day in days for item in (day.get("items") or [])]
        return (
            {"draft", "candidate_source"}.issubset(data)
            and ("revision" in data or "evidence_ref" in data)
            and bool(days)
            and bool(items)
            and all(
                {"poi_id", "name", "evidence_id"}.issubset(item)
                for item in items
            )
        )
    if name == "mobility.search_ferry":
        sailings = data.get("sailings") or []
        return (
            {"origin_pier", "destination_pier", "date", "sailings"}.issubset(data)
            and all(
                {
                    "departure_time",
                    "status",
                    "remaining",
                    "passenger_inventory",
                }.issubset(item)
                for item in sailings
            )
        )
    if name.startswith("mobility."):
        required = {"options"}
    elif name.startswith("booking."):
        required = {"accepted"}
    else:
        required = required_by_name.get(name, {"accepted"})
    return required.issubset(data)


def _validate_call(
    *,
    index: int,
    call: Any,
    tool_map: dict[str, dict[str, Any]],
    payload: ToolBatchExecuteIn,
    ctx: RequestContext,
) -> tuple[int, Any, dict[str, Any]]:
    tool = tool_map.get(call.name)
    if tool is None:
        raise HTTPException(status_code=404, detail={"code": "tool_not_found", "tool_name": call.name})
    if get_settings().environment not in tool["environments"]:
        raise HTTPException(status_code=403, detail={"code": "tool_not_available", "tool_name": call.name})
    if not set(tool["permissions"]).issubset(ctx.permissions):
        raise HTTPException(status_code=403, detail={"code": "permission_denied", "tool_name": call.name})
    if tool["requires_confirmation"] and not payload.approval_id:
        raise HTTPException(status_code=409, detail={"code": "approval_required", "tool_name": call.name})
    _validate_arguments(tool, call.arguments)
    if call.name == "itinerary.save":
        if ctx.user_id == "anonymous":
            raise HTTPException(
                status_code=401,
                detail={"code": "authenticated_user_required", "tool_name": call.name},
            )
        if not payload.current_itinerary_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "current_itinerary_required", "tool_name": call.name},
            )
        if call.arguments["itinerary_id"] != payload.current_itinerary_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "itinerary_context_mismatch",
                    "tool_name": call.name,
                    "requested_itinerary_id": call.arguments["itinerary_id"],
                },
            )
    return index, call, tool


def _execute_validated_call(
    item: tuple[int, Any, dict[str, Any]], response_mode: str, ctx: RequestContext
) -> dict[str, Any]:
    index, call, tool = item
    started = time.perf_counter()
    time.sleep(_simulated_latency_seconds(tool))
    data = _mock_result(call.name, call.arguments, ctx)
    if response_mode == "concise":
        data = _concise_result(call.name, data)
    result_contract_valid = _result_contract_valid(call.name, data)
    return {
        "index": index,
        "tool_name": call.name,
        "status": "ok",
        "risk_level": tool["risk_level"],
        "side_effect": tool["side_effect"],
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "result_contract_valid": result_contract_valid,
        "data": data,
    }


def tool_requires_verification(name: str) -> bool:
    tool = _tool_map().get(name)
    return bool(tool and tool["side_effect"] != "none")


def execute_batch(payload: ToolBatchExecuteIn, ctx: RequestContext) -> dict[str, Any]:
    tool_map = _tool_map()
    validated = [
        _validate_call(index=index, call=call, tool_map=tool_map, payload=payload, ctx=ctx)
        for index, call in enumerate(payload.calls)
    ]
    if payload.execution_mode == "parallel" and any(
        tool["side_effect"] != "none" for _, _, tool in validated
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "parallel_side_effect_forbidden",
                "message": "parallel mode only accepts side-effect-free calls",
            },
        )

    started = time.perf_counter()
    if payload.execution_mode == "parallel" and len(validated) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(validated))) as executor:
            results = list(
                executor.map(
                    lambda item: _execute_validated_call(item, payload.response_mode, ctx),
                    validated,
                )
            )
    else:
        results = [
            _execute_validated_call(item, payload.response_mode, ctx)
            for item in validated
        ]
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    return {
        "status": "ok",
        "catalog_revision": catalog()["revision"],
        "execution_mode": payload.execution_mode,
        "response_mode": payload.response_mode,
        "elapsed_ms": elapsed_ms,
        "result_chars": len(json.dumps(results, ensure_ascii=False)),
        "result_contract_valid": all(item["result_contract_valid"] for item in results),
        "results": results,
        "correlation_id": ctx.correlation_id,
        "simulated": True,
    }
