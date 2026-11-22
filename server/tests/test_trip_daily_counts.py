"""First-time itinerary counts are scoped to the days the user requested."""
import asyncio

import pytest
from pydantic import ValidationError

from supertravel_server.conversations import ConversationInput
from supertravel_server.trips import TripRequest
from .test_agents import FakeModel, finish, reply, route, session_for
from .test_trip_api import app
from .test_trip_count_constraints import fixed_maps, initial_submission, send_and_finish
from .test_trips import trip_route


def planned_stops(counts):
    result = initial_submission()
    stops = [stop for day in result["days"] for stop in day["activities"]]
    for index, day in enumerate(result["days"]):
        visits = stops[index:] + stops[:index]
        day["activities"] = [
            {**stop, "time": f"{9 + hour * 3:02d}:00"}
            for hour, stop in enumerate(visits[:counts[index]])
        ]
    return result


@pytest.mark.parametrize(("request_text", "proposed", "submitted", "accepted", "error"), [
    ("杭州两天。第一天严格三站：地点已给。第二天仅一站：按原时间。共4个活动。", 3, [3, 1], [3, 1], None),
    ("杭州两天，第一天三站、第二天一站。", 3, [3, 3], [3, 1], "第 2 天必须恰好安排 1"),
    ("杭州两天，第 1 天 3 站，第 2 天 1 站。", None, [1, 3], [3, 1], "第 1 天必须恰好安排 3"),
    ("杭州两天，每天三站。", 3, [3, 3], 3, None),
    ("杭州两天，每天三站。", None, [3, 1], 3, "每天必须恰好安排 3"),
    ("杭州两天，每天三站。", [3, 1], [3, 4], 3, "每天必须恰好安排 3"),
    ("杭州两天，每天三站。第一天早点出发。", [3, None], [3, 1], 3, "每天必须恰好安排 3"),
    ("杭州两天，第二天只安排一个景点。", 1, [2, 1], [None, 1], None),
])
def test_first_plan_checks_counts_before_saving(app, request_text, proposed, submitted, accepted, error):
    async def run():
        service = app.state.agents

        def execute(messages):
            if messages[-1]["role"] == "tool":
                return reply(name="submit_trip", arguments=planned_stops(submitted))
            return reply(name="search_trip_places", arguments={"query": "固定测试地点"})

        model = FakeModel([trip_route(days=2, stops_per_day=proposed), route("clarification")], execute)
        service.model, service.tools = model, fixed_maps()
        document = app.state.store.create(ConversationInput(title="站数", data={
            "schema": 1, "days": [], "draft": None, "messages": [], "favorites": []}))
        try:
            state = await send_and_finish(service, document.id, request_text)
            saved = app.state.store.get(document.id).data
            assert state["trip_request"]["stops_per_day"] == accepted
            assert saved["draft"] is None
            if error is None:
                assert state["status"] == "completed" and state["replans"] == 0
                assert [len(day["activities"]) for day in saved["days"]] == submitted
            else:
                assert state["status"] == "needs_input" and not saved["days"]
                assert error in state["replan_diagnostics"][0]["failure"]
        finally:
            await service.stop()
    asyncio.run(run())


@pytest.mark.parametrize("counts", [[3], [3, 1, 2], [3, 0], [9, 1], [True, 1], ["3", 1]])
def test_daily_counts_match_trip_days_and_existing_limits(counts):
    with pytest.raises(ValidationError):
        TripRequest(destination="杭州", days=2, stops_per_day=counts)


def test_replan_cannot_swap_locked_day_counts():
    async def run():
        model = FakeModel([trip_route(days=2, stops_per_day=[3, 1]), trip_route(days=2, stops_per_day=[1, 3])],
                          lambda messages: reply(name="report_plan_issue", arguments={"reason": "修订查询步骤"}))
        session, _ = session_for(model, fixed_maps())
        try:
            await session.chat("杭州两天，第一天三站、第二天一站。")
            state = await finish(session)
            assert state["status"] == "failed" and "trip_result" not in state
            assert state["replan_diagnostics"][0]["trip_changes"] == {
                "stops_per_day": {"before": [3, 1], "after": [1, 3]}}
        finally:
            await session.aclose()
    asyncio.run(run())


def test_complex_day_wording_corrects_scalar_before_any_lookup():
    async def run():
        first = trip_route(days=2, stops_per_day=3)
        corrected = trip_route(days=2, stops_per_day=[3, 1])
        model = FakeModel([first, corrected])
        maps = fixed_maps()
        session, _ = session_for(model, maps)
        try:
            result = await session._route("杭州第一天选三个艺术馆，次日只去一座古塔。", [])
            assert result.trip.stops_per_day == [3, 1]
            assert len(model.planner_calls) == 2 and not model.executor_calls and not maps.calls
        finally:
            await session.aclose()
    asyncio.run(run())


@pytest.mark.parametrize(("text", "proposed", "expected"), [
    ("杭州两天，第一天最多三个景点，第二天不一定去一个。", None, None),
    ("杭州两天，全程三个景点。", None, None),
    ("杭州两天，第一天三站，第二天一站。", [3, 3], [3, 1]),
])
def test_only_exact_day_counts_are_bound(text, proposed, expected):
    async def run():
        session, _ = session_for(FakeModel([trip_route(days=2, stops_per_day=proposed)]))
        try:
            result = await session._route(text, [])
            assert result.trip.stops_per_day == expected
        finally:
            await session.aclose()
    asyncio.run(run())


def test_conflicting_counts_clarify_without_research():
    async def run():
        model = FakeModel([trip_route(days=2, stops_per_day=3), route("clarification")])
        tools = fixed_maps()
        session, _ = session_for(model, tools)
        try:
            result = await session.chat("杭州两天，每天三站。第一天三站，第二天一站。")
            assert result["kind"] == "clarification"
            assert len(model.planner_calls) == 2 and not model.executor_calls and not tools.calls
            assert session.snapshot()["status"] == "idle"
        finally:
            await session.aclose()
    asyncio.run(run())
