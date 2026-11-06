"""Candidate edits use fixtures only; no paid upstream requests."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from time import monotonic
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from backend import candidates, trip_api
from backend.candidates import as_candidate
from backend.main import create_app
from backend.tests.test_place_selection import app, token
from backend.tests.test_trip_api import AUTH, create, save
from backend.tests.test_trip_mutations import activity, document, post, read
from backend.trip_routes import unknown_route


def park(client, doc, identifier="b"):
    response = post(client, doc, "candidates/park", activity_id=identifier)
    assert response.status_code == 200, response.text
    return response.json()


def slots(client, doc, candidate_id="b", day=1, duration=60):
    return post(client, doc, "candidates/slots", candidate_id=candidate_id, day=day, duration_minutes=duration)


def insert(client, doc, option, time=None):
    return post(client, doc, "candidates/insert", option_id=option["option_id"], time=time or option["earliest_start"])


def fake_routes(monkeypatch, duration=13):
    calls = []
    async def query(tools, origin, target, mode, city):
        calls.append((origin["id"], target["id"]))
        return {**unknown_route(origin, target, mode), "status": "verified", "durationMinutes": duration,
                "distanceMeters": 500, "provider": "baidu", "queriedAt": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(candidates, "query_route", query)
    return calls


def test_park_preserves_identity_note_cost_empty_day_and_favorites_through_restart(app, tmp_path):
    with TestClient(app) as client:
        original = document(client, draft=False)
        pending = park(client, original)
        candidate = pending["data"]["draftCandidates"][0]
        old = original["data"]["days"][0]["activities"][1]
        assert candidate == as_candidate(old, "杭州")
        assert not {"time", "x", "y", "incomingRoute"}.intersection(candidate)
        assert "incomingRoute" not in pending["data"]["draft"][0]["activities"][1]
        pending = park(client, pending, "d")
        assert pending["data"]["draft"][1]["activities"] == []
        assert pending["data"]["days"] == original["data"]["days"]
    with TestClient(create_app(tmp_path, "trip-api-fixture")) as client:
        assert client.get(f"/api/conversations/{original['id']}", headers=AUTH).json() == pending
        saved = post(client, pending, "apply").json()
        assert saved["data"]["favorites"] == original["data"]["favorites"]
        assert [item["id"] for item in saved["data"]["candidates"]] == ["b", "d"]
        assert saved["data"]["previousTrip"]["candidates"] == []
        assert saved["data"]["previousTrip"]["removedFavorites"] == []
        restored = post(client, saved, "restore").json()
        assert restored["data"]["days"] == original["data"]["days"]
        assert restored["data"]["candidates"] == []
        assert restored["data"]["previousTrip"]["candidates"] == saved["data"]["candidates"]


def test_park_last_activity_is_rejected_without_changing_formal(app):
    with TestClient(app) as client:
        original = create(client)
        response = post(client, original, "candidates/park", activity_id="fixture-stop")
        assert response.status_code == 422 and response.json()["detail"]["code"] == "trip_empty"
        assert read(app, original) == original


def test_candidate_only_draft_applies_discards_restores_and_restores_deleted_favorite(app):
    with TestClient(app) as client:
        original = document(client)
        saved = post(client, park(client, original), "apply").json()
        data = deepcopy(saved["data"])
        data["draftCandidates"] = deepcopy(data["candidates"])
        data["draftCandidates"][0]["description"] = "仅改候选的备注"
        pending = save(client, saved, data).json()
        assert pending["data"]["draft"] is None
        assert post(client, pending, "restore").json()["detail"]["code"] == "draft_exists"
        discarded = post(client, pending, "discard").json()
        assert discarded["data"]["draftCandidates"] is None
        assert discarded["data"]["candidates"] == saved["data"]["candidates"]
        data = deepcopy(discarded["data"])
        data["draftCandidates"] = []
        pending = save(client, discarded, data).json()
        removed = post(client, pending, "apply").json()
        assert removed["data"]["candidates"] == [] and "b" not in removed["data"]["favorites"]
        assert removed["data"]["previousTrip"]["removedFavorites"] == ["b"]
        restored = post(client, removed, "restore").json()
        assert restored["data"]["candidates"] == saved["data"]["candidates"]
        assert "b" in restored["data"]["favorites"]


def test_old_json_read_defaults_do_not_rewrite_and_old_draft_inherits_formal_pool(app):
    with TestClient(app) as client:
        original = document(client)
        path = app.state.store._path(UUID(original["id"]))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["data"].pop("candidates")
        raw["data"].pop("draftCandidates")
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()
        current = read(app, original)
        assert current["data"]["candidates"] == current["data"]["draftCandidates"] == []
        assert path.read_bytes() == before
        saved = post(client, park(client, current), "apply").json()
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["data"]["draft"] = deepcopy(raw["data"]["days"])
        raw["data"].pop("draftCandidates")
        raw["data"]["previousTrip"].pop("candidates")
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()
        current = read(app, saved)
        assert current["data"]["draftCandidates"] == current["data"]["candidates"]
        assert current["data"]["previousTrip"]["candidates"] == [] and path.read_bytes() == before
        old_payload = deepcopy(current["data"])
        old_payload.pop("candidates")
        old_payload.pop("draftCandidates")
        old_payload["previousTrip"].pop("candidates")
        updated = save(client, current, old_payload)
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["candidates"] == saved["data"]["candidates"]
        assert updated.json()["data"]["draftCandidates"] == saved["data"]["candidates"]


@pytest.mark.parametrize("tamper", ["coordinates", "destination", "time", "route", "duplicate", "cost", "formal", "identity"])
def test_generic_write_rejects_candidate_tampering_and_duplicate_identity(app, tamper):
    with TestClient(app) as client:
        current = park(client, document(client))
        data = deepcopy(current["data"])
        candidate = data["draftCandidates"][0]
        if tamper == "coordinates": candidate["place"]["longitude"] += 1
        elif tamper == "destination": candidate["destination"] = "苏州"
        elif tamper == "time": candidate["time"] = "12:00"
        elif tamper == "route": candidate["incomingRoute"] = {}
        elif tamper == "duplicate": candidate["id"] = data["draft"][0]["activities"][0]["id"]
        elif tamper == "cost": candidate["cost"]["amountFen"] = True
        elif tamper == "formal": data["candidates"] = deepcopy(data["draftCandidates"])
        elif tamper == "identity": candidate["id"] = "forged-candidate"
        response = save(client, current, data)
        assert response.status_code == 422, response.text
        assert read(app, current) == current


def test_candidate_busy_apis_and_autosave_are_rejected(app):
    with TestClient(app) as client:
        current = park(client, document(client))
        busy = app.state.store.mutate(UUID(current["id"]), lambda data: data.update(agentPending=True)).model_dump(mode="json")
        for operation, payload in [("park", {"activity_id": "a"}), ("delete", {"candidate_id": "b"}),
                                   ("slots", {"candidate_id": "b", "day": 1, "duration_minutes": 60}),
                                   ("insert", {"option_id": "fake", "time": "12:00"})]:
            result = post(client, busy, "candidates/" + operation, **payload)
            assert result.status_code == 409 and result.json()["detail"]["code"] == "trip_busy"
        data = deepcopy(busy["data"])
        data["draftCandidates"][0]["description"] = "忙碌时不能保存"
        assert save(client, busy, data).json()["detail"]["code"] == "trip_busy"
        assert read(app, busy) == busy


def test_search_to_candidate_from_formal_is_credential_bound_and_does_not_require_day(app):
    with TestClient(app) as client:
        current = create(client)
        selected = token(client, current)
        assert read(app, current) == current
        other = create(client)
        assert post(client, other, "candidates/from-place", selection_id=selected, duration_minutes=60).status_code == 409
        assert post(client, current, "candidates/from-place", selection_id=selected, duration_minutes=61).status_code == 422
        result = post(client, current, "candidates/from-place", selection_id=selected, duration_minutes=90, description="备选备注")
        assert result.status_code == 200, result.text
        updated = result.json()
        candidate = updated["data"]["draftCandidates"][0]
        assert candidate["place"]["id"] == "baidu:fixture-west" and candidate["destination"] == "杭州"
        assert candidate["duration"] == "约 90 分钟" and candidate["description"] == "备选备注"
        assert updated["data"]["days"] == current["data"]["days"]
        assert post(client, updated, "candidates/from-place", selection_id=selected, duration_minutes=60).status_code == 409
        empty = create(client, real=False)
        assert post(client, empty, "candidates/from-place", selection_id="fake", duration_minutes=60).status_code == 409


def test_known_routes_bound_slots_and_insert_is_atomic_preserving_identity(app, monkeypatch):
    calls = fake_routes(monkeypatch)
    with TestClient(app) as client:
        pending = park(client, document(client))
        formal = post(client, pending, "apply").json()
        result = slots(client, formal, duration=120)
        assert result.status_code == 200, result.text
        assert read(app, formal) == formal
        middle = next(item for item in result.json()["options"] if item["position"] == 1)
        assert (middle["earliest_start"], middle["latest_start"]) == ("10:15", "10:45")
        assert middle["warnings"] == [] and len(calls) == len(set(calls))
        for time in ("10:00", "10:16", "11:00"):
            assert insert(client, formal, middle, time).status_code == 422
            assert read(app, formal) == formal
        response = insert(client, formal, middle, "10:30")
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["data"]["draftCandidates"] == [] and formal["data"]["candidates"] == updated["data"]["candidates"]
        activities = updated["data"]["draft"][0]["activities"]
        assert [item["id"] for item in activities] == ["a", "b", "c"]
        assert [item["time"] for item in activities] == ["09:00", "10:30", "13:00"]
        assert activities[1]["cost"]["amountFen"] == 2500 and activities[1]["description"] == "保留备注b"
        assert activities[1]["incomingRoute"]["status"] == activities[2]["incomingRoute"]["status"] == "verified"
        assert insert(client, formal, middle).status_code == 409
        assert insert(client, updated, middle).status_code == 409


def test_unknown_routes_warn_but_allow_insert_and_apply(app):
    with TestClient(app) as client:
        current = park(client, document(client))
        options = slots(client, current).json()["options"]
        assert options and all(option["warnings"] for option in options)
        updated = insert(client, current, options[1]).json()
        assert updated["data"]["draft"][0]["activities"][1]["incomingRoute"]["status"] == "unknown"
        applied = post(client, updated, "apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["data"]["candidates"] == []


def test_known_impossible_routes_and_full_day_offer_no_slots(app, monkeypatch):
    fake_routes(monkeypatch, duration=1200)
    with TestClient(app) as client:
        current = park(client, document(client))
        result = slots(client, current)
        assert result.json()["options"] == [] and result.json()["reason"]
        assert read(app, current) == current
        data = deepcopy(current["data"])
        data["draft"][0]["activities"][0].update(time="00:00", duration="约 720 分钟")
        data["draft"][0]["activities"][1].update(time="12:00", duration="约 720 分钟")
        current = save(client, current, data).json()
        assert slots(client, current).json()["options"] == []


def test_empty_day_has_all_day_slot_and_candidate_city_is_enforced(app):
    with TestClient(app) as client:
        current = park(client, document(client), "d")
        options = slots(client, current, candidate_id="d", day=2, duration=60).json()["options"]
        assert len(options) == 1 and options[0]["earliest_start"] == "00:00" and options[0]["latest_start"] == "23:00"
        assert options[0]["warnings"] == []
        current = app.state.store.mutate(UUID(current["id"]), lambda data: data["draftTrip"].update(destination="苏州")).model_dump(mode="json")
        assert slots(client, current, candidate_id="d", day=2).json()["detail"]["code"] == "candidate_destination"
        assert read(app, current) == current


def test_slot_total_deadline_turns_remaining_routes_unknown_without_repeated_waits(app, monkeypatch):
    calls = []
    async def slow(*args):
        calls.append(1)
        await asyncio.sleep(5)
    monkeypatch.setattr(candidates, "query_route", slow)
    monkeypatch.setattr(candidates, "SLOT_QUERY_TIMEOUT", .02)
    with TestClient(app) as client:
        current = park(client, document(client))
        start = monotonic()
        result = slots(client, current)
        assert monotonic() - start < 1
        assert len(calls) == 1 and result.status_code == 200
        assert all(option["warnings"] for option in result.json()["options"])
        assert read(app, current) == current


@pytest.mark.parametrize("invalidate", ["revision", "connection", "expiry", "cross_session", "query", "route_age"])
def test_stale_or_foreign_insert_options_cannot_consume_candidate(app, monkeypatch, invalidate):
    fake_routes(monkeypatch)
    with TestClient(app) as client:
        current = park(client, document(client))
        option = slots(client, current).json()["options"][0]
        if invalidate == "revision": current = save(client, current, current["data"]).json()
        elif invalidate == "connection": app.state.agents.tools = object()
        elif invalidate == "expiry": monkeypatch.setattr(trip_api, "monotonic", lambda: monotonic() + 301)
        elif invalidate == "cross_session": current = park(client, document(client))
        elif invalidate == "query": slots(client, current, day=2)
        elif invalidate == "route_age": monkeypatch.setattr(trip_api, "route_fresh", lambda route: False)
        rejected = insert(client, current, option)
        assert rejected.status_code == 409, rejected.text
        assert read(app, current) == current


def test_insert_save_failure_preserves_candidate_and_reusable_option(app, monkeypatch):
    with TestClient(app) as client:
        current = park(client, document(client))
        option = slots(client, current).json()["options"][1]
        original = app.state.store._atomic_replace
        def fail(path, payload):
            if path.parent.name == "conversations": raise OSError("fixture disk full")
            return original(path, payload)
        monkeypatch.setattr(app.state.store, "_atomic_replace", fail)
        response = insert(client, current, option)
        assert response.status_code == 503
        assert read(app, current) == current
        monkeypatch.setattr(app.state.store, "_atomic_replace", original)
        assert insert(client, current, option).status_code == 200


def test_legacy_non_quarter_duration_is_preserved_but_new_duration_must_use_quarters(app):
    with TestClient(app) as client:
        current = document(client, change=lambda data: data["days"][0]["activities"][1].update(duration="约 55 分钟"))
        current = park(client, current)
        response = slots(client, current, duration=55)
        assert response.status_code == 200 and response.json()["options"]
        assert slots(client, current, duration=56).status_code == 422
        inserted = insert(client, current, response.json()["options"][1])
        assert inserted.status_code == 200, inserted.text
        assert inserted.json()["data"]["draft"][0]["activities"][1]["duration"] == "约 55 分钟"


def test_new_valid_query_with_no_slots_invalidates_previously_returned_options(app):
    with TestClient(app) as client:
        current = document(client, change=lambda data: data["days"][1].update(activities=[
            {**activity("full-a", "00:00"), "duration": "约 720 分钟"},
            {**activity("full-b", "12:00"), "duration": "约 720 分钟"}]))
        current = park(client, current)
        option = slots(client, current, day=1).json()["options"][1]
        empty = slots(client, current, day=2)
        assert empty.status_code == 200 and empty.json()["options"] == []
        rejected = insert(client, current, option)
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "candidate_option_expired"
        assert read(app, current) == current


def test_park_empty_note_explains_validation_without_losing_the_saved_draft(app):
    with TestClient(app) as client:
        current = document(client, change=lambda data: data["days"][0]["activities"][1].update(description=""))
        response = post(client, current, "candidates/park", activity_id="b")
        assert response.status_code == 400 and response.json()["detail"]["code"] == "candidate_note_missing"
        assert "备注不能为空" in response.json()["detail"]["message"]
        assert read(app, current) == current


@pytest.mark.parametrize("from_formal", [False, True])
@pytest.mark.parametrize("renamed_id", [False, True])
def test_generic_put_cannot_bypass_candidate_insert_checks(app, from_formal, renamed_id):
    with TestClient(app) as client:
        current = park(client, document(client))
        if from_formal:
            current = post(client, current, "apply").json()
        data = deepcopy(current["data"])
        pool = data["draftCandidates"] if data["draftCandidates"] is not None else data["candidates"]
        candidate = deepcopy(pool[0])
        candidate.pop("destination")
        candidate.update(time="11:00", x=0, y=0)
        if renamed_id:
            candidate["id"] = "alias-of-candidate"
        data["draft"] = deepcopy(data["draft"] if data["draft"] is not None else data["days"])
        data["draft"][0]["activities"].insert(1, candidate)
        data["draft"][0]["activities"][2].pop("incomingRoute", None)
        data["draftCandidates"] = []
        rejected = save(client, current, data)
        assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "candidate_insert_required"
        assert read(app, current) == current
        # The normal discard path restores the formal snapshot and is still valid.
        data = deepcopy(current["data"])
        data.update(draft=None, draftTrip=None, draftCandidates=None)
        discarded = save(client, current, data)
        assert discarded.status_code == 200, discarded.text
        assert discarded.json()["data"]["days"] == current["data"]["days"]
