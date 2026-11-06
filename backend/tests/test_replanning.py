"""Replanning must retain its first failure without leaking history into later runs."""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.main import ConversationInput
from backend.tests.test_agents import FakeModel, finish, reply, route, session_for
from backend.tests.test_trip_api import app
from backend.tests.test_trip_count_constraints import EDIT_REQUEST, count_route, execute_time_edit, moved_context, send_and_finish
from backend.tests.test_trips import Maps, trip_route


def test_replan_diagnostics_persist_and_next_run_starts_clean(app):
    async def run():
        changed = reply(name="route_request", arguments={"kind": "plan", "content": "调整步骤", "steps": ["提交修改"],
            "trip": {"destination": "杭州", "days": 2, "map_provider": "amap"}})
        model = FakeModel([count_route(None), changed, count_route(None)])
        calls = 0
        def execute(messages):
            nonlocal calls
            calls += 1
            return reply(name="report_plan_issue", arguments={"reason": "固定反馈：请明确沿用原限制"}) if calls == 1 else execute_time_edit(messages)
        model.execute = execute
        service = app.state.agents
        service.model, service.tools = model, Maps(names=[])
        document = app.state.store.create(ConversationInput(title="重规划诊断持久化", data={
            "schema": 1, "days": [], "draft": None, "messages": [], "favorites": [], "candidates": []}))
        document = app.state.store.mutate(document.id, lambda data: data.update(**moved_context()))
        original = deepcopy(document.data)
        try:
            failed = await send_and_finish(service, document.id, EDIT_REQUEST)
            assert failed["status"] == "failed"
            saved = app.state.store.get(document.id)
            assert saved.data["agent"]["replan_diagnostics"] == [{
                "failure": "执行反馈：固定反馈：请明确沿用原限制",
                "trip_changes": {"map_provider": {"before": None, "after": "amap"}}}]
            assert saved.data["days"] == original["days"] and saved.data["draft"] is None
            completed = await send_and_finish(service, document.id, EDIT_REQUEST)
            assert completed["status"] == "completed"
            assert "replan_diagnostics" not in completed
            assert "replan_diagnostics" not in app.state.store.get(document.id).data["agent"]
        finally:
            await service.stop()
    asyncio.run(run())


def test_final_execution_failure_is_retained_when_replan_budget_is_exhausted():
    async def run():
        model = FakeModel([route(), route(), route()], lambda messages:
            reply(name="report_plan_issue", arguments={"reason": "固定反馈：必要信息不足"}))
        session, _ = session_for(model)
        try:
            await session.chat("查询天气")
            state = await finish(session)
            assert state["status"] == "failed" and state["replans"] == 2
            assert state["replan_diagnostics"] == [{"failure": "执行反馈：固定反馈：必要信息不足"}] * 3
        finally:
            await session.aclose()
    asyncio.run(run())

def test_replan_gets_exact_current_trip_request_and_keeps_completed_work():
    async def run():
        expected = {"destination": "杭州", "days": 2, "stops_per_day": None, "map_provider": "baidu", "transport_mode": "walking"}
        class FeedbackModel(FakeModel):
            async def chat(self, messages, tools=None, **kwargs):
                if tools and tools[0]["function"]["name"] == "route_request" and not self.routes:
                    context = json.loads(messages[-1]["content"])
                    locked = (context["current_run"] or {}).get("trip_request")
                    # Model inference without the execution contract is deliberately different.
                    proposed = locked or {**expected, "map_provider": None}
                    self.routes.append(trip_route(steps=["调整后提交"], **proposed))
                return await super().chat(messages, tools, **kwargs)
        def execute(messages):
            context = json.loads(messages[1]["content"])
            if context["step"] == "核对当前安排":
                return reply("当前安排已核对，不需要新增地点。")
            if context["step"] == "原提交步骤":
                return reply(name="report_plan_issue", arguments={"reason": "固定反馈：请保留已完成核对，调整剩余提交步骤"})
            assert context["completed_steps"] == [{"step": "核对当前安排", "content": "当前安排已核对，不需要新增地点。"}]
            assert context["trip_request"] == expected
            return execute_time_edit(messages)
        model = FeedbackModel([trip_route(steps=["核对当前安排", "原提交步骤"], **expected)], execute)
        tools = Maps(names=[])
        session, _ = session_for(model, tools)
        try:
            await session.chat(EDIT_REQUEST, trip_context=moved_context())
            state = await finish(session)
            assert state["status"] == "completed", state["error"]
            assert state["trip_request"] == expected and state["replans"] == 1
            feedback = json.loads(model.planner_calls[-1][-1]["content"])
            assert feedback["current_run"]["trip_request"] == expected
            assert len(state["results"]) == 2 and len(model.executor_calls) == 3 and not tools.calls
            assert [len(day["activities"]) for day in state["trip_result"]["days"]] == [1, 3]
            assert state["trip_result"]["days"][1]["activities"][-1]["time"] == "15:15"
            assert state["replan_diagnostics"] == [{"failure": "执行反馈：固定反馈：请保留已完成核对，调整剩余提交步骤"}]
        finally:
            await session.aclose()
    asyncio.run(run())

@pytest.mark.parametrize("field,value", [("destination", "苏州"), ("days", 3), ("stops_per_day", None),
                                        ("map_provider", "amap"), ("transport_mode", "driving")])
def test_replan_cannot_change_any_locked_trip_field(field, value):
    async def run():
        expected = {"destination": "杭州", "days": 2, "stops_per_day": 1, "map_provider": "baidu", "transport_mode": "walking"}
        def planned(trip):
            return reply(name="route_request", arguments={"kind": "plan", "content": "核对后提交", "steps": ["提交行程"],
                "trip": trip, "stops_per_day_evidence": "每天恰好1个地点"})
        model = FakeModel([planned(expected), planned({**expected, field: value})], lambda messages:
            reply(name="report_plan_issue", arguments={"reason": "固定反馈：请调整步骤"}))
        session, _ = session_for(model, Maps(names=[]))
        try:
            await session.chat("本次改为每天恰好1个地点。", trip_context=moved_context())
            state = await finish(session)
            assert state["status"] == "failed" and "改变了目的地" in state["error"]
            assert state["trip_request"] == expected
            assert state["replan_diagnostics"][-1]["trip_changes"] == {field: {"before": expected[field], "after": value}}
            assert len(model.executor_calls) == 1 and "trip_result" not in state
        finally:
            await session.aclose()
    asyncio.run(run())


def test_replan_cannot_omit_the_locked_trip_request():
    async def run():
        model = FakeModel([count_route(None), route()], lambda messages:
            reply(name="report_plan_issue", arguments={"reason": "固定反馈：提交格式需要调整"}))
        session, _ = session_for(model, Maps(names=[]))
        try:
            await session.chat(EDIT_REQUEST, trip_context=moved_context())
            state = await finish(session)
            assert state["status"] == "failed" and "改变了目的地" in state["error"]
            assert state["replan_diagnostics"][-1]["trip_changes"] == {
                "destination": {"before": "杭州", "after": None}, "days": {"before": 2, "after": None},
                "transport_mode": {"before": "walking", "after": None}}
            assert len(model.executor_calls) == 1 and "trip_result" not in state
        finally:
            await session.aclose()
    asyncio.run(run())