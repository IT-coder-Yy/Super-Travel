"""Invalid planner preference declarations get one bounded correction before execution."""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.model import ModelError
from backend.tests.test_agents import FakeModel, FakeTools, reply, session_for
from backend.tests.test_preference_agents import DEFAULTS, applied, user_value
from backend.tests.test_preference_application import trip_plan
from backend.tests.test_trip_count_constraints import moved_context

REQUEST = "仅将第2天断桥从15:00改为15:30，其他条件保持。"
HISTORY = [{"role": "user", "content": "杭州2天，本次预算5000元，紧凑节奏，不安排餐馆。"}]


def confirmed():
    return applied(budgetTargetCny=user_value("5000.00", "预算5000元", "history"),
        pace=user_value("packed", "紧凑节奏", "history"),
        dietaryNotes={"status": "not_applicable", "value": None,
            "evidence": {"source": "history", "quote": "不安排餐馆"}})


def route(application, *, old_count=False):
    value = json.loads(trip_plan(application).message.tool_calls[0].function.arguments)
    value["trip"] = {"destination": "杭州", "days": 2, "stops_per_day": 2 if old_count else None}
    return reply(name="route_request", arguments=value)


@pytest.mark.parametrize("fault,label", [("source", "预算目标"), ("dropped", "同行"),
    ("enum", "同行"), ("missing", "同行")])
def test_invalid_declaration_is_corrected_once_without_changing_request_or_context(fault, label):
    async def run():
        correct = confirmed()
        bad = deepcopy(correct)
        if fault == "source":
            bad["budgetTargetCny"]["evidence"]["source"] = "request"
        elif fault == "dropped":
            bad["companionMode"] = deepcopy(bad["dietaryNotes"])
        elif fault == "enum":
            bad["companionMode"]["value"] = "children"
        else:
            del bad["companionMode"]
        model = FakeModel([route(bad), route(correct)])
        tools = FakeTools()
        session, events = session_for(model, tools)
        context = moved_context()
        before = deepcopy(context)
        try:
            result = await session._route(REQUEST, HISTORY, trip_context=context, travel_preferences=DEFAULTS)
            assert result.preference_application.model_dump() == correct
            assert result.trip.stops_per_day is None
            assert "亲子（常用偏好）" in result.preference_summary
            assert len(model.planner_calls) == 2
            first, second = [json.loads(call[-1]["content"]) for call in model.planner_calls]
            feedback = second.pop("planning_feedback")
            assert label in feedback
            if fault == "source":
                assert "source 必须为 history" in feedback
            assert first == second and context == before
            assert model.planner_calls[0][1:-1] == model.planner_calls[1][1:-1] == HISTORY
            assert not events and tools.listed == 0 and not tools.calls
            assert session.snapshot()["status"] == "idle"
        finally:
            await session.aclose()
    asyncio.run(run())


def test_repeated_invalid_evidence_retains_field_reason_and_starts_no_task():
    async def run():
        bad = confirmed()
        bad["budgetTargetCny"]["evidence"]["quote"] = "private-invalid-quote"
        model = FakeModel([route(bad), route(bad)])
        tools = FakeTools()
        session, events = session_for(model, tools)
        try:
            with pytest.raises(ModelError, match="预算目标.*依据不在用户原文中") as captured:
                await session.chat(REQUEST, HISTORY, trip_context=moved_context(), travel_preferences=DEFAULTS)
            assert captured.value.code == "agent_preferences"
            assert "private-invalid-quote" not in str(captured.value)
            assert len(model.planner_calls) == 2 and not events and tools.listed == 0 and not tools.calls
            assert session.snapshot()["status"] == "idle"
        finally:
            await session.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("count_first", [False, True])
def test_count_and_preference_corrections_share_a_single_retry(count_first):
    async def run():
        bad = confirmed()
        bad["companionMode"] = deepcopy(bad["dietaryNotes"])
        routes = [route(confirmed(), old_count=True), route(bad)]
        if not count_first:
            routes.reverse()
        model = FakeModel(routes)
        session, events = session_for(model)
        try:
            with pytest.raises(ModelError):
                await session._route(REQUEST, HISTORY, trip_context=moved_context(), travel_preferences=DEFAULTS)
            assert len(model.planner_calls) == 2 and not events
        finally:
            await session.aclose()
    asyncio.run(run())


def test_confirmed_application_is_unchanged_during_replan_recovery():
    async def run():
        original = confirmed()
        changed = deepcopy(original)
        changed["pace"] = {"status": "default", "value": "relaxed", "evidence": None}
        model = FakeModel([route(changed), route(original)])
        session, events = session_for(model)
        try:
            result = await session._route(REQUEST, HISTORY, failure="执行步骤无法完成", trip_context=moved_context(),
                travel_preferences=DEFAULTS, preference_application=original)
            assert result.preference_application.model_dump() == original
            context = json.loads(model.planner_calls[1][-1]["content"])
            assert context["preference_application"] == original and context["execution_failure"] == "执行步骤无法完成"
            assert "重规划不能改写已确认偏好" in context["planning_feedback"]
            assert not events
        finally:
            await session.aclose()
    asyncio.run(run())
