import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.agents import AgentSession
from backend.model import ModelReply, ModelError


TOOL = {"name": "amap_weather", "description": "查询天气", "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": False}}


def reply(content=None, name=None, arguments=None):
    message = {"role": "assistant", "content": content}
    if name:
        message["tool_calls"] = [{"id": "call-1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]
    return ModelReply.model_validate({"message": message, "finish_reason": "tool_calls" if name else "stop"})


def route(kind="plan", content="开始查询。", steps=None, replace_active=False):
    return reply(name="route_request", arguments={"kind": kind, "content": content, "steps": steps or (["查询杭州天气"] if kind == "plan" else []), "replace_active": replace_active})


class FakeModel:
    def __init__(self, routes, execute=None, summarize=None):
        self.routes = list(routes)
        self.execute = execute or (lambda messages: reply("查询结果已整理。"))
        self.planner_calls = []
        self.executor_calls = []
        self.summary_calls = []
        self.summarize = summarize

    async def chat(self, messages, tools=None, **kwargs):
        if not tools:
            self.summary_calls.append(deepcopy(messages))
            if self.summarize:
                return self.summarize(messages)
            context = json.loads(messages[-1]["content"])
            trip = context.get("trip_result")
            return reply(f"已生成{trip['trip']['destination']} {len(trip['days'])} 天行程。" if trip else context["completed_steps"][-1]["content"])
        if tools and tools[0]["function"]["name"] == "route_request":
            self.planner_calls.append(deepcopy(messages))
            return self.routes.pop(0)
        self.executor_calls.append(deepcopy(messages))
        return self.execute(messages)


class FakeTools:
    def __init__(self, respond=None):
        self.respond = respond
        self.calls = []
        self.listed = 0
        self.tools = [deepcopy(TOOL)]

    async def list_tools(self):
        self.listed += 1
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.respond:
            return await self.respond(name, arguments)
        return {"content": [{"type": "text", "text": "杭州：晴"}], "isError": False}


class TemporaryToolError(Exception):
    retryable = True


def execute_weather(messages):
    return reply("工具返回杭州晴。") if messages[-1]["role"] == "tool" else reply(name="amap_weather", arguments={"city": "杭州"})


def session_for(model, tools=None, **kwargs):
    events = []

    async def record(state):
        events.append(deepcopy(state))

    return AgentSession(model, tools or FakeTools(), record, **kwargs), events


async def finish(session):
    await asyncio.wait_for(session._task, timeout=1)
    return session.snapshot()


def test_missing_information_asks_without_starting_tools():
    async def run():
        tools = FakeTools()
        session, events = session_for(FakeModel([route("clarification", "你打算去哪个城市？")]), tools)
        result = await session.chat("帮我查天气")
        assert result == {"kind": "clarification", "content": "你打算去哪个城市？"}
        assert session.snapshot()["status"] == "idle" and not events
        assert not tools.calls and tools.listed == 0
    asyncio.run(run())


def test_chat_and_progress_do_not_interrupt_active_execution():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(name, arguments):
            started.set()
            await release.wait()
            return {"content": [{"type": "text", "text": "杭州晴"}]}

        model = FakeModel([route(), route("answer", "你好，很高兴聊旅行。"), route("progress", "ignored model progress")], execute_weather)
        session, events = session_for(model, FakeTools(slow))
        first = await session.chat("查询杭州天气")
        await started.wait()
        task = session._task
        answer = await session.chat("你好")
        progress = await session.chat("现在到哪一步了？")
        assert answer["kind"] == "answer" and progress["kind"] == "progress"
        assert "已调用工具 1 次" in progress["content"]
        assert task is session._task and not task.done()
        assert session.snapshot()["run_id"] == first["run_id"]
        release.set()
        state = await finish(session)
        assert state["status"] == "completed" and state["content"] == "工具返回杭州晴。"
        assert all(event["run_id"] == first["run_id"] for event in events)
    asyncio.run(run())


def test_replacement_rejects_late_result_even_if_tool_ignores_cancel():
    async def run():
        started, late = asyncio.Event(), asyncio.Event()

        async def slow(name, arguments):
            if arguments["city"] == "新任务":
                return {"content": [{"type": "text", "text": "新查询结果"}]}
            started.set()
            try:
                await late.wait()
            except asyncio.CancelledError:
                await late.wait()
            return {"content": [{"type": "text", "text": "旧结果"}]}

        def execute(messages):
            step = json.loads(messages[1]["content"])["step"]
            if step == "新步骤":
                return reply("新的任务结果") if messages[-1]["role"] == "tool" else reply(name="amap_weather", arguments={"city": "新任务"})
            return execute_weather(messages)

        session, events = session_for(FakeModel([route(), route(steps=["新步骤"])], execute), FakeTools(slow))
        old = await session.chat("旧任务")
        await started.wait()
        old_task = session._task
        new = await session.chat("改为新任务")
        assert new["run_id"] != old["run_id"]
        await finish(session)
        event_count = len(events)
        late.set()
        with pytest.raises(asyncio.CancelledError):
            await old_task
        assert len(events) == event_count
        assert session.snapshot()["content"] == "新的任务结果"
        assert session.snapshot()["run_id"] == new["run_id"]
    asyncio.run(run())


def test_cancel_propagates_to_pending_tool_and_preserves_cancelled_state():
    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(name, arguments):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        session, events = session_for(FakeModel([route()], execute_weather), FakeTools(slow))
        await session.chat("查询杭州")
        await started.wait()
        await session.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session._task
        assert cancelled.is_set()
        assert session.snapshot()["status"] == "cancelled"
        assert events[-1]["status"] == "cancelled"
    asyncio.run(run())


def test_modified_request_needing_details_cancels_old_task():
    async def run():
        started = asyncio.Event()

        async def slow(name, arguments):
            started.set()
            await asyncio.Event().wait()

        session, _ = session_for(FakeModel([route(), route("clarification", "改到哪个城市？", replace_active=True)], execute_weather), FakeTools(slow))
        await session.chat("杭州天气")
        await started.wait()
        result = await session.chat("改去别的城市")
        assert result["kind"] == "clarification"
        assert session.snapshot()["status"] == "cancelled"
        with pytest.raises(asyncio.CancelledError):
            await session._task
    asyncio.run(run())


def test_transient_error_has_one_retry_and_counts_both_calls():
    async def run():
        attempts = 0

        async def temporary(name, arguments):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TemporaryToolError("secret provider details")
            return {"content": [{"type": "text", "text": "杭州晴"}]}

        session, _ = session_for(FakeModel([route()], execute_weather), FakeTools(temporary))
        await session.chat("杭州天气")
        state = await finish(session)
        assert attempts == 2 and state["tool_calls"] == 2
        assert state["replans"] == 0 and state["status"] == "completed"
    asyncio.run(run())


def test_business_failures_stop_after_two_replans_without_retrying():
    async def run():
        async def bad(name, arguments):
            return {"isError": True, "content": [{"type": "text", "text": "bad query"}]}

        model = FakeModel([route(), route(), route()], execute_weather)
        tools = FakeTools(bad)
        session, _ = session_for(model, tools)
        await session.chat("查询天气")
        state = await finish(session)
        assert state["status"] == "failed" and state["replans"] == 2
        assert len(model.planner_calls) == 3 and len(tools.calls) == 3
        assert not state["results"]
        assert "业务错误" in model.planner_calls[-1][-1]["content"]
    asyncio.run(run())


def test_tool_budget_includes_retry_and_stops_replanning():
    async def run():
        async def failed(name, arguments):
            raise TemporaryToolError("upstream secret")

        model = FakeModel([route()], execute_weather)
        tools = FakeTools(failed)
        session, _ = session_for(model, tools, max_tool_calls=2)
        await session.chat("查询天气")
        state = await finish(session)
        assert state["status"] == "failed" and state["tool_calls"] == 2
        assert len(tools.calls) == 2 and len(model.planner_calls) == 1
        assert "upstream secret" not in json.dumps(state)
    asyncio.run(run())


@pytest.mark.parametrize("arguments", [{}, {"city": 123}, {"city": "杭州", "shell": "read secrets"}])
def test_invalid_tool_arguments_are_not_sent_or_retried(arguments):
    async def run():
        model = FakeModel([route(), route("clarification", "请补充有效城市名。")], lambda messages: reply(name="amap_weather", arguments=arguments))
        tools = FakeTools()
        session, _ = session_for(model, tools)
        await session.chat("查询天气")
        state = await finish(session)
        assert state["status"] == "needs_input"
        assert state["content"] == "请补充有效城市名。"
        assert state["tool_calls"] == 0 and not tools.calls
    asyncio.run(run())


def test_remote_schema_reference_is_not_fetched_or_executed():
    async def run():
        tools = FakeTools()
        tools.tools[0]["inputSchema"] = {"$ref": "https://not-authorized.invalid/schema"}
        session, _ = session_for(FakeModel([route(), route("clarification", "工具配置待修复。")], execute_weather), tools)
        await session.chat("查询杭州")
        state = await finish(session)
        assert state["status"] == "needs_input" and not tools.calls
    asyncio.run(run())


def test_unexpected_background_error_becomes_safe_failed_status():
    async def run():
        def fail(messages):
            raise RuntimeError("internal credential value")

        session, events = session_for(FakeModel([route()], fail))
        await session.chat("查询杭州")
        state = await finish(session)
        assert state["status"] == "failed" and events[-1]["status"] == "failed"
        assert "internal credential" not in json.dumps(state)
    asyncio.run(run())


def test_persistence_failure_stops_before_tool_execution():
    async def run():
        async def reject(state):
            if state["tool_calls"]:
                raise OSError("disk unavailable")

        tools = FakeTools()
        session = AgentSession(FakeModel([route()], execute_weather), tools, reject)
        await session.chat("查询杭州")
        await finish(session)
        assert session.snapshot()["status"] == "failed"
        assert "保存失败" in session.snapshot()["error"]
        assert not tools.calls
    asyncio.run(run())


def test_shutdown_waits_for_replaced_task_that_ignores_cancellation():
    async def run():
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def stubborn(name, arguments):
            if arguments["city"] == "新任务":
                return {"content": [{"type": "text", "text": "新查询结果"}]}
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            return {"content": [{"type": "text", "text": "late"}]}

        def execute(messages):
            if json.loads(messages[1]["content"])["step"] == "新步骤":
                return reply("新任务已完成。") if messages[-1]["role"] == "tool" else reply(name="amap_weather", arguments={"city": "新任务"})
            return execute_weather(messages)

        session, events = session_for(FakeModel([route(), route(steps=["新步骤"])], execute), FakeTools(stubborn))
        await session.chat("旧查询")
        await started.wait()
        await session.chat("换成新任务")
        await finish(session)
        cancelled.clear()
        closing = asyncio.create_task(session.aclose())
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not closing.done()
        event_count = len(events)
        release.set()
        await asyncio.wait_for(closing, 1)
        assert len(events) == event_count
        assert not session._tasks
        with pytest.raises(ModelError, match="已关闭"):
            await session.chat("再来一次")
    asyncio.run(run())


def test_no_tools_fails_honestly_without_fake_execution():
    async def run():
        tools = FakeTools()
        tools.tools = []
        model = FakeModel([route()])
        session, _ = session_for(model, tools)
        await session.chat("查询实时天气")
        state = await finish(session)
        assert state["status"] == "failed" and "可用工具" in state["error"]
        assert not model.executor_calls
    asyncio.run(run())


@pytest.mark.parametrize("failed_attempt_first", [False, True])
def test_executor_without_successful_tool_result_never_completes(failed_attempt_first):
    async def run():
        attempts = 0

        def execute(messages):
            nonlocal attempts
            attempts += 1
            if failed_attempt_first and attempts == 1:
                return reply(name="amap_weather", arguments={"city": "杭州"})
            return reply("未查询就捏造的天气资料")

        async def failed(name, arguments):
            return {"isError": True, "content": [{"type": "text", "text": "query failed"}]}

        model = FakeModel([route(), route(), route()], execute)
        tools = FakeTools(failed)
        session, events = session_for(model, tools)
        await session.chat("查询杭州实时天气")
        state = await finish(session)
        assert state["status"] == "failed" and state["replans"] == 2
        assert len(model.planner_calls) == 3 and len(model.executor_calls) == 3
        assert state["tool_calls"] == int(failed_attempt_first)
        assert state["results"] == []
        assert all(event["status"] != "completed" for event in events)
        assert "捏造" not in json.dumps(state, ensure_ascii=False)
    asyncio.run(run())


def test_step_can_summarize_existing_successful_tool_results_without_another_call():
    async def run():
        def execute(messages):
            context = json.loads(messages[1]["content"])
            if context["step"] == "整理查询结果":
                assert context["completed_steps"]
                return reply("依据前一步查询结果整理。")
            return execute_weather(messages)

        tools = FakeTools()
        session, _ = session_for(FakeModel([route(steps=["查询杭州天气", "整理查询结果"])], execute), tools)
        await session.chat("查询天气并整理")
        state = await finish(session)
        assert state["status"] == "completed" and len(state["results"]) == 2
        assert state["tool_calls"] == 1 and len(tools.calls) == 1
    asyncio.run(run())


def test_planner_gives_final_answer_after_executor_observes_tool_result():
    async def run():
        def summarize(messages):
            context = json.loads(messages[-1]["content"])
            assert context["request"] == "查询杭州天气"
            assert context["completed_steps"] == [{"step": "查询杭州天气", "content": "工具返回杭州晴。"}]
            assert context["trip_result"] is None
            return reply("查询结果显示杭州晴，出行前请留意变化。")

        model = FakeModel([route()], execute_weather, summarize)
        tools = FakeTools()
        session, events = session_for(model, tools)
        await session.chat("查询杭州天气")
        state = await finish(session)
        assert state["status"] == "completed" and state["content"] == "查询结果显示杭州晴，出行前请留意变化。"
        assert len(model.summary_calls) == 1 and len(tools.calls) == 1
        assert any(event["content"] == "规划 Agent 正在汇总结果。" and event["status"] == "running" for event in events)
        assert events[-1]["status"] == "completed"
    asyncio.run(run())


def test_executor_plan_feedback_is_replanned_without_calling_remote_tool():
    async def run():
        def execute(messages):
            step = json.loads(messages[1]["content"])["step"]
            if step == "查询杭州天气":
                return reply(name="report_plan_issue", arguments={"reason": "用户指定日期超过天气服务可查询范围，请确认日期。"})
            return execute_weather(messages)

        model = FakeModel([route(), route(steps=["查询近期杭州天气"])], execute)
        tools = FakeTools()
        session, events = session_for(model, tools)
        await session.chat("查询杭州天气")
        state = await finish(session)
        feedback = json.loads(model.planner_calls[1][-1]["content"])
        assert "超过天气服务" in feedback["execution_failure"]
        assert feedback["current_run"]["results"] == []
        assert state["status"] == "completed" and state["replans"] == 1
        assert len(tools.calls) == state["tool_calls"] == 1
        assert len(model.summary_calls) == 1 and len(state["results"]) == 1
        assert any(event["status"] == "replanning" for event in events)
    asyncio.run(run())


def test_executor_feedback_can_return_to_user_for_clarification():
    async def run():
        model = FakeModel([route(), route("clarification", "请补充出行日期。")],
                          lambda messages: reply(name="report_plan_issue", arguments={"reason": "缺少出行日期，无法查询。"}))
        tools = FakeTools()
        session, _ = session_for(model, tools)
        await session.chat("查天气")
        state = await finish(session)
        assert state["status"] == "needs_input" and state["content"] == "请补充出行日期。"
        assert not tools.calls and not model.summary_calls and not state["results"]
        assert state["tool_calls"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("summary_reply", [reply(""), reply(name="amap_weather", arguments={"city": "杭州"}), ModelError("model_timeout", "secret provider details")])
def test_summary_failure_keeps_completed_work_without_repeating_tools(summary_reply):
    async def run():
        def summarize(messages):
            if isinstance(summary_reply, Exception):
                raise summary_reply
            return summary_reply

        model = FakeModel([route()], execute_weather, summarize)
        tools = FakeTools()
        session, _ = session_for(model, tools)
        await session.chat("查询杭州天气")
        state = await finish(session)
        assert state["status"] == "completed"
        assert "规划汇总暂时不可用" in state["content"] and "工具返回杭州晴" in state["content"]
        assert "secret" not in state["content"]
        assert len(tools.calls) == 1 and len(model.summary_calls) == 1 and state["replans"] == 0
    asyncio.run(run())


def test_cancelled_summary_cannot_publish_even_when_model_ignores_cancellation():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        class SlowSummaryModel(FakeModel):
            async def chat(self, messages, tools=None, **kwargs):
                if not tools:
                    started.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                    return reply("迟到的规划汇总")
                return await super().chat(messages, tools, **kwargs)

        session, events = session_for(SlowSummaryModel([route()], execute_weather))
        await session.chat("查询杭州天气")
        await started.wait()
        await session.cancel()
        count = len(events)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await session._task
        assert len(events) == count and session.snapshot()["status"] == "cancelled"
        assert all(event["status"] != "completed" for event in events)
    asyncio.run(run())


@pytest.mark.parametrize("days,explicit,expected", [(1, None, 15), (3, None, 29), (7, None, 57), (7, 2, 2)])
def test_trip_tool_budget_is_bounded_and_respects_explicit_limit(days, explicit, expected):
    async def run():
        plan = reply(name="route_request", arguments={"kind": "plan", "content": "查询地点和交通", "steps": ["查询"], "trip": {"destination": "杭州", "days": days}})
        session, _ = session_for(FakeModel([plan]), max_tool_calls=explicit)
        await session.chat(f"杭州{days}天")
        assert session._tool_limit == expected
        await session.aclose()
    asyncio.run(run())


def test_trip_summary_uses_structured_result_only_and_identifies_draft():
    async def run():
        trip = {"days": [{"day": 1, "activities": []}], "trip": {"destination": "杭州"}}
        def summarize(messages):
            context = json.loads(messages[-1]["content"])
            assert context["trip_result"] == trip and context["is_draft"] is True
            assert context["completed_steps"] == []
            assert "不可用的步骤文字" not in messages[-1]["content"]
            raise ModelError("model_timeout", "provider details")

        model = FakeModel([], summarize=summarize)
        session, _ = session_for(model)
        session._run_id = "summary-test"
        content = await session._summarize("summary-test", "修改备注", [{"step": "旧步骤", "content": "不可用的步骤文字"}], trip, is_draft=True)
        assert "修改草稿已生成，请检查后应用" in content
        assert "不可用的步骤文字" not in content and "provider details" not in content
        assert len(model.summary_calls) == 1
    asyncio.run(run())


def test_executor_place_options_omit_coordinates_without_changing_backend_evidence():
    async def run():
        from backend.trips import TripRequest
        place = {"id": "baidu:test", "name": "测试公园", "address": "测试地址", "provider": "baidu",
                 "latitude": 30.25, "longitude": 120.15, "coordinateSystem": "wgs84",
                 "sourceLocation": {"latitude": 30.26, "longitude": 120.16, "coordinateSystem": "bd09ll"}}
        expected = {key: place[key] for key in ("id", "name", "address", "provider")}
        class Research:
            places = {place["id"]: deepcopy(place)}
            routes = {}
            async def search(self, query, provider):
                return list(self.places.values())

        def execute(messages):
            context = json.loads(messages[1]["content"])
            assert context["available_places"] == [expected]
            assert "sourceLocation" not in messages[1]["content"]
            return reply("已整理现有地点。")

        session, _ = session_for(FakeModel([], execute))
        session._run_id = "projection-test"
        research = Research()
        result = await session._call_tool("projection-test", "search_trip_places", {"query": "公园"}, research=research)
        assert json.loads(result["content"][0]["text"])["places"] == [expected]
        await session._execute_step("projection-test", "安排杭州", "整理已有地点", [], {}, research=research, trip=TripRequest(destination="杭州", days=1))
        assert research.places[place["id"]] == place
    asyncio.run(run())
