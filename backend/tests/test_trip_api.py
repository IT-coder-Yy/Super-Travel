from copy import deepcopy
import json
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from backend.main import create_app


AUTH = {"Authorization": "Bearer trip-api-fixture"}


def trip_result(note="固定测试安排"):
    return {"trip": {"destination": "杭州", "summary": note, "generatedAt": "2026-10-27T00:00:00+08:00"}, "days": [
        {"day": 1, "title": "固定测试日程", "subtitle": "并非实时资料", "activities": [
            {"id": "fixture-stop", "title": "固定测试公园", "time": "09:00", "duration": "约 60 分钟", "description": note, "x": 0, "y": 0,
             "place": {"id": "baidu:fixturepoi", "name": "固定测试公园", "address": "固定测试地址", "latitude": 30.2, "longitude": 120.1, "coordinateSystem": "wgs84", "provider": "baidu"}}
        ]}
    ]}


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ("STA_MODEL_API_KEY", "STA_MODEL_BASE_URL", "STA_MODEL_NAME", "STA_MCP_URL", "STA_SHARED_TOOLS_URL", "AMAP_MAPS_API_KEY", "BAIDU_MAP_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return create_app(tmp_path, "trip-api-fixture")


def create(client, real=True):
    value = client.post("/api/conversations", headers=AUTH, json={"title": "固定测试会话", "data": {
        "schema": 1, "days": [], "draft": None, "selectedDay": 1, "messages": [], "favorites": []}}).json()
    if real:
        value = client.app.state.store.mutate(UUID(value["id"]), lambda data: data.update(**trip_result())).model_dump(mode="json")
    return value


def save(client, document, data):
    return client.put(f"/api/conversations/{document['id']}", headers=AUTH, json={"title": document["title"], "expected_revision": document["revision"], "data": data})


def test_user_cost_is_saved_but_forged_route_metrics_cannot_enter_draft(app):
    with TestClient(app) as client:
        document = create(client)
        data = deepcopy(document["data"])
        data["draft"] = deepcopy(data["days"])
        activity = data["draft"][0]["activities"][0]
        activity["cost"] = {"amountFen": 1999, "currency": "CNY", "source": "user"}
        saved = save(client, document, data)
        assert saved.status_code == 200
        document = saved.json()
        for bad in [True, -1, 0.1]:
            activity["cost"]["amountFen"] = bad
            assert save(client, document, data).status_code == 422
        activity["cost"]["amountFen"] = 1999
        activity["incomingRoute"] = {"fromPlaceId": "baidu:forged", "toPlaceId": activity["place"]["id"], "mode": "walking", "status": "verified",
            "durationMinutes": 1, "distanceMeters": 1, "provider": "baidu", "queriedAt": "2026-10-28T00:00:00+08:00"}
        assert save(client, document, data).status_code == 422
        actual = app.state.store.get(UUID(document["id"]))
        assert actual.data["draft"][0]["activities"][0]["cost"]["amountFen"] == 1999
        assert "incomingRoute" not in actual.data["draft"][0]["activities"][0]


def test_known_transfer_conflict_rejects_apply_without_losing_formal_or_draft(app):
    import asyncio
    from backend.tests.test_trip_routes import research_pair
    from backend.trips import TripRequest
    async def result():
        tools, research, places, value = await research_pair()
        await research.route(places[0]["id"], places[1]["id"])
        return tools, research.submit(value, TripRequest(destination="杭州", days=1))
    tools, trip = asyncio.run(result())
    app.state.agents.tools = tools
    with TestClient(app) as client:
        document = create(client, real=False)
        document = app.state.store.mutate(UUID(document["id"]), lambda data: data.update(**trip)).model_dump(mode="json")
        data = deepcopy(document["data"])
        data["draft"] = deepcopy(data["days"])
        data["draft"][0]["activities"][1]["time"] = "10:01"
        saved = save(client, document, data).json()
        response = client.post(f"/api/conversations/{document['id']}/trip/apply", headers=AUTH, json={"expected_revision": saved["revision"]})
        assert response.status_code == 422 and response.json()["detail"]["code"] == "trip_transfer_conflict"
        assert "至少需要 13 分钟" in response.json()["detail"]["message"]
        assert app.state.store.get(UUID(document["id"])).model_dump(mode="json") == saved


def test_same_id_refreshed_place_can_autosave_but_cannot_reuse_old_coordinate_route(app):
    import asyncio
    from backend.tests.test_trip_routes import research_pair
    from backend.trips import TripRequest
    async def result():
        _, research, places, value = await research_pair()
        await research.route(places[0]["id"], places[1]["id"])
        return research.submit(value, TripRequest(destination="杭州", days=1))
    trip = asyncio.run(result())
    with TestClient(app) as client:
        document = create(client, real=False)
        def refresh(data):
            data.update(**trip, draft=deepcopy(trip["days"]))
            activity = data["draft"][0]["activities"][1]
            activity.pop("incomingRoute")
            activity["place"]["sourceLocation"]["latitude"] += .01
            activity["place"]["latitude"] += .01
        document = app.state.store.mutate(UUID(document["id"]), refresh).model_dump(mode="json")
        saved = save(client, document, document["data"])
        assert saved.status_code == 200
        document = saved.json()
        data = deepcopy(document["data"])
        data["draft"][0]["activities"][1]["incomingRoute"] = deepcopy(data["days"][0]["activities"][1]["incomingRoute"])
        rejected = save(client, document, data)
        assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "route_unverified"


def test_resolving_unchanged_place_keeps_user_cost(app):
    import asyncio
    from backend.tests.test_trip_routes import RouteMaps, research_pair
    from backend.trips import TripRequest
    async def result():
        _, research, _, value = await research_pair()
        return research.submit(value, TripRequest(destination="杭州", days=1))
    trip = asyncio.run(result())
    app.state.agents.tools = RouteMaps()
    with TestClient(app) as client:
        document = create(client, real=False)
        def draft(data):
            data.update(**trip, draft=deepcopy(trip["days"]))
            data["draft"][0]["activities"][0]["cost"] = {"amountFen": 2580, "source": "user", "currency": "CNY"}
        document = app.state.store.mutate(UUID(document["id"]), draft).model_dump(mode="json")
        activity = document["data"]["draft"][0]["activities"][0]
        response = client.post(f"/api/conversations/{document['id']}/resolve-place", headers=AUTH, json={
            "expected_revision": document["revision"], "activity_id": activity["id"], "query": activity["title"]})
        assert response.status_code == 200
        assert response.json()["data"]["draft"][0]["activities"][0]["cost"]["amountFen"] == 2580


def test_generation_first_formal_then_draft_without_overwriting(app):
    with TestClient(app) as client:
        document = create(client, real=False)
        session = app.state.agents.session(UUID(document["id"]))
        first = {"run_id": "first", "status": "completed", "content": "已生成", "trip_result": trip_result()}
        client.portal.call(session.on_state, first)
        saved = client.get(f"/api/conversations/{document['id']}", headers=AUTH).json()
        assert saved["data"]["days"] == first["trip_result"]["days"]
        client.portal.call(session.on_state, first)
        assert app.state.store.get(UUID(document["id"])).data["draft"] is None
        second = {**first, "run_id": "second", "trip_result": trip_result("调整后的草稿")}
        client.portal.call(session.on_state, second)
        changed = app.state.store.get(UUID(document["id"])).data
        assert changed["days"] == saved["data"]["days"]
        assert changed["draft"][0]["activities"][0]["description"] == "调整后的草稿"
        client.portal.call(session.on_state, {**second, "run_id": "failed", "status": "failed"})
        assert app.state.store.get(UUID(document["id"])).data["days"] == saved["data"]["days"]


def test_apply_restore_revision_and_restart(app):
    with TestClient(app) as client:
        document = create(client)
        draft = deepcopy(document["data"])
        draft["draft"] = deepcopy(draft["days"])
        draft["draft"][0]["activities"][0]["description"] = "用户手动修改"
        saved = save(client, document, draft).json()
        base = f"/api/conversations/{document['id']}"
        applied = client.post(base + "/trip/apply", headers=AUTH, json={"expected_revision": saved["revision"]})
        assert applied.status_code == 200, applied.text
        applied = applied.json()
        assert applied["data"]["days"][0]["activities"][0]["description"] == "用户手动修改"
        assert applied["data"]["previousTrip"]["days"] == document["data"]["days"]
        assert client.post(base + "/trip/restore", headers=AUTH, json={"expected_revision": saved["revision"]}).status_code == 409
        restored = client.post(base + "/trip/restore", headers=AUTH, json={"expected_revision": applied["revision"]}).json()
        assert restored["data"]["days"] == document["data"]["days"]
    with TestClient(create_app(app.state.store.root, "trip-api-fixture")) as restarted:
        assert restarted.get(base, headers=AUTH).json() == restored


def test_forged_coordinates_and_unresolved_or_overlapping_draft_are_rejected(app):
    with TestClient(app) as client:
        document = create(client)
        proposed = deepcopy(document["data"])
        proposed["days"][0]["activities"][0]["place"]["longitude"] = 110.0
        assert save(client, document, proposed).status_code == 422
        proposed = deepcopy(document["data"])
        proposed["draft"] = deepcopy(proposed["days"])
        proposed["draft"][0]["activities"][0].pop("place")
        proposed["draft"][0]["activities"][0]["title"] = "尚未查询的新地点"
        saved = save(client, document, proposed).json()
        assert client.post(f"/api/conversations/{document['id']}/trip/apply", headers=AUTH, json={"expected_revision": saved["revision"]}).status_code == 422
        assert app.state.store.get(UUID(document["id"])).data["days"] == document["data"]["days"]
        proposed["draft"] = deepcopy(proposed["days"])
        second = deepcopy(proposed["draft"][0]["activities"][0])
        second.update(id="second", time="09:30")
        proposed["draft"][0]["activities"].append(second)
        saved = save(client, saved, proposed).json()
        assert client.post(f"/api/conversations/{document['id']}/trip/apply", headers=AUTH, json={"expected_revision": saved["revision"]}).status_code == 422


def test_apply_write_failure_retains_formal_and_draft(app, monkeypatch):
    with TestClient(app) as client:
        document = create(client)
        proposed = deepcopy(document["data"])
        proposed["draft"] = deepcopy(proposed["days"])
        proposed["draft"][0]["activities"][0]["description"] = "不能丢失的草稿"
        saved = save(client, document, proposed).json()
        original = app.state.store._atomic_replace
        def fail(path, payload):
            if path.parent.name == "conversations":
                raise OSError("fixture disk error")
            return original(path, payload)
        monkeypatch.setattr(app.state.store, "_atomic_replace", fail)
        response = client.post(f"/api/conversations/{document['id']}/trip/apply", headers=AUTH, json={"expected_revision": saved["revision"]})
        assert response.status_code == 503
        assert app.state.store.get(UUID(document["id"])).model_dump(mode="json") == saved


def test_resolve_place_uses_real_shape_and_preserves_draft_on_ambiguity(app):
    class Tools:
        async def list_tools(self):
            return [{"name": "baidu_map_search_places"}]
        async def call_tool(self, name, arguments):
            assert name == "baidu_map_search_places"
            assert arguments["region"] == "杭州"
            return {"isError": False, "content": [{"type": "text", "text": json.dumps({"status": 0, "results": [
                {"uid": "fixture-new", "name": "固定新地点", "address": "测试地址", "city": "杭州市", "location": {"lng": 120.16, "lat": 30.25}}
            ]}, ensure_ascii=False)}]}
    app.state.agents.tools = Tools()
    with TestClient(app) as client:
        document = create(client)
        data = deepcopy(document["data"])
        data["draft"] = deepcopy(data["days"])
        data["draft"][0]["activities"][0].pop("place")
        data["draft"][0]["activities"][0]["title"] = "固定新地点"
        saved = save(client, document, data).json()
        url = f"/api/conversations/{document['id']}/resolve-place"
        payload = {"expected_revision": saved["revision"], "activity_id": "fixture-stop", "query": "公园"}
        assert client.post(url, headers=AUTH, json=payload).status_code == 422
        payload["query"] = "固定新地点"
        resolved = client.post(url, headers=AUTH, json=payload)
        assert resolved.status_code == 200, resolved.text
        value = resolved.json()["data"]
        assert value["days"] == document["data"]["days"]
        assert value["draft"][0]["activities"][0]["place"]["id"] == "baidu:fixture-new"
