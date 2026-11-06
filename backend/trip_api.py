"""Apply verified trips through the existing revisioned JSON store."""
import asyncio
from copy import deepcopy
from secrets import token_urlsafe
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from backend.candidates import (Candidate, active_candidates, as_candidate, has_draft, insertion_options,
                                minutes, validate_candidate_snapshot)
from backend.mcp_client import ToolError
from backend.trips import ActivityCost, IncomingRoute, TripResearch, TripResult, TripTimingError, validate_result
from backend.trip_routes import route_fresh, route_matches, unknown_route


class TripRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)


class ResolvePlace(TripRevision):
    activity_id: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=100)


class SearchPlaces(TripRevision):
    query: str = Field(min_length=2, max_length=100)
    category: Literal["景点", "餐饮", "购物"] | None = None


class SelectPlace(TripRevision):
    selection_id: str = Field(min_length=1, max_length=100)
    day: int = Field(ge=1, le=7)
    activity_id: str | None = Field(default=None, min_length=1, max_length=200)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int = Field(ge=15, le=720)
    description: str | None = Field(default=None, max_length=500)


class TripActivityChange(TripRevision):
    activity_id: str = Field(min_length=1, max_length=200)


class CandidateChange(TripRevision):
    candidate_id: str = Field(min_length=1, max_length=200)


class CandidateFromPlace(TripRevision):
    selection_id: str = Field(min_length=1, max_length=100)
    duration_minutes: int = Field(ge=15, le=720)
    description: str | None = Field(default=None, max_length=500)


class CandidateSlots(CandidateChange):
    day: int = Field(ge=1, le=7)
    duration_minutes: int = Field(ge=15, le=720)


class CandidateInsert(TripRevision):
    option_id: str = Field(min_length=1, max_length=100)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class MoveActivity(TripActivityChange):
    target_day: int = Field(ge=1, le=7)
    target_index: int = Field(ge=0, le=7)
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


def _day_activities(days):
    return [[activity for activity in day["activities"] if isinstance(activity, dict)]
            for day in days if isinstance(day, dict) and isinstance(day.get("activities"), list)] if isinstance(days, list) else []


def _activities(days):
    return [activity for activities in _day_activities(days) for activity in activities]


def guard_trip_write(previous, proposed, error_type):
    if proposed.get("candidates", []) != previous.get("candidates", []):
        raise error_type(422, "trip_protected", "候选池正式版需要通过应用草稿或恢复操作保存。")
    try:
        validate_candidate_snapshot(proposed.get("days"), proposed.get("candidates", []))
        if has_draft(proposed):
            validate_candidate_snapshot(proposed.get("draft") if proposed.get("draft") is not None else proposed.get("days"), active_candidates(proposed))
    except (ValueError, TypeError, AttributeError):
        raise error_type(422, "candidate_invalid", "候选字段不完整、包含排期字段或与日程活动 ID 重复。") from None
    active_days = previous.get("draft") if previous.get("draft") is not None else previous.get("days")
    scheduled_ids = {item.get("id") for item in _activities(active_days)}
    prior_pool = active_candidates(previous)
    for activity in _activities(proposed.get("draft")):
        if activity.get("id") not in scheduled_ids and any(activity.get("id") == candidate["id"] or activity.get("place") == candidate["place"]
                                                           for candidate in prior_pool):
            raise error_type(422, "candidate_insert_required", "请先检查候选空档，再通过候选插入操作排入日程。")
    prior_candidates = [item for pool in (previous.get("candidates", []), previous.get("draftCandidates") or [],
                                         (previous.get("previousTrip") or {}).get("candidates", [])) for item in pool]
    for candidate in active_candidates(proposed):
        if not any(candidate["id"] == old["id"] and candidate["destination"] == old["destination"] and candidate["place"] == old["place"] for old in prior_candidates):
            raise error_type(422, "candidate_unverified", "请通过真实地点搜索或移入候选创建候选，不能修改候选目的地。")
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
    known.extend(item["place"] for item in prior_candidates)
    known_routes = [activity["incomingRoute"] for days in (previous.get("days"), previous.get("draft"), (previous.get("previousTrip") or {}).get("days"))
                    for activity in _activities(days) if activity.get("incomingRoute")]
    route_sources = [(target.get("incomingRoute"), origin.get("place"), target.get("place"))
                     for days in (previous.get("days"), previous.get("draft"), (previous.get("previousTrip") or {}).get("days"))
                     for activities in _day_activities(days) for origin, target in zip(activities, activities[1:])]
    for activity in _activities(proposed.get("days")) + _activities(proposed.get("draft")) + active_candidates(proposed):
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
    selections = {}
    slot_options = {}

    def editable(identifier, revision):
        document = store.get(identifier)
        if document.revision != revision:
            raise error_type(409, "revision_conflict", "会话已被更新，请重新加载。", current_revision=document.revision)
        if agents._active(document.data):
            raise error_type(409, "trip_busy", "请等待当前任务结束或取消后，再修改行程。")
        return document

    def draft_context(document):
        data = document.data
        meta = data.get("draftTrip") or data.get("trip")
        if not data.get("trip") or not meta or data.get("draft") is None:
            raise error_type(409, "draft_missing", "请先打开真实行程的修改草稿。")
        return meta, deepcopy(data["draft"])

    def candidate_context(document):
        data = document.data
        meta = data.get("draftTrip") or data.get("trip")
        if not data.get("trip") or not meta:
            raise error_type(409, "draft_missing", "请先生成真实正式行程，再使用候选池。")
        days = deepcopy(data["draft"] if data.get("draft") is not None else data.get("days", []))
        pool = deepcopy(active_candidates(data))
        try:
            validate_candidate_snapshot(days, pool)
        except (ValueError, TypeError, AttributeError):
            raise error_type(422, "candidate_invalid", "候选池格式不完整或活动 ID 重复，原文件已保留。") from None
        return meta, days, pool

    def find_candidate(pool, candidate_id, meta=None):
        candidate = next((item for item in pool if item["id"] == candidate_id), None)
        if candidate is None:
            raise error_type(422, "candidate_missing", "候选已不在当前候选池中，请重新选择。")
        if meta and candidate["destination"].removesuffix("市") != meta["destination"].removesuffix("市"):
            raise error_type(422, "candidate_destination", "该候选属于其他目的地，不能排入当前行程；候选仍保留。")
        return candidate

    def save_candidate_draft(identifier, revision, days, pool, meta, selected_day, check_days=False):
        try:
            validate_candidate_snapshot(days, pool)
            if check_days:
                clear_changed_routes(days)
                if not _activities(days):
                    raise error_type(422, "trip_empty", "正式行程至少需要保留一个活动，不能移出最后一个活动。")
                validate_result({"days": days, "trip": meta})
        except TripTimingError as error:
            raise error_type(422, "trip_transfer_conflict", str(error)) from None
        except (ValueError, TypeError):
            raise error_type(422, "trip_invalid", "草稿存在重叠、跨午夜或不完整活动，请调整后重试；候选仍保留。") from None
        return store.mutate(identifier, lambda current: current.update(draft=days, draftCandidates=pool, draftTrip=meta,
                            selectedDay=selected_day), expected_revision=revision)

    def check_duration(duration):
        if duration % 15:
            raise error_type(422, "trip_time_step", "新设置的停留时长请按 15 分钟填写。")

    def clear_options(identifier):
        for token, option in list(slot_options.items()):
            if option["identifier"] == identifier:
                slot_options.pop(token)

    def expire_options():
        for token, option in list(slot_options.items()):
            if option["expires_at"] <= monotonic() or option["tools"] is not agents.tools:
                slot_options.pop(token, None)

    def find_activity(days, activity_id):
        for day in days:
            for index, activity in enumerate(day["activities"]):
                if activity["id"] == activity_id:
                    return day, index, activity
        raise error_type(422, "activity_missing", "活动已不在当前草稿中，请重新选择。")

    def save_edited_draft(identifier, revision, days, meta, selected_day):
        clear_changed_routes(days)
        if not _activities(days):
            raise error_type(422, "trip_empty", "正式行程至少需要保留一个活动，不能删除最后一个活动。")
        try:
            validate_result({"days": days, "trip": meta})
        except TripTimingError as error:
            raise error_type(422, "trip_transfer_conflict", str(error)) from None
        except (ValueError, TypeError):
            raise error_type(422, "trip_invalid", "活动位置与时间不一致，或存在重叠、跨午夜及不完整活动；请调整后重试，原草稿已保留。") from None
        return store.mutate(identifier, lambda current: current.update(draft=days, draftTrip=meta, selectedDay=selected_day), expected_revision=revision)

    def replace_formal(current, result, pool, restore_favorites=()):
        old_ids = {activity["id"] for activity in _activities(current["days"]) + current.get("candidates", [])}
        new_ids = {activity["id"] for activity in _activities(result["days"]) + pool}
        favorites = current.get("favorites", [])
        previous = {"days": current["days"], "trip": current["trip"], "candidates": current.get("candidates", []),
                    "removedFavorites": [identifier for identifier in favorites if identifier in old_ids and identifier not in new_ids]}
        kept = [identifier for identifier in favorites if identifier in new_ids]
        for identifier in restore_favorites:
            if identifier not in kept:
                kept.append(identifier)
        current.update(previousTrip=previous, days=result["days"], trip=result["trip"],
                       favorites=kept, candidates=pool, draft=None, draftTrip=None, draftCandidates=None, selectedDay=1)

    @router.post("/{identifier}/trip/candidates/park")
    async def park_candidate(identifier: UUID, payload: TripActivityChange):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            meta, days, pool = candidate_context(document)
            day, index, activity = find_activity(days, payload.activity_id)
            if not activity.get("description", "").strip():
                raise error_type(400, "candidate_note_missing", "活动备注不能为空，请填写备注后再移入候选；原草稿已保留。")
            try:
                pool.append(as_candidate(activity, meta["destination"]))
            except (ValueError, TypeError):
                raise error_type(422, "place_unverified", "请先确认该活动的真实地点及完整信息，再移入候选。") from None
            day["activities"].pop(index)
            return save_candidate_draft(identifier, payload.expected_revision, days, pool, meta, day["day"], True)

    @router.post("/{identifier}/trip/candidates/from-place")
    async def candidate_from_place(identifier: UUID, payload: CandidateFromPlace):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            meta, days, pool = candidate_context(document)
            check_duration(payload.duration_minutes)
            expire_selections()
            selection = selections.get(identifier)
            if (not selection or selection["revision"] != document.revision or selection["destination"] != meta["destination"]
                    or payload.selection_id not in selection["places"]):
                raise error_type(409, "place_selection_expired", "地点搜索结果已失效，请重新搜索并选择；候选已保留。")
            place = deepcopy(selection["places"][payload.selection_id])
            pool.append({"id": str(uuid4()), "destination": meta["destination"], "title": place["name"], "place": place,
                         "duration": f"约 {payload.duration_minutes} 分钟", "description": (payload.description or "").strip() or "用户候选"})
            saved = save_candidate_draft(identifier, payload.expected_revision, days, pool, meta, document.data.get("selectedDay", 1))
            selections.pop(identifier, None)
            return saved

    @router.post("/{identifier}/trip/candidates/delete")
    async def delete_candidate(identifier: UUID, payload: CandidateChange):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            meta, days, pool = candidate_context(document)
            pool.remove(find_candidate(pool, payload.candidate_id))
            return save_candidate_draft(identifier, payload.expected_revision, days, pool, meta, document.data.get("selectedDay", 1))

    @router.post("/{identifier}/trip/candidates/slots")
    async def candidate_slots(identifier: UUID, payload: CandidateSlots):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            meta, days, pool = candidate_context(document)
            candidate = find_candidate(pool, payload.candidate_id, meta)
            if payload.duration_minutes != int(candidate["duration"].split()[1]):
                check_duration(payload.duration_minutes)
            clear_options(identifier)
            day = next((item for item in days if item["day"] == payload.day), None)
            if day is None:
                raise error_type(422, "trip_day_missing", "所选日期不在当前行程中。")
            response = {"revision": document.revision, "options": []}
            if len(day["activities"]) >= 8:
                return {**response, "reason": "当天已安排 8 个活动，请先减少活动。"}
            if any(item.get("place", {}).get("id") == candidate["place"]["id"] for item in day["activities"]):
                return {**response, "reason": "当天已安排同一地点，请选择其他日期。"}
            try:
                clear_changed_routes(days)
                validate_result({"days": days, "trip": meta})
            except (ValueError, TypeError):
                return {**response, "reason": "当前草稿存在时间冲突或不完整地点，请先修正日程再检查空档。"}
            expire_options()
            options = await insertion_options(agents.tools, day["activities"], candidate, payload.duration_minutes, meta)
            editable(identifier, payload.expected_revision)
            # Keep only one query per conversation; stale or superseded choices cannot be reused.
            for option in options:
                token = token_urlsafe(24)
                slot_options[token] = {**option, "identifier": identifier, "revision": document.revision, "candidate": candidate,
                                      "day": payload.day, "duration": payload.duration_minutes, "tools": agents.tools,
                                      "expires_at": monotonic() + 300}
                response["options"].append({key: value for key, value in {**option, "option_id": token}.items()
                                            if key not in ("incoming", "outgoing")})
            if not options:
                response["reason"] = "当天没有能容纳停留时间及已知相邻接驳的空档，请调整日期或时长。"
            return response

    @router.post("/{identifier}/trip/candidates/insert")
    async def insert_candidate(identifier: UUID, payload: CandidateInsert):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            meta, days, pool = candidate_context(document)
            expire_options()
            option = slot_options.get(payload.option_id)
            if not option or option["identifier"] != identifier or option["revision"] != document.revision:
                raise error_type(409, "candidate_option_expired", "插入选项已失效，请重新检查空档；候选仍保留。")
            candidate = find_candidate(pool, option["candidate"]["id"], meta)
            if candidate != option["candidate"] or any(route and route["status"] == "verified" and not route_fresh(route)
                                                       for route in (option["incoming"], option["outgoing"])):
                raise error_type(409, "candidate_option_expired", "候选或路线资料已变化，请重新检查空档；候选仍保留。")
            if minutes(payload.time) % 15 or not minutes(option["earliest_start"]) <= minutes(payload.time) <= minutes(option["latest_start"]):
                raise error_type(422, "candidate_time_invalid", "请选择返回空档内的 15 分钟整刻时间；候选仍保留。")
            day = next((item for item in days if item["day"] == option["day"]), None)
            if day is None or len(day["activities"]) >= 8 or option["position"] > len(day["activities"]):
                raise error_type(409, "candidate_option_expired", "目标日程已变化，请重新检查空档；候选仍保留。")
            activity = {key: deepcopy(value) for key, value in candidate.items() if key != "destination"}
            activity.update(time=payload.time, duration=f"约 {option['duration']} 分钟", x=0, y=0)
            if option["incoming"]:
                activity["incomingRoute"] = deepcopy(option["incoming"])
            if option["outgoing"]:
                day["activities"][option["position"]]["incomingRoute"] = deepcopy(option["outgoing"])
            day["activities"].insert(option["position"], activity)
            pool.remove(candidate)
            saved = save_candidate_draft(identifier, payload.expected_revision, days, pool, meta, day["day"], True)
            slot_options.pop(payload.option_id, None)
            return saved

    @router.post("/{identifier}/trip/discard")
    async def discard_trip(identifier: UUID, payload: TripRevision):
        async with agents.configuration_lock:
            editable(identifier, payload.expected_revision)
            return store.mutate(identifier, lambda current: current.update(draft=None, draftTrip=None, draftCandidates=None),
                                expected_revision=payload.expected_revision)

    @router.post("/{identifier}/trip/activities/move")
    async def move_activity(identifier: UUID, payload: MoveActivity):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            meta, draft = draft_context(document)
            source, index, activity = find_activity(draft, payload.activity_id)
            target = next((day for day in draft if day["day"] == payload.target_day), None)
            if target is None:
                raise error_type(422, "trip_day_missing", "所选日期不在当前行程中。")
            if payload.time != activity["time"] and int(payload.time[3:]) % 15:
                raise error_type(422, "trip_time_step", "新设置的开始时间请按 15 分钟填写。")
            source["activities"].pop(index)
            if len(target["activities"]) >= 8:
                raise error_type(422, "trip_day_full", "每天最多安排 8 个活动，请先调整目标日期。")
            if payload.target_index > len(target["activities"]):
                raise error_type(422, "trip_position_invalid", "目标位置已变化，请重新选择活动位置。")
            place_id = (activity.get("place") or {}).get("id")
            if place_id and any((item.get("place") or {}).get("id") == place_id for item in target["activities"]):
                raise error_type(422, "place_duplicate", "目标日期已安排这个地点，请选择其他日期。")
            if source is not target:
                activity.pop("incomingRoute", None)
            activity["time"] = payload.time
            target["activities"].insert(payload.target_index, activity)
            return save_edited_draft(identifier, payload.expected_revision, draft, meta, payload.target_day)

    @router.post("/{identifier}/trip/activities/delete-preview")
    async def preview_delete_activity(identifier: UUID, payload: TripActivityChange):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            _, draft = draft_context(document)
            day, index, activity = find_activity(draft, payload.activity_id)
            neighbors = day["activities"][index:index + 2]
            place = activity.get("place") or {}
            return {"revision": document.revision, "activity_id": activity["id"], "title": activity["title"], "day": day["day"],
                    "has_note": bool(activity.get("description", "").strip()), "is_favorite": activity["id"] in document.data.get("favorites", []),
                    "has_map_point": place.get("coordinateSystem") == "wgs84" and isinstance(place.get("latitude"), (float, int))
                                     and isinstance(place.get("longitude"), (float, int)) and -85 <= place["latitude"] <= 85 and -180 <= place["longitude"] <= 180,
                    "affected_routes": sum(bool(item.get("incomingRoute")) for item in neighbors)}

    @router.post("/{identifier}/trip/activities/delete")
    async def delete_activity(identifier: UUID, payload: TripActivityChange):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            meta, draft = draft_context(document)
            day, index, _ = find_activity(draft, payload.activity_id)
            day["activities"].pop(index)
            return save_edited_draft(identifier, payload.expected_revision, draft, meta, document.data.get("selectedDay", 1))

    def expire_selections():
        for key, selection in list(selections.items()):
            if selection["expires_at"] <= monotonic() or selection["tools"] is not agents.tools:
                selections.pop(key)

    @router.post("/{identifier}/trip/places/search")
    async def search_places(identifier: UUID, payload: SearchPlaces):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            meta, _, _ = candidate_context(document)
            query = payload.query.strip()
            if len(query) < 2:
                raise error_type(422, "place_query", "请输入至少两个字的地点名称或地址。")
            expire_selections()
            if payload.category:
                query = f"{query} {payload.category}"
            try:
                places = await TripResearch(agents.tools, meta["destination"]).search(query)
            except ToolError as error:
                raise error_type(502, error.code, error.message) from None
            # An autosave can finish while the external search is in flight.
            editable(identifier, payload.expected_revision)
            found = {token_urlsafe(24): place for place in places}
            selections[identifier] = {"revision": document.revision, "destination": meta["destination"],
                                     "tools": agents.tools, "expires_at": monotonic() + 300, "places": found}
            return {"results": [{"selection_id": token, "name": place["name"], "address": place["address"],
                                 "provider": place["provider"]} for token, place in found.items()],
                    "revision": document.revision}

    @router.post("/{identifier}/trip/places/select")
    async def select_place(identifier: UUID, payload: SelectPlace):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            meta, draft = draft_context(document)
            expire_selections()
            selection = selections.get(identifier)
            if (not selection or selection["revision"] != document.revision
                    or selection["destination"] != meta["destination"] or payload.selection_id not in selection["places"]):
                raise error_type(409, "place_selection_expired", "地点搜索结果已失效，请重新搜索并选择；草稿已保留。")
            place = deepcopy(selection["places"][payload.selection_id])
            day = next((item for item in draft if item.get("day") == payload.day), None)
            if day is None:
                raise error_type(422, "trip_day_missing", "所选日期不在当前行程中。")
            activity = next((item for item in day["activities"] if item.get("id") == payload.activity_id), None) if payload.activity_id else None
            if payload.activity_id and activity is None:
                raise error_type(422, "activity_missing", "待替换活动不在所选日期，请重新选择。")
            if activity is None and len(day["activities"]) >= 8:
                raise error_type(422, "trip_day_full", "每天最多安排 8 个活动，请先调整当天安排。")
            if any(item is not activity and (item.get("place") or {}).get("id") == place["id"] for item in day["activities"]):
                raise error_type(422, "place_duplicate", "当天已安排这个地点，请选择其他地点。")
            duration = f"约 {payload.duration_minutes} 分钟"
            if ((not activity or payload.time != activity["time"]) and int(payload.time[3:]) % 15
                    or (not activity or duration != activity["duration"]) and payload.duration_minutes % 15):
                raise error_type(422, "trip_time_step", "新设置的开始时间和停留时长请按 15 分钟填写。")
            if int(payload.time[:2]) * 60 + int(payload.time[3:]) + payload.duration_minutes > 1440:
                raise error_type(422, "trip_time_invalid", "活动结束时间不能跨越当天，请调整时间或时长。")
            if activity is None:
                activity = {"id": str(uuid4()), "description": "用户安排"}
                day["activities"].append(activity)
            previous_place = activity.get("place")
            activity.update(title=place["name"], place=place, time=payload.time, duration=duration, x=0, y=0)
            if payload.description and payload.description.strip():
                activity["description"] = payload.description.strip()
            if not previous_place or previous_place["id"] != place["id"]:
                activity.pop("cost", None)
            if previous_place != place:
                changed_ids = {place["id"]} | ({previous_place["id"]} if previous_place else set())
                for item in _activities(draft):
                    route = item.get("incomingRoute") or {}
                    if changed_ids.intersection((route.get("fromPlaceId"), route.get("toPlaceId"))):
                        item.pop("incomingRoute", None)
            day["activities"].sort(key=lambda item: item["time"])
            clear_changed_routes(draft)
            try:
                validate_result({"days": draft, "trip": meta})
            except TripTimingError as error:
                raise error_type(422, "trip_transfer_conflict", str(error)) from None
            except (ValueError, TypeError):
                raise error_type(422, "trip_invalid", "草稿存在重叠时间或不完整活动，请调整后重试；原草稿已保留。") from None
            saved = store.mutate(identifier, lambda current: current.update(draft=draft, draftTrip=meta), expected_revision=payload.expected_revision)
            selections.pop(identifier, None)
            return saved

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
            if not has_draft(data) or not data.get("trip"):
                raise error_type(409, "draft_missing", "没有可应用的真实行程草稿。")
            from backend.trips import validate_result
            try:
                days = deepcopy(data["draft"] if data.get("draft") is not None else data["days"])
                pool = deepcopy(active_candidates(data))
                validate_candidate_snapshot(days, pool)
                clear_changed_routes(days)
                meta = data.get("draftTrip") or data["trip"]
                TripResult.model_validate({"days": days, "trip": meta})
                await refresh_routes(agents.tools, days, meta["destination"], meta.get("transportMode") or "walking")
                result = validate_result({"days": days, "trip": meta})
            except TripTimingError as error:
                raise error_type(422, "trip_transfer_conflict", str(error)) from None
            except (ValueError, TypeError):
                raise error_type(422, "trip_invalid", "草稿尚未完整：请确认每个地点的位置、有效时间和每日安排后再应用。") from None
            return store.mutate(identifier, lambda current: replace_formal(current, result, pool), expected_revision=payload.expected_revision)

    @router.post("/{identifier}/trip/restore")
    async def restore_trip(identifier: UUID, payload: TripRevision):
        async with agents.configuration_lock:
            document = editable(identifier, payload.expected_revision)
            if has_draft(document.data):
                raise error_type(409, "draft_exists", "请先应用或放弃当前草稿，再恢复上一版。")
            previous = document.data.get("previousTrip")
            if not previous:
                raise error_type(409, "previous_trip_missing", "还没有可恢复的上一版行程。")
            from backend.trips import validate_result
            try:
                result = validate_result({"days": previous["days"], "trip": previous["trip"]})
                pool = deepcopy(previous.get("candidates", []))
                validate_candidate_snapshot(result["days"], pool)
                restored_ids = {activity["id"] for activity in _activities(result["days"]) + pool}
                removed_favorites = previous.get("removedFavorites", [])
                if not isinstance(removed_favorites, list) or any(not isinstance(item, str) or item not in restored_ids for item in removed_favorites):
                    raise ValueError("invalid restored favorite references")
            except (KeyError, ValueError, TypeError):
                raise error_type(422, "trip_invalid", "上一版行程格式不完整，原文件保持不变。") from None
            return store.mutate(identifier, lambda current: replace_formal(current, result, pool, removed_favorites), expected_revision=payload.expected_revision)

    return router
