"""B3 fixed model/map fixtures: candidate identity and atomic chat writeback."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from backend.agent_service import SendMessage
from backend.candidates import as_candidate
from backend.main import ConversationInput
from backend.tests.test_agents import FakeModel, finish, reply, route, session_for
from backend.tests.test_trip_api import app, trip_result
from backend.tests.test_trip_identity import submission
from backend.tests.test_trips import Maps, trip_route
from backend.trips import TripRequest, TripResearch


def fixture():
    trip = trip_result("已有日程备注")
    item = deepcopy(trip["days"][0]["activities"][0])
    item.update(id="candidate-a", title="候选公园", duration="约 90 分钟", description="用户候选备注：先取预约票")
    item["place"].update(id="baidu:candidate-a", name=item["title"])
    item["cost"] = {"amountFen": 2580, "currency": "CNY", "source": "user"}
    return trip, as_candidate(item, "杭州市")


def plan_with_candidate(trip, candidate, *, explicit=True):
    value = submission(trip, identifiers=True)
    stop = {"place_id": candidate["place"]["id"], "time": "12:00", "duration_minutes": 30, "description": "模型改写的备注"}
    if explicit:
        stop["activity_id"] = candidate["id"]
    value["days"][0]["activities"].append(stop)
    return value


def research_for(trip, candidate):
    return TripResearch(Maps(names=[]), "杭州", trip["days"], existing_candidates=[candidate])


def test_explicit_candidate_retains_identity_trusted_place_notes_duration_and_cost():
    trip, candidate = fixture()
    original = deepcopy(candidate)
    research = research_for(trip, candidate)
    assert candidate["place"]["id"] not in research.places
    activity = research.submit(plan_with_candidate(trip, candidate), TripRequest(destination="杭州", days=1))["days"][0]["activities"][-1]
    for key in ("id", "title", "place", "description", "duration", "cost"):
        assert activity[key] == candidate[key]
    assert research.submitted_candidate_ids == [candidate["id"]]
    assert activity["incomingRoute"]["status"] == "unknown"
    activity["cost"]["amountFen"] = 0
    assert candidate == original and not research.tools.calls


def test_candidate_note_and_duration_change_require_explicit_flags():
    trip, candidate = fixture()
    planned = plan_with_candidate(trip, candidate)
    planned["days"][0]["activities"][-1].update(replace_description=True, replace_duration=True)
    activity = research_for(trip, candidate).submit(planned, TripRequest(destination="杭州", days=1))["days"][0]["activities"][-1]
    assert activity["description"] == "模型改写的备注" and activity["duration"] == "约 30 分钟"
    assert activity["cost"] == candidate["cost"]


@pytest.mark.parametrize("researched", [False, True])
def test_candidate_place_id_alone_never_claims_candidate_identity(researched):
    trip, candidate = fixture()
    research = research_for(trip, candidate)
    planned = plan_with_candidate(trip, candidate, explicit=False)
    if not researched:
        with pytest.raises(ValueError, match="未经地图查询"):
            research.submit(planned, TripRequest(destination="杭州", days=1))
    else:
        research.places[candidate["place"]["id"]] = deepcopy(candidate["place"])
        activity = research.submit(planned, TripRequest(destination="杭州", days=1))["days"][0]["activities"][-1]
        assert activity["id"] != candidate["id"] and "cost" not in activity
        assert activity["description"] == "模型改写的备注"
    assert research.submitted_candidate_ids == []


@pytest.mark.parametrize("problem", ["unknown", "mismatch", "duplicate", "cross_city"])
def test_invalid_candidate_references_fail_without_consuming_pool(problem):
    trip, candidate = fixture()
    planned = plan_with_candidate(trip, candidate)
    stop = planned["days"][0]["activities"][-1]
    if problem == "unknown":
        stop["activity_id"] = "not-a-candidate"
    elif problem == "mismatch":
        stop["place_id"] = trip["days"][0]["activities"][0]["place"]["id"]
    elif problem == "duplicate":
        planned["days"][0]["activities"].append(deepcopy(stop))
    else:
        candidate["destination"] = "成都"
    research = research_for(trip, candidate)
    with pytest.raises(ValueError):
        research.submit(planned, TripRequest(destination="杭州", days=1))
    assert research.submitted_candidate_ids == [] and research.candidates[candidate["id"]] == candidate


def test_candidate_id_cannot_collide_with_existing_schedule():
    trip, candidate = fixture()
    candidate["id"] = trip["days"][0]["activities"][0]["id"]
    with pytest.raises(ValueError, match="unique"):
        research_for(trip, candidate)


def test_route_can_use_trusted_same_city_candidate_without_exposing_it_as_available_place():
    trip, candidate = fixture()
    research = research_for(trip, candidate)
    async def run():
        result = await research.route(trip["days"][0]["activities"][0]["place"]["id"], candidate["place"]["id"])
        assert result["status"] == "unknown"
        assert candidate["place"]["id"] not in research.places and not research.tools.calls
    asyncio.run(run())


def test_two_agent_feedback_loop_keeps_candidates_separate_and_only_reports_explicit_selection():
    trip, candidate = fixture()
    context = {**trip, "candidates": [candidate]}
    planned = plan_with_candidate(trip, candidate)
    calls = 0
    def execute(messages):
        nonlocal calls
        calls += 1
        value = json.loads(messages[1]["content"])
        assert value["current_trip"] == trip
        assert value["current_candidates"][0]["id"] == candidate["id"]
        assert candidate["place"]["id"] not in {item["id"] for item in value["available_places"]}
        assert "只有用户明确要求" in messages[0]["content"]
        if calls == 1:
            return reply(name="report_plan_issue", arguments={"reason": "请明确保留候选原时长"})
        return reply(name="submit_trip", arguments=planned)
    async def run():
        model = FakeModel([trip_route(), trip_route()], execute)
        session, events = session_for(model, Maps(names=[]))
        await session.chat("请把候选公园安排到十二点", trip_context=context)
        state = await finish(session)
        assert state["status"] == "completed" and state["replans"] == 1
        assert len(model.planner_calls) == 2 and not model.summary_calls
        assert "修改草稿已生成，尚未应用" in state["content"]
        assert all(json.loads(messages[-1]["content"])["current_candidates"] == [candidate] for messages in model.planner_calls)
        assert state["candidate_snapshot"] == [candidate] and state["submitted_candidate_ids"] == [candidate["id"]]
        assert all("submitted_candidate_ids" not in event for event in events[:-1])
        assert context == {**trip, "candidates": [candidate]}
    asyncio.run(run())


def saved_conversation(app, *, kind="formal"):
    trip, candidate = fixture()
    data = {"schema": 1, "messages": [], "favorites": [candidate["id"]], **trip, "draft": None, "candidates": [candidate]}
    if kind in ("legacy", "draft"):
        data.update(draft=deepcopy(trip["days"]), draftTrip=deepcopy(trip["trip"]))
    if kind in ("draft", "candidate_only"):
        candidate = deepcopy(candidate)
        candidate.update(id="draft-candidate", description="仅草稿中的用户备注")
        data["draftCandidates"] = [candidate]
    document = app.state.store.create(ConversationInput(title="候选聊天固定资料", data={"schema": 1, "messages": [], "days": [], "draft": None}))
    app.state.store.mutate(document.id, lambda value: value.update(data))
    return document.id, trip, candidate


async def run_service(app, identifier, planned, *, destination="杭州", content="修改当前行程", execute=None):
    service = app.state.agents
    model = FakeModel([trip_route(destination=destination)], execute or (lambda _: reply(name="submit_trip", arguments=planned)))
    service.model, service.tools = model, Maps(names=[])
    await service.send(identifier, SendMessage(message_id=uuid4(), content=content))
    await asyncio.wait_for(service.turns[identifier], 2)
    state = await finish(service.sessions[identifier])
    return state, model


@pytest.mark.parametrize("kind", ["formal", "legacy", "draft", "candidate_only"])
def test_ordinary_chat_edit_preserves_effective_pool_and_formal_snapshot(app, kind):
    identifier, trip, candidate = saved_conversation(app, kind=kind)
    before = app.state.store.get(identifier).data
    planned = submission(trip, identifiers=True)
    planned["days"][0]["activities"][0]["time"] = "10:00"
    async def run():
        state, model = await run_service(app, identifier, planned)
        assert state["status"] == "completed", state
        saved = app.state.store.get(identifier).data
        assert saved["days"] == before["days"] and saved["candidates"] == before["candidates"]
        assert saved["draftCandidates"] == [candidate] and saved["draft"][0]["activities"][0]["time"] == "10:00"
        assert saved["favorites"] == before["favorites"]
        assert json.loads(model.planner_calls[0][-1]["content"])["current_candidates"] == [candidate]
        assert json.loads(model.planner_calls[0][-1]["content"])["current_trip"] == trip
        await app.state.agents.stop()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["formal", "draft"])
def test_explicit_chat_insertion_consumes_only_effective_candidate_atomically(app, kind):
    identifier, trip, candidate = saved_conversation(app, kind=kind)
    before = deepcopy(app.state.store.get(identifier).data)
    other = deepcopy(candidate)
    other["id"] = "unselected-candidate"
    field = "draftCandidates" if kind == "draft" else "candidates"
    app.state.store.mutate(identifier, lambda data: data[field].append(other))
    async def run():
        state, _ = await run_service(app, identifier, plan_with_candidate(trip, candidate), content="把候选公园安排到十二点")
        assert state["status"] == "completed", state
        saved = app.state.store.get(identifier).data
        activity = saved["draft"][0]["activities"][-1]
        for key in ("id", "place", "description", "duration", "cost"):
            assert activity[key] == candidate[key]
        assert saved["draftCandidates"] == [other] and saved["days"] == before["days"]
        assert saved["favorites"] == before["favorites"]
        await app.state.agents.stop()
    asyncio.run(run())


def test_city_change_keeps_old_city_candidate_and_uses_only_new_city_search(app):
    identifier, trip, candidate = saved_conversation(app)
    new_place = deepcopy(trip["days"][0]["activities"][0]["place"])
    new_place.update(id="baidu:chengdu", name="成都固定地点")
    planned = {"summary": "成都固定测试", "days": [{"day": 1, "title": "成都", "subtitle": "固定资料", "activities": [
        {"place_id": new_place["id"], "time": "09:00", "duration_minutes": 60, "description": "成都固定资料"}]}]}
    class ChengduMaps(Maps):
        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"structuredContent": {"results": [{"uid": "chengdu", "name": "成都固定地点", "address": "固定地址", "city": "成都市", "location": {"lng": 104.1, "lat": 30.6}}]}}
    def execute(messages):
        context = json.loads(messages[1]["content"])
        assert context["available_places"] == []
        return reply(name="submit_trip", arguments=planned) if messages[-1]["role"] == "tool" else reply(name="search_trip_places", arguments={"query": "成都固定地点"})
    async def run():
        service = app.state.agents
        service.model, service.tools = FakeModel([trip_route(destination="成都")], execute), ChengduMaps()
        await service.send(identifier, SendMessage(message_id=uuid4(), content="这次改为成都一天"))
        await asyncio.wait_for(service.turns[identifier], 2)
        state = await finish(service.sessions[identifier])
        assert state["status"] == "completed", state
        saved = app.state.store.get(identifier).data
        assert saved["draftTrip"]["destination"] == "成都" and saved["draftCandidates"] == [candidate]
        assert saved["trip"] == trip["trip"] and saved["candidates"] == [candidate]
        assert service.tools.calls[0][1]["region"] == "成都"
        await service.stop()
    asyncio.run(run())


@pytest.mark.parametrize("problem", ["stale_snapshot", "missing_selection", "changed_place"])
def test_service_rejects_stale_or_forged_candidate_result_without_writing_trip(app, problem):
    identifier, trip, candidate = saved_conversation(app)
    research = research_for(trip, candidate)
    result = research.submit(plan_with_candidate(trip, candidate), TripRequest(destination="杭州", days=1))
    state = {"run_id": "fixture-run", "status": "completed", "content": "固定结果", "trip_result": result,
             "candidate_snapshot": [candidate], "submitted_candidate_ids": [candidate["id"]]}
    if problem == "stale_snapshot":
        state["candidate_snapshot"] = []
    elif problem == "missing_selection":
        state["submitted_candidate_ids"] = []
    else:
        result["days"][0]["activities"][-1]["place"]["latitude"] = 31.0
    before = app.state.store.get(identifier).model_dump()
    async def run():
        session = app.state.agents.session(identifier)
        with pytest.raises(ValueError):
            await session.on_state(state)
        assert app.state.store.get(identifier).model_dump() == before
        await app.state.agents.stop()
    asyncio.run(run())


def test_cancelled_late_model_submission_never_consumes_candidate(app):
    identifier, trip, candidate = saved_conversation(app)
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class LateModel(FakeModel):
            async def chat(self, messages, tools=None, **kwargs):
                if tools and tools[0]["function"]["name"] != "route_request":
                    started.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                    return reply(name="submit_trip", arguments=plan_with_candidate(trip, candidate))
                return await super().chat(messages, tools, **kwargs)
        service = app.state.agents
        service.model, service.tools = LateModel([trip_route()]), Maps(names=[])
        await service.send(identifier, SendMessage(message_id=uuid4(), content="把候选安排到十二点"))
        await asyncio.wait_for(started.wait(), 2)
        cancelling = asyncio.create_task(service.cancel(identifier))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(cancelling, 2)
        saved = app.state.store.get(identifier).data
        assert saved["candidates"] == [candidate] and saved.get("draft") is None and saved.get("draftCandidates") is None
        assert saved["days"] == trip["days"] and saved["agent"]["status"] == "cancelled"
        await service.stop()
    asyncio.run(run())


def test_failed_final_save_keeps_schedule_and_candidate_pool_together(app, monkeypatch):
    identifier, trip, candidate = saved_conversation(app)
    save = app.state.store._save
    def reject_completion(document, previous=None):
        if document.data.get("agent", {}).get("status") == "completed":
            raise OSError("fixed final-save failure")
        return save(document, previous)
    monkeypatch.setattr(app.state.store, "_save", reject_completion)
    async def run():
        state, _ = await run_service(app, identifier, plan_with_candidate(trip, candidate))
        assert state["status"] == "failed"
        saved = app.state.store.get(identifier).data
        assert saved["days"] == trip["days"] and saved["candidates"] == [candidate]
        assert saved.get("draft") is None and saved.get("draftCandidates") is None
        await app.state.agents.stop()
    asyncio.run(run())


def test_direct_chat_answer_keeps_candidate_only_draft(app):
    identifier, trip, candidate = saved_conversation(app, kind="candidate_only")
    before = app.state.store.get(identifier).data
    async def run():
        service = app.state.agents
        service.model, service.tools = FakeModel([route("answer", "候选会继续保留。")]), Maps(names=[])
        await service.send(identifier, SendMessage(message_id=uuid4(), content="候选里的地点还在吗"))
        await asyncio.wait_for(service.turns[identifier], 2)
        saved = app.state.store.get(identifier).data
        for key in ("days", "trip", "draft", "candidates", "draftCandidates"):
            assert saved.get(key) == before.get(key)
        assert saved["draftCandidates"] == [candidate]
        await service.stop()
    asyncio.run(run())
