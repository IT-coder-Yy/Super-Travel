"""Ground trip stops in map results; the model chooses IDs, never coordinates."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import re
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from supertravel_server.mcp_client import ToolError
from supertravel_server.trip_routes import MODES, RouteSelectionExpired, TripLegError, leg_transport_mode, query_route, route_cache_key, route_contexts, route_fresh, route_matches, unknown_route


TransportMode = Literal["walking", "driving", "transit"]
StopCount = Annotated[int, Field(ge=1, le=8)]
DailyStopCounts = Annotated[list[StopCount | None], Field(min_length=1, max_length=7)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, allow_inf_nan=False)


class TripRequest(StrictModel):
    destination: str = Field(min_length=1, max_length=60)
    days: int = Field(ge=1, le=7)
    stops_per_day: StopCount | DailyStopCounts | None = Field(default=None, description="统一数量填整数；按天指定时按日期顺序填数组，长度等于 days，例如 [3,1]。没有数量要求的天填 null；全程未指定填 null。")
    map_provider: Literal["baidu", "amap"] | None = Field(default=None, description="用户明确只使用百度或高德时分别填baidu或amap，未限定为null。限定时本次仅查询该地图地点。")
    transport_mode: TransportMode = Field(default="walking", description="活动间交通方式，遵守用户选择；未指定时按步行估算并明确说明。")

    @model_validator(mode="after")
    def check_daily_counts(self):
        if isinstance(self.stops_per_day, list) and len(self.stops_per_day) != self.days:
            raise ValueError("逐日地点数量必须与行程天数一致。")
        return self


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


class TaxiEstimate(TransitFare):
    kind: Literal["estimate"] = "estimate"
    label: str | None = Field(default=None, min_length=1, max_length=100)
    note: str | None = Field(default=None, min_length=1, max_length=500)


class IncomingRoute(StrictModel):
    fromPlaceId: str = Field(min_length=1, max_length=200)
    toPlaceId: str = Field(min_length=1, max_length=200)
    mode: TransportMode
    status: Literal["verified", "unknown"]
    durationMinutes: int | None = Field(default=None, ge=1, le=2880)
    distanceMeters: int | None = Field(default=None, ge=0, le=3000000)
    provider: Literal["baidu", "amap"] | None = None
    queriedAt: str | None = Field(default=None, max_length=50)
    reason: str | None = Field(default=None, max_length=500)
    transit: TransitDetails | None = None
    taxiEstimates: list[TaxiEstimate] | None = Field(default=None, max_length=8)
    selection: Literal["user"] | None = None

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
        if self.taxiEstimates is not None and (self.mode == "walking" or self.status != "verified"):
            raise ValueError("taxi estimates need a verified transit or driving route")
        if self.selection is not None and (self.mode != "transit" or self.status != "verified"):
            raise ValueError("user selection needs a verified transit route")
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
    transportMode: TransportMode | None = None

    @model_validator(mode="after")
    def timestamp(self):
        if datetime.fromisoformat(self.generatedAt).tzinfo is None:
            raise ValueError("trip timestamp needs a timezone")
        return self


class IncomingTransportOverride(StrictModel):
    day: int = Field(ge=1, le=7)
    fromActivityId: str = Field(min_length=1, max_length=200)
    mode: TransportMode


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
    incomingTransportOverride: IncomingTransportOverride | None = None
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
            for index, activity in enumerate(day.activities):
                override = activity.incomingTransportOverride
                if override and (override.day != day.day or index == 0 or override.fromActivityId != day.activities[index - 1].id):
                    raise ValueError("单段方式与当前行程日或前序活动不符。")
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


class TripTimingError(TripLegError):
    pass


def check_transfers(days, transport_mode=None):
    for day in days:
        previous = None
        for activity in day["activities"]:
            route = activity.get("incomingRoute")
            if route is not None:
                if previous is None or not route_matches(route, previous["place"], activity["place"]):
                    raise TripTimingError("地点顺序已改变，请重新查询相邻活动间的交通。", activity["id"])
                mode = leg_transport_mode(activity, transport_mode)
                if route["mode"] != mode:
                    # Legacy snapshots may lack a declared mode. Ignore other modes' travel times.
                    if transport_mode or activity.get("incomingTransportOverride"):
                        raise TripTimingError("交通方式已改变，请重新查询相邻活动间的交通。", activity["id"])
                elif route["status"] == "verified" and route_fresh(route):
                    end = int(previous["time"][:2]) * 60 + int(previous["time"][3:]) + int(re.search(r"\d+", previous["duration"])[0])
                    start = int(activity["time"][:2]) * 60 + int(activity["time"][3:])
                    gap, needed = start - end, route["durationMinutes"]
                    if gap < needed:
                        raise TripTimingError(f"第 {day['day']} 天前往{activity['title']}的接驳至少需要 {needed} 分钟，"
                                             f"当前只预留 {gap} 分钟，还差 {needed - gap} 分钟，请调整时间。", activity["id"])
            previous = activity


SEARCH_TOOL = {"name": "search_trip_places", "description": "查询目的地内真实地点，取得可用于 submit_trip 的 place_id 和位置。优先百度；某服务失败可明确改用另一可用服务。", "inputSchema": {
    "type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 120},
    "provider": {"type": "string", "enum": ["baidu", "amap"]}}, "required": ["query"], "additionalProperties": False,
}}
ROUTE_TOOL = {"name": "query_trip_route", "description": "查询两个已验证地点间的接驳。编辑已有访问时传目标日期和两端活动 ID，以复用本段指定方式和路线；新路段沿用整程方式。失败返回 unknown，不传坐标。", "inputSchema": {
    "type": "object", "properties": {"from_place_id": {"type": "string", "minLength": 1, "maxLength": 200},
    "to_place_id": {"type": "string", "minLength": 1, "maxLength": 200},
    "day": {"type": "integer", "minimum": 1, "maximum": 7},
    "from_activity_id": {"type": "string", "minLength": 1, "maxLength": 200},
    "to_activity_id": {"type": "string", "minLength": 1, "maxLength": 200}, "mode": {"type": "string", "enum": ["walking", "driving", "transit"]}},
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
    def __init__(self, tools, destination: str, existing_days: list | None = None, *, map_provider: str | None = None,
                 existing_candidates: list | None = None, default_mode: str = "walking"):
        self.default_mode = default_mode
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
        self.routes: dict[str, dict] = {}  # Only evidence queried during this task can serve a new leg.
        self.unavailable: set[str] = set()
        contexts = route_contexts(existing_days)
        for day in existing_days or []:
            for activity in day.get("activities", []):
                try:
                    place = Place.model_validate(activity.get("place")).model_dump(exclude_none=True)
                    if map_provider and place["provider"] != map_provider:
                        continue
                    self.places[place["id"]] = place
                    identifier, number = activity.get("id"), day.get("day")
                    if not (isinstance(identifier, str) and 1 <= len(identifier.strip()) <= 200 and type(number) is int and 1 <= number <= 7):
                        continue
                    previous = {"id": identifier.strip(), "day": number, "place": place, "description": activity.get("description")}
                    if activity.get("cost") is not None:
                        previous["cost"] = ActivityCost.model_validate(activity["cost"]).model_dump()
                    self.activities[identifier.strip()] = previous
                    if identifier in contexts:
                        previous["from_id"] = contexts[identifier][1]
                        if activity.get("incomingTransportOverride"):
                            override = IncomingTransportOverride.model_validate(activity["incomingTransportOverride"])
                            if (override.day, override.fromActivityId) == (number, previous["from_id"]):
                                previous["incomingTransportOverride"] = override.model_dump()
                        if activity.get("incomingRoute"):
                            previous["incomingRoute"] = IncomingRoute.model_validate(activity["incomingRoute"]).model_dump(exclude_none=True)
                except (ValidationError, TypeError):
                    continue

        # Place-only queries must not guess between visits with different map coordinates.
        self.ambiguous_places: set[str] = set()
        known = {}
        for visit in (*self.activities.values(), *self.candidates.values()):
            if visit.get("destination", destination).removesuffix("市") == destination.removesuffix("市"):
                place = visit["place"]
                if known.setdefault(place["id"], place) != place:
                    self.ambiguous_places.add(place["id"])

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
        changed = {identifier for identifier, place in found.items() if identifier in self.places and self.places[identifier] != place}
        self.routes = {key: route for key, route in self.routes.items() if not changed.intersection((route["fromPlaceId"], route["toPlaceId"]))}
        self.places.update(found)
        self.ambiguous_places.difference_update(found)
        return list(found.values())

    def _route_place(self, place_id, activity_id):
        if activity_id is not None:
            visit = self.activities.get(activity_id) or self.candidates.get(activity_id)
            if (not visit or visit["place"]["id"] != place_id or
                    visit.get("destination", self.destination).removesuffix("市") != self.destination.removesuffix("市")):
                raise ToolError("tool_arguments", False, "活动 ID 必须对应当前行程或同城候选的地点。")
            return visit["place"]
        if place_id in self.ambiguous_places:
            raise ToolError("tool_arguments", False, "同一地点有多条不同的位置记录，请提供活动 ID 以确定本次访问。")
        return self.places.get(place_id) or next((item["place"] for item in self.candidates.values()
                   if item["place"]["id"] == place_id and item["destination"].removesuffix("市") == self.destination.removesuffix("市")), None)

    def _saved_leg(self, day, from_id, to_id, origin, destination):
        previous, target = self.activities.get(from_id), self.activities.get(to_id)
        if (target and previous and target.get("from_id") == from_id and previous["day"] == target["day"] == day
                and previous["place"] == origin and target["place"] == destination):
            return target
        return {}

    @staticmethod
    def _saved_route(leg, origin, destination, mode):
        route = leg.get("incomingRoute")
        if (not route_matches(route, origin, destination, mode) or
                route.get("provider") and not (route["provider"] == origin["provider"] == destination["provider"])):
            return None
        if route.get("selection") == "user" and not route_fresh(route):
            raise RouteSelectionExpired(f"第 {leg['day']} 天前往{destination['name']}的用户选用公交方案已过期，请在编辑中重新比较并选择；原草稿仍保留。", leg["id"])
        return route

    async def route(self, from_place_id: str, to_place_id: str, mode: str | None = None, *,
                    day: int | None = None, from_activity_id: str | None = None, to_activity_id: str | None = None) -> dict:
        origin = self._route_place(from_place_id, from_activity_id)
        destination = self._route_place(to_place_id, to_activity_id)
        if self.map_provider and any(place and place["provider"] != self.map_provider for place in (origin, destination)):
            raise ToolError("tool_arguments", False, "地点来源不符合用户指定的地图服务。")
        if day is None and (from_activity_id is not None or to_activity_id is not None):
            raise ToolError("tool_arguments", False, "按活动 ID 查询时请同时提供本段的目标行程日。")
        mode = mode or self.default_mode
        if not origin or not destination or from_place_id == to_place_id or mode not in MODES:
            raise ToolError("tool_arguments", False, "请先查询两个不同的真实地点，并选择有效交通方式。")
        leg = self._saved_leg(day, from_activity_id, to_activity_id, origin, destination)
        if day is not None:
            mode = leg_transport_mode(leg, self.default_mode)
        elif from_activity_id is None and to_activity_id is None:
            # A legacy place-only call may reuse evidence only for an unambiguous visit.
            matches = [visit for visit in self.activities.values() if visit["place"] == destination and
                       self.activities.get(visit.get("from_id"), {}).get("place") == origin]
            if len(matches) == 1 and leg_transport_mode(matches[0], self.default_mode) == mode:
                leg = matches[0]
        saved = self._saved_route(leg, origin, destination, mode)
        if saved and saved["status"] == "verified" and route_fresh(saved):
            return deepcopy(saved)
        key = route_cache_key(origin, destination, mode)
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
                    place_id = previous["place"]["id"] if previous else None
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
                                if identifier not in claimed and item["day"] == day.day and item["place"]["id"] == stop.place_id]
                    if len(same_day) == 1:
                        stop.activity_id = same_day[0]["id"]
                        claimed.add(stop.activity_id)
        days = []
        for day in planned.days:
            if not self.editing and not day.activities:
                raise ValueError("首次生成行程时每天至少需要安排一个活动。")
            counts = request.stops_per_day
            required = counts[day.day - 1] if isinstance(counts, list) else counts
            if required is not None and len(day.activities) != required:
                scope = f"第 {day.day} 天" if isinstance(counts, list) else "每天"
                raise ValueError(f"{scope}必须恰好安排 {required} 个不同地点，请按用户要求调整。")
            end, used, activities = 0, set(), []
            for stop in day.activities:
                candidate = self.candidates.get(stop.activity_id) if stop.activity_id else None
                if stop.place_id in used:
                    raise ValueError("同一天不应重复安排同一个地点。")
                start = int(stop.time[:2]) * 60 + int(stop.time[3:])
                if start < end or start + stop.duration_minutes > 24 * 60:
                    raise ValueError("活动时段重叠或跨越当天，请调整时间。")
                used.add(stop.place_id)
                end = start + stop.duration_minutes
                previous = candidate or (self.activities.get(stop.activity_id) if stop.activity_id else None)
                if stop.activity_id is None:
                    candidates = [item for identifier, item in self.activities.items() if identifier not in claimed and item["place"]["id"] == stop.place_id]
                    destinations = sum(item.activity_id is None and item.place_id == stop.place_id for target in planned.days for item in target.activities)
                    if len(candidates) == 1 and destinations == 1:
                        previous = candidates[0]
                    elif candidates:
                        raise ValueError("同一地点对应多个已有活动或多个目标安排，请提供 activity_id 明确要保留哪一项。")
                    if previous:
                        claimed.add(previous["id"])
                place = previous["place"] if previous else self.places.get(stop.place_id)
                if not previous and stop.place_id in self.ambiguous_places:
                    raise ValueError("新访问的地点有多条位置记录，请先用 search_trip_places 确认地点。")
                if not place:
                    raise ValueError("行程引用了未经地图查询验证的地点，请先 search_trip_places。")
                if request.map_provider and place["provider"] != request.map_provider:
                    raise ValueError("地点来源不符合用户指定的地图服务。")
                activities.append({"id": previous["id"] if previous else str(uuid4()), "title": place["name"], "time": stop.time,
                    "duration": f"约 {stop.duration_minutes} 分钟", "description": previous["description"] if previous and not stop.replace_description else stop.description,
                    "x": 0, "y": 0, "place": deepcopy(place)})
                if previous and "cost" in previous:
                    activities[-1]["cost"] = deepcopy(previous["cost"])
                if len(activities) > 1:
                    source, target = activities[-2:]
                    origin = source["place"]
                    leg = self._saved_leg(day.day, source["id"], target["id"], origin, place)
                    if "incomingTransportOverride" in leg:
                        target["incomingTransportOverride"] = deepcopy(leg["incomingTransportOverride"])
                    mode = leg_transport_mode(target, request.transport_mode)
                    route = self._saved_route(leg, origin, place, mode)
                    if not (route and route["status"] == "verified" and route_fresh(route)):
                        cached = self.routes.get(route_cache_key(origin, place, mode))
                        if cached and route_fresh(cached):
                            route = cached
                    target["incomingRoute"] = deepcopy(route) if route else unknown_route(origin, place, mode)
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
