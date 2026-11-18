"""D2 offline contracts: one response, scoped tickets, explicit draft selection."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from supertravel_server import trip_api
from supertravel_server.agent_service import SendMessage
from supertravel_server.app import create_app
from supertravel_server.mcp_client import ToolError
from supertravel_server.trip_routes import RouteSelectionExpired, query_route, query_transit_options
from supertravel_server.trips import IncomingRoute, TripResearch
from .test_agents import FakeModel, finish, reply
from .test_exports import export
from .test_pdf_exports import read_pdf, plain_text, compact
from .test_trip_api import AUTH, app, save
from .test_trip_identity import submission, request
from .test_trip_mutations import post, read
from .test_trip_transport import setup_document
from .test_transit_details import baidu_path, amap_path
from .test_trips import trip_route


def paths(provider="baidu", count=5):
    result = []
    for i in range(count):
        price = [9, 0, "2.29", None, 5][i]
        path = baidu_path(price) if provider == "baidu" else amap_path(price)
        path.update(duration=(i + 1) * 600, distance=(i + 1) * 1000)
        if provider == "baidu":
            path["steps"][1][0]["vehicle"]["name"] = f"离线方案{i + 1}号线𠮷🏞️"
        else:
            path["segments"][0]["bus"]["buslines"][0]["name"] = f"离线方案{i + 1}号线𠮷🏞️"
        result.append(path)
    return result


def response(provider="baidu", values=None):
    values = paths(provider) if values is None else values
    return {"structuredContent": {"status": 0, "result": {"routes": values, "taxi": {"detail": [{"total_price": 0, "desc": "白天"}]}}}} if provider == "baidu" else {
        "structuredContent": {"status": "1", "route": {"transits": values, "taxi_cost": "0"}}}


class OptionsTools:
    def __init__(self, provider="baidu", result=None, error=None, during=None):
        self.provider, self.result = provider, response(provider) if result is None else result
        self.calls, self.error, self.during = [], error, during

    async def list_tools(self):
        return [{"name": "baidu_map_directions" if self.provider == "baidu" else "amap_maps_direction_transit_integrated", "inputSchema": {"type": "object"}}]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.during:
            self.during()
        if self.error:
            raise self.error
        return deepcopy(self.result)


def pair(provider):
    origin = {"id": provider + ":a", "provider": provider, "sourceLocation": {
        "latitude": 30.2, "longitude": 120.1, "coordinateSystem": "bd09ll" if provider == "baidu" else "gcj02"}}
    return origin, {**deepcopy(origin), "id": provider + ":b"}


@pytest.mark.parametrize("provider", ["baidu", "amap"])
@pytest.mark.parametrize("count", [0, 1, 2, 5])
def test_options_count_order_single_response_and_zero_reference(provider, count):
    tools = OptionsTools(provider, response(provider, paths(provider, count)))
    options, reason = asyncio.run(query_transit_options(tools, *pair(provider), "杭州"))
    assert len(tools.calls) == 1 and len(options) == min(count, 3)
    assert [route["durationMinutes"] for route in options] == [10, 20, 30][:count]
    assert bool(reason) == (count == 0)
    for i, route in enumerate(options):
        IncomingRoute.model_validate(route)
        assert route["distanceMeters"] == (i + 1) * 1000
        assert route["transit"]["fare"]["amountFen"] == [900, 0, 229][i]
        assert f"离线方案{i + 1}号线𠮷🏞️" in str(route)
        assert "同段备选" not in str(route)
        assert route["taxiEstimates"][0]["amountFen"] == 0
        assert "selection" not in route
    assert len({route["queriedAt"] for route in options}) <= 1


@pytest.mark.parametrize("provider", ["baidu", "amap"])
def test_skip_bad_top_level_deduplicate_without_changing_default_first(provider):
    valid = paths(provider)
    values = [None, {"duration": True, "distance": 3}, {"duration": "NaN", "distance": 0}, valid[0], deepcopy(valid[0]), *valid[1:]]
    tools = OptionsTools(provider, response(provider, values))
    options, _ = asyncio.run(query_transit_options(tools, *pair(provider), "杭州"))
    assert [route["durationMinutes"] for route in options] == [10, 20, 30]
    assert asyncio.run(query_route(tools, *pair(provider), "transit", "杭州"))["status"] == "unknown"


def test_do_not_combine_response_representations_or_invent_missing_fares():
    values = paths(count=2)
    values[0]["price"], values[1]["price"] = "1.001", None
    raw = response(values=values)
    raw["content"] = [{"type": "text", "text": json.dumps(response()["structuredContent"])}]
    options, _ = asyncio.run(query_transit_options(OptionsTools(result=raw), *pair("baidu"), "杭州"))
    assert len(options) == 2 and all("fare" not in route["transit"] for route in options)


@pytest.mark.parametrize("error", [ToolError("tool_quota", False, "fixture"), TimeoutError()])
def test_query_failure_has_no_retry(error):
    tools = OptionsTools(error=error)
    options, reason = asyncio.run(query_transit_options(tools, *pair("baidu"), "杭州"))
    assert not options and reason and len(tools.calls) == 1


def query(client, doc, target="b"):
    result = post(client, doc, "routes/options", activity_id=target)
    assert result.status_code == 200, result.text
    return result.json()


def select(client, doc, index=1, target="b"):
    options = query(client, doc, target)
    result = post(client, doc, "routes/select", option_id=options["options"][index]["option_id"])
    assert result.status_code == 200, result.text
    return result.json()


def choice(doc, day=0, snapshot="draft"):
    return doc["data"][snapshot][day]["activities"][1]["incomingRoute"]


def test_independent_same_pair_choices_apply_chat_restart_exports_and_restore(app, tmp_path):
    tools = app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="transit", draft=False)
        def repeat(data):
            data["days"][0]["activities"] = data["days"][0]["activities"][:2]
            data["days"][1]["activities"] = deepcopy(data["days"][0]["activities"])
            for item in data["days"][1]["activities"]:
                item["id"] += "-day2"
        original = app.state.store.mutate(UUID(original["id"]), repeat).model_dump(mode="json")
        first = select(client, original, 1)
        second = select(client, first, 2, "b-day2")
        assert second["data"]["days"] == original["data"]["days"]
        assert [choice(second, day)["durationMinutes"] for day in (0, 1)] == [20, 30]
        assert len(tools.calls) == 2
        applied = post(client, second, "apply").json()
        assert [choice(applied, day, "days") for day in (0, 1)] == [choice(second, day) for day in (0, 1)]
        assert len(tools.calls) == 2

        planned = submission({"days": applied["data"]["days"]}, identifiers=True)
        planned["days"][0]["activities"][1]["time"] = "11:15"
        app.state.agents.model = FakeModel([trip_route(days=2, transport_mode="transit")], lambda _: reply(name="submit_trip", arguments=planned))
        async def chat():
            service = app.state.agents
            await service.send(UUID(applied["id"]), SendMessage(message_id=uuid4(), content="第一天第二个活动改为11:15，其他保持"))
            await asyncio.wait_for(service.turns[UUID(applied["id"])], 3)
            assert (await finish(service.sessions[UUID(applied["id"])]))["status"] == "completed"
        client.portal.call(chat)
        chatted = read(app, applied)
        assert chatted["data"]["days"] == applied["data"]["days"]
        assert [choice(chatted, day) for day in (0, 1)] == [choice(second, day) for day in (0, 1)]
        assert chatted["data"]["draft"][0]["activities"][1]["time"] == "11:15"
        for day in (0, 1):
            for old, new in zip(applied["data"]["days"][day]["activities"], chatted["data"]["draft"][day]["activities"]):
                for key in ("id", "description", "cost"):
                    assert new.get(key) == old.get(key)
        final = post(client, chatted, "apply").json()
        assert len(tools.calls) == 2
        for field in ("candidates", "favorites"):
            assert final["data"][field] == original["data"][field]
        md = export(client, final).json()["markdown"]
        pdf = export(client, final, format="pdf")
        assert pdf.status_code == 200, pdf.text
        for content in (compact(md), compact(plain_text(read_pdf(pdf.json())))):
            assert "用户选用方案" in content and "供应商首方案" not in content
            assert "方案2号线" in content and "方案3号线" in content
            assert "¥0.00" in content and "¥2.29" in content and "¥50.00" in content
        restored = post(client, final, "restore").json()
        assert restored["data"]["days"] == applied["data"]["days"]
    with TestClient(create_app(tmp_path, "trip-api-fixture")) as restarted:
        assert restarted.get(f"/api/conversations/{restored['id']}", headers=AUTH).json() == restored


@pytest.mark.parametrize("invalidator", ["new_query", "other_leg", "configuration", "ttl", "revision", "busy", "forged", "other_conversation", "restart"])
def test_options_are_read_only_scoped_and_expire(app, tmp_path, monkeypatch, invalidator):
    app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        options = query(client, original)
        assert read(app, original) == original
        token = options["options"][1]["option_id"]
        current = original
        if invalidator == "new_query": query(client, original)
        elif invalidator == "other_leg": query(client, original, "c")
        elif invalidator == "configuration": app.state.agents.tools = OptionsTools()
        elif invalidator == "ttl": monkeypatch.setattr(trip_api, "monotonic", lambda: float("inf"))
        elif invalidator in ("revision", "busy"):
            current = app.state.store.mutate(UUID(original["id"]), lambda data: data.update(agentPending=invalidator == "busy")).model_dump(mode="json")
        elif invalidator == "forged": token = "made-up-ticket"
        elif invalidator == "other_conversation": current = setup_document(client, mode="transit")
        elif invalidator == "restart":
            with TestClient(create_app(tmp_path, "trip-api-fixture")) as other:
                assert post(other, original, "routes/select", option_id=token).status_code == 409
            return
        assert post(client, current, "routes/select", option_id=token).status_code == 409
        assert read(app, current) == current


@pytest.mark.parametrize("change", ["revision", "busy"])
def test_query_rechecks_revision_and_busy_after_await(app, change):
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        tools = app.state.agents.tools = OptionsTools(during=lambda: app.state.store.mutate(UUID(original["id"]), lambda data: data.update(agentPending=change == "busy")))
        result = post(client, original, "routes/options", activity_id="b")
        assert result.status_code == 409 and len(tools.calls) == 1
        current = read(app, original)
        assert current["data"]["draft"] == original["data"]["draft"]


def test_write_failure_retries_same_ticket_success_consumes_and_payload_cannot_inject(app, monkeypatch):
    app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        options = query(client, original)
        token = options["options"][1]["option_id"]
        assert post(client, original, "routes/select", option_id=token, route=options["options"][0]["route"]).status_code == 422
        write = app.state.store._atomic_replace
        def fail(path, content):
            if path.parent.name == "conversations": raise OSError("fixture write failure")
            return write(path, content)
        monkeypatch.setattr(app.state.store, "_atomic_replace", fail)
        assert post(client, original, "routes/select", option_id=token).status_code == 503
        assert read(app, original) == original
        monkeypatch.setattr(app.state.store, "_atomic_replace", write)
        selected = post(client, original, "routes/select", option_id=token).json()
        assert choice(selected)["selection"] == "user"
        assert post(client, selected, "routes/select", option_id=token).status_code == 409
        assert post(client, original, "routes/select", option_id=token).status_code == 409


def test_expired_selection_never_replaced_by_first_on_apply_or_chat(app):
    tools = app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        selected = select(client, original)
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        def age(data): data["draft"][0]["activities"][1]["incomingRoute"]["queriedAt"] = old
        selected = app.state.store.mutate(UUID(original["id"]), age).model_dump(mode="json")
        assert post(client, selected, "apply").json()["detail"]["code"] == "route_selection_expired"
        assert read(app, selected) == selected and len(tools.calls) == 1
        research = TripResearch(tools, "杭州", selected["data"]["draft"])
        with pytest.raises(RouteSelectionExpired):
            research.submit(submission({"days": selected["data"]["draft"]}, identifiers=True), request(transport_mode="transit"))
        assert len(tools.calls) == 1


@pytest.mark.parametrize("tamper", ["drop", "default", "target_id", "origin_id", "day", "place"])
def test_autosave_cannot_drop_replace_or_transfer_selected_evidence(app, tamper):
    app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        selected = select(client, original)
        data = deepcopy(selected["data"])
        leg = data["draft"][0]["activities"]
        if tamper == "drop": leg[1].pop("incomingRoute")
        elif tamper == "default": leg[1]["incomingRoute"] = original["data"]["days"][0]["activities"][1]["incomingRoute"]
        elif tamper == "target_id": leg[1]["id"] = "copied-target"
        elif tamper == "origin_id": leg[0]["id"] = "copied-origin"
        elif tamper == "day": data["draft"][0]["day"] = 3
        elif tamper == "place": leg[0]["place"]["sourceLocation"]["latitude"] += 0.01
        assert save(client, selected, data).status_code == 422
        assert read(app, selected) == selected


def test_conflicting_choice_can_be_saved_then_time_fixed_and_applied(app):
    values = paths()
    values[1]["duration"] = 4500
    tools = app.state.agents.tools = OptionsTools(result=response(values=values))
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        selected = select(client, original)
        assert choice(selected)["durationMinutes"] == 75
        assert post(client, selected, "apply").status_code == 422
        assert read(app, selected) == selected
        data = deepcopy(selected["data"])
        data["draft"][0]["activities"][1].update(time="11:15", description="新的用户备注𠮷🏞️", cost={"amountFen": 0, "currency": "CNY", "source": "user"})
        fixed = save(client, selected, data)
        assert fixed.status_code == 200, fixed.text
        applied = post(client, fixed.json(), "apply")
        assert applied.status_code == 200, applied.text
        assert choice(applied.json(), snapshot="days") == choice(selected) and len(tools.calls) == 1


@pytest.mark.parametrize("change", ["day", "origin", "target", "place", "time"])
def test_context_invalidation_keeps_only_unchanged_identity_and_places(app, change):
    app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        selected = select(client, setup_document(client, mode="transit"))
        before = selected["data"]["draft"]
        after = deepcopy(before)
        leg = after[0]["activities"]
        if change == "day": after[0]["day"] = 3
        elif change == "origin": leg[0]["id"] = "new-origin"
        elif change == "target": leg[1]["id"] = "new-target"
        elif change == "place": leg[0]["place"]["sourceLocation"]["latitude"] += 0.01
        elif change == "time": leg[1]["time"] = "11:15"
        trip_api.clear_changed_routes(after, before)
        assert ("incomingRoute" in leg[1]) == (change == "time")
        parked = post(client, selected, "candidates/park", activity_id="b").json()
        assert all("incomingRoute" not in item for item in parked["data"]["draftCandidates"])


@pytest.mark.parametrize("mode,status,selection", [("walking", "verified", "user"), ("transit", "unknown", "user"), ("transit", "verified", "first"), ("transit", "verified", True)])
def test_selection_marker_is_strict(mode, status, selection):
    route = {"fromPlaceId": "a", "toPlaceId": "b", "mode": mode, "status": status}
    if status == "verified":
        route.update(durationMinutes=10, distanceMeters=500, provider="baidu", queriedAt=datetime.now(timezone.utc).isoformat())
    IncomingRoute.model_validate(route)  # The base route is valid; rejection must be due to selection.
    route["selection"] = selection
    with pytest.raises(ValueError, match="selection"): IncomingRoute.model_validate(route)


def test_non_transit_first_stop_and_unknown_coordinates_do_not_query(app):
    tools = app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="walking")
        assert post(client, original, "routes/options", activity_id="b").status_code == 409
        transit = post(client, original, "transport", mode="transit").json()
        assert post(client, transit, "routes/options", activity_id="a").status_code == 422
        def remove(data): data["draft"][0]["activities"][0]["place"].pop("sourceLocation")
        missing = app.state.store.mutate(UUID(original["id"]), remove).model_dump(mode="json")
        assert query(client, missing)["options"] == []
        assert tools.calls == []


def test_mcp_text_result_is_not_hidden_by_unrelated_structured_metadata():
    raw = {"structuredContent": {"metadata": "not a route array"},
           "content": [{"type": "text", "text": json.dumps(response()["structuredContent"])}]}
    routes, reason = asyncio.run(query_transit_options(OptionsTools(result=raw), *pair("baidu"), "杭州"))
    assert len(routes) == 3 and reason is None


def test_edited_name_without_location_must_be_resolved_before_compare(app):
    tools = app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        data = deepcopy(original["data"])
        target = data["draft"][0]["activities"][1]
        target["title"] = "尚未定位的新地点"
        target.pop("place")
        trip_api.clear_changed_routes(data["draft"], original["data"]["draft"])
        pending = save(client, original, data)
        assert pending.status_code == 200, pending.text
        result = post(client, pending.json(), "routes/options", activity_id="b")
        assert result.status_code == 422 and result.json()["detail"]["code"] == "place_unverified"
        assert tools.calls == [] and read(app, original) == pending.json()
