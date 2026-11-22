"""Offline contracts for chat, formal export and recovery of per-leg transport."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from supertravel_server.app import create_app
from supertravel_server.mcp_client import ToolError
from supertravel_server.trip_routes import unknown_route
from supertravel_server.trips import TripResearch, TripTimingError
from .test_agents import FakeModel, reply, session_for
from .test_candidates import insert, park, slots
from .test_exports import export
from .test_leg_editing import mixed_draft
from .test_leg_transport import set_mode
from .test_pdf_exports import compact, plain_text, read_pdf
from .test_place_selection import SearchTools, select as replace_place, token
from .test_trip_api import AUTH, app, save
from .test_trip_count_constraints import send_and_finish
from .test_trip_identity import request, submission
from .test_trip_mutations import move, post, read
from .test_trips import trip_route


class TwoDaysLater(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(days=2)


@pytest.mark.parametrize("planner_mode", [{}, {"transport_mode": "walking"}], ids=["omitted", "incorrect-default"])
def test_time_edit_exports_only_applied_legs_and_restores_snapshot_after_restart(app, tmp_path, planner_mode):
    with TestClient(app) as client:
        original, tools = mixed_draft(client)
        changed = post(client, original, "transport", mode="transit").json()
        changed = set_mode(client, changed, "e", None)
        data = deepcopy(changed["data"])
        data["draft"][0]["activities"][0].update(duration="约 45 分钟", description="仅在草稿的集合备注")
        data["draftCandidates"][0]["description"] = "用户刚写的候选备注"
        current = save(client, changed, data).json()
        expected = deepcopy(data["draft"])
        expected[0]["activities"][1]["time"] = "10:15"

        def execute(messages):
            context = json.loads(messages[1]["content"])
            assert context["current_trip"] == {"days": data["draft"], "trip": data["draftTrip"]}
            assert context["current_candidates"][0]["description"] == data["draftCandidates"][0]["description"]
            if messages[-1]["role"] != "tool":
                # Deliberate wrong-mode evidence for B -> C, which is pinned to walking.
                return reply(name="query_trip_route", arguments={"from_place_id": "baidu:b", "to_place_id": "baidu:c", "mode": "transit"})
            return reply(name="submit_trip", arguments=submission({"days": expected}, identifiers=True))

        app.state.agents.model = FakeModel([trip_route(days=2, **planner_mode)], execute)
        state = client.portal.call(send_and_finish, app.state.agents, UUID(current["id"]), "只把第一天第二站改到 10:15")
        assert state["status"] == "completed", state["error"]
        pending = read(app, current)
        assert pending["data"]["draftTrip"]["transportMode"] == "transit"
        assert pending["data"]["draft"] == expected
        assert pending["data"]["draftCandidates"] == data["draftCandidates"]
        assert len(tools.calls) == 7
        for field in ("days", "trip", "candidates", "favorites"):
            assert pending["data"][field] == original["data"][field]
        before = export(client, pending).json()["markdown"]
        assert "整程默认交通方式：驾车" in before and "仅在草稿的集合备注" not in before
        assert post(client, pending, "restore").json()["detail"]["code"] == "draft_exists"

        response = post(client, pending, "apply")
        assert response.status_code == 200, response.text
        applied = response.json()
        assert applied["data"]["days"] == expected and applied["data"]["candidates"] == data["draftCandidates"]
        markdown = export(client, applied).json()["markdown"]
        pdf = export(client, applied, format="pdf")
        assert pdf.status_code == 200, pdf.text
        for content in (markdown, plain_text(read_pdf(pdf.json())), pdf.json()["previewText"]):
            text = compact(content.replace(r"\:", ":"))
            assert "整程默认交通方式：公共交通" in text and "10:15" in text and "仅在草稿的集合备注" in text
            first, second = text.split("第2天")[-2:]
            assert "本段指定步行" in first and "步行约10分钟" in first
            assert "跟随整程" in first and "方案2号线" in first and "参考总票价：¥0.00" in first
            assert "方案3号线" in second and "参考总票价：¥2.29" in second
            assert "已知费用小计（人民币，用户填写）：¥50.00" in text and "费用未知项：4项" in text
            assert "用户刚写的候选备注" not in text
        assert read(app, applied) == applied and len(tools.calls) == 7

    restarted = create_app(tmp_path, "trip-api-fixture")
    restarted.state.agents.tools = tools
    with TestClient(restarted) as client:
        assert client.get(f"/api/conversations/{applied['id']}", headers=AUTH).json() == applied
        restored = post(client, applied, "restore").json()
        for field in ("days", "trip", "candidates", "favorites"):
            assert restored["data"][field] == original["data"][field]
        again = post(client, restored, "restore").json()
        for field in ("days", "trip", "candidates", "favorites"):
            assert again["data"][field] == applied["data"][field]
        assert len(tools.calls) == 7


@pytest.mark.parametrize("identifiers", [False, True])
def test_queries_use_this_visits_coordinates_and_never_borrow_another_days_choice(app, identifiers):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        days = deepcopy(doc["data"]["draft"])
        first, second = [day["activities"] for day in days]
        first[0]["place"]["sourceLocation"]["longitude"] = 121
        first[1]["place"]["sourceLocation"]["longitude"] = 122
        # Same POI pair on both days, but different coordinates and an as yet unverified first leg.
        first[1].pop("incomingRoute")
        research = TripResearch(tools, "杭州", days)
        with pytest.raises(ToolError, match="活动 ID"):
            asyncio.run(research.route("baidu:a", "baidu:b", "transit"))
        assert len(tools.calls) == 6
        for day, origin, target in ((2, "u", "v"), (1, "a", "b")):
            result = asyncio.run(research.route("baidu:a", "baidu:b", "transit", day=day, from_activity_id=origin, to_activity_id=target))
            if day == 2:
                assert result == second[1]["incomingRoute"]
        assert len(tools.calls) == 7
        assert tools.calls[-1][1] == {"model": "transit", "origin": "30.250000,121.000000", "destination": "30.250000,122.000000"}
        built = research.submit(submission({"days": days}, identifiers=identifiers), request(transport_mode="driving"))
        first[1]["incomingRoute"] = result
        assert built["days"] == days


@pytest.mark.parametrize(("operation", "apply_queries"), [("same_day", 1), ("cross_day", 3), ("replace", 2), ("insert", 0)])
def test_chat_after_manual_edit_then_apply_and_restore(app, operation, apply_queries):
    with TestClient(app) as client:
        original, tools = mixed_draft(client)
        if operation == "same_day":
            response = move(client, original, activity_id="a", target_day=1, target_index=4, time="19:00")
        elif operation == "cross_day":
            response = move(client, original, activity_id="e", target_day=2, target_index=1, time="10:00")
        elif operation == "replace":
            lookup = app.state.agents.tools = SearchTools()
            response = replace_place(client, original, token(client, original), activity_id="b")
            assert len(lookup.calls) == 1
            app.state.agents.tools = tools
        else:
            parked = park(client, original, "c")
            options = slots(client, parked, candidate_id="c").json()["options"]
            response = insert(client, parked, next(item for item in options if item["position"] == 2), "13:00")
        assert response.status_code == 200, response.text
        edited = response.json()
        expected = deepcopy(edited["data"]["draft"])
        expected[1]["activities"][-1]["time"] = "16:15"
        for day in expected:
            for previous, target in zip(day["activities"], day["activities"][1:]):
                if "incomingRoute" not in target:
                    assert "incomingTransportOverride" not in target
                    target["incomingRoute"] = unknown_route(previous["place"], target["place"], "driving")
        count = len(tools.calls)
        assert count == (14 if operation == "insert" else 6)
        payload = submission({"days": expected}, identifiers=True)
        app.state.agents.model = FakeModel([trip_route(days=2)], lambda _: reply(name="submit_trip", arguments=payload))
        state = client.portal.call(send_and_finish, app.state.agents, UUID(edited["id"]), "只把第二天末项改到 16:15")
        assert state["status"] == "completed" and len(tools.calls) == count
        draft = read(app, edited)
        assert draft["data"]["draft"] == expected and draft["data"]["days"] == original["data"]["days"]
        assert draft["data"]["draftCandidates"] == edited["data"]["draftCandidates"]
        assert draft["data"]["favorites"] == edited["data"]["favorites"]
        applied = post(client, draft, "apply")
        assert applied.status_code == 200, applied.text
        restored = post(client, applied.json(), "restore").json()
        for field in ("days", "trip", "candidates", "favorites"):
            assert restored["data"][field] == original["data"][field]
        assert len(tools.calls) == count + apply_queries


def test_chat_reorder_invalidates_old_overrides_and_evidence_even_for_a_repeated_pair(app):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        days = deepcopy(doc["data"]["draft"])
        research = TripResearch(tools, "杭州", days)
        first, second = [day["activities"] for day in days]
        first[:2], second[:2] = second[:2], first[:2]
        for source, times in ((first, ("09:00", "11:00")), (second, ("08:00", "12:00"))):
            for stop, time in zip(source, times):
                stop["time"] = time
        result = research.submit(submission({"days": days}, identifiers=True), request(transport_mode="walking"))
        for day in result["days"]:
            for item in day["activities"][1:3]:
                assert "incomingTransportOverride" not in item
                assert item["incomingRoute"]["mode"] == "walking" and item["incomingRoute"]["status"] == "unknown"
        assert result["days"][0]["activities"][3:] == days[0]["activities"][3:]
        assert len(tools.calls) == 6


def test_mixed_leg_timing_uses_real_walking_minutes_not_the_default_cache(app):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        days = doc["data"]["draft"]
        before, target = days[0]["activities"][1:3]
        before.update(time="09:00", duration="约 45 分钟")
        days[0]["activities"][0]["time"] = "07:00"
        target["incomingRoute"]["durationMinutes"] = 20
        target["time"] = "10:00"
        research = TripResearch(tools, "杭州", days)
        asyncio.run(research.route("baidu:b", "baidu:c", "driving"))
        with pytest.raises(TripTimingError, match="还差 5 分钟"):
            research.submit(submission({"days": days}, identifiers=True), request(transport_mode="driving"))
        target["time"] = "10:15"
        fixed = research.submit(submission({"days": days}, identifiers=True), request(transport_mode="driving"))
        assert fixed["days"][0]["activities"][2]["incomingRoute"]["durationMinutes"] == 20


@pytest.mark.parametrize("field", ["incomingTransportOverride", "incomingRoute", "selection"])
def test_submit_does_not_accept_model_supplied_transport_fields(app, field):
    with TestClient(app) as client:
        original, tools = mixed_draft(client)
        days = original["data"]["draft"]
        research = TripResearch(tools, "杭州", days)
        payload = submission({"days": days}, identifiers=True)
        payload["days"][0]["activities"][1][field] = days[0]["activities"][1].get(field, "user")
        with pytest.raises(ValueError, match="行程字段无效"):
            research.submit(payload, request(transport_mode="driving"))
        assert read(app, original) == original and len(tools.calls) == 6


def test_expired_choice_keeps_the_active_draft_and_requires_manual_reselection(app):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        def expire(data):
            data["draft"][0]["activities"][1]["incomingRoute"]["queriedAt"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        current = app.state.store.mutate(UUID(doc["id"]), expire).model_dump(mode="json")
        planned = submission({"days": current["data"]["draft"]}, identifiers=True)
        model = app.state.agents.model = FakeModel([trip_route(days=2, transport_mode="driving")], lambda _: reply(name="submit_trip", arguments=planned))
        state = client.portal.call(send_and_finish, app.state.agents, UUID(doc["id"]), "只改时间，保留原选公交")
        assert state["status"] == "needs_input" and "重新比较" in state["content"]
        for field in ("days", "draft", "trip", "draftTrip", "candidates", "draftCandidates", "favorites"):
            assert read(app, doc)["data"][field] == current["data"][field]
        assert len(model.planner_calls) == 1 and len(tools.calls) == 6


@pytest.mark.parametrize(("message", "scope", "proposed", "kind"), [
    ("只把公园到博物馆这段改为驾车", "leg", "driving", "answer"),
    ("只把公园到博物馆这段改为驾车", "whole_trip", "driving", "answer"),
    ("整程驾车，只把公园到博物馆改为步行", None, "walking", "answer"),
    ("不要把全程改成步行，只改公园到博物馆", "whole_trip", "walking", "clarification"),
])
def test_single_leg_chat_points_to_editor_without_starting_execution(app, message, scope, proposed, kind):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        context = {"trip": doc["data"]["draftTrip"], "days": doc["data"]["draft"]}
        plan = reply(name="route_request", arguments={"kind": "plan", "content": "修改出行方式", "steps": ["提交"],
                     "trip": {"destination": "杭州", "days": 2, "transport_mode": proposed}, "transport_scope": scope})
        session, events = session_for(FakeModel([plan]), tools)
        result = asyncio.run(session.chat(message, trip_context=context))
        assert result["kind"] == kind and "接驳编辑" in result["content"]
        assert not session.model.executor_calls and not events and len(tools.calls) == 6
        assert read(app, doc) == doc


def test_whole_trip_chat_changes_only_following_legs(app):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        doc = set_mode(client, doc, "e", None)
        before = doc["data"]["draft"]
        model = FakeModel([trip_route(days=2, transport_mode="walking")],
                          lambda _: reply(name="submit_trip", arguments=submission({"days": before}, identifiers=True)))
        app.state.agents.model = model
        state = client.portal.call(send_and_finish, app.state.agents, UUID(doc["id"]), "我想把整趟旅行的默认方式换成步行吧。")
        assert state["status"] == "completed"
        data = read(app, doc)["data"]
        assert data["draftTrip"]["transportMode"] == "walking" and data["days"] == doc["data"]["days"]
        for day, old_day in zip(data["draft"], before):
            for item, old in zip(day["activities"], old_day["activities"]):
                if item["id"] == "e":
                    assert "incomingTransportOverride" not in item
                    assert item["incomingRoute"]["mode"] == "walking" and item["incomingRoute"]["status"] == "unknown"
                else:
                    assert item == old
        assert len(tools.calls) == 6


def test_changing_default_does_not_block_on_an_expired_choice_that_no_longer_applies(app):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        transit = post(client, doc, "transport", mode="transit").json()
        following = set_mode(client, transit, "e", None)
        days = following["data"]["draft"]
        target = days[0]["activities"][3]
        assert "incomingTransportOverride" not in target and target["incomingRoute"]["selection"] == "user"
        target["incomingRoute"]["queriedAt"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        research = TripResearch(tools, "杭州", days, default_mode="walking")
        queried = asyncio.run(research.route("baidu:c", "baidu:e", "transit"))
        assert "selection" not in queried
        result = research.submit(submission({"days": days}, identifiers=True), request(transport_mode="walking"))
        current = result["days"][0]["activities"][3]
        assert "incomingTransportOverride" not in current
        assert current["incomingRoute"]["mode"] == "walking" and current["incomingRoute"]["status"] == "unknown"
        assert result["days"][0]["activities"][1] == days[0]["activities"][1] and len(tools.calls) == 7


def test_unknown_and_stale_walking_proof_never_borrow_the_other_days_transit(app):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        days = deepcopy(doc["data"]["draft"])
        target = days[0]["activities"][2]
        route = target["incomingRoute"]
        route["queriedAt"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        research = TripResearch(tools, "杭州", days, default_mode="driving")
        payload = submission({"days": days}, identifiers=True)
        assert research.submit(payload, request(transport_mode="driving"))["days"] == days
        tools.error = ToolError("tool_quota", False, "离线配额夹具")
        args = {"day": 1, "from_activity_id": "b", "to_activity_id": "c"}
        failed = asyncio.run(research.route("baidu:b", "baidu:c", "transit", **args))
        assert failed["mode"] == "walking" and failed["status"] == "unknown" and "额度" in failed["reason"]
        assert "durationMinutes" not in failed and "transit" not in failed
        assert asyncio.run(research.route("baidu:b", "baidu:c", **args)) == failed
        target["incomingRoute"] = failed
        assert research.submit(payload, request(transport_mode="driving"))["days"] == days
        assert len(tools.calls) == 7 and tools.calls[-1][1]["model"] == "walking"


def test_selection_expiring_between_chat_submission_and_storage_keeps_both_snapshots(app, monkeypatch):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        service = app.state.agents
        days = doc["data"]["draft"]
        payload = submission({"days": days}, identifiers=True)
        service.model = FakeModel([trip_route(days=2)], lambda _: reply(name="submit_trip", arguments=payload))
        session = service.session(UUID(doc["id"]))
        summarize = session._summarize

        async def advance_after_submit(*args, **kwargs):
            content = await summarize(*args, **kwargs)
            monkeypatch.setattr("supertravel_server.trip_routes.datetime", TwoDaysLater)
            return content

        monkeypatch.setattr(session, "_summarize", advance_after_submit)
        state = client.portal.call(send_and_finish, service, UUID(doc["id"]), "只改时间")
        assert state["status"] == "needs_input" and "重新比较" in state["content"] and "trip_result" not in state
        data = read(app, doc)["data"]
        for field in ("days", "draft", "trip", "draftTrip", "candidates", "draftCandidates", "favorites"):
            assert data[field] == doc["data"][field]
        assert len(service.model.planner_calls) == 1 and len(tools.calls) == 6


def test_restoring_an_expired_mixed_snapshot_keeps_timestamps_and_exports_without_old_quotes(app, monkeypatch):
    with TestClient(app) as client:
        doc, tools = mixed_draft(client)
        data = deepcopy(doc["data"])
        data["draft"][0]["activities"][0]["description"] = "新版备注"
        applied = post(client, save(client, doc, data).json(), "apply").json()
        assert applied["data"]["days"][0]["activities"][0]["description"] == "新版备注"

        monkeypatch.setattr("supertravel_server.trip_routes.datetime", TwoDaysLater)
        restored = post(client, applied, "restore").json()
        for field in ("days", "trip", "candidates", "favorites"):
            assert restored["data"][field] == doc["data"][field]
        before = restored["data"]["days"][0]["activities"][1]["incomingRoute"]
        pdf = export(client, restored, format="pdf").json()
        for content in (pdf["previewText"], plain_text(read_pdf(pdf))):
            text = compact(content)
            assert before["queriedAt"] in text and "用户选用的公交方案请重新比较" in text
            assert "本段指定步行" in text and "已过期或查询时间无效" in text
            assert "方案2号线" not in text and "参考总票价：" not in text
            assert "用户填写）：¥50.00" in text
        assert read(app, restored) == restored and len(tools.calls) == 6
