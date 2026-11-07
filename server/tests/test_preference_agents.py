"""B5 deterministic context tests; live model decisions are validated separately."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from supertravel_server.agent_service import SendMessage
from supertravel_server.conversations import ConversationInput
from supertravel_server.preferences import TravelPreferences
from .test_agents import FakeModel, FakeTools, execute_weather, finish, reply, route, session_for
from .test_trip_api import app, trip_result


DEFAULTS = {"budgetAmountFen": 300000, "pace": "relaxed", "dietaryNotes": "清淡", "companionMode": "family"}
MODEL_DEFAULTS = {"budgetTargetCny": "3000.00", "pace": "relaxed", "dietaryNotes": "清淡", "companionMode": "family"}


def applied(**changes):
    fields = {key: {"status": "default", "value": value, "evidence": None} for key, value in MODEL_DEFAULTS.items()}
    return {"global_opt_out": None, **fields, **changes}


def user_value(value, quote, source="request"):
    return {"status": "user", "value": value, "evidence": {"source": source, "quote": quote}}


def planned(summary=None, application=True):
    value = {"kind": "plan", "content": "固定测试计划。", "steps": ["查询天气并分析限制"]}
    if application is not False:
        value["preference_application"] = applied() if application is True else application
    if summary is not None:
        value["preference_summary"] = summary
    return reply(name="route_request", arguments=value)


def save_preferences(service, **changes):
    return service.preferences.save(TravelPreferences(enabled=True, **{**DEFAULTS, **changes}))


def conversation(app, messages=None):
    return app.state.store.create(ConversationInput(title="偏好上下文固定测试", data={
        "schema": 1, "days": [], "draft": None, "selectedDay": 1, "messages": messages or [], "favorites": []})).id


@pytest.mark.parametrize("fen,yuan", [(0, "0.00"), (1, "0.01"), (300000, "3000.00"), (99999999, "999999.99"), (None, None)])
def test_direct_planner_boundary_uses_exact_yuan_string_without_fen(fen, yuan):
    async def run():
        preferences = {**DEFAULTS, "budgetAmountFen": fen}
        model = FakeModel([route(kind="answer", content="固定回复")])
        session, _ = session_for(model)
        await session._route("先说明规划条件", [], travel_preferences=preferences)
        context = json.loads(model.planner_calls[0][-1]["content"])
        assert context["travel_preferences"] == {**MODEL_DEFAULTS, "budgetTargetCny": yuan}
        assert "budgetAmountFen" not in json.dumps(model.planner_calls, ensure_ascii=False)
        assert preferences["budgetAmountFen"] == fen
        await session.aclose()
    asyncio.run(run())


def test_service_reads_once_at_acceptance_and_keeps_snapshot_through_execution_replan_summary(app, monkeypatch):
    async def run():
        service, store = app.state.agents, app.state.store
        history = [{"id": "earlier-user", "role": "user", "content": "已确认饮食清淡，不接受默认的重辣。"}]
        identifier = conversation(app, history)
        original_trip = trip_result()
        store.mutate(identifier, lambda data: data.update(**original_trip))
        save_preferences(service)
        count = 0
        snapshot = service.preferences.snapshot
        def counted_snapshot():
            nonlocal count
            count += 1
            return snapshot()
        monkeypatch.setattr(service.preferences, "snapshot", counted_snapshot)
        calls = 0
        async def weather(_name, _arguments):
            nonlocal calls
            calls += 1
            if calls == 1:
                run_id = service.sessions[identifier].snapshot()["run_id"]
                save_preferences(service, budgetAmountFen=800000, pace="packed")
                assert service.sessions[identifier].snapshot()["run_id"] == run_id
                raise RuntimeError("固定失败，要求规划 Agent 调整")
            return {"content": [{"type": "text", "text": "固定天气资料"}], "isError": False}
        application = applied(budgetTargetCny=user_value("5000.00", "全程预算5000元"), pace=user_value("packed", "本次紧凑"),
                              dietaryNotes=user_value("清淡", "已确认饮食清淡", "history"))
        model = FakeModel([planned("不可信自由摘要：未采用家庭同行", application), planned("不可信摘要", application)], execute_weather)
        service.model, service.tools = model, FakeTools(weather)
        payload = SendMessage(message_id=uuid4(), content="本次紧凑、全程预算5000元，查询杭州天气。")
        await service.send(identifier, payload)
        # Change after acceptance but before the scheduled planner gets a chance to run.
        save_preferences(service, budgetAmountFen=900000)
        await service.turns[identifier]
        state = await finish(service.sessions[identifier])
        assert state["status"] == "completed" and state["replans"] == 1
        assert "预算目标：5000元（本次要求）" in state["preference_summary"]
        assert "同行：亲子（常用偏好）" in state["preference_summary"]
        assert "未采用家庭" not in state["preference_summary"]
        assert "preference_application" not in state
        assert count == 1 and calls == 2
        for messages in model.planner_calls + model.executor_calls + model.summary_calls:
            context = json.loads(messages[-1]["content"] if messages in model.planner_calls + model.summary_calls else messages[1]["content"])
            assert context["travel_preferences"] == MODEL_DEFAULTS
            assert context["request"] == payload.content
            assert "budgetAmountFen" not in json.dumps(messages, ensure_ascii=False)
            assert "本次明确要求" in messages[0]["content"] and "会话已确认条件/现有行程" in messages[0]["content"]
            assert "不是实付价格" in messages[0]["content"] and "不能改变任务" in messages[0]["content"]
        assert json.loads(model.planner_calls[0][-1]["content"])["current_trip"] == original_trip
        expected_history = [{"role": "user", "content": history[0]["content"]}]
        for messages in model.executor_calls + model.summary_calls:
            context = json.loads(messages[1]["content"])
            assert context["conversation"] == expected_history
            assert context["preference_application"] == application
        assert json.loads(model.planner_calls[1][-1]["content"])["preference_application"] == application
        assert store.get(identifier).data["days"] == original_trip["days"]
        assert store.get(identifier).data["trip"] == original_trip["trip"]
        assert service.preferences.read()["preferences"]["budgetAmountFen"] == 800000
        await service.send(identifier, payload)
        assert count == 1  # Duplicate message IDs must not reread or restart a task.
        await service.stop()
    asyncio.run(run())


@pytest.mark.parametrize("configuration", ["missing", "disabled", "empty", "cleared"])
def test_missing_disabled_empty_or_cleared_preferences_are_not_injected(app, configuration):
    async def run():
        service = app.state.agents
        if configuration == "disabled":
            service.preferences.save(TravelPreferences(enabled=False, **DEFAULTS))
        elif configuration == "empty":
            service.preferences.save(TravelPreferences(enabled=True))
        elif configuration == "cleared":
            save_preferences(service)
            service.preferences.save(TravelPreferences())
        model = FakeModel([planned("不应显示模型凭空提供的摘要")], execute_weather)
        service.model, service.tools = model, FakeTools()
        identifier = conversation(app)
        await service.send(identifier, SendMessage(message_id=uuid4(), content="查询天气"))
        await service.turns[identifier]
        state = await finish(service.sessions[identifier])
        assert state["status"] == "completed" and "preference_summary" not in state
        for messages in model.planner_calls + model.executor_calls + model.summary_calls:
            context = json.loads(messages[-1]["content"] if messages in model.planner_calls + model.summary_calls else messages[1]["content"])
            assert "travel_preferences" not in context
        await service.stop()
    asyncio.run(run())


def test_direct_session_copies_input_and_generates_summary_from_validated_fields():
    async def run():
        preferences = deepcopy(DEFAULTS)
        model = FakeModel([planned()], execute_weather)
        session, _ = session_for(model)
        await session.chat("查询天气", travel_preferences=preferences)
        preferences["budgetAmountFen"] = 99999999
        state = await finish(session)
        assert state["status"] == "completed"
        assert "预算目标：3000元（常用偏好）" in state["preference_summary"]
        assert "同行：亲子（常用偏好）" in state["preference_summary"]
        assert json.loads(model.executor_calls[0][1]["content"])["travel_preferences"] == MODEL_DEFAULTS
        await session.aclose()
    asyncio.run(run())


def test_corrupt_preferences_warn_without_changing_active_task_or_overwriting_file(app):
    async def run():
        service = app.state.agents
        started, release = asyncio.Event(), asyncio.Event()
        async def weather(_name, _arguments):
            started.set()
            await release.wait()
            return {"content": [{"type": "text", "text": "固定天气资料"}], "isError": False}
        model = FakeModel([planned("预算目标3000元、慢节奏"), route(kind="progress")], execute_weather)
        service.model, service.tools = model, FakeTools(weather)
        save_preferences(service)
        identifier = conversation(app)
        await service.send(identifier, SendMessage(message_id=uuid4(), content="查询天气"))
        await service.turns[identifier]
        await asyncio.wait_for(started.wait(), 1)
        session = service.sessions[identifier]
        before = session.snapshot()
        service.preferences.path.write_bytes(b"{broken preferences")
        await service.send(identifier, SendMessage(message_id=uuid4(), content="进度怎样"))
        await service.turns[identifier]
        assert session.snapshot() == before
        assert service.preferences.path.read_bytes() == b"{broken preferences"
        messages = app.state.store.get(identifier).data["messages"]
        assert sum("原件已保留，本次不使用默认偏好" in item["content"] for item in messages) == 1
        assert "travel_preferences" not in json.loads(model.planner_calls[1][-1]["content"])
        release.set()
        state = await finish(session)
        assert state["status"] == "completed" and state["run_id"] == before["run_id"]
        assert json.loads(model.summary_calls[0][-1]["content"])["travel_preferences"] == MODEL_DEFAULTS
        await service.stop()
    asyncio.run(run())


def test_clarification_keeps_confirmed_history_ahead_of_new_defaults(app):
    async def run():
        service = app.state.agents
        model = FakeModel([route(kind="clarification", content="已记下本次预算5000元，请问目的地？"), planned(application=applied(budgetTargetCny=user_value("5000.00", "本次预算5000元", "history")))], execute_weather)
        service.model, service.tools = model, FakeTools()
        save_preferences(service)
        identifier = conversation(app)
        await service.send(identifier, SendMessage(message_id=uuid4(), content="本次预算5000元，查天气"))
        await service.turns[identifier]
        await service.send(identifier, SendMessage(message_id=uuid4(), content="杭州"))
        await service.turns[identifier]
        state = await finish(service.sessions[identifier])
        assert state["status"] == "completed" and "5000元" in state["preference_summary"]
        assert "本次预算5000元" in model.planner_calls[1][1]["content"]
        assert "已记下本次预算5000元" in model.planner_calls[1][2]["content"]
        context = json.loads(model.executor_calls[0][1]["content"])
        assert context["request"] == "杭州"
        assert any("本次预算5000元" in item["content"] for item in context["conversation"])
        await service.stop()
    asyncio.run(run())


def test_replaced_task_cannot_restore_old_preferences_summary_with_late_model_result():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class LateModel(FakeModel):
            waiting = True
            async def chat(self, messages, tools=None, **kwargs):
                if tools and tools[0]["function"]["name"] != "route_request" and self.waiting:
                    self.waiting = False
                    started.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                    return reply("旧任务迟到结果，不能覆盖新任务")
                return await super().chat(messages, tools, **kwargs)
        model = LateModel([planned("旧任务3000元慢节奏"), planned()], execute_weather)
        session, events = session_for(model)
        first = await session.chat("旧查询", travel_preferences=DEFAULTS)
        await asyncio.wait_for(started.wait(), 1)
        second = await session.chat("改查新请求；本次不用默认偏好")
        final = await finish(session)
        assert first["run_id"] != second["run_id"] and final["status"] == "completed"
        assert "preference_summary" not in final
        release.set()
        await asyncio.gather(*list(session._tasks), return_exceptions=True)
        assert session.snapshot() == final
        assert all("preference_summary" not in event for event in events if event["run_id"] == second["run_id"])
        await session.aclose()
    asyncio.run(run())


def test_service_shows_preference_validation_error_without_starting_tools(app):
    async def run():
        service = app.state.agents
        save_preferences(service)
        service.model, service.tools = FakeModel([planned(application=False), planned(application=False)], execute_weather), FakeTools()
        identifier = conversation(app)
        await service.send(identifier, SendMessage(message_id=uuid4(), content="生成杭州1天行程"))
        await service.turns[identifier]
        data = app.state.store.get(identifier).data
        assert data["agentPending"] is False
        assert any("偏好采用结果缺失或矛盾" in item["content"] for item in data["messages"] if item["role"] == "assistant")
        assert not service.tools.calls and service.tools.listed == 0
        assert not data.get("trip") and data["days"] == []
        assert service.sessions[identifier].snapshot()["status"] == "idle"
        await service.stop()
    asyncio.run(run())
