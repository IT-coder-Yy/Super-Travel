from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from backend import trip_api
from backend.main import create_app
from backend.mcp_client import ToolError
from backend.tests.test_trip_api import AUTH, create, save


class SearchTools:
    def __init__(self):
        self.calls = []
        self.error = None
        self.rows = [
            {"uid": "fixture-west", "name": "同名公园", "address": "杭州西侧测试地址", "city": "杭州市", "location": {"lng": 120.15, "lat": 30.25}},
            {"uid": "fixture-east", "name": "同名公园", "address": "杭州东侧测试地址", "city": "杭州市", "location": {"lng": 120.17, "lat": 30.27}},
        ]

    async def list_tools(self):
        return [{"name": "baidu_map_search_places"}]

    async def call_tool(self, name, arguments):
        assert name == "baidu_map_search_places"
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return {"structuredContent": {"status": 0, "results": deepcopy(self.rows)}}


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ("STA_MODEL_API_KEY", "STA_MODEL_BASE_URL", "STA_MODEL_NAME", "STA_MCP_URL", "STA_SHARED_TOOLS_URL", "AMAP_MAPS_API_KEY", "BAIDU_MAP_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    application = create_app(tmp_path, "trip-api-fixture")
    application.state.agents.tools = SearchTools()
    return application


def draft_document(client, change=None):
    document = create(client)
    data = deepcopy(document["data"])
    data["draft"] = deepcopy(data["days"])
    data["draftTrip"] = deepcopy(data["trip"])
    if change:
        change(data)
    response = save(client, document, data)
    assert response.status_code == 200, response.text
    return response.json()


def search(client, document, **extra):
    return client.post(f"/api/conversations/{document['id']}/trip/places/search", headers=AUTH,
                       json={"expected_revision": document["revision"], "query": "公园", **extra})


def select(client, document, selection_id, **extra):
    return client.post(f"/api/conversations/{document['id']}/trip/places/select", headers=AUTH,
                       json={"expected_revision": document["revision"], "selection_id": selection_id,
                             "day": 1, "time": "11:00", "duration_minutes": 60, **extra})


def token(client, document, index=0):
    response = search(client, document)
    assert response.status_code == 200, response.text
    return response.json()["results"][index]["selection_id"]


def read(app, document):
    return app.state.store.get(UUID(document["id"])).model_dump(mode="json")


def route(origin, target):
    return {"fromPlaceId": origin, "toPlaceId": target, "mode": "walking", "status": "verified", "durationMinutes": 10,
            "distanceMeters": 500, "provider": "baidu", "queriedAt": datetime.now(timezone.utc).isoformat()}


def test_search_returns_distinguishable_choices_without_mutating_or_exposing_coordinates(app):
    with TestClient(app) as client:
        document = draft_document(client)
        result = search(client, document, category="景点")
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["revision"] == document["revision"] and len(body["results"]) == 2
        assert body["results"][0]["name"] == body["results"][1]["name"]
        assert body["results"][0]["address"] != body["results"][1]["address"]
        assert len({item["selection_id"] for item in body["results"]}) == 2
        assert set(body["results"][0]) == {"selection_id", "name", "address", "provider"}
        assert app.state.agents.tools.calls[-1][1] == {"query": "公园 景点", "region": "杭州"}
        assert read(app, document) == document


def test_select_adds_chosen_real_place_in_time_order_and_only_changes_draft(app):
    with TestClient(app) as client:
        document = draft_document(client)
        selected = token(client, document, 1)
        response = select(client, document, selected, time="08:00", duration_minutes=30, description="   ")
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["data"]["days"] == document["data"]["days"]
        activities = updated["data"]["draft"][0]["activities"]
        assert [item["time"] for item in activities] == ["08:00", "09:00"]
        activity = activities[0]
        assert activity["place"]["id"] == "baidu:fixture-east"
        assert activity["place"]["sourceLocation"]["coordinateSystem"] == "bd09ll"
        assert activity["place"]["coordinateSystem"] == "wgs84"
        assert activity["id"] != activities[1]["id"] and activity["description"] == "用户安排"
        assert "cost" not in activity and "incomingRoute" not in activity
        assert len(app.state.agents.tools.calls) == 1
        assert select(client, document, selected).status_code == 409
        assert select(client, updated, selected).json()["detail"]["code"] == "place_selection_expired"
        assert read(app, document) == updated


def test_replacement_keeps_identity_and_notes_clears_cost_and_affected_routes(app):
    with TestClient(app) as client:
        document = draft_document(client)
        def prepared(data):
            activities = data["draft"][0]["activities"]
            base = deepcopy(activities[0])
            for index in range(1, 4):
                item = deepcopy(base)
                item.update(id=f"activity-{index}", time=f"{9 + index * 2:02}:00", title=f"地点{index}")
                item["place"].update(id=f"baidu:place-{index}", name=item["title"])
                item["incomingRoute"] = route(activities[-1]["place"]["id"], item["place"]["id"])
                activities.append(item)
            activities[1]["description"] = "用户保留的备注"
            activities[1]["cost"] = {"amountFen": 2500, "currency": "CNY", "source": "user"}
        document = app.state.store.mutate(UUID(document["id"]), prepared).model_dump(mode="json")
        response = select(client, document, token(client, document), activity_id="activity-1", description=" ")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        activities = data["draft"][0]["activities"]
        assert activities[1]["id"] == "activity-1" and activities[1]["description"] == "用户保留的备注"
        assert "cost" not in activities[1]
        assert "incomingRoute" not in activities[1] and "incomingRoute" not in activities[2]
        assert activities[3]["incomingRoute"] == document["data"]["draft"][0]["activities"][3]["incomingRoute"]
        assert data["days"] == document["data"]["days"]


def test_same_poi_keeps_cost_and_uses_refreshed_coordinates(app):
    with TestClient(app) as client:
        document = draft_document(client, lambda data: data["draft"][0]["activities"][0].update(cost={"amountFen": 0, "currency": "CNY", "source": "user"}))
        app.state.agents.tools.rows[0].update(uid="fixturepoi", name="固定测试公园")
        response = select(client, document, token(client, document), activity_id="fixture-stop", time="09:00", description="明确的新备注")
        assert response.status_code == 200, response.text
        activity = response.json()["data"]["draft"][0]["activities"][0]
        assert activity["cost"]["amountFen"] == 0 and activity["description"] == "明确的新备注"
        assert activity["place"]["sourceLocation"]["longitude"] == 120.15


@pytest.mark.parametrize("extra", [{"time": "09:06"}, {"duration_minutes": 56}])
def test_legacy_time_and_duration_may_stay_but_changed_values_require_quarters(app, extra):
    with TestClient(app) as client:
        document = draft_document(client, lambda data: data["draft"][0]["activities"][0].update(time="09:05", duration="约 55 分钟"))
        selected = token(client, document)
        payload = {"activity_id": "fixture-stop", "time": "09:05", "duration_minutes": 55}
        rejected = select(client, document, selected, **{**payload, **extra})
        assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "trip_time_step"
        assert read(app, document) == document
        response = select(client, document, selected, **payload)
        assert response.status_code == 200, response.text
        activity = response.json()["data"]["draft"][0]["activities"][0]
        assert activity["time"] == "09:05" and activity["duration"] == "约 55 分钟"


@pytest.mark.parametrize("extra", [{"time": "11:01"}, {"duration_minutes": 16}, {"time": "23:45", "duration_minutes": 30},
                                   {"time": "09:30"}, {"duration_minutes": True}, {"duration_minutes": 0}, {"time": "24:00"}])
def test_invalid_new_schedule_does_not_consume_choice_or_change_draft(app, extra):
    with TestClient(app) as client:
        document = draft_document(client)
        selected = token(client, document)
        response = select(client, document, selected, **extra)
        assert response.status_code == 422
        assert read(app, document) == document
        assert select(client, document, selected).status_code == 200


def test_duplicate_poi_capacity_and_wrong_day_are_rejected(app):
    with TestClient(app) as client:
        document = draft_document(client)
        app.state.agents.tools.rows[0].update(uid="fixturepoi", name="固定测试公园")
        response = select(client, document, token(client, document))
        assert response.status_code == 422 and response.json()["detail"]["code"] == "place_duplicate"
        selected = token(client, document, 1)
        assert select(client, document, selected, day=2).json()["detail"]["code"] == "trip_day_missing"
        assert select(client, document, selected, activity_id="not-this-activity").json()["detail"]["code"] == "activity_missing"
        def full(data):
            base = data["draft"][0]["activities"][0]
            for index in range(1, 8):
                item = deepcopy(base)
                item.update(id=f"full-{index}", title=f"地点{index}", time=f"{9 + index:02}:00")
                item["place"].update(id=f"baidu:full-{index}", name=item["title"])
                data["draft"][0]["activities"].append(item)
        document = app.state.store.mutate(UUID(document["id"]), full).model_dump(mode="json")
        rejected = select(client, document, token(client, document, 1), time="18:00")
        assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "trip_day_full"
        assert read(app, document) == document


def test_empty_search_and_provider_failure_preserve_document(app):
    with TestClient(app) as client:
        document = draft_document(client)
        app.state.agents.tools.rows = []
        result = search(client, document)
        assert result.status_code == 200 and result.json() == {"results": [], "revision": document["revision"]}
        app.state.agents.tools.error = ToolError("tool_quota", False, "服务额度不足")
        failed = search(client, document)
        assert failed.status_code == 502 and failed.json()["detail"]["code"] == "tool_quota"
        assert read(app, document) == document


@pytest.mark.parametrize("invalid", ["expired", "connection", "other-conversation", "forged", "revision", "new-search"])
def test_selection_is_bound_to_current_search_session_revision_and_connection(app, monkeypatch, invalid):
    monkeypatch.setattr(trip_api, "monotonic", lambda: 100)
    with TestClient(app) as client:
        document = draft_document(client)
        selected = token(client, document)
        if invalid == "expired":
            monkeypatch.setattr(trip_api, "monotonic", lambda: 401)
        elif invalid == "connection":
            app.state.agents.tools = SearchTools()
        elif invalid == "other-conversation":
            document = draft_document(client)
        elif invalid == "forged":
            selected = "invented-selection"
        elif invalid == "revision":
            document = save(client, document, document["data"]).json()
        elif invalid == "new-search":
            token(client, document)
        response = select(client, document, selected)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "place_selection_expired"
        assert read(app, document) == document


def test_search_rechecks_revision_after_the_provider_returns(app):
    with TestClient(app) as client:
        document = draft_document(client)
        tools = app.state.agents.tools
        call = tools.call_tool
        async def with_autosave(name, arguments):
            result = await call(name, arguments)
            app.state.store.mutate(UUID(document["id"]), lambda data: data.update(selectedDay=0))
            return result
        tools.call_tool = with_autosave
        response = search(client, document)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "revision_conflict"
        assert read(app, document)["data"]["draft"] == document["data"]["draft"]


def test_missing_draft_busy_task_and_coordinate_payload_are_rejected(app):
    with TestClient(app) as client:
        document = create(client)
        assert search(client, document).status_code == 200
        assert read(app, document) == document
        assert select(client, document, token(client, document)).json()["detail"]["code"] == "draft_missing"
        document = draft_document(client)
        selected = token(client, document)
        assert select(client, document, selected, place={"latitude": 1}).status_code == 422
        assert search(client, document, query="  ").status_code == 422
        document = app.state.store.mutate(UUID(document["id"]), lambda data: data.update(agentPending=True)).model_dump(mode="json")
        assert search(client, document).json()["detail"]["code"] == "trip_busy"
        assert select(client, document, selected).json()["detail"]["code"] == "trip_busy"


def test_write_failure_keeps_choice_retryable_and_preserves_formal_and_draft(app, monkeypatch):
    with TestClient(app) as client:
        document = draft_document(client)
        selected = token(client, document)
        write = app.state.store._atomic_replace
        def fail(path, payload):
            if path.parent.name == "conversations":
                raise OSError("fixture write failure")
            return write(path, payload)
        monkeypatch.setattr(app.state.store, "_atomic_replace", fail)
        response = select(client, document, selected)
        assert response.status_code == 503
        assert read(app, document) == document
        monkeypatch.setattr(app.state.store, "_atomic_replace", write)
        assert select(client, document, selected).status_code == 200


def test_selection_applies_through_existing_route_checks_and_survives_restart(app, tmp_path):
    with TestClient(app) as client:
        document = draft_document(client)
        response = select(client, document, token(client, document), description="确认后的测试安排")
        assert response.status_code == 200, response.text
        updated = response.json()
        applied = client.post(f"/api/conversations/{document['id']}/trip/apply", headers=AUTH,
                              json={"expected_revision": updated["revision"]})
        assert applied.status_code == 200, applied.text
        saved = applied.json()
        assert saved["data"]["draft"] is None
        assert saved["data"]["previousTrip"]["days"] == document["data"]["days"]
        assert saved["data"]["days"][0]["activities"][1]["incomingRoute"]["status"] == "unknown"
    with TestClient(create_app(tmp_path, "trip-api-fixture")) as restarted:
        recovered = restarted.get(f"/api/conversations/{document['id']}", headers=AUTH).json()
        assert recovered == saved
        restored = restarted.post(f"/api/conversations/{document['id']}/trip/restore", headers=AUTH,
                                  json={"expected_revision": recovered["revision"]})
        assert restored.status_code == 200 and restored.json()["data"]["days"] == document["data"]["days"]


def test_unchanged_place_cannot_silently_shorten_a_verified_transfer(app):
    import asyncio
    from backend.tests.test_trip_routes import research_pair
    from backend.trips import TripRequest

    async def prepared():
        tools, research, places, value = await research_pair()
        await research.route(places[0]["id"], places[1]["id"])
        return tools, research.submit(value, TripRequest(destination="杭州", days=1))

    tools, trip = asyncio.run(prepared())
    app.state.agents.tools = tools
    with TestClient(app) as client:
        document = create(client)
        document = app.state.store.mutate(UUID(document["id"]), lambda data: data.update(**trip, draft=deepcopy(trip["days"]), draftTrip=deepcopy(trip["trip"]))).model_dump(mode="json")
        selected = token(client, document, 1)
        activity_id = document["data"]["draft"][0]["activities"][1]["id"]
        rejected = select(client, document, selected, activity_id=activity_id, time="10:00")
        assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "trip_transfer_conflict"
        assert "至少需要 13 分钟" in rejected.json()["detail"]["message"]
        assert read(app, document) == document
        accepted = select(client, document, selected, activity_id=activity_id, time="10:30")
        assert accepted.status_code == 200, accepted.text
