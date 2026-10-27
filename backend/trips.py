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

from backend.mcp_client import ToolError


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, allow_inf_nan=False)


class TripRequest(StrictModel):
    destination: str = Field(min_length=1, max_length=60)
    days: int = Field(ge=1, le=7)
    stops_per_day: int | None = Field(default=None, ge=1, le=8, description="用户指定每天的地点数；单日行程指定3个地点时必须为3。未指定为null。")
    map_provider: Literal["baidu", "amap"] | None = Field(default=None, description="用户明确只使用百度或高德时分别填baidu或amap，未限定为null。限定时本次仅查询该地图地点。")


class Place(StrictModel):
    id: str = Field(pattern=r"^(baidu|amap):[A-Za-z0-9_-]{1,180}$")
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(max_length=500)
    latitude: float = Field(ge=-85, le=85)
    longitude: float = Field(ge=-180, le=180)
    coordinateSystem: Literal["wgs84"] = "wgs84"
    provider: Literal["baidu", "amap"]

    @model_validator(mode="after")
    def matching_provider(self):
        if not self.id.startswith(self.provider + ":"):
            raise ValueError("place identifier and provider disagree")
        return self


class StopInput(StrictModel):
    place_id: str = Field(min_length=1, max_length=200)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int = Field(ge=15, le=720)
    description: str = Field(min_length=1, max_length=500)


class DayInput(StrictModel):
    day: int = Field(ge=1, le=7)
    title: str = Field(min_length=1, max_length=100)
    subtitle: str = Field(min_length=1, max_length=200)
    activities: list[StopInput] = Field(min_length=1, max_length=8)


class TripInput(StrictModel):
    summary: str = Field(min_length=1, max_length=800)
    days: list[DayInput] = Field(min_length=1, max_length=7)


class TripMeta(StrictModel):
    destination: str = Field(min_length=1, max_length=60)
    summary: str = Field(min_length=1, max_length=800)
    generatedAt: str = Field(min_length=1, max_length=50)

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


class TripDay(StrictModel):
    day: int = Field(ge=1, le=7)
    title: str = Field(min_length=1, max_length=100)
    subtitle: str = Field(min_length=1, max_length=200)
    activities: list[TripActivity] = Field(min_length=1, max_length=8)


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
        return self


def validate_result(value: dict) -> dict:
    return TripResult.model_validate(value).model_dump()


SEARCH_TOOL = {"name": "search_trip_places", "description": "查询目的地内真实地点，取得可用于 submit_trip 的 place_id 和位置。优先百度；某服务失败可明确改用另一可用服务。", "inputSchema": {
    "type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 120},
    "provider": {"type": "string", "enum": ["baidu", "amap"]}}, "required": ["query"], "additionalProperties": False,
}}
SUBMIT_TOOL = {"name": "submit_trip", "description": "完成行程：只能引用 available_places / search_trip_places 的真实 place_id。每天按时间升序，时段不得重叠。时间和停留为建议，未核实的费用、营业、预约须注明待确认。", "inputSchema": TripInput.model_json_schema()}


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
    def __init__(self, tools, destination: str, existing_days: list | None = None, *, map_provider: str | None = None):
        self.tools, self.destination = tools, destination
        self.map_provider = map_provider
        self.places: dict[str, dict] = {}
        self.unavailable: set[str] = set()
        for day in existing_days or []:
            for activity in day.get("activities", []):
                try:
                    place = Place.model_validate(activity.get("place")).model_dump()
                    if map_provider is None or place["provider"] == map_provider:
                        self.places[place["id"]] = place
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
                lng, lat = to_wgs84(lng, lat, provider)
                address = row.get("address", "")
                place = Place(id=provider + ":" + row["uid" if provider == "baidu" else "id"], name=row["name"],
                              address=address if isinstance(address, str) else "", latitude=lat, longitude=lng, provider=provider).model_dump()
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            found[place["id"]] = place
            if len(found) == 10:
                break
        self.places.update(found)
        return list(found.values())

    def submit(self, arguments: dict, request: TripRequest) -> dict:
        try:
            planned = TripInput.model_validate(arguments)
        except ValidationError:
            raise ValueError("行程字段无效，请检查天数、时间与停留时长。") from None
        if [day.day for day in planned.days] != list(range(1, request.days + 1)):
            raise ValueError("行程天数与请求不一致，或日期编号不连续。")
        days = []
        for day in planned.days:
            if request.stops_per_day is not None and len(day.activities) != request.stops_per_day:
                raise ValueError(f"每天必须恰好安排 {request.stops_per_day} 个不同地点，请按用户要求调整。")
            end, used, activities = 0, set(), []
            for stop in day.activities:
                place = self.places.get(stop.place_id)
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
                activities.append({"id": str(uuid4()), "title": place["name"], "time": stop.time,
                    "duration": f"约 {stop.duration_minutes} 分钟", "description": stop.description,
                    "x": 0, "y": 0, "place": deepcopy(place)})
            days.append({"day": day.day, "title": day.title, "subtitle": day.subtitle, "activities": activities})
        return validate_result({"days": days, "trip": {"destination": request.destination, "summary": planned.summary,
                                      "generatedAt": datetime.now(timezone.utc).isoformat()}})


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
