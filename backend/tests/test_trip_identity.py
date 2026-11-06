import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from backend.agent_service import SendMessage
from backend.main import ConversationInput
from backend.tests.test_agents import FakeModel, finish, reply, session_for
from backend.tests.test_trip_api import app, trip_result
from backend.tests.test_trips import Maps, trip_route
from backend.trips import TripRequest, TripResearch, validate_result


def existing_trip(*, repeated=False):
    value = trip_result("用户备注：先取预约票")
    first = value["days"][0]["activities"][0]
    first["id"] = "activity-a"
    first["cost"] = {"amountFen": 2580, "currency": "CNY", "source": "user"}
    second = deepcopy(first)
    second.update(id="activity-b", time="11:00", description="用户备注：与朋友会合")
    second["cost"]["amountFen"] = 0
    if not repeated:
        second["place"].update(id="baidu:second", name="第二个测试地点")
        second["title"] = second["place"]["name"]
    value["days"].append({"day": 2, "title": "第二天", "subtitle": "固定测试", "activities": [second]})
    return value


def submission(value, *, identifiers=False):
    days = []
    for day in value["days"]:
        activities = []
        for activity in day["activities"]:
            stop = {"place_id": activity["place"]["id"], "time": activity["time"],
                    "duration_minutes": int(activity["duration"].split()[1]), "description": "模型普通重述，不应覆盖用户备注"}
            if identifiers:
                stop["activity_id"] = activity["id"]
            activities.append(stop)
        days.append({**{key: day[key] for key in ("day", "title", "subtitle")}, "activities": activities})
    return {"summary": "固定测试修改草稿", "days": days}


def request(days=2, **values):
    return TripRequest(destination="杭州", days=days, **values)


def test_saved_trip_accepts_empty_day_but_rejects_completely_empty_trip():
    value = existing_trip()
    value["days"][0]["activities"] = []
    assert validate_result(value)["days"][0]["activities"] == []
    value["days"][1]["activities"] = []
    with pytest.raises(ValueError, match="至少需要保留一个活动"):
        validate_result(value)


def test_first_generation_requires_each_day_nonempty_while_edits_keep_free_days():
    existing = existing_trip()
    planned = submission(existing)
    planned["days"][0]["activities"] = []
    research = TripResearch(Maps(), "杭州")
    research.places = {activity["place"]["id"]: activity["place"] for day in existing["days"] for activity in day["activities"]}
    with pytest.raises(ValueError, match="首次生成"):
        research.submit(planned, request())
    edited = TripResearch(Maps(), "杭州", existing["days"]).submit(planned, request())
    assert edited["days"][0]["activities"] == []
    assert edited["days"][1]["activities"][0]["id"] == "activity-b"
    with pytest.raises(ValueError, match="恰好安排 1"):
        TripResearch(Maps(), "杭州", existing["days"]).submit(planned, request(stops_per_day=1))


@pytest.mark.parametrize("identifiers", [False, True])
def test_moving_unique_activity_retains_identity_notes_cost_and_does_not_mutate_context(identifiers):
    existing = existing_trip()
    original = deepcopy(existing)
    planned = submission(existing, identifiers=identifiers)
    moved = planned["days"][0]["activities"].pop()
    moved["time"] = "09:15"
    planned["days"][1]["activities"].insert(0, moved)
    result = TripResearch(Maps(), "杭州", existing["days"]).submit(planned, request())
    assert result["days"][0]["activities"] == []
    activity = result["days"][1]["activities"][0]
    assert activity["id"] == "activity-a" and activity["time"] == "09:15"
    assert activity["description"] == original["days"][0]["activities"][0]["description"]
    assert activity["cost"] == original["days"][0]["activities"][0]["cost"]
    assert result["days"][1]["activities"][1]["cost"]["amountFen"] == 0
    assert existing == original
    activity["cost"]["amountFen"] = 9999
    assert existing == original


def test_explicit_ids_keep_repeated_poi_visits_distinct_when_days_are_swapped():
    existing = existing_trip(repeated=True)
    planned = submission(existing, identifiers=True)
    planned["days"][0]["activities"], planned["days"][1]["activities"] = planned["days"][1]["activities"], planned["days"][0]["activities"]
    result = TripResearch(Maps(), "杭州", existing["days"]).submit(planned, request())
    for output, original in zip(result["days"], reversed(existing["days"])):
        activity, previous = output["activities"][0], original["activities"][0]
        assert activity["id"] == previous["id"]
        assert activity["description"] == previous["description"] and activity["cost"] == previous["cost"]


def test_legacy_same_day_match_is_stable_and_ambiguous_cross_day_reference_is_rejected():
    existing = existing_trip(repeated=True)
    research = TripResearch(Maps(), "杭州", existing["days"])
    result = research.submit(submission(existing), request())
    assert [day["activities"][0]["id"] for day in result["days"]] == ["activity-a", "activity-b"]
    planned = submission(existing)
    stop = planned["days"][0]["activities"].pop()
    planned["days"][1]["activities"] = []
    planned["days"].append({"day": 3, "title": "第三天", "subtitle": "移入一天", "activities": [stop]})
    with pytest.raises(ValueError, match="多个已有活动"):
        research.submit(planned, request(3))
    stop["activity_id"] = "activity-b"
    assert research.submit(planned, request(3))["days"][2]["activities"][0]["id"] == "activity-b"


@pytest.mark.parametrize("problem", ["unknown", "mismatch", "duplicate"])
def test_invalid_explicit_identity_is_rejected_without_falling_back(problem):
    existing = existing_trip()
    planned = submission(existing, identifiers=True)
    if problem == "unknown":
        planned["days"][0]["activities"][0]["activity_id"] = "not-known"
    elif problem == "mismatch":
        planned["days"][0]["activities"][0]["activity_id"] = "activity-b"
    else:
        planned["days"][1]["activities"] = [deepcopy(planned["days"][0]["activities"][0])]
    research = TripResearch(Maps(), "杭州", existing["days"])
    with pytest.raises(ValueError, match="activity_id"):
        research.submit(planned, request())
    assert research.submit(submission(existing, identifiers=True), request())["days"][0]["activities"][0]["id"] == "activity-a"


def test_explicit_identity_is_reserved_before_legacy_entries_are_matched():
    existing = existing_trip()
    planned = submission(existing, identifiers=True)
    old = planned["days"][0]["activities"].pop()
    new_visit = deepcopy(old)
    new_visit.pop("activity_id")
    planned["days"][0]["activities"].append(new_visit)
    planned["days"][1]["activities"].insert(0, old)
    result = TripResearch(Maps(), "杭州", existing["days"]).submit(planned, request())
    added = result["days"][0]["activities"][0]
    moved = result["days"][1]["activities"][0]
    assert added["id"] not in {"activity-a", "activity-b"} and "cost" not in added
    assert added["description"] == new_visit["description"]
    assert moved["id"] == "activity-a" and moved["cost"]["amountFen"] == 2580


def test_existing_notes_change_only_with_explicit_override_and_new_notes_still_work():
    existing = existing_trip()
    planned = submission(existing, identifiers=True)
    planned["days"][0]["activities"][0].update(description="用户明确要求的新备注", replace_description=True)
    result = TripResearch(Maps(), "杭州", existing["days"]).submit(planned, request())
    assert result["days"][0]["activities"][0]["description"] == "用户明确要求的新备注"
    assert result["days"][1]["activities"][0]["description"] == existing["days"][1]["activities"][0]["description"]
    research = TripResearch(Maps(), "杭州")
    research.places = {existing["days"][0]["activities"][0]["place"]["id"]: existing["days"][0]["activities"][0]["place"]}
    new_plan = submission(existing)
    new_plan["days"] = new_plan["days"][:1]
    assert research.submit(new_plan, request(1))["days"][0]["activities"][0]["description"] == new_plan["days"][0]["activities"][0]["description"]


def test_two_agent_execution_uses_moved_draft_ids_and_preserves_notes_without_place_queries():
    existing = existing_trip()
    moved = existing["days"][0]["activities"].pop()
    existing["days"][1]["activities"].insert(0, moved)
    planned = submission(existing, identifiers=True)
    planned["days"][1]["activities"][0]["time"] = "09:15"
    async def run():
        def execute(messages):
            context = json.loads(messages[1]["content"])
            assert context["current_trip"] == existing
            assert "activity_id" in messages[0]["content"] and "replace_description=false" in messages[0]["content"]
            return reply(name="submit_trip", arguments=planned)
        model = FakeModel([trip_route(days=2)], execute)
        tools = Maps(names=[])
        session, _ = session_for(model, tools)
        await session.chat("把第二天第一个活动的时间改到09:15，其他保持", trip_context=existing)
        state = await finish(session)
        assert state["status"] == "completed"
        assert model.planner_calls and model.executor_calls and not model.summary_calls
        assert "修改草稿已生成，尚未应用" in state["content"]
        assert not tools.calls
        activity = state["trip_result"]["days"][1]["activities"][0]
        assert activity["id"] == moved["id"] and activity["description"] == moved["description"] and activity["cost"] == moved["cost"]
    asyncio.run(run())


def test_chat_after_move_writes_preserved_activity_to_draft_and_leaves_formal_untouched(app):
    original = existing_trip()
    moved_trip = deepcopy(original)
    moved = moved_trip["days"][0]["activities"].pop()
    moved_trip["days"][1]["activities"].insert(0, moved)
    planned = submission(moved_trip, identifiers=True)
    planned["days"][1]["activities"][0]["duration_minutes"] = 75
    document = app.state.store.create(ConversationInput(title="移动后继续聊天", data={"schema": 1, "days": [], "draft": None, "messages": [], "favorites": []}))
    app.state.store.mutate(document.id, lambda data: data.update(**original, draft=moved_trip["days"], draftTrip=moved_trip["trip"], favorites=[moved["id"]]))
    async def run():
        service = app.state.agents
        model = FakeModel([trip_route(days=2)], lambda _: reply(name="submit_trip", arguments=planned))
        service.model, service.tools = model, Maps(names=[])
        await service.send(document.id, SendMessage(message_id=uuid4(), content="把第二天第一个活动延长到75分钟，其他不变"))
        await asyncio.wait_for(service.turns[document.id], 2)
        state = await finish(service.sessions[document.id])
        assert state["status"] == "completed"
        saved = app.state.store.get(document.id).data
        assert saved["days"] == original["days"] and saved["trip"] == original["trip"]
        assert saved["draft"][0]["activities"] == []
        activity = saved["draft"][1]["activities"][0]
        assert activity["duration"] == "约 75 分钟"
        assert activity["id"] == moved["id"] and activity["cost"] == moved["cost"] and activity["description"] == moved["description"]
        assert saved["favorites"] == [moved["id"]]
        assert json.loads(model.planner_calls[0][-1]["content"])["current_trip"] == moved_trip
        await service.stop()
    asyncio.run(run())


def test_legacy_same_day_identity_is_reserved_before_a_new_earlier_visit():
    existing = existing_trip()
    planned = submission(existing)
    planned["days"][0]["activities"] = [deepcopy(planned["days"][1]["activities"][0])]
    result = TripResearch(Maps(), "杭州", existing["days"]).submit(planned, request())
    added = result["days"][0]["activities"][0]
    unchanged = result["days"][1]["activities"][0]
    assert added["id"] not in {"activity-a", "activity-b"} and "cost" not in added
    assert unchanged["id"] == "activity-b" and unchanged["cost"]["amountFen"] == 0
    assert unchanged["description"] == existing["days"][1]["activities"][0]["description"]


def test_legacy_unique_original_cannot_be_assigned_arbitrarily_to_two_new_days():
    existing = existing_trip()
    planned = submission(existing)
    moved = planned["days"][1]["activities"].pop()
    planned["days"][0]["activities"] = [deepcopy(moved)]
    planned["days"].append({"day": 3, "title": "第三天", "subtitle": "另一次访问", "activities": [moved]})
    research = TripResearch(Maps(), "杭州", existing["days"])
    with pytest.raises(ValueError, match="多个目标安排"):
        research.submit(planned, request(3))
    moved["activity_id"] = "activity-b"
    result = research.submit(planned, request(3))
    assert result["days"][2]["activities"][0]["id"] == "activity-b"
    assert result["days"][0]["activities"][0]["id"] not in {"activity-a", "activity-b"}
