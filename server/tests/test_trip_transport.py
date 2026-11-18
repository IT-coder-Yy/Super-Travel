"""D1 offline contract: mode drafts, bounded queries and revisioned application."""
import asyncio
from copy import deepcopy
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from supertravel_server.app import create_app
from supertravel_server.candidates import as_candidate
from supertravel_server.mcp_client import ToolError
from supertravel_server.trip_api import refresh_routes
from .test_trip_api import AUTH, app, create, save
from .test_trip_mutations import activity, document, post, read
from .test_exports import export
from .test_pdf_exports import read_pdf, plain_text, compact
from .test_trip_routes import RouteMaps


def setup_document(client, *, mode=None, draft=True, full=False):
    def setup(data):
        if mode is not None:
            data["trip"]["transportMode"] = mode
        if full:
            data["days"] = [{"day": day, "title": f"第{day}天", "subtitle": "离线夹具",
                             "activities": [activity(f"d{day}-a{n}", f"{n * 2:02}:00") for n in range(8)]} for day in range(1, 8)]
        for day in data["days"]:
            for item in day["activities"]:
                item["place"]["sourceLocation"] = {"latitude": 30.25, "longitude": 120.15, "coordinateSystem": "bd09ll"}
                if item.get("incomingRoute"):
                    item["incomingRoute"]["mode"] = mode or "walking"
        candidate = as_candidate(activity("candidate", "16:00"), "杭州")
        candidate["cost"] = {"amountFen": 0, "currency": "CNY", "source": "user"}
        data["candidates"] = [candidate]
        data["favorites"] = [data["days"][0]["activities"][0]["id"], "candidate"]
        data["previousTrip"] = {"days": deepcopy(data["days"]), "trip": deepcopy(data["trip"]), "candidates": deepcopy(data["candidates"])}
    result = document(client, draft=draft, change=setup)
    if draft:
        def pending(data):
            data["draftCandidates"] = deepcopy(data["candidates"])
            data["draftCandidates"][0]["description"] = "候选草稿备注，不能被正式版本覆盖"
            data["draft"][0]["activities"][0]["description"] = "用户尚未应用的备注𠮷🏞️"
        result = client.app.state.store.mutate(UUID(result["id"]), pending).model_dump(mode="json")
    return result


@pytest.mark.parametrize("before,after", [(a, b) for a in ("walking", "driving", "transit") for b in ("walking", "driving", "transit") if a != b])
def test_switch_only_changes_draft_mode_and_clears_old_route_evidence(app, before, after):
    tools = RouteMaps()
    app.state.agents.tools = tools
    with TestClient(app) as client:
        original = setup_document(client, mode=before)
        result = post(client, original, "transport", mode=after)
        assert result.status_code == 200, result.text
        changed = result.json()
        expected = deepcopy(original["data"])
        expected["draftTrip"]["transportMode"] = after
        for day in expected["draft"]:
            for item in day["activities"]:
                item.pop("incomingRoute", None)
        assert changed["data"] == expected
        assert changed["revision"] == original["revision"] + 1
        assert tools.calls == []
        assert post(client, changed, "transport", mode=after).json() == changed
        assert read(app, original) == changed
        discarded = post(client, changed, "discard").json()
        for field in ("days", "trip", "candidates", "favorites", "previousTrip"):
            assert discarded["data"][field] == original["data"][field]


@pytest.mark.parametrize("mode", [None, "walking", "driving", "transit"])
def test_same_mode_is_no_write_and_does_not_create_a_draft(app, monkeypatch, mode):
    with TestClient(app) as client:
        original = setup_document(client, mode=mode, draft=False)
        def forbidden(*args, **kwargs):
            pytest.fail("same mode must not write")
        monkeypatch.setattr(app.state.store, "mutate", forbidden)
        assert post(client, original, "transport", mode=mode or "walking").json() == original
        assert read(app, original) == original


def test_legacy_default_switch_apply_export_restart_and_restore(app, tmp_path):
    tools = RouteMaps()
    app.state.agents.tools = tools
    with TestClient(app) as client:
        original = setup_document(client, draft=False)
        pending = post(client, original, "transport", mode="driving").json()
        assert pending["data"]["draftCandidates"] == original["data"]["candidates"]
        assert tools.calls == []
        old_export = export(client, pending).json()["markdown"]
        assert "已查询估算：步行" in old_export and "已查询估算：驾车" not in old_export
        applied = post(client, pending, "apply")
        assert applied.status_code == 200, applied.text
        saved = applied.json()
        assert len(tools.calls) == 2 and all(call[1]["model"] == "driving" for call in tools.calls)
        assert all(call[1]["origin"] == "30.250000,120.150000" for call in tools.calls)
        assert saved["data"]["trip"]["transportMode"] == "driving" and saved["data"]["draft"] is None
        for old, new in zip(original["data"]["days"], saved["data"]["days"]):
            for a, b in zip(old["activities"], new["activities"]):
                assert {k: v for k, v in a.items() if k != "incomingRoute"} == {k: v for k, v in b.items() if k != "incomingRoute"}
        rendered = export(client, saved).json()["markdown"]
        assert "已查询估算：驾车" in rendered and "¥25.00" in rendered
        pdf = export(client, saved, format="pdf")
        assert pdf.status_code == 200, pdf.text
        pdf_text = compact(plain_text(read_pdf(pdf.json())))
        assert "驾车约13分钟" in pdf_text and "¥25.00" in pdf_text
        assert "步行约" not in pdf_text and read(app, saved) == saved
        assert len(tools.calls) == 2
    with TestClient(create_app(tmp_path, "trip-api-fixture")) as restarted:
        assert restarted.get(f"/api/conversations/{saved['id']}", headers=AUTH).json() == saved
        restored = post(restarted, saved, "restore").json()
        assert restored["data"]["days"] == original["data"]["days"]
        assert restored["data"]["trip"].get("transportMode") is None
        assert restored["data"]["candidates"] == original["data"]["candidates"]
        assert restored["data"]["favorites"] == original["data"]["favorites"]


def test_switch_keeps_empty_days_and_single_stop_needs_no_query(app):
    tools = RouteMaps()
    app.state.agents.tools = tools
    with TestClient(app) as client:
        original = document(client, draft=False, change=lambda data: [data["days"][0].update(activities=data["days"][0]["activities"][:1]), data["days"][1].update(activities=[])])
        pending = post(client, original, "transport", mode="transit").json()
        response = post(client, pending, "apply")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["days"][1]["activities"] == []
        assert tools.calls == []


@pytest.mark.parametrize("extra", [{"mode": "bicycle"}, {"mode": None}, {"mode": "transit", "routes": []}, {"mode": "driving", "expected_revision": "3"}])
def test_strict_transport_payload(app, extra):
    with TestClient(app) as client:
        original = setup_document(client)
        assert post(client, original, "transport", **extra).status_code == 422
        assert read(app, original) == original


def test_busy_stale_missing_formal_and_generic_autosave_protection(app):
    with TestClient(app) as client:
        empty = create(client, real=False)
        assert post(client, empty, "transport", mode="driving").status_code == 409
        original = setup_document(client)
        changed = post(client, original, "transport", mode="transit").json()
        assert post(client, original, "transport", mode="driving").json()["detail"]["code"] == "revision_conflict"
        forged = deepcopy(changed["data"])
        forged["draftTrip"]["transportMode"] = "driving"
        assert save(client, changed, forged).json()["detail"]["code"] == "trip_protected"
        busy = app.state.store.mutate(UUID(original["id"]), lambda data: data.update(agentPending=True)).model_dump(mode="json")
        assert post(client, busy, "transport", mode="driving").json()["detail"]["code"] == "trip_busy"
        assert read(app, original) == busy


def test_write_failure_preserves_original_and_can_be_retried(app, monkeypatch):
    with TestClient(app) as client:
        original = setup_document(client)
        write = app.state.store._atomic_replace
        def fail(path, content):
            if path.parent.name == "conversations":
                raise OSError("offline write failure")
            return write(path, content)
        monkeypatch.setattr(app.state.store, "_atomic_replace", fail)
        assert post(client, original, "transport", mode="driving").status_code == 503
        assert read(app, original) == original
        monkeypatch.setattr(app.state.store, "_atomic_replace", write)
        assert post(client, original, "transport", mode="driving").status_code == 200


def test_known_conflict_keeps_saved_mode_draft_then_user_can_fix_time(app):
    tools = RouteMaps(result={"structuredContent": {"result": {"routes": [{"duration": 4500, "distance": 1000}]}}})
    app.state.agents.tools = tools
    with TestClient(app) as client:
        original = setup_document(client)
        pending = post(client, original, "transport", mode="transit").json()
        failed = post(client, pending, "apply")
        assert failed.status_code == 422 and failed.json()["detail"]["code"] == "trip_transfer_conflict"
        assert read(app, original) == pending and len(tools.calls) == 2
        data = deepcopy(pending["data"])
        data["draft"][0]["activities"][1]["time"] = "11:30"
        data["draft"][0]["activities"][2]["time"] = "14:00"
        fixed = save(client, pending, data).json()
        response = post(client, fixed, "apply")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["days"][0]["activities"][0]["description"] == "用户尚未应用的备注𠮷🏞️"
        assert len(tools.calls) == 4


@pytest.mark.parametrize("failure", [None, "tool_quota", "tool_connection"])
def test_seven_full_days_budget_includes_failures_and_explicit_retry(app, failure):
    tools = RouteMaps(error=ToolError(failure, False, "offline failure") if failure else None)
    app.state.agents.tools = tools
    with TestClient(app) as client:
        original = setup_document(client, full=True)
        pending = post(client, original, "transport", mode="driving").json()
        response = post(client, pending, "apply")
        assert response.status_code == 200, response.text
        saved = response.json()
        routes = [a["incomingRoute"] for d in saved["data"]["days"] for a in d["activities"][1:]]
        assert len(routes) == 49 and len(tools.calls) == 12
        assert sum(route["status"] == "verified" for route in routes) == (0 if failure else 12)
        assert all("12 次" in route["reason"] and "durationMinutes" not in route for route in routes[12:])
        if failure:
            assert all("durationMinutes" not in route for route in routes)
        else:
            data = deepcopy(saved["data"])
            data.update(draft=deepcopy(data["days"]), draftTrip=deepcopy(data["trip"]), draftCandidates=deepcopy(data["candidates"]))
            again = post(client, save(client, saved, data).json(), "apply").json()
            assert len(tools.calls) == 24
            assert sum(a["incomingRoute"]["status"] == "verified" for d in again["data"]["days"] for a in d["activities"][1:]) == 24


def test_cross_provider_places_remain_unknown_without_fallback(app):
    tools = RouteMaps()
    app.state.agents.tools = tools
    with TestClient(app) as client:
        original = setup_document(client, draft=False)
        def mixed(data):
            place = data["days"][0]["activities"][1]["place"]
            place.update(id="amap:b", provider="amap", sourceLocation={"latitude": 30.2, "longitude": 120.1, "coordinateSystem": "gcj02"})
        original = app.state.store.mutate(UUID(original["id"]), mixed).model_dump(mode="json")
        pending = post(client, original, "transport", mode="transit").json()
        result = post(client, pending, "apply")
        assert result.status_code == 200, result.text
        for item in result.json()["data"]["days"][0]["activities"][1:]:
            assert item["incomingRoute"]["status"] == "unknown" and "durationMinutes" not in item["incomingRoute"]
        assert tools.calls == []


def test_total_timeout_stops_remaining_queries(monkeypatch):
    deadlines = []
    original_timeout = asyncio.timeout_at
    def shortened(deadline):
        deadlines.append(deadline - asyncio.get_running_loop().time())
        return original_timeout(asyncio.get_running_loop().time() + 0.01)
    monkeypatch.setattr("supertravel_server.trip_api.asyncio.timeout_at", shortened)
    class SlowMaps(RouteMaps):
        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            await asyncio.sleep(60)
            pytest.fail("the outer deadline must cancel the route")
    tools = SlowMaps()
    days = [{"day": 1, "activities": [activity(str(n), f"{n + 8:02}:00") for n in range(4)]}]
    for item in days[0]["activities"]:
        item["place"]["sourceLocation"] = {"latitude": 30.2, "longitude": 120.1, "coordinateSystem": "bd09ll"}
    asyncio.run(refresh_routes(tools, days, "杭州", "driving"))
    assert len(deadlines) == 1 and 29 < deadlines[0] <= 30
    assert len(tools.calls) == 1
    assert all(item["incomingRoute"]["status"] == "unknown" and "超时" in item["incomingRoute"]["reason"] for item in days[0]["activities"][1:])
