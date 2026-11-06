"""Preference decisions are checked once, then kept through the task."""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.model import ModelError
from backend.tests.test_agents import FakeModel, FakeTools, execute_weather, finish, reply, session_for
from backend.tests.test_preference_agents import DEFAULTS, applied, planned, user_value

REQUEST = "生成杭州1天行程，本次紧凑、全程预算5000元，不安排餐馆。"


def trip_plan(application):
    return reply(name="route_request", arguments={"kind": "plan", "content": "开始规划", "steps": ["查询并提交"],
        "trip": {"destination": "杭州", "days": 1}, "preference_application": application,
        "preference_summary": "模型声称没有采用家庭同行，不应作为实际采用摘要"})


def override():
    return applied(budgetTargetCny=user_value("5000.00", "全程预算5000元"), pace=user_value("packed", "本次紧凑"),
        dietaryNotes={"status": "not_applicable", "value": None, "evidence": {"source": "request", "quote": "不安排餐馆"}})


def check(application, *, request=REQUEST, preferences=None, history=None, previous=None):
    async def run():
        session, _ = session_for(FakeModel([trip_plan(application), trip_plan(application)]))
        try:
            return await session._route(request, history or [], travel_preferences=preferences or DEFAULTS, preference_application=previous)
        finally:
            await session.aclose()
    return asyncio.run(run())


def test_explicit_budget_pace_override_keeps_family_and_ignores_untrusted_summary():
    result = check(override())
    assert result.preference_summary == "预算目标：5000元（本次要求）；节奏：紧凑（本次要求）；饮食：本次不适用；同行：亲子（常用偏好）。预算为目标，非实际费用或达标保证；同行方式不推定人数。"
    assert result.preference_application.companionMode.value == "family"


@pytest.mark.parametrize("field", ["budgetTargetCny", "pace", "companionMode"])
def test_trip_cannot_drop_other_preferences_as_not_applicable_with_dietary_evidence(field):
    application = override()
    application[field] = deepcopy(application["dietaryNotes"])
    with pytest.raises(ModelError, match="偏好采用结果缺失或矛盾"):
        check(application)


@pytest.mark.parametrize("field,value", [("budgetTargetCny", "5000.00"), ("pace", "packed"), ("dietaryNotes", "重辣"), ("companionMode", "solo")])
def test_default_must_match_frozen_default_exactly(field, value):
    application = applied()
    application[field]["value"] = value
    with pytest.raises(ModelError):
        check(application)


@pytest.mark.parametrize("field,value", [("budgetTargetCny", 5000), ("budgetTargetCny", True), ("budgetTargetCny", "-1.00"),
    ("budgetTargetCny", "NaN"), ("budgetTargetCny", "1e3"), ("budgetTargetCny", "1.001"), ("budgetTargetCny", "1000000.01"),
    ("budgetTargetCny", "9999999.99"), ("pace", "fast"), ("companionMode", "children"), ("dietaryNotes", "x" * 1001), ("pace", None)])
def test_user_values_are_strict_and_bounded(field, value):
    application = applied(**{field: user_value(value, "本次紧凑")})
    with pytest.raises(ModelError):
        check(application)


@pytest.mark.parametrize("value", ["0.00", "0.01", "1000000.00"])
def test_budget_boundaries_keep_exact_yuan(value):
    result = check(applied(budgetTargetCny=user_value(value, value)), request=f"生成杭州1天，预算{value}元")
    assert result.preference_application.budgetTargetCny.value == value
    assert "预算目标：" + value.rstrip("0").rstrip(".") + "元" in result.preference_summary


@pytest.mark.parametrize("evidence,history", [
    (None, []), ({"source": "request", "quote": "用户没有说过"}, []),
    ({"source": "request", "quote": "  "}, []),
    ({"source": "history", "quote": "预算5000元"}, [{"role": "assistant", "content": "预算5000元"}]),
    ({"source": "history", "quote": "预算5000元"}, [{"role": "tool", "content": "预算5000元"}]),
    ({"source": "history", "quote": "预算5000元"}, [{"role": "user", "content": "预算3000元"}]),
])
def test_override_needs_actual_user_source(evidence, history):
    application = applied(budgetTargetCny={"status": "user", "value": "5000.00", "evidence": evidence})
    with pytest.raises(ModelError):
        check(application, history=history)


def test_confirmed_user_history_is_allowed_but_dietary_text_is_not_enum_translated():
    result = check(applied(budgetTargetCny=user_value("5000.00", "预算5000元", "history"), dietaryNotes=user_value("packed", "packed", "history")),
        history=[{"role": "user", "content": "已确认预算5000元，饮食备注packed。"}])
    assert "预算目标：5000元（已确认用户条件）" in result.preference_summary
    assert "饮食：packed（已确认用户条件）" in result.preference_summary


def opt_out():
    return {"global_opt_out": {"source": "request", "quote": "本次不用默认偏好"},
        **{key: {"status": "opt_out", "value": None, "evidence": None} for key in DEFAULTS if key != "budgetAmountFen"},
        "budgetTargetCny": {"status": "opt_out", "value": None, "evidence": None}}


def test_explicit_global_optout_and_new_user_condition_can_coexist():
    application = opt_out()
    application["budgetTargetCny"] = user_value("5000.00", "预算5000元")
    result = check(application, request="生成杭州1天，本次不用默认偏好，预算5000元")
    assert result.preference_summary.count("本次不使用默认") == 3
    assert "预算目标：5000元（本次要求）" in result.preference_summary


@pytest.mark.parametrize("field,decision", [("companionMode", {"status": "default", "value": "family"}),
    ("dietaryNotes", {"status": "not_applicable", "value": None, "evidence": {"source": "request", "quote": "不安排餐馆"}}),
    ("pace", {"status": "unset", "value": None})])
def test_global_optout_cannot_leave_default_or_local_discard(field, decision):
    application = opt_out()
    application[field] = decision
    with pytest.raises(ModelError):
        check(application, request=REQUEST + "本次不用默认偏好")


@pytest.mark.parametrize("mutation", ["missing", "null", "field_missing", "extra", "unset_nonempty", "optout_no_evidence", "unused_with_value", "default_with_evidence"])
def test_missing_or_contradictory_application_fails_before_tools(mutation):
    application = applied()
    if mutation in ("missing", "null"):
        application = None
    elif mutation == "field_missing":
        del application["companionMode"]
    elif mutation == "extra":
        application["unknown"] = "discard"
    elif mutation == "unset_nonempty":
        application["companionMode"] = {"status": "unset", "value": None}
    elif mutation == "optout_no_evidence":
        application["companionMode"] = {"status": "opt_out", "value": None}
    elif mutation == "unused_with_value":
        application["dietaryNotes"] = {"status": "not_applicable", "value": "清淡", "evidence": {"source": "request", "quote": "不安排餐馆"}}
    else:
        application["companionMode"]["evidence"] = {"source": "request", "quote": "不安排餐馆"}
    async def run():
        model = FakeModel([planned(application=False) if mutation == "missing" else trip_plan(application) for _ in range(2)])
        tools = FakeTools()
        session, events = session_for(model, tools)
        with pytest.raises(ModelError):
            await session.chat(REQUEST, travel_preferences=DEFAULTS)
        assert len(model.planner_calls) == 2
        assert not tools.calls and tools.listed == 0 and not events
        assert session.snapshot()["status"] == "idle"
        await session.aclose()
    asyncio.run(run())


def test_empty_defaults_use_unset_not_adopted_null():
    preferences = {"budgetAmountFen": None, "pace": None, "dietaryNotes": "  ", "companionMode": "family"}
    application = applied(**{key: {"status": "unset", "value": None} for key in ("budgetTargetCny", "pace", "dietaryNotes")})
    assert "预算目标：未设置" in check(application, preferences=preferences).preference_summary
    application["pace"]["status"] = "default"
    with pytest.raises(ModelError):
        check(application, preferences=preferences)


def test_replan_cannot_change_confirmed_application_even_with_original_evidence():
    async def run():
        async def failed_tool(_name, _arguments):
            raise RuntimeError("fixture failure")
        original = applied()
        changed = applied(budgetTargetCny=user_value("5000.00", "预算5000元"))
        model = FakeModel([planned(application=original), planned(application=changed), planned(application=changed)], execute_weather)
        session, events = session_for(model, FakeTools(failed_tool))
        await session.chat("预算5000元，查询杭州天气", travel_preferences=DEFAULTS)
        state = await finish(session)
        assert state["status"] == "failed" and "偏好采用结果缺失或矛盾" in state["error"]
        assert "预算目标：3000元（常用偏好）" in state["preference_summary"]
        assert json.loads(model.planner_calls[1][-1]["content"])["preference_application"] == original
        assert not model.summary_calls and len(model.executor_calls) == 1
        assert all("preference_application" not in event for event in events)
        await session.aclose()
    asyncio.run(run())


def test_old_model_without_preferences_still_runs_without_application():
    async def run():
        model = FakeModel([planned(application=False)], execute_weather)
        session, _ = session_for(model)
        await session.chat("查询杭州天气")
        state = await finish(session)
        assert state["status"] == "completed" and "preference_summary" not in state
        await session.aclose()
    asyncio.run(run())


def test_long_dietary_summary_is_explicitly_shortened_but_applied_value_stays_complete():
    notes = "清淡" * 500
    preferences = {**DEFAULTS, "dietaryNotes": notes}
    result = check(applied(dietaryNotes={"status": "default", "value": notes}), preferences=preferences)
    assert result.preference_application.dietaryNotes.value == notes
    assert "…（摘要已截断）" in result.preference_summary and len(result.preference_summary) <= 1000
