"""Offline contracts for transport choices on one adjacent pair of activities."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from supertravel_server.app import create_app
from supertravel_server.mcp_client import ToolError
from .test_route_options import OptionsTools, paths, response, select
from .test_trip_api import AUTH, app, create, save
from .test_trip_mutations import activity, post, read
from .test_trip_routes import RouteMaps
from .test_trip_transport import setup_document


def set_mode(client, document, target, mode):
    response = post(client, document, "routes/transport", activity_id=target, mode=mode)
    assert response.status_code == 200, response.text
    return response.json()


def test_only_middle_leg_queries_and_both_undo_paths_preserve_the_snapshot(app, tmp_path):
    tools = app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="transit", draft=False)

        def fourth_stop(data):
            stops = data["days"][0]["activities"]
            last = activity("e", "15:00", previous="c")
            last["incomingRoute"]["mode"] = "transit"
            stops.append(last)
            for index, stop in enumerate(stops):
                stop["place"]["sourceLocation"] = {"latitude": 30.25, "longitude": 120 + index / 10, "coordinateSystem": "bd09ll"}

        original = app.state.store.mutate(UUID(original["id"]), fourth_stop).model_dump(mode="json")
        chosen = select(client, select(client, original, target="b"), target="e")
        formal = post(client, chosen, "apply").json()
        assert len(tools.calls) == 2
        before = formal["data"]["days"][0]["activities"]
        assert [before[i]["incomingRoute"]["transit"]["fare"]["amountFen"] for i in (1, 3)] == [0, 0]

        draft = set_mode(client, formal, "c", "walking")
        expected = deepcopy(formal["data"]["days"])
        middle = expected[0]["activities"][2]
        middle.pop("incomingRoute")
        middle["incomingTransportOverride"] = {"day": 1, "fromActivityId": "b", "mode": "walking"}
        assert draft["data"]["draft"] == expected
        assert draft["revision"] == formal["revision"] + 1
        assert set_mode(client, draft, "c", "walking") == draft
        assert post(client, draft, "routes/options", activity_id="c").json()["detail"]["code"] == "route_mode"
        assert draft["data"]["days"] == formal["data"]["days"]
        assert draft["data"]["draftCandidates"] == formal["data"]["candidates"]
        discarded = post(client, draft, "discard").json()
        assert discarded["data"] == formal["data"] and len(tools.calls) == 2

        draft = set_mode(client, discarded, "c", "walking")
        applied = post(client, draft, "apply")
        assert applied.status_code == 200, applied.text
        applied = applied.json()
        after = applied["data"]["days"][0]["activities"]
        assert len(tools.calls) == 3
        assert tools.calls[-1][1] == {"origin": "30.250000,120.100000", "destination": "30.250000,120.200000", "model": "walking"}
        assert [s["incomingRoute"]["mode"] for s in after[1:]] == ["transit", "walking", "transit"]
        assert [after[i] for i in (0, 1, 3)] == [before[i] for i in (0, 1, 3)]
        assert after[2]["incomingTransportOverride"] == middle["incomingTransportOverride"]
        assert "transit" not in after[2]["incomingRoute"] and "taxiEstimates" not in after[2]["incomingRoute"]
        for field in ("candidates", "favorites"):
            assert applied["data"][field] == formal["data"][field]

    restarted_app = create_app(tmp_path, "trip-api-fixture")
    restarted_app.state.agents.tools = tools
    with TestClient(restarted_app) as client:
        assert client.get(f"/api/conversations/{applied['id']}", headers=AUTH).json() == applied
        restored = post(client, applied, "restore").json()
        for field in ("trip", "days", "candidates", "favorites"):
            assert restored["data"][field] == formal["data"][field]
        assert post(client, restored, "restore").json()["data"]["days"] == applied["data"]["days"]
        assert len(tools.calls) == 3


def test_explicit_equal_to_default_survives_whole_trip_changes_and_allows_comparison(app):
    tools = app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        assert set_mode(client, original, "b", None) == original
        selected = select(client, original, target="b")
        evidence = selected["data"]["draft"][0]["activities"][1]["incomingRoute"]
        pinned = set_mode(client, selected, "b", "transit")
        assert pinned["data"]["draft"][0]["activities"][1]["incomingRoute"] == evidence
        following = set_mode(client, pinned, "b", None)
        assert following["data"]["draft"] == selected["data"]["draft"]
        pinned = set_mode(client, following, "b", "transit")
        switched = post(client, pinned, "transport", mode="driving").json()
        stops = switched["data"]["draft"][0]["activities"]
        assert switched["data"]["draftTrip"]["transportMode"] == "driving"
        assert stops[1]["incomingRoute"] == evidence and stops[1]["incomingTransportOverride"]["mode"] == "transit"
        assert "incomingRoute" not in stops[2] and len(tools.calls) == 1
        selected_again = select(client, switched, index=2, target="b")
        cleared = set_mode(client, selected_again, "b", None)
        stop = cleared["data"]["draft"][0]["activities"][1]
        assert "incomingRoute" not in stop and "incomingTransportOverride" not in stop
        assert cleared["data"]["draftCandidates"] == original["data"]["draftCandidates"]
        assert cleared["data"]["draft"][0]["activities"][0] == original["data"]["draft"][0]["activities"][0]
        assert len(tools.calls) == 2


def test_old_autosaves_preserve_only_the_current_choice_and_cannot_set_or_replay_it(app):
    tools = app.state.agents.tools = RouteMaps()
    with TestClient(app) as client:
        original = setup_document(client, draft=False)
        pinned = set_mode(client, original, "b", "driving")
        mixed = post(client, pinned, "apply").json()
        pending = set_mode(client, mixed, "c", "transit")
        formal = post(client, pending, "apply").json()
        legacy = deepcopy(formal["data"])
        legacy["draft"] = deepcopy(legacy["days"])
        legacy["draftTrip"] = deepcopy(legacy["trip"])
        for days in (legacy["days"], legacy["draft"], legacy["previousTrip"]["days"]):
            for stop in days[0]["activities"]:
                stop.pop("incomingTransportOverride", None)
        legacy["draft"][0]["activities"][2]["description"] = "旧客户端保存的备注"
        saved = save(client, formal, legacy)
        assert saved.status_code == 200, saved.text
        saved = saved.json()
        assert saved["data"]["days"] == formal["data"]["days"]
        assert saved["data"]["previousTrip"] == formal["data"]["previousTrip"]
        assert saved["data"]["draft"][0]["activities"][1]["incomingTransportOverride"]["mode"] == "driving"
        assert saved["data"]["draft"][0]["activities"][2]["incomingTransportOverride"]["mode"] == "transit"

        for bad in (None, {"day": 1, "fromActivityId": "a", "mode": "walking"},
                    {"day": True, "fromActivityId": "a", "mode": "driving"}):
            data = deepcopy(saved["data"])
            data["draft"][0]["activities"][1]["incomingTransportOverride"] = bad
            assert save(client, saved, data).status_code == 422
        cleared = set_mode(client, saved, "b", None)
        assert save(client, cleared, saved["data"]).status_code == 422
        legacy = deepcopy(cleared["data"])
        legacy["draft"][0]["activities"][0]["incomingTransportOverride"] = {"day": 1, "fromActivityId": "a", "mode": "driving"}
        assert save(client, cleared, legacy).status_code == 422
        legacy = deepcopy(cleared["data"])
        legacy.update(draft=None, draftTrip=None, draftCandidates=None)
        assert save(client, cleared, legacy).status_code == 422
        assert read(app, saved) == cleared and len(tools.calls) == 2


def test_conflict_does_not_publish_and_unknown_is_not_zero(app):
    tools = app.state.agents.tools = RouteMaps(result={"structuredContent": {"result": {"routes": [{"duration": 1200, "distance": 800}]}}})
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        data = deepcopy(original["data"])
        stops = data["draft"][0]["activities"]
        stops[0]["duration"], stops[1]["time"] = "约 45 分钟", "10:00"
        prepared = save(client, original, data).json()
        pending = set_mode(client, prepared, "b", "walking")
        rejected = post(client, pending, "apply")
        assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "trip_transfer_conflict"
        assert rejected.json()["detail"]["activity_id"] == "b"
        assert "20 分钟" in rejected.text and "15 分钟" in rejected.text and "还差 5 分钟" in rejected.text
        assert read(app, pending) == pending and len(tools.calls) == 1
        data = deepcopy(pending["data"])
        data["draft"][0]["activities"][1]["time"] = "10:15"
        fixed = save(client, pending, data).json()
        applied = post(client, fixed, "apply").json()
        assert applied["data"]["days"][0]["activities"][1]["incomingRoute"]["durationMinutes"] == 20
        tools.error = ToolError("tool_quota", False, "offline quota")
        driving = set_mode(client, applied, "b", "driving")
        unknown = post(client, driving, "apply").json()["data"]["days"][0]["activities"][1]
        assert unknown["incomingTransportOverride"]["mode"] == "driving"
        assert unknown["incomingRoute"]["mode"] == "driving" and unknown["incomingRoute"]["status"] == "unknown"
        assert "durationMinutes" not in unknown["incomingRoute"] and len(tools.calls) == 3


def test_expired_selected_override_blocks_before_querying_other_legs(app):
    tools = app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="driving")
        draft = set_mode(client, set_mode(client, original, "b", "walking"), "c", "transit")
        selected = select(client, draft, target="c")

        def age(data):
            data["draft"][0]["activities"][2]["incomingRoute"]["queriedAt"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()

        expired = app.state.store.mutate(UUID(selected["id"]), age).model_dump(mode="json")
        detail = post(client, expired, "apply").json()["detail"]
        assert detail["code"] == "route_selection_expired" and detail["activity_id"] == "c"
        assert read(app, expired) == expired and len(tools.calls) == 1


@pytest.mark.parametrize(("selected_id", "other_id"), [("b", "c"), ("c", "b")])
def test_user_choice_expiring_during_another_query_never_falls_back(app, monkeypatch, selected_id, other_id):
    tools = app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="driving")
        pending = set_mode(client, original, selected_id, "transit")
        chosen = select(client, pending, target=selected_id)
        draft = set_mode(client, chosen, other_id, "walking")
        stops = draft["data"]["draft"][0]["activities"]
        selected = next(item["incomingRoute"] for item in stops if item["id"] == selected_id)
        limit = datetime.fromisoformat(selected["queriedAt"]) + timedelta(hours=24)

        class FixtureClock(datetime):
            current = limit

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz)

        monkeypatch.setattr("supertravel_server.trip_routes.datetime", FixtureClock)
        tools.during = lambda: setattr(FixtureClock, "current", limit + timedelta(seconds=1))
        result = post(client, draft, "apply")
        assert result.status_code == 409 and result.json()["detail"]["code"] == "route_selection_expired"
        assert [args["model"] for _, args in tools.calls] == ["transit", "walking"]
        assert read(app, draft) == draft


def test_changing_the_default_keeps_a_slow_pinned_transit_choice_editable(app):
    values = paths()
    values[1]["duration"] = 4500
    app.state.agents.tools = OptionsTools(result=response(values=values))
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        pinned = set_mode(client, original, "b", "transit")
        selected = select(client, pinned, target="b")
        changed = post(client, selected, "transport", mode="walking")
        assert changed.status_code == 200, changed.text
        changed = changed.json()
        assert changed["data"]["draft"][0]["activities"][1] == selected["data"]["draft"][0]["activities"][1]
        assert changed["data"]["days"] == original["data"]["days"]
        assert post(client, changed, "apply").json()["detail"]["code"] == "trip_transfer_conflict"
        assert read(app, changed) == changed


def test_legacy_snapshot_with_no_declared_mode_restores_without_using_a_mismatched_estimate(app):
    tools = app.state.agents.tools = RouteMaps()
    with TestClient(app) as client:
        original = setup_document(client, draft=False)

        def old_snapshot(data):
            leg = data["previousTrip"]["days"][0]["activities"][1]
            leg["incomingRoute"].update(mode="driving", durationMinutes=100)

        original = app.state.store.mutate(UUID(original["id"]), old_snapshot).model_dump(mode="json")
        restored = post(client, original, "restore")
        assert restored.status_code == 200, restored.text
        restored = restored.json()
        assert restored["data"]["days"] == original["data"]["previousTrip"]["days"]
        assert not tools.calls
        changed = set_mode(client, restored, "c", "walking")
        applied = post(client, changed, "apply").json()
        first_leg = applied["data"]["days"][0]["activities"][1]
        assert first_leg["incomingRoute"]["mode"] == "walking" and "incomingTransportOverride" not in first_leg
        assert len(tools.calls) == 1


@pytest.mark.parametrize("missing", ["place", "sourceLocation"])
def test_unlocated_endpoint_or_missing_original_coordinates(app, missing):
    tools = app.state.agents.tools = RouteMaps()
    with TestClient(app) as client:
        doc = setup_document(client, mode="transit")

        def without_location(data):
            target = data["draft"][0]["activities"][2]
            target.pop("incomingRoute", None)
            if missing == "place":
                target.pop("place")
            else:
                target["place"].pop("sourceLocation")

        doc = app.state.store.mutate(UUID(doc["id"]), without_location).model_dump(mode="json")
        pending = post(client, doc, "routes/transport", activity_id="c", mode="walking")
        if missing == "place":
            assert pending.status_code == 422 and pending.json()["detail"]["code"] == "place_unverified"
            assert read(app, doc) == doc
        else:
            assert pending.status_code == 200, pending.text
            unknown = post(client, pending.json(), "apply").json()["data"]["days"][0]["activities"][2]
            assert unknown["incomingTransportOverride"]["mode"] == "walking"
            assert unknown["incomingRoute"]["status"] == "unknown" and "durationMinutes" not in unknown["incomingRoute"]
        assert not tools.calls


def test_input_revision_busy_and_storage_failures_do_not_modify_the_draft(app, monkeypatch):
    tools = app.state.agents.tools = RouteMaps()
    with TestClient(app) as client:
        empty = create(client, real=False)
        assert post(client, empty, "routes/transport", activity_id="b", mode="walking").status_code == 409
        original = setup_document(client, mode="transit", draft=False)
        assert set_mode(client, original, "b", None) == original
        for fields in ({"activity_id": "b"}, {"activity_id": "missing", "mode": None}, {"activity_id": "a", "mode": "walking"},
                       {"activity_id": "b", "mode": "bike"}, {"activity_id": "b", "mode": "walking", "route": {}}):
            assert post(client, original, "routes/transport", **fields).status_code == 422
        assert read(app, original) == original
        with monkeypatch.context() as patch:
            def fail_write(*_args):
                raise OSError("fixture write failure")
            patch.setattr(app.state.store, "_atomic_replace", fail_write)
            assert post(client, original, "routes/transport", activity_id="b", mode="walking").status_code == 503
        assert read(app, original) == original
        saved = set_mode(client, original, "b", "walking")
        assert post(client, original, "routes/transport", activity_id="b", mode="transit").json()["detail"]["code"] == "revision_conflict"
        busy = app.state.store.mutate(UUID(saved["id"]), lambda data: data.update(agentPending=True)).model_dump(mode="json")
        assert post(client, busy, "routes/transport", activity_id="b", mode=None).json()["detail"]["code"] == "trip_busy"
        assert read(app, original) == busy and not tools.calls
