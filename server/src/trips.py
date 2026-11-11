"""Ground trip stops in map results; the model chooses IDs, never coordinates."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from supertravel_server.mcp_client import ToolError
from supertravel_server.trip_routes import MODES, query_route, route_fresh, route_matches, unknown_route


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, allow_inf_nan=False)


class TripRequest(StrictModel):
    destination: str = Field(min_length=1, max_length=60)
    days: int = Field(ge=1, le=7)
    stops_per_day: int | None = Field(default=None, ge=1, le=8, description="用户指定每天的地点数；单日行程指定3个地点时必须为3。未指定为null。")
    map_provider: Literal["baidu", "amap"] | None = Field(default=None, description="用户明确只使用百度或高德时分别填baidu或amap，未限定为null。限定时本次仅查询该地图地点。")
    transport_mode: Literal["walking", "driving", "transit"] = Field(default="walking", description="活动间交通方式，遵守用户选择；未指定时按步行估算并明确说明。")


class SourceLocation(StrictModel):
    latitude: float = Field(ge=-85, le=85)
    longitude: float = Field(ge=-180, le=180)
    coordinateSystem: Literal["bd09ll", "gcj02"]


class ActivityCost(StrictModel):
    amountFen: int = Field(ge=0, le=100000000)
    currency: Literal["CNY"] = "CNY"
    source: Literal["user"] = "user"


class TransitSegment(StrictModel):
    mode: Literal["walking", "bus", "subway", "other"]
    lineName: str | None = Field(default=None, min_length=1, max_length=200)
    departureStop: str | None = Field(default=None, min_length=1, max_length=200)
    arrivalStop: str | None = Field(default=None, min_length=1, max_length=200)


class TransitFare(StrictModel):
    amountFen: int = Field(ge=0, le=100000000)
    currency: Literal["CNY"] = "CNY"


class TransitDetails(StrictModel):
    segments: list[TransitSegment] = Field(max_length=64)
    transferCount: int | None = Field(default=None, ge=0, le=63)
    fare: TransitFare | None = None


class IncomingRoute(StrictModel):
    fromPlaceId: str = Field(min_length=1, max_length=200)
    toPlaceId: str = Field(min_length=1, max_length=200)
    mode: Literal["walking", "driving", "transit"]
    status: Literal["verified", "unknown"]
    durationMinutes: int | None = Field(default=None, ge=1, le=2880)
    distanceMeters: int | None = Field(default=None, ge=0, le=3000000)
    provider: Literal["baidu", "amap"] | None = None
    queriedAt: str | None = Field(default=None, max_length=50)
    reason: str | None = Field(default=None, max_length=500)
    transit: TransitDetails | None = None

    @model_validator(mode="after")
    def complete_evidence(self):
        if self.queriedAt is not None and datetime.fromisoformat(self.queriedAt).tzinfo is None:
            raise ValueError("route timestamp needs a timezone")
        if self.status == "verified" and (self.durationMinutes is None or self.distanceMeters is None or not self.provider or not self.queriedAt):
            raise ValueError("verified route needs provider metrics and time")
        if self.status == "unknown" and (self.durationMinutes is not None or self.distanceMeters is not None):
            raise ValueError("unknown route cannot contain metrics")
        if self.transit is not None and (self.mode != "transit" or self.status != "verified"):
            raise ValueError("transit details need a verified transit route")
        return self


class Place(StrictModel):
    id: str = Field(pattern=r"^(baidu|amap):[A-Za-z0-9_-]{1,180}$")
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(max_length=500)
    latitude: float = Field(ge=-85, le=85)
    longitude: float = Field(ge=-180, le=180)
    coordinateSystem: Literal["wgs84"] = "wgs84"
    provider: Literal["baidu", "amap"]
    sourceLocation: SourceLocation | None = None

    @model_validator(mode="after")
    def matching_provider(self):
        if not self.id.startswith(self.provider + ":"):
            raise ValueError("place identifier and provider disagree")
        if self.sourceLocation and self.sourceLocation.coordinateSystem != ("bd09ll" if self.provider == "baidu" else "gcj02"):
            raise ValueError("source coordinate system and provider disagree")
        return self


class StopInput(StrictModel):
    place_id: str = Field(min_length=1, max_length=200)
    activity_id: str | None = Field(default=None, min_length=1, max_length=200, description="修改已有活动时引用 current_trip 中的活动 id；仅用户明确安排候选时可引用 current_candidates 的 id。跨日保持原 id，必须对应相同 place_id。")
    replace_description: bool = Field(default=False, description="仅当用户明确要求修改该活动备注时为 true；普通时间、顺序或日期调整保持 false。")
    replace_duration: bool = Field(default=False, description="仅用户明确要求修改候选的停留时长时为 true；候选普通排入保留原时长。已有日程活动仍使用 duration_minutes。")
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int = Field(ge=15, le=720)
    description: str = Field(min_length=1, max_length=500)


class DayInput(StrictModel):
    day: int = Field(ge=1, le=7)
    title: str = Field(min_length=1, max_length=100)
    subtitle: str = Field(min_length=1, max_length=200)
    activities: list[StopInput] = Field(max_length=8)


class TripInput(StrictModel):
    summary: str = Field(min_length=1, max_length=800)
    days: list[DayInput] = Field(min_length=1, max_length=7)


class TripMeta(StrictModel):
    destination: str = Field(min_length=1, max_length=60)
    summary: str = Field(min_length=1, max_length=800)
    generatedAt: str = Field(min_length=1, max_length=50)
    transportMode: Literal["walking", "driving", "transit"] | None = None

    @model_validator(mode="after")
    def timestamp(self):
        if datetime.fromisoformat(self.generatedAt).tzinfo is None:
            raise ValueError("trip timestamp needs a timezone")
        return self


class TripActivity(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration: str = Field(pattern=r"^约 \d{1,3} 分钟$")
    description: str = Field(min_length=1, max_length=500)
    x: float = 0
    y: float = 0
    place: Place
    incomingRoute: IncomingRoute | None = None
    cost: ActivityCost | None = None


class TripDay(StrictModel):
    day: int = Field(ge=1, le=7)
    title: str = Field(min_length=1, max_length=100)
    subtitle: str = Field(min_length=1, max_length=200)
    activities: list[TripActivity] = Field(max_length=8)


class TripResult(StrictModel):
    days: list[TripDay] = Field(min_length=1, max_length=7)
    trip: TripMeta

    @model_validator(mode="after")
    def consistent_days(self):
        if [day.day for day in self.days] != list(range(1, len(self.days) + 1)):
            raise ValueError("trip days must be consecutive")
        identifiers = set()
        for day in self.days:
            end, places = 0, set()
            for activity in day.activities:
                duration = int(re.search(r"\d+", activity.duration)[0])
                start = int(activity.time[:2]) * 60 + int(activity.time[3:])
                if not 15 <= duration <= 720 or start < end or start + duration > 1440:
                    raise ValueError("invalid or overlapping activity times")
                if activity.title != activity.place.name or activity.id in identifiers or activity.place.id in places:
                    raise ValueError("duplicate activity or mismatched place title")
                identifiers.add(activity.id)
                places.add(activity.place.id)
                end = start + duration
        if not identifiers:
            raise ValueError("整份行程至少需要保留一个活动。")
        return self


def trip_overview(days) -> str:
    entries = [f"第 {day['day']} 天 {len(day['activities'])} 项活动" if day["activities"] else f"第 {day['day']} 天自由安排" for day in days]
    return f"{len(days)} 天行程；" + "；".join(entries) + "。"


def validate_result(value: dict) -> dict:
    result = TripResult.model_validate(value).model_dump(exclude_none=True)
    check_transfers(result["days"], result["trip"].get("transportMode"))
    result["trip"]["summary"] = trip_overview(result["days"])
    return result


class TripTimingError(ValueError):
    pass


def check_transfers(days, transport_mode=None):
    for day in days:
        previous = None
        for activity in day["activities"]:
            route = activity.get("incomingRoute")
            if route is not None:
                if previous is None or not route_matches(route, previous["place"], activity["place"]):
                    raise TripTimingError("地点顺序已改变，请重新查询相邻活动间的交通。")
                if transport_mode and route["mode"] != transport_mode:
                    raise TripTimingError("交通方式已改变，请重新查询相邻活动间的交通。")
                if route["status"] == "verified" and route_fresh(route):
                    end = int(previous["time"][:2]) * 60 + int(previous["time"][3:]) + int(re.search(r"\d+", previous["duration"])[0])
                    start = int(activity["time"][:2]) * 60 + int(activity["time"][3:])
                    if start - end < route["durationMinutes"]:
                        raise TripTimingError(f"第 {day['day']} 天前往{activity['title']}的接驳至少需要 {route['durationMinutes']} 分钟，当前只预留 {start - end} 分钟，请调整时间。")
            previous = activity


SEARCH_TOOL = {"name": "search_trip_places", "description": "查询目的地内真实地点，取得可用于 submit_trip 的 place_id 和位置。优先百度；某服务失败可明确改用另一可用服务。", "inputSchema": {
    "type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 120},
    "provider": {"type": "string", "enum": ["baidu", "amap"]}}, "required": ["query"], "additionalProperties": False,
}}
ROUTE_TOOL = {"name": "query_trip_route", "description": "查询两个已验证地点间的实际路线距离与交通耗时；传地点ID，不传坐标。失败返回unknown。提交行程前应查询每段相邻地点交通；使用用户指定方式。", "inputSchema": {
    "type": "object", "properties": {"from_place_id": {"type": "string", "minLength": 1, "maxLength": 200},
    "to_place_id": {"type": "string", "minLength": 1, "maxLength": 200}, "mode": {"type": "string", "enum": ["walking", "driving", "transit"]}},
    "required": ["from_place_id", "to_place_id"], "additionalProperties": False,
}}
SUBMIT_TOOL = {"name": "submit_trip", "description": "完成行程：只能引用 available_places / search_trip_places 的真实 place_id；仅用户明确安排候选时可通过 current_candidates 的 activity_id 引用其真实地点。已有活动须引用 current_trip 的 activity_id；普通调整保留原备注，明确改备注才设置 replace_description=true。首次生成每天有活动，编辑可保留空日，整程至少一项。每天按时间升序且不重叠；未核实信息须注明待确认。", "inputSchema": TripInput.model_json_schema()}


def to_wgs84(longitude: float, latitude: float, provider: str) -> tuple[float, float]:
    # shortcut: mainland map-display approximation; use a surveyed conversion for navigation.
    # Adapted from https://github.com/wandergis/coordtransform (MIT; notice below).
    if provider == "baidu":
        x, y = longitude - .0065, latitude - .006
        scale = math.pi * 3000 / 180
        radius = math.hypot(x, y) - .00002 * math.sin(y * scale)
        angle = math.atan2(y, x) - .000003 * math.cos(x * scale)
        longitude, latitude = radius * math.cos(angle), radius * math.sin(angle)
    if not (73.66 < longitude < 135.05 and 3.86 < latitude < 53.55):
        return longitude, latitude
    x, y = longitude - 105, latitude - 35
    common = (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    dlat = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x)) + common
    dlng = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x)) + common
    dlat += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi) + 160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    dlng += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi) + 150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    radians, eccentricity, axis = latitude * math.pi / 180, .00669342162296594323, 6378245
    magic = 1 - eccentricity * math.sin(radians) ** 2
    dlat = dlat * 180 / (axis * (1 - eccentricity) / (magic * math.sqrt(magic)) * math.pi)
    dlng = dlng * 180 / (axis / math.sqrt(magic) * math.cos(radians) * math.pi)
    return round(longitude - dlng, 7), round(latitude - dlat, 7)


def _map_payload(result):
    candidates = [result.get("structuredContent")]
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                candidates.append(json.loads(block["text"]))
            except (ValueError, TypeError, KeyError):
                continue
    for candidate in candidates:
        if isinstance(candidate, dict) and ("results" in candidate or "pois" in candidate):
            return candidate
    return {}


class TripResearch:
    def __init__(self, tools, destination: str, existing_days: list | None = None, *, map_provider: str | None = None, existing_candidates: list | None = None):
        self.tools, self.destination = tools, destination
        self.map_provider = map_provider
        self.places: dict[str, dict] = {}
        self.editing = bool(existing_days)
        self.activities: dict[str, dict] = {}
        self.candidates: dict[str, dict] = {}
        self.submitted_candidate_ids: list[str] = []
        if existing_candidates:
            from supertravel_server.candidates import Candidate, validate_candidate_snapshot
            validate_candidate_snapshot(existing_days or [], existing_candidates)
            self.candidates = {item["id"]: Candidate.model_validate(item).model_dump(exclude_none=True) for item in existing_candidates}
        self.routes: dict[tuple[str, str, str], dict] = {}
        self.unavailable: set[str] = set()
        for day in existing_days or []:
            for activity in day.get("activities", []):
                try:
                    place = Place.model_validate(activity.get("place")).model_dump(exclude_none=True)
                    if map_provider is None or place["provider"] == map_provider:
                        self.places[place["id"]] = place
                        identifier, number = activity.get("id"), day.get("day")
                        if isinstance(identifier, str) and 1 <= len(identifier.strip()) <= 200 and type(number) is int and 1 <= number <= 7:
                            previous = {"id": identifier.strip(), "day": number, "place_id": place["id"], "description": activity.get("description")}
                            if activity.get("cost") is not None:
                                previous["cost"] = ActivityCost.model_validate(activity["cost"]).model_dump()
                            self.activities[identifier.strip()] = previous
                        if activity.get("incomingRoute") is not None:
                            route = IncomingRoute.model_validate(activity["incomingRoute"]).model_dump(exclude_none=True)
                            if route["toPlaceId"] == place["id"] and route["status"] == "verified" and route_fresh(route):
                                self.routes[route["fromPlaceId"], route["toPlaceId"], route["mode"]] = route
                except (ValidationError, TypeError):
                    continue

    async def search(self, query: str, provider: str | None = None) -> list[dict]:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
            raise ToolError("tool_arguments", False, "请输入 1～120 字的地点名称或类型。")
        if self.map_provider and provider and provider != self.map_provider:
            raise ToolError("tool_arguments", False, "此次行程已限定地图服务，不能改用其他供应商。")
        provider = self.map_provider or provider
        names = {tool["name"] for tool in await self.tools.list_tools()}
        routes = {"baidu": "baidu_map_search_places", "amap": "amap_maps_text_search"}
        provider = provider or next((key for key, name in routes.items() if name in names and key not in self.unavailable), None)
        if provider not in routes or routes[provider] not in names:
            raise ToolError("tool_configuration", False, "目前没有可用的地点查询服务，请检查地图连接。")
        if provider in self.unavailable:
            raise ToolError("tool_quota", False, "此地图服务本轮额度或权限不可用，请选择其他服务。")
        args = {"query": query.strip(), "region": self.destination} if provider == "baidu" else {"keywords": query.strip(), "city": self.destination}
        try:
            result = await self.tools.call_tool(routes[provider], args)
        except ToolError as error:
            if error.code in ("tool_quota", "tool_auth"):
                self.unavailable.add(provider)
            raise
        if result.get("isError"):
            raise ToolError("tool_result", False, "地图服务未完成地点查询。")
        payload = _map_payload(result)
        rows = payload.get("results" if provider == "baidu" else "pois", [])
        found = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            city = row.get("city" if provider == "baidu" else "cityname")
            if isinstance(city, str) and city and self.destination.removesuffix("市") not in city and city.removesuffix("市") not in self.destination:
                continue
            try:
                location = row["location"]
                lng, lat = (location["lng"], location["lat"]) if provider == "baidu" else location.split(",")
                if isinstance(lng, bool) or isinstance(lat, bool):
                    continue
                lng, lat = float(lng), float(lat)
                if not math.isfinite(lng) or not math.isfinite(lat) or not -180 <= lng <= 180 or not -85 <= lat <= 85:
                    continue
                source = {"latitude": lat, "longitude": lng, "coordinateSystem": "bd09ll" if provider == "baidu" else "gcj02"}
                lng, lat = to_wgs84(lng, lat, provider)
                address = row.get("address", "")
                place = Place(id=provider + ":" + row["uid" if provider == "baidu" else "id"], name=row["name"],
                              address=address if isinstance(address, str) else "", latitude=lat, longitude=lng, provider=provider, sourceLocation=source).model_dump(exclude_none=True)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            found[place["id"]] = place
            if len(found) == 10:
                break
        changed = {identifier for identifier, place in found.items() if identifier in self.places and self.places[identifier].get("sourceLocation") != place.get("sourceLocation")}
        self.routes = {key: route for key, route in self.routes.items() if not changed.intersection(key[:2])}
        self.places.update(found)
        return list(found.values())

    async def route(self, from_place_id: str, to_place_id: str, mode: str = "walking") -> dict:
        candidate_places = {item["place"]["id"]: item["place"] for item in self.candidates.values()
                            if item["destination"].removesuffix("市") == self.destination.removesuffix("市")}
        origin = self.places.get(from_place_id) or candidate_places.get(from_place_id)
        destination = self.places.get(to_place_id) or candidate_places.get(to_place_id)
        if self.map_provider and any(place and place["provider"] != self.map_provider for place in (origin, destination)):
            raise ToolError("tool_arguments", False, "地点来源不符合用户指定的地图服务。")
        if not origin or not destination or from_place_id == to_place_id or mode not in MODES:
            raise ToolError("tool_arguments", False, "请先查询两个不同的真实地点，并选择有效交通方式。")
        key = from_place_id, to_place_id, mode
        cached = self.routes.get(key)
        if cached and route_fresh(cached):
            return deepcopy(cached)
        self.routes[key] = await query_route(self.tools, origin, destination, mode, self.destination)
        return deepcopy(self.routes[key])

    def submit(self, arguments: dict, request: TripRequest) -> dict:
        self.submitted_candidate_ids = []
        try:
            planned = TripInput.model_validate(arguments)
        except ValidationError:
            raise ValueError("行程字段无效，请检查天数、时间与停留时长。") from None
        if [day.day for day in planned.days] != list(range(1, request.days + 1)):
            raise ValueError("行程天数与请求不一致，或日期编号不连续。")
        claimed = set()
        selected_candidates = []
        for day in planned.days:
            for stop in day.activities:
                if stop.activity_id is not None:
                    candidate = self.candidates.get(stop.activity_id)
                    previous = candidate or self.activities.get(stop.activity_id)
                    place_id = candidate["place"]["id"] if candidate else previous.get("place_id") if previous else None
                    if not previous or place_id != stop.place_id:
                        raise ValueError("activity_id 必须引用当前行程或候选池中相同地点的已有活动。")
                    if candidate:
                        if candidate["destination"].removesuffix("市") != request.destination.removesuffix("市"):
                            raise ValueError("候选地点不属于本次目的地，不能排入；候选仍会保留。")
                        selected_candidates.append(stop.activity_id)
                        if not stop.replace_duration:
                            stop.duration_minutes = int(re.search(r"\d+", candidate["duration"])[0])
                    if stop.activity_id in claimed:
                        raise ValueError("同一个 activity_id 不能在行程中重复引用。")
                    claimed.add(stop.activity_id)
        # Reserve unchanged-day visits before an earlier new visit can claim their identity.
        for day in planned.days:
            for stop in day.activities:
                if stop.activity_id is None:
                    same_day = [item for identifier, item in self.activities.items()
                                if identifier not in claimed and item["day"] == day.day and item["place_id"] == stop.place_id]
                    if len(same_day) == 1:
                        stop.activity_id = same_day[0]["id"]
                        claimed.add(stop.activity_id)
        days = []
        for day in planned.days:
            if not self.editing and not day.activities:
                raise ValueError("首次生成行程时每天至少需要安排一个活动。")
            if request.stops_per_day is not None and len(day.activities) != request.stops_per_day:
                raise ValueError(f"每天必须恰好安排 {request.stops_per_day} 个不同地点，请按用户要求调整。")
            end, used, activities = 0, set(), []
            for stop in day.activities:
                candidate = self.candidates.get(stop.activity_id) if stop.activity_id else None
                place = candidate["place"] if candidate else self.places.get(stop.place_id)
                if not place:
                    raise ValueError("行程引用了未经地图查询验证的地点，请先 search_trip_places。")
                if request.map_provider and place["provider"] != request.map_provider:
                    raise ValueError("地点来源不符合用户指定的地图服务。")
                if stop.place_id in used:
                    raise ValueError("同一天不应重复安排同一个地点。")
                start = int(stop.time[:2]) * 60 + int(stop.time[3:])
                if start < end or start + stop.duration_minutes > 24 * 60:
                    raise ValueError("活动时段重叠或跨越当天，请调整时间。")
                used.add(stop.place_id)
                end = start + stop.duration_minutes
                previous = candidate or (self.activities.get(stop.activity_id) if stop.activity_id else None)
                if stop.activity_id is None:
                    candidates = [item for identifier, item in self.activities.items() if identifier not in claimed and item["place_id"] == stop.place_id]
                    destinations = sum(item.activity_id is None and item.place_id == stop.place_id for target in planned.days for item in target.activities)
                    if len(candidates) == 1 and destinations == 1:
                        previous = candidates[0]
                    elif candidates:
                        raise ValueError("同一地点对应多个已有活动或多个目标安排，请提供 activity_id 明确要保留哪一项。")
                    if previous:
                        claimed.add(previous["id"])
                activities.append({"id": previous["id"] if previous else str(uuid4()), "title": place["name"], "time": stop.time,
                    "duration": f"约 {stop.duration_minutes} 分钟", "description": previous["description"] if previous and not stop.replace_description else stop.description,
                    "x": 0, "y": 0, "place": deepcopy(place)})
                if previous and "cost" in previous:
                    activities[-1]["cost"] = deepcopy(previous["cost"])
                if len(activities) > 1:
                    origin = activities[-2]["place"]
                    route = self.routes.get((origin["id"], place["id"], request.transport_mode))
                    activities[-1]["incomingRoute"] = deepcopy(route) if route and route_fresh(route) else unknown_route(origin, place, request.transport_mode)
            days.append({"day": day.day, "title": day.title, "subtitle": day.subtitle, "activities": activities})
        result = validate_result({"days": days, "trip": {"destination": request.destination, "summary": planned.summary,
                                      "generatedAt": datetime.now(timezone.utc).isoformat(), "transportMode": request.transport_mode}})
        self.submitted_candidate_ids = selected_candidates
        return result


COORDTRANSFORM_LICENSE = """The MIT License (MIT)
Copyright (c) 2015 记忆的残骸
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
