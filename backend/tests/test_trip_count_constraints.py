"""Historical per-day limits must not undo confirmed manual itinerary edits."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
import pytest

from backend.agent_service import SendMessage
from backend.candidates import as_candidate
from backend.main import ConversationInput
from backend.model import ModelError
from backend.tests.test_agents import FakeModel, finish, reply, route, session_for
from backend.tests.test_trip_api import AUTH, app
from backend.tests.test_trip_identity import submission
from backend.tests.test_trip_mutations import activity
from backend.tests.test_trips import Maps, trip_route


INITIAL_REQUEST = "杭州两天，每天恰好2个地点。"
EDIT_REQUEST = "仅将第2天断桥残雪从15:00改为15:15，其他行程、备注、费用和候选保持。"
PLACE_NAMES = {"bridge": "断桥残雪", "causeway": "白堤", "hill": "孤山", "lake": "平湖秋月"}


def count_route(count, *, evidence=None, steps=None):
    return reply(name="route_request", arguments={"kind": "plan", "content": "按本次请求核对日程。",
        "steps": steps or ["核对并提交两天行程"], "trip": {"destination": "杭州", "days": 2, "stops_per_day": count},
        "stops_per_day_evidence": evidence})


def fixed_maps():
    places = [{"uid": identifier, "name": name, "address": "固定测试地址", "city": "杭州市",
               "location": {"lat": 30.25 + index * .001, "lng": 120.15 + index * .001}}
              for index, (identifier, name) in enumerate(PLACE_NAMES.items())]
    return Maps({"content": [{"type": "text", "text": json.dumps({"status": 0, "results": places})}]},
                names=["baidu_map_search_places"])


def initial_submission():
    return {"summary": "固定资料，每天2项", "days": [
        {"day": day, "title": f"第{day}天", "subtitle": "固定回归",
         "activities": [{"place_id": f"baidu:{identifier}", "time": time, "duration_minutes": 60,
                         "description": "固定测试安排"} for identifier, time in stops]}
        for day, stops in [(1, [("bridge", "09:00"), ("causeway", "11:00")]),
                           (2, [("hill", "09:00"), ("lake", "11:00")])]]}


def execute_time_edit(messages):
    context = json.loads(messages[1]["content"])
    if context.get("current_trip"):
        planned = submission(context["current_trip"], identifiers=True)
        for day in planned["days"]:
            for stop in day["activities"]:
                if stop["place_id"] == "baidu:bridge":
                    stop["time"] = "15:15"
        return reply(name="submit_trip", arguments=planned)
    if messages[-1]["role"] != "tool":
        return reply(name="search_trip_places", arguments={"query": "固定测试地点"})
    return reply(name="submit_trip", arguments=initial_submission())


class HistoricalRunModel(FakeModel):
    """A fixed planner reproduces the observed temptation to reuse a completed run."""
    async def chat(self, messages, tools=None, **kwargs):
        if tools and tools[0]["function"]["name"] == "route_request" and not self.routes:
            context = json.loads(messages[-1]["content"])
            old_run = context.get("current_run") or {}
            count = 2 if old_run.get("status") == "completed" else None
            self.routes.append(trip_route(days=2, stops_per_day=count))
        return await super().chat(messages, tools, **kwargs)


async def send_and_finish(service, identifier, content):
    await service.send(identifier, SendMessage(message_id=uuid4(), content=content))
    await asyncio.wait_for(service.turns[identifier], 2)
    return await finish(service.sessions[identifier])


def test_manual_cross_day_apply_then_time_only_chat_keeps_confirmed_shape_and_user_data(app):
    async def run():
        service = app.state.agents
        model = HistoricalRunModel([trip_route(days=2, stops_per_day=2)], execute_time_edit)
        service.model, service.tools = model, fixed_maps()
        doc = app.state.store.create(ConversationInput(title="跨日后只改时间", data={
            "schema": 1, "days": [], "draft": None, "messages": [], "favorites": []}))
        try:
            assert (await send_and_finish(service, doc.id, INITIAL_REQUEST))["status"] == "completed"
            generated = app.state.store.get(doc.id)
            assert [len(day["activities"]) for day in generated.data["days"]] == [2, 2]
            bridge_id = generated.data["days"][0]["activities"][0]["id"]
            candidate = as_candidate(activity("candidate-kept", "09:00"), "杭州")
            candidate["cost"] = {"amountFen": 4800, "currency": "CNY", "source": "user"}
            def user_data(data):
                bridge = data["days"][0]["activities"][0]
                bridge.update(description="用户原备注：先取票𠮷 😀", cost={"amountFen": 2580, "currency": "CNY", "source": "user"})
                data.update(candidates=[candidate], favorites=[bridge_id, candidate["id"]])
            prepared = app.state.store.mutate(doc.id, user_data)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                draft = deepcopy(prepared.data)
                draft.update(draft=deepcopy(draft["days"]), draftTrip=deepcopy(draft["trip"]), draftCandidates=deepcopy(draft["candidates"]))
                opened = await client.put(f"/api/conversations/{doc.id}", headers=AUTH, json={
                    "title": prepared.title, "expected_revision": prepared.revision, "data": draft})
                assert opened.status_code == 200, opened.text
                moved = await client.post(f"/api/conversations/{doc.id}/trip/activities/move", headers=AUTH, json={
                    "expected_revision": opened.json()["revision"], "activity_id": bridge_id, "target_day": 2, "target_index": 2, "time": "15:00"})
                assert moved.status_code == 200, moved.text
                applied = await client.post(f"/api/conversations/{doc.id}/trip/apply", headers=AUTH,
                                           json={"expected_revision": moved.json()["revision"]})
                assert applied.status_code == 200, applied.text
            formal = deepcopy(applied.json()["data"])
            assert [len(day["activities"]) for day in formal["days"]] == [1, 3]
            state = await send_and_finish(service, doc.id, EDIT_REQUEST)
            assert state["status"] == "completed", state["error"]
            saved = app.state.store.get(doc.id).data
            assert saved["days"] == formal["days"] and saved["trip"] == formal["trip"]
            assert saved["previousTrip"] == formal["previousTrip"] and saved["favorites"] == formal["favorites"]
            assert saved["candidates"] == saved["draftCandidates"] == formal["candidates"]
            assert [len(day["activities"]) for day in saved["draft"]] == [1, 3]
            old = {item["id"]: item for day in formal["days"] for item in day["activities"]}
            new = {item["id"]: item for day in saved["draft"] for item in day["activities"]}
            assert new.keys() == old.keys()
            for identifier, item in new.items():
                assert item["description"] == old[identifier]["description"]
                assert item.get("cost") == old[identifier].get("cost")
                assert item["time"] == ("15:15" if identifier == bridge_id else old[identifier]["time"])
            context = json.loads(model.planner_calls[1][-1]["content"])
            assert [len(day["activities"]) for day in context["current_trip"]["days"]] == [1, 3]
            assert (context.get("current_run") or {}).get("status") != "completed"
        finally:
            await service.stop()
    asyncio.run(run())


@pytest.mark.parametrize("editing", [False, True])
@pytest.mark.parametrize("required", [1, 3])
def test_explicit_count_still_rejects_different_daily_size(editing, required):
    async def run():
        text = f"本次杭州两天，每天恰好{required}个地点。"
        quote = f"每天恰好{required}个地点"
        existing = two_day_context()
        def execute(messages):
            if editing:
                return reply(name="submit_trip", arguments=submission(existing, identifiers=True))
            return execute_time_edit(messages)
        model = FakeModel([count_route(required, evidence=quote if editing else None) for _ in range(3)], execute)
        session, _ = session_for(model, fixed_maps())
        try:
            await session.chat(text, trip_context=existing if editing else None)
            state = await finish(session)
            assert state["status"] == "failed" and state["replans"] == 2
            assert f"恰好安排 {required}" in state["error"]
            assert state.get("trip_result") is None
            assert all(json.loads(call[1]["content"])["trip_request"]["stops_per_day"] == required
                       for call in model.executor_calls)
        finally:
            await session.aclose()
    asyncio.run(run())


def test_running_progress_and_replan_receive_same_live_run_not_a_finished_run():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class ControlledModel(FakeModel):
            async def chat(self, messages, tools=None, **kwargs):
                if tools and tools[0]["function"]["name"] != "route_request" and not self.executor_calls:
                    started.set()
                    await release.wait()
                return await super().chat(messages, tools, **kwargs)
        existing = two_day_context()
        planned = submission(existing, identifiers=True)
        for day in planned["days"]:
            day["activities"] = day["activities"][:1]
        calls = 0
        def execute(messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                return reply(name="report_plan_issue", arguments={"reason": "固定测试：重新核对安排后提交"})
            assert json.loads(messages[1]["content"])["trip_request"]["stops_per_day"] == 1
            return reply(name="submit_trip", arguments=planned)
        model = ControlledModel([count_route(1, evidence="每天恰好1个地点"), route("progress"),
                                 count_route(1, evidence="每天恰好1个地点", steps=["修订后步骤"])], execute)
        session, _ = session_for(model, Maps(names=[]))
        try:
            first = await session.chat("本次改为每天恰好1个地点。", trip_context=existing)
            await asyncio.wait_for(started.wait(), 1)
            progress = await session.chat("现在进度如何？")
            assert progress["run_id"] == first["run_id"]
            live = json.loads(model.planner_calls[1][-1]["content"])["current_run"]
            assert live["run_id"] == first["run_id"] and live["status"] == "running"
            release.set()
            state = await finish(session)
            assert state["status"] == "completed" and state["replans"] == 1
            feedback = json.loads(model.planner_calls[2][-1]["content"])
            assert feedback["current_run"]["run_id"] == first["run_id"]
            assert feedback["current_run"]["status"] == "replanning"
            assert "固定测试" in feedback["execution_failure"]
            assert feedback["request"] == "本次改为每天恰好1个地点。"
            assert [len(day["activities"]) for day in state["trip_result"]["days"]] == [1, 1]
        finally:
            release.set()
            await session.aclose()
    asyncio.run(run())

def two_day_context():
    days = []
    for day, names in [(1, ["bridge", "causeway"]), (2, ["hill", "lake"])]:
        stops = [activity(name, "09:00" if index == 0 else "11:00") for index, name in enumerate(names)]
        for stop in stops:
            stop["title"] = stop["place"]["name"] = PLACE_NAMES[stop["id"]]
        days.append({"day": day, "title": f"第{day}天", "subtitle": "固定回归", "activities": stops})
    return {"trip": {"destination": "杭州", "summary": "已确认行程", "generatedAt": "2026-10-28T00:00:00+08:00"}, "days": days}


def test_delete_from_two_per_day_replaces_the_whole_stale_plan_before_execution():
    async def run():
        existing = two_day_context()
        planned = submission(existing, identifiers=True)
        planned["days"][0]["activities"].pop(0)
        def execute(messages):
            context = json.loads(messages[1]["content"])
            assert context["step"] == "删除第一天断桥后提交，其他活动不变"
            assert context["trip_request"]["stops_per_day"] is None
            return reply(name="submit_trip", arguments=planned)
        model = FakeModel([
            trip_route(days=2, stops_per_day=2, steps=["过期步骤：删除后补足每天2项"]),
            trip_route(days=2, steps=["删除第一天断桥后提交，其他活动不变"]),
        ], execute)
        session, _ = session_for(model, Maps(names=[]))
        try:
            await session.chat("删除第一天的断桥残雪，其他活动保持不变。",
                               history=[{"role": "user", "content": INITIAL_REQUEST}], trip_context=existing)
            state = await finish(session)
            assert state["status"] == "completed", state["error"]
            assert [len(day["activities"]) for day in state["trip_result"]["days"]] == [1, 2]
            assert len(model.planner_calls) == 2 and len(model.executor_calls) == 1
        finally:
            await session.aclose()
    asyncio.run(run())


def moved_context():
    existing = two_day_context()
    bridge = existing["days"][0]["activities"].pop(0)
    bridge["time"] = "15:00"
    existing["days"][1]["activities"].append(bridge)
    return existing


@pytest.mark.parametrize("evidence", [None, "   ", INITIAL_REQUEST, "本次并未说过这句话"])
def test_stale_count_without_current_quote_gets_one_complete_correction(evidence):
    async def run():
        def execute(messages):
            context = json.loads(messages[1]["content"])
            assert context["step"] == "只调整断桥时间，不增删活动"
            assert context["trip_request"]["stops_per_day"] is None
            return execute_time_edit(messages)
        model = FakeModel([count_route(2, evidence=evidence, steps=["错误旧计划补足每天2项"]),
                           count_route(None, steps=["只调整断桥时间，不增删活动"])], execute)
        session, _ = session_for(model, Maps(names=[]))
        try:
            await session.chat(EDIT_REQUEST, history=[{"role": "user", "content": INITIAL_REQUEST}], trip_context=moved_context())
            state = await finish(session)
            assert state["status"] == "completed" and state["replans"] == 0
            assert [len(day["activities"]) for day in state["trip_result"]["days"]] == [1, 3]
            assert len(model.planner_calls) == 2 and len(model.executor_calls) == 1
            correction = json.loads(model.planner_calls[1][-1]["content"])
            assert correction["planning_feedback"] and correction["request"] == EDIT_REQUEST
        finally:
            await session.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("evidence", [None, "   ", INITIAL_REQUEST, "本次并未说过这句话"])
def test_repeated_bad_count_evidence_stops_before_execution(evidence):
    async def run():
        model = FakeModel([count_route(2, evidence=evidence), count_route(2, evidence=evidence)], execute_time_edit)
        tools = Maps(names=[])
        session, events = session_for(model, tools)
        try:
            with pytest.raises(ModelError) as caught:
                await session.chat(EDIT_REQUEST, history=[{"role": "user", "content": INITIAL_REQUEST}], trip_context=moved_context())
            assert caught.value.code == "agent_plan"
            assert "本次" in str(caught.value) and "数量" in str(caught.value)
            assert len(model.planner_calls) == 2
            assert not model.executor_calls and not tools.calls and not events
            assert session.snapshot()["status"] == "idle"
        finally:
            await session.aclose()
    asyncio.run(run())


def test_replan_cannot_drop_a_valid_explicit_count():
    async def run():
        existing = two_day_context()
        def execute(messages):
            return reply(name="report_plan_issue", arguments={"reason": "固定反馈：现有数量不符，请修订步骤"})
        model = FakeModel([count_route(1, evidence="每天恰好1个地点"), count_route(None)], execute)
        session, _ = session_for(model, Maps(names=[]))
        try:
            await session.chat("本次改为每天恰好1个地点。", trip_context=existing)
            state = await finish(session)
            assert state["status"] == "failed"
            assert "改变了目的地、天数或用户限制" in state["error"]
            assert len(model.executor_calls) == 1
        finally:
            await session.aclose()
    asyncio.run(run())


def test_message_api_repeated_bad_count_preserves_trip_and_explains_evidence(app):
    async def run():
        service = app.state.agents
        model = FakeModel([count_route(2, evidence=INITIAL_REQUEST), count_route(2, evidence=INITIAL_REQUEST)], execute_time_edit)
        tools = Maps(names=[])
        service.model, service.tools = model, tools
        doc = app.state.store.create(ConversationInput(title="数量依据错误不能覆盖数据", data={
            "schema": 1, "days": [], "draft": None, "messages": [], "favorites": []}))
        existing = moved_context()
        candidate = as_candidate(activity("candidate-api-kept", "09:00"), "杭州")
        def setup(data):
            data.update(**existing, draft=deepcopy(existing["days"]), draftTrip=deepcopy(existing["trip"]),
                        candidates=[candidate], draftCandidates=[deepcopy(candidate)],
                        previousTrip={**two_day_context(), "candidates": []}, favorites=["bridge"],
                        messages=[{"id": "initial", "role": "user", "content": INITIAL_REQUEST}])
        prepared = app.state.store.mutate(doc.id, setup)
        before = deepcopy(prepared.data)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(f"/api/conversations/{doc.id}/messages", headers=AUTH,
                                             json={"message_id": str(uuid4()), "content": EDIT_REQUEST})
                assert response.status_code == 202, response.text
                turn = service.turns.get(doc.id)
                if turn is not None:
                    await asyncio.wait_for(turn, 2)
                current = (await client.get(f"/api/conversations/{doc.id}", headers=AUTH)).json()["data"]
            for key in ("days", "trip", "draft", "draftTrip", "candidates", "draftCandidates", "previousTrip", "favorites"):
                assert current[key] == before[key]
            message = current["messages"][-1]["content"]
            assert current["messages"][-1]["role"] == "assistant"
            assert "本次" in message and "数量" in message and "依据" in message
            assert "检查模型设置" not in message
            assert current["agentPending"] is False
            assert service.sessions[doc.id].snapshot()["status"] == "idle"
            assert len(model.planner_calls) == 2 and not model.executor_calls and not tools.calls
        finally:
            await service.stop()
    asyncio.run(run())
