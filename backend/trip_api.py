"""Apply verified trips through the existing revisioned JSON store."""
from copy import deepcopy
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from backend.mcp_client import ToolError
from backend.trips import TripResearch


class TripRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)


class ResolvePlace(TripRevision):
    activity_id: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=100)


def _activities(days):
    return [activity for day in days or [] if isinstance(day, dict)
            for activity in day.get("activities", []) if isinstance(activity, dict)] if isinstance(days, list) else []


def guard_trip_write(previous, proposed, error_type):
    # Normal autosaves may edit a draft, but cannot introduce unverified coordinates or replace a formal trip.
    for key in ("trip", "previousTrip"):
        if proposed.get(key) != previous.get(key):
            raise error_type(422, "trip_protected", "正式行程需要通过生成、应用草稿或恢复操作保存。")
    if previous.get("trip") and proposed.get("days") != previous.get("days"):
        raise error_type(422, "trip_protected", "请在草稿中修改，再应用到正式行程。")
    if proposed.get("draftTrip") is not None and proposed["draftTrip"] not in (previous.get("draftTrip"), previous.get("trip")):
        raise error_type(422, "trip_protected", "草稿目的地需要由规划 Agent 确认。")
    known = {place["id"]: place for days in (previous.get("days"), previous.get("draft"), (previous.get("previousTrip") or {}).get("days"))
             for activity in _activities(days) if isinstance(place := activity.get("place"), dict) and "id" in place}
    for activity in _activities(proposed.get("days")) + _activities(proposed.get("draft")):
        place = activity.get("place")
        if place is not None and (not isinstance(place, dict) or not isinstance(place.get("id"), str) or place != known.get(place.get("id")) or activity.get("title") != place.get("name")):
            raise error_type(422, "place_unverified", "地点已变化，请先查询并确认新的地图位置。")


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
            activity.update(title=matches[0]["name"], place=matches[0], x=0, y=0)
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
                result = validate_result({"days": data["draft"], "trip": data.get("draftTrip") or data["trip"]})
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
