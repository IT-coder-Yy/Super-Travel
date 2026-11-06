"""Candidate snapshot helpers and bounded, non-reordering insertion checks."""
from copy import deepcopy
import asyncio
import json

from pydantic import Field, model_validator

from backend.trips import ActivityCost, Place, StrictModel
from backend.trip_routes import query_route, route_fresh, unknown_route


class Candidate(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=200)
    duration: str = Field(pattern=r"^约 \d{1,3} 分钟$")
    description: str = Field(min_length=1, max_length=500)
    place: Place
    cost: ActivityCost | None = None

    @model_validator(mode="after")
    def valid_candidate(self):
        if self.title != self.place.name or not 15 <= int(self.duration.split()[1]) <= 720:
            raise ValueError("candidate title or duration is invalid")
        return self


def has_draft(data):
    return data.get("draft") is not None or data.get("draftCandidates") is not None


def active_candidates(data):
    return data["draftCandidates"] if data.get("draftCandidates") is not None else data.get("candidates", [])


def normalize_candidate_fields(data):
    if not any(key in data for key in ("schema", "days", "trip", "candidates", "draftCandidates")):
        return data
    data.setdefault("candidates", [])
    data.setdefault("draftCandidates", deepcopy(data["candidates"]) if data.get("draft") is not None else None)
    previous = data.get("previousTrip")
    if isinstance(previous, dict):
        previous.setdefault("candidates", [])
    return data


def merge_candidate_fields(previous, proposed):
    """Omitted fields in a legacy autosave cannot erase a newer candidate pool."""
    proposed = deepcopy(proposed)
    if not any(key in snapshot for snapshot in (previous, proposed) for key in ("schema", "days", "trip", "candidates", "draftCandidates")):
        return proposed
    proposed.setdefault("candidates", deepcopy(previous.get("candidates", [])))
    if "draftCandidates" not in proposed:
        proposed["draftCandidates"] = (deepcopy(active_candidates(previous)) if proposed.get("draft") is not None
                                       else deepcopy(previous.get("draftCandidates")) if previous.get("draft") is None or previous.get("draftCandidates") else None)
    if isinstance(proposed.get("previousTrip"), dict) and "candidates" not in proposed["previousTrip"]:
        proposed["previousTrip"]["candidates"] = deepcopy((previous.get("previousTrip") or {}).get("candidates", []))
    return proposed


def validate_candidate_snapshot(days, pool):
    if not isinstance(pool, list):
        raise ValueError("candidate pool must be an array")
    identifiers = []
    for day in days if isinstance(days, list) else []:
        if isinstance(day, dict):
            for activity in day.get("activities", []) if isinstance(day.get("activities"), list) else []:
                if isinstance(activity, dict) and activity.get("id") is not None:
                    identifiers.append(activity["id"])
    for candidate in pool:
        Candidate.model_validate(candidate)
        identifiers.append(candidate["id"])
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("activity and candidate IDs must be unique within a snapshot")


def as_candidate(activity, destination):
    value = {key: deepcopy(activity[key]) for key in ("id", "title", "duration", "description", "place", "cost") if key in activity}
    value["destination"] = destination
    return Candidate.model_validate(value).model_dump(exclude_none=True)


def minutes(time):
    return int(time[:2]) * 60 + int(time[3:])


def clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


SLOT_QUERY_TIMEOUT = 30


async def insertion_options(tools, activities, candidate, duration, meta):
    """Return each gap with both real neighbor routes; unknown travel remains explicit."""
    mode = meta.get("transportMode") or "walking"
    deadline = asyncio.get_running_loop().time() + SLOT_QUERY_TIMEOUT
    cache = {}
    exhausted = False

    async def route(origin, target):
        nonlocal exhausted
        key = json.dumps([origin, target, mode], sort_keys=True, ensure_ascii=False)
        if key not in cache:
            if exhausted:
                value = unknown_route(origin, target, mode, "本次空档核查超时，交通耗时未知。")
            else:
                try:
                    async with asyncio.timeout_at(deadline):
                        value = await query_route(tools, origin, target, mode, meta["destination"])
                except TimeoutError:
                    exhausted = True
                    value = unknown_route(origin, target, mode, "本次空档核查超时，交通耗时未知。")
                # A stale upstream result must never narrow a slot as verified evidence.
                if value.get("status") == "verified" and not route_fresh(value):
                    value = unknown_route(origin, target, mode, "路线资料已过期，交通耗时未知。")
            cache[key] = value
        return deepcopy(cache[key])

    options = []
    for position in range(len(activities) + 1):
        before = activities[position - 1] if position else None
        after = activities[position] if position < len(activities) else None
        earliest = minutes(before["time"]) + int(before["duration"].split()[1]) if before else 0
        latest = minutes(after["time"]) - duration if after else 1440 - duration
        if earliest > latest:
            continue
        incoming = await route(before["place"], candidate["place"]) if before else None
        outgoing = await route(candidate["place"], after["place"]) if after else None
        warnings = []
        for label, evidence in (("前一活动到候选", incoming), ("候选到后一活动", outgoing)):
            if evidence is None:
                continue
            if evidence["status"] == "verified":
                if label == "前一活动到候选":
                    earliest += evidence["durationMinutes"]
                else:
                    latest -= evidence["durationMinutes"]
            else:
                warnings.append(f"{label}：{evidence.get('reason') or '交通耗时未知，请预留接驳时间。'}")
        earliest, latest = (earliest + 14) // 15 * 15, latest // 15 * 15
        if earliest <= latest:
            options.append({"earliest_start": clock(earliest), "latest_start": clock(latest), "position": position,
                            "warnings": warnings, "incoming": incoming, "outgoing": outgoing})
    return options
