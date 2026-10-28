"""Apply verified trips through the existing revisioned JSON store."""
import asyncio
from copy import deepcopy
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from backend.mcp_client import ToolError
from backend.trips import ActivityCost, IncomingRoute, TripResearch, TripResult, TripTimingError
from backend.trip_routes import route_fresh, route_matches, unknown_route


class TripRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)


class ResolvePlace(TripRevision):
    activity_id: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=100)


def _day_activities(days):
    return [[activity for activity in day["activities"] if isinstance(activity, dict)]
            for day in days if isinstance(day, dict) and isinstance(day.get("activities"), list)] if isinstance(days, list) else []


def _activities(days):
    return [activity for activities in _day_activities(days) for activity in activities]


def guard_trip_write(previous, proposed, error_type):
    # Normal autosaves may edit a draft, but cannot introduce unverified coordinates or replace a formal trip.
    for key in ("trip", "previousTrip"):
        if proposed.get(key) != previous.get(key):
            raise error_type(422, "trip_protected", "正式行程需要通过生成、应用草稿或恢复操作保存。")
    if previous.get("trip") and proposed.get("days") != previous.get("days"):
        raise error_type(422, "trip_protected", "请在草稿中修改，再应用到正式行程。")
    if proposed.get("draftTrip") is not None and proposed["draftTrip"] not in (previous.get("draftTrip"), previous.get("trip")):
        raise error_type(422, "trip_protected", "草稿目的地需要由规划 Agent 确认。")
    known = [place for days in (previous.get("days"), previous.get("draft"), (previous.get("previousTrip") or {}).get("days"))
             for activity in _activities(days) if isinstance(place := activity.get("place"), dict) and "id" in place]
    known_routes = [activity["incomingRoute"] for days in (previous.get("days"), previous.get("draft"), (previous.get("previousTrip") or {}).get("days"))
                    for activity in _activities(days) if activity.get("incomingRoute")]
    route_sources = [(target.get("incomingRoute"), origin.get("place"), target.get("place"))
                     for days in (previous.get("days"), previous.get("draft"), (previous.get("previousTrip") or {}).get("days"))
                     for activities in _day_activities(days) for origin, target in zip(activities, activities[1:])]
    for activity in _activities(proposed.get("days")) + _activities(proposed.get("draft")):
        place = activity.get("place")
        if place is not None and (not isinstance(place, dict) or not isinstance(place.get("id"), str) or place not in known or activity.get("title") != place.get("name")):
            raise error_type(422, "place_unverified", "地点已变化，请先查询并确认新的地图位置。")
        try:
            if activity.get("cost") is not None:
                ActivityCost.model_validate(activity["cost"])
            route = activity.get("incomingRoute")
            if route is not None:
                IncomingRoute.model_validate(route)
                if route.get("status") == "verified" and route not in known_routes:
                    raise ValueError
        except (ValueError, TypeError):
            raise error_type(422, "trip_evidence", "费用需要为用户填写的人民币金额；交通数据只能来自路线查询。") from None
    for days in (proposed.get("days"), proposed.get("draft")):
        for activities in _day_activities(days):
            origin = None
            for target in activities:
                route = target.get("incomingRoute")
                if route and route["status"] == "verified":
                    source, destination = (origin or {}).get("place"), target.get("place")
                    if not source or not destination or not any(route == known_route and source == old_source and destination == old_destination
                                                               for known_route, old_source, old_destination in route_sources):
                        raise error_type(422, "route_unverified", "地点位置或顺序已变化，请重新查询交通。")
                origin = target


def clear_changed_routes(days):
    for activities in _day_activities(days):
        previous = None
        for activity in activities:
            if activity.get("incomingRoute") is not None and (not previous or not activity.get("place") or not previous.get("place")
                    or not route_matches(activity["incomingRoute"], previous["place"], activity["place"])):
                activity.pop("incomingRoute", None)
            previous = activity


async def refresh_routes(tools, days, destination, transport_mode="walking"):
    clear_changed_routes(days)
    research = TripResearch(tools, destination, days)
    exhausted = False
    deadline = asyncio.get_running_loop().time() + 30
    for day in days:
        for previous, activity in zip(day["activities"], day["activities"][1:]):
            route = activity.get("incomingRoute")
            if route and route.get("status") == "verified" and route_matches(route, previous["place"], activity["place"], transport_mode) and route_fresh(route):
                continue
            mode = transport_mode
            origin, target = previous["place"], activity["place"]
            if exhausted:
                activity["incomingRoute"] = unknown_route(origin, target, mode, "本次路线核查超时，剩余交通耗时未知。")
                continue
            # Unknown results can be retried explicitly when applying the draft again.
            research.routes.pop((origin["id"], target["id"], mode), None)
            try:
                async with asyncio.timeout_at(deadline):
                    activity["incomingRoute"] = await research.route(origin["id"], target["id"], mode)
            except TimeoutError:
                exhausted = True
                activity["incomingRoute"] = unknown_route(origin, target, mode, "本次路线核查超时，交通耗时未知。")


def trip_router(agents, store, error_type):
    router = APIRouter(prefix="/api/conversations")

    def editable(identifier, revision):
        document = store.get(identifier)
        if document.revision != revision:
            raise error_type(409, "revision_conflict", "会话已被更新，请重新加载。", current_revision=document.revision)
        if agents._active(document.data):
            raise error_type(409, "trip_busy", "请等待当前任务结束或取消后，再修改行程。")
        return document

    @router.post("/{identifier}/resolve-place")
    async def resolve_place(identifier: UUID, payload: ResolvePlace):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            data = document.data
            meta = data.get("draftTrip") or data.get("trip")
            draft = deepcopy(data.get("draft"))
            activity = next((item for item in _activities(draft) if item.get("id") == payload.activity_id), None)
            if not meta or activity is None:
                raise error_type(409, "draft_missing", "请先打开真实行程的修改草稿。")
            query = payload.query.strip()
            if not query:
                raise error_type(422, "place_query", "请输入完整地点名称。")
            try:
                places = await TripResearch(agents.tools, meta["destination"]).search(query)
            except ToolError as error:
                raise error_type(502, error.code, error.message) from None
            matches = [place for place in places if place["name"].strip() == query]
            if len(matches) != 1:
                raise error_type(422, "place_ambiguous", "未找到唯一同名地点。请填写完整名称，或通过对话补充地址后调整；草稿仍保留。")
            previous_place = activity.get("place")
            activity.update(title=matches[0]["name"], place=matches[0], x=0, y=0)
            if not previous_place or previous_place["id"] != matches[0]["id"]:
                activity.pop("cost", None)
            if previous_place != matches[0]:
                for item in _activities(draft):
                    route = item.get("incomingRoute") or {}
                    if matches[0]["id"] in (route.get("fromPlaceId"), route.get("toPlaceId")):
                        item.pop("incomingRoute", None)
            clear_changed_routes(draft)
            return store.mutate(identifier, lambda current: current.update(draft=draft, draftTrip=meta), expected_revision=payload.expected_revision)

    @router.post("/{identifier}/trip/apply")
    async def apply_trip(identifier: UUID, payload: TripRevision):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            data = document.data
            if not data.get("draft") or not data.get("trip"):
                raise error_type(409, "draft_missing", "没有可应用的真实行程草稿。")
            from backend.trips import validate_result
            try:
                days = deepcopy(data["draft"])
                clear_changed_routes(days)
                meta = data.get("draftTrip") or data["trip"]
                TripResult.model_validate({"days": days, "trip": meta})
                await refresh_routes(agents.tools, days, meta["destination"], meta.get("transportMode") or "walking")
                result = validate_result({"days": days, "trip": meta})
            except TripTimingError as error:
                raise error_type(422, "trip_transfer_conflict", str(error)) from None
            except (ValueError, TypeError):
                raise error_type(422, "trip_invalid", "草稿尚未完整：请确认每个地点的位置、有效时间和每日安排后再应用。") from None
            def change(current):
                current.update(previousTrip={"days": current["days"], "trip": current["trip"]},
                               days=result["days"], trip=result["trip"], draft=None, draftTrip=None, selectedDay=1)
            return store.mutate(identifier, change, expected_revision=payload.expected_revision)

    @router.post("/{identifier}/trip/restore")
    async def restore_trip(identifier: UUID, payload: TripRevision):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            if document.data.get("draft") is not None:
                raise error_type(409, "draft_exists", "请先应用或放弃当前草稿，再恢复上一版。")
            previous = document.data.get("previousTrip")
            if not previous:
                raise error_type(409, "previous_trip_missing", "还没有可恢复的上一版行程。")
            from backend.trips import validate_result
            try:
                result = validate_result(previous)
            except (ValueError, TypeError):
                raise error_type(422, "trip_invalid", "上一版行程格式不完整，原文件保持不变。") from None
            def change(current):
                current.update(previousTrip={"days": current["days"], "trip": current["trip"]},
                               days=result["days"], trip=result["trip"], selectedDay=1)
            return store.mutate(identifier, change, expected_revision=payload.expected_revision)

    return router
