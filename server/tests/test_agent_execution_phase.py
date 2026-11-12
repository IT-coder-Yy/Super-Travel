"""Execution phases stay explicit without relaxing tool or submission validation."""
import asyncio
from copy import deepcopy
import json

import pytest

from supertravel_server.agents import StepFailure
from .test_agents import FakeModel, finish, reply, session_for
from .test_preference_agents import DEFAULTS, MODEL_DEFAULTS, applied, user_value
from .test_trips import Maps, submitted, trip_route
from supertravel_server.trips import TripRequest, TripResearch
from supertravel_server.agent_service import SendMessage
from supertravel_server.conversations import ConversationInput
from uuid import uuid4
from .test_trip_api import app
from .test_trip_identity import existing_trip, submission


def test_query_then_text_arrangement_then_final_submission_keeps_phase_and_schema():
    async def run():
        phases = []
        class PhaseModel(FakeModel):
            async def chat(self, messages, tools=None, **kwargs):
                if tools and tools[0]["function"]["name"] != "route_request":
                    context = json.loads(messages[1]["content"])
                    definitions = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
                    phases.append((context["step"], context["is_final_step"], "submit_trip" in definitions))
                    assert context["travel_preferences"] == MODEL_DEFAULTS
                    assert "report_plan_issue" in definitions
                    if context["step"] == "查询地点":
                        assert context["is_final_step"] is False and "submit_trip" not in definitions
                        if messages[-1]["role"] != "tool":
                            return reply(name="search_trip_places", arguments={"query": "断桥残雪"})
                        return reply("地点已查到；票价与营业时间没有资料，均未知。")
                    if context["step"] == "整理时间":
                        assert context["is_final_step"] is False and "submit_trip" not in definitions
                        return reply("建议09:00停留60分钟，费用与营业时间待确认。")
                    assert context["is_final_step"] is True
                    assert set(definitions["submit_trip"]["required"]) == {"summary", "days"}
                    return reply(name="submit_trip", arguments=submitted())
                return await super().chat(messages, tools, **kwargs)
        model = PhaseModel([reply(name="route_request", arguments={"kind": "plan", "content": "查询并提交", "steps": ["查询地点", "整理时间", "提交行程"],
            "trip": {"destination": "杭州", "days": 1}, "preference_application": applied(budgetTargetCny=user_value("5000.00", "预算5000"), pace=user_value("packed", "紧凑"))})])
        tools = Maps()
        session, _ = session_for(model, tools)
        await session.chat("紧凑预算5000，仅一个地点", travel_preferences=DEFAULTS)
        state = await finish(session)
        assert state["status"] == "completed" and state["replans"] == 0
        assert [phase for phase in phases if phase[0] != "查询地点"] == [("整理时间", False, False), ("提交行程", True, True)]
        assert len(tools.calls) == 1 and len(state["results"]) == 3
        assert "cost" not in state["trip_result"]["days"][0]["activities"][0]
        assert not model.summary_calls
        assert "预算目标：5000元（本次要求）" in state["preference_summary"]
        assert "同行：亲子（常用偏好）" in state["preference_summary"]
        await session.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("final,name,arguments,message", [
    (False, "submit_trip", submitted(), "未提供的工具"),
    (True, "submit_trip", {"activities": [{"place_id": "baidu:fixture", "start_time": "09:00", "end_time": "10:00"}]}, "参数不符合要求"),
    (False, "report_plan_issue", {"reason": "用户固定时间与已查路线冲突，需要调整"}, "执行反馈：用户固定时间"),
])
def test_phase_does_not_allow_early_or_guessed_submission_and_preserves_real_feedback(final, name, arguments, message):
    async def run():
        model = FakeModel([], execute=lambda messages: reply(name=name, arguments=deepcopy(arguments)))
        tools = Maps()
        research = TripResearch(tools, "杭州")
        await research.search("断桥残雪")
        session, _ = session_for(model, tools)
        session._run_id = "phase-check"
        with pytest.raises(StepFailure, match=message):
            await session._execute_step("phase-check", "生成行程", "当前步骤", [], {},
                research=research, trip=TripRequest(destination="杭州", days=1), final=final)
        assert json.loads(model.executor_calls[0][1]["content"])["is_final_step"] is final
        assert len(tools.calls) == 1
        await session.aclose()
    asyncio.run(run())


def test_continuation_corrects_plan_and_keeps_user_fields_in_unapplied_draft(app):
    async def run():
        original = existing_trip()
        original["days"][1]["activities"][0]["description"] = "正式备注：𠮷 🚕"
        original["days"][1]["activities"][0]["cost"]["amountFen"] = 1250
        planned = submission(original, identifiers=True)
        planned["days"][1]["activities"][0]["time"] = "11:15"
        phases = []
        def execute(messages):
            context = json.loads(messages[1]["content"])
            phases.append(context["is_final_step"])
            assert context["current_trip"] == original
            assert "cost 是用户填写的费用，由服务端按 activity_id 原样保留" in messages[0]["content"]
            if context["is_final_step"]:
                assert "当前步骤是最终提交" in messages[0]["content"]
                return reply(name="submit_trip", arguments=planned)
            assert "当前步骤仅查询或整理" in messages[0]["content"]
            return reply("已有真实地点；只调整时间，现有费用由服务端保留。")
        model = FakeModel([reply(name="route_request", arguments={"kind": "plan"}),
            trip_route(days=2, steps=["核对时间和保留字段", "提交修改草稿"])], execute)
        service, store = app.state.agents, app.state.store
        service.model, service.tools = model, Maps()
        document = store.create(ConversationInput(title="续改回归", data={"schema": 1, "messages": [], "days": [], "draft": None}))
        store.mutate(document.id, lambda data: data.update(deepcopy(original)))
        await service.send(document.id, SendMessage(message_id=uuid4(), content="只把第二天开始时间延后15分钟，其余不变，生成草稿"))
        await service.turns[document.id]
        state = await finish(service.sessions[document.id])
        data = store.get(document.id).data
        assert state["status"] == "completed" and state["replans"] == 0
        assert len(model.planner_calls) == 2 and phases == [False, True] and not service.tools.calls
        assert data["days"] == original["days"] and data["trip"] == original["trip"]
        for before, after in zip(original["days"], data["draft"]):
            before, after = before["activities"][0], after["activities"][0]
            for key in ("id", "description", "cost", "place"):
                assert before[key] == after[key]
        assert data["draft"][1]["activities"][0]["time"] == "11:15"
        await service.stop()
    asyncio.run(run())
