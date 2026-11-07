from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from supertravel_server.app import create_app
from .test_trip_api import AUTH, create, save


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ("STA_MODEL_API_KEY", "STA_MODEL_BASE_URL", "STA_MODEL_NAME", "STA_MCP_URL", "STA_SHARED_TOOLS_URL", "AMAP_MAPS_API_KEY", "BAIDU_MAP_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return create_app(tmp_path, "trip-api-fixture")


def activity(identifier, time, *, previous=None):
    result = {"id": identifier, "title": f"固定地点{identifier}", "time": time, "duration": "约 60 分钟", "description": f"保留备注{identifier}", "x": 0, "y": 0,
              "place": {"id": f"baidu:{identifier}", "name": f"固定地点{identifier}", "address": "固定地址", "latitude": 30.2, "longitude": 120.1,
                        "coordinateSystem": "wgs84", "provider": "baidu"}}
    if previous:
        result["incomingRoute"] = {"fromPlaceId": f"baidu:{previous}", "toPlaceId": f"baidu:{identifier}", "mode": "walking", "status": "verified",
                                   "durationMinutes": 15, "distanceMeters": 500, "provider": "baidu", "queriedAt": datetime.now(timezone.utc).isoformat()}
    return result


def document(client, *, draft=True, change=None):
    created = create(client, real=False)
    first = [activity("a", "09:00"), activity("b", "11:00", previous="a"), activity("c", "13:00", previous="b")]
    first[1]["cost"] = {"amountFen": 2500, "currency": "CNY", "source": "user"}
    data = {"trip": {"destination": "杭州", "summary": "固定双日行程", "generatedAt": "2026-10-28T00:00:00+08:00"},
            "days": [{"day": 1, "title": "第一天", "subtitle": "固定安排", "activities": first},
                     {"day": 2, "title": "第二天", "subtitle": "固定安排", "activities": [activity("d", "10:00")]}],
            "favorites": ["a", "b", "d"]}
    if change:
        change(data)
    if draft:
        data.update(draft=deepcopy(data["days"]), draftTrip=deepcopy(data["trip"]))
    return client.app.state.store.mutate(UUID(created["id"]), lambda current: current.update(data)).model_dump(mode="json")


def post(client, doc, operation, **payload):
    return client.post(f"/api/conversations/{doc['id']}/trip/{operation}", headers=AUTH,
                       json={"expected_revision": doc["revision"], **payload})


def move(client, doc, **payload):
    return post(client, doc, "activities/move", **{"activity_id": "b", "target_day": 2, "target_index": 1, "time": "12:00", **payload})


def read(app, doc):
    return app.state.store.get(UUID(doc["id"])).model_dump(mode="json")


def test_move_preserves_identity_notes_cost_and_rechecks_both_days(app):
    with TestClient(app) as client:
        original = document(client)
        moved = move(client, original)
        assert moved.status_code == 200, moved.text
        result = moved.json()
        assert result["data"]["days"] == original["data"]["days"]
        assert result["data"]["favorites"] == original["data"]["favorites"]
        assert result["data"]["selectedDay"] == 2
        first, second = result["data"]["draft"]
        assert [item["id"] for item in first["activities"]] == ["a", "c"]
        assert [item["id"] for item in second["activities"]] == ["d", "b"]
        target = second["activities"][1]
        expected = {**original["data"]["draft"][0]["activities"][1], "time": "12:00"}
        expected.pop("incomingRoute")
        assert target == expected and "incomingRoute" not in first["activities"][1]
        assert move(client, original).status_code == 409
        assert read(app, original) == result


def test_same_day_position_is_after_removal_and_is_not_implicitly_sorted(app):
    with TestClient(app) as client:
        original = document(client)
        invalid = move(client, original, target_day=1, target_index=0, time="12:00")
        assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "trip_invalid"
        assert read(app, original) == original
        moved = move(client, original, target_day=1, target_index=2, time="15:00")
        assert moved.status_code == 200, moved.text
        items = moved.json()["data"]["draft"][0]["activities"]
        assert [item["id"] for item in items] == ["a", "c", "b"]
        assert items[0]["time"] == "09:00" and items[1]["time"] == "13:00"


def test_moving_only_activity_leaves_an_empty_day_that_can_be_applied_and_reloaded(app, tmp_path):
    with TestClient(app) as client:
        original = document(client)
        moved = move(client, original, activity_id="d", target_day=1, target_index=3, time="15:00")
        assert moved.status_code == 200, moved.text
        pending = moved.json()
        assert pending["data"]["draft"][1]["activities"] == []
        applied = post(client, pending, "apply")
        assert applied.status_code == 200, applied.text
        saved = applied.json()
        assert saved["data"]["days"][1]["activities"] == []
    with TestClient(create_app(tmp_path, "trip-api-fixture")) as restarted:
        assert restarted.get(f"/api/conversations/{saved['id']}", headers=AUTH).json() == saved


@pytest.mark.parametrize("payload,code", [({"target_index": 2}, "trip_position_invalid"), ({"target_day": 3}, "trip_day_missing"),
                                          ({"activity_id": "missing"}, "activity_missing"), ({"time": "12:01"}, "trip_time_step"),
                                          ({"time": "23:30"}, "trip_invalid"), ({"time": "10:30"}, "trip_invalid")])
def test_invalid_move_is_rejected_without_changing_the_saved_draft(app, payload, code):
    with TestClient(app) as client:
        original = document(client)
        result = move(client, original, **payload)
        assert result.status_code == 422 and result.json()["detail"]["code"] == code
        assert read(app, original) == original


def test_full_day_and_duplicate_poi_are_rejected(app):
    with TestClient(app) as client:
        duplicate = document(client, change=lambda data: data["days"][1]["activities"][0].update(place=deepcopy(data["days"][0]["activities"][1]["place"]), title="固定地点b"))
        assert move(client, duplicate).json()["detail"]["code"] == "place_duplicate"
        full = document(client, change=lambda data: data["days"][1].update(activities=[activity(f"full-{index}", f"{index + 8:02}:00") for index in range(8)]))
        assert move(client, full).json()["detail"]["code"] == "trip_day_full"
        assert read(app, duplicate) == duplicate and read(app, full) == full


def test_old_nonquarter_time_and_duration_survive_only_when_time_is_unchanged(app):
    with TestClient(app) as client:
        original = document(client, change=lambda data: data["days"][0]["activities"][1].update(time="11:05", duration="约 55 分钟"))
        assert move(client, original, time="11:06").json()["detail"]["code"] == "trip_time_step"
        result = move(client, original, time="11:05")
        assert result.status_code == 200, result.text
        target = result.json()["data"]["draft"][1]["activities"][1]
        assert target["time"] == "11:05" and target["duration"] == "约 55 分钟"


def test_same_position_time_change_checks_existing_transfer_and_unknown_stays_unknown(app):
    with TestClient(app) as client:
        original = document(client)
        result = move(client, original, target_day=1, target_index=1, time="10:00")
        assert result.status_code == 422 and result.json()["detail"]["code"] == "trip_transfer_conflict"
        def unknown(data):
            route = data["days"][0]["activities"][1]["incomingRoute"]
            route.update(status="unknown", reason="固定测试未知路线")
            route.pop("durationMinutes")
            route.pop("distanceMeters")
        original = document(client, change=unknown)
        accepted = move(client, original, target_day=1, target_index=1, time="10:00")
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["data"]["draft"][0]["activities"][1]["incomingRoute"]["status"] == "unknown"


def test_delete_preview_is_read_only_and_matches_existing_references(app):
    with TestClient(app) as client:
        original = document(client)
        preview = post(client, original, "activities/delete-preview", activity_id="b")
        assert preview.status_code == 200, preview.text
        assert preview.json() == {"revision": original["revision"], "activity_id": "b", "title": "固定地点b", "day": 1,
                                  "has_note": True, "is_favorite": True, "has_map_point": True, "affected_routes": 2}
        assert read(app, original) == original
        tail = post(client, original, "activities/delete-preview", activity_id="d")
        assert tail.json()["affected_routes"] == 0
        assert post(client, original, "activities/delete-preview", activity_id="missing").status_code == 422
        assert read(app, original) == original


def test_delete_only_changes_draft_and_stale_preview_cannot_delete(app):
    with TestClient(app) as client:
        original = document(client)
        assert post(client, original, "activities/delete-preview", activity_id="b").status_code == 200
        newer = save(client, original, original["data"]).json()
        assert post(client, original, "activities/delete", activity_id="b").status_code == 409
        deleted = post(client, newer, "activities/delete", activity_id="b")
        assert deleted.status_code == 200, deleted.text
        result = deleted.json()
        assert result["data"]["days"] == original["data"]["days"] and result["data"]["favorites"] == original["data"]["favorites"]
        assert [item["id"] for item in result["data"]["draft"][0]["activities"]] == ["a", "c"]
        assert "incomingRoute" not in result["data"]["draft"][0]["activities"][1]
        assert post(client, result, "activities/delete", activity_id="b").json()["detail"]["code"] == "activity_missing"
        assert read(app, original) == result


def test_delete_can_empty_one_day_but_cannot_remove_the_last_trip_activity(app):
    with TestClient(app) as client:
        original = document(client)
        deleted = post(client, original, "activities/delete", activity_id="d")
        assert deleted.status_code == 200 and deleted.json()["data"]["draft"][1]["activities"] == []
        for identifier in ("a", "b"):
            deleted = post(client, deleted.json(), "activities/delete", activity_id=identifier)
            assert deleted.status_code == 200, deleted.text
        final = deleted.json()
        rejected = post(client, final, "activities/delete", activity_id="c")
        assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "trip_empty"
        assert read(app, original) == final


def test_apply_restore_preserve_later_favorite_changes_and_two_way_history(app):
    with TestClient(app) as client:
        original = document(client)
        deleted = post(client, original, "activities/delete", activity_id="b").json()
        applied = post(client, deleted, "apply")
        assert applied.status_code == 200, applied.text
        applied = applied.json()
        assert applied["data"]["favorites"] == ["a", "d"]
        assert applied["data"]["previousTrip"]["removedFavorites"] == ["b"]
        data = deepcopy(applied["data"])
        data["favorites"] = ["d", "c"]
        changed = save(client, applied, data).json()
        restored = post(client, changed, "restore")
        assert restored.status_code == 200, restored.text
        restored = restored.json()
        assert restored["data"]["favorites"] == ["d", "c", "b"]
        assert restored["data"]["days"] == original["data"]["days"]
        assert restored["data"]["previousTrip"]["removedFavorites"] == []
        again = post(client, restored, "restore").json()
        assert again["data"]["favorites"] == ["d", "c"]
        assert again["data"]["previousTrip"]["removedFavorites"] == ["b"]
        recovered = post(client, again, "restore").json()
        assert recovered["data"]["favorites"] == ["d", "c", "b"]
        assert "a" not in recovered["data"]["favorites"]


def test_new_draft_favorite_deleted_before_apply_does_not_leave_a_dangling_reference(app):
    with TestClient(app) as client:
        original = document(client)
        def add(data):
            data["draft"][1]["activities"].append(activity("draft-only", "12:00"))
            data["favorites"].append("draft-only")
        original = app.state.store.mutate(UUID(original["id"]), add).model_dump(mode="json")
        deleted = post(client, original, "activities/delete", activity_id="draft-only").json()
        assert "draft-only" in deleted["data"]["favorites"]
        applied = post(client, deleted, "apply").json()
        assert "draft-only" not in applied["data"]["favorites"]
        assert applied["data"]["previousTrip"]["removedFavorites"] == []
        restored = post(client, applied, "restore").json()
        assert "draft-only" not in restored["data"]["favorites"]


def test_legacy_previous_without_favorite_metadata_does_not_invent_restored_favorites(app):
    with TestClient(app) as client:
        original = document(client, draft=False)
        def legacy(data):
            previous = {"days": deepcopy(data["days"]), "trip": deepcopy(data["trip"])}
            data["days"][0]["activities"].pop(1)
            data["days"][0]["activities"][1].pop("incomingRoute")
            data.update(previousTrip=previous, favorites=["d"])
        original = app.state.store.mutate(UUID(original["id"]), legacy).model_dump(mode="json")
        restored = post(client, original, "restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["data"]["favorites"] == ["d"]


def test_generic_autosave_cannot_forge_removed_favorites_and_corrupt_snapshot_is_retained(app):
    with TestClient(app) as client:
        original = document(client)
        applied = post(client, original, "apply").json()
        data = deepcopy(applied["data"])
        data["previousTrip"]["removedFavorites"] = ["b"]
        forged = save(client, applied, data)
        assert forged.status_code == 422 and forged.json()["detail"]["code"] == "trip_protected"
        assert read(app, original) == applied
        corrupt = app.state.store.mutate(UUID(original["id"]), lambda data: data["previousTrip"].update(removedFavorites=["outside-snapshot"])).model_dump(mode="json")
        rejected = post(client, corrupt, "restore")
        assert rejected.status_code == 422 and read(app, original) == corrupt


@pytest.mark.parametrize("operation,payload", [("activities/move", {"activity_id": "b", "target_day": 2, "target_index": 1, "time": "12:00"}),
                                               ("activities/delete-preview", {"activity_id": "b"}), ("activities/delete", {"activity_id": "b"})])
def test_missing_draft_and_busy_task_block_all_mutations(app, operation, payload):
    with TestClient(app) as client:
        original = document(client, draft=False)
        assert post(client, original, operation, **payload).json()["detail"]["code"] == "draft_missing"
        original = document(client)
        busy = app.state.store.mutate(UUID(original["id"]), lambda data: data.update(agentPending=True)).model_dump(mode="json")
        assert post(client, busy, operation, **payload).json()["detail"]["code"] == "trip_busy"
        assert read(app, original) == busy


@pytest.mark.parametrize("operation,payload", [("activities/move", {"activity_id": "b", "target_day": 2, "target_index": 1, "time": "12:00"}),
                                               ("activities/delete", {"activity_id": "b"}), ("apply", {})])
def test_write_failures_preserve_formal_draft_and_favorites_and_allow_retry(app, monkeypatch, operation, payload):
    with TestClient(app) as client:
        original = document(client)
        if operation == "apply":
            original = post(client, original, "activities/delete", activity_id="b").json()
        write = app.state.store._atomic_replace
        def fail(path, content):
            if path.parent.name == "conversations":
                raise OSError("fixture write failure")
            return write(path, content)
        monkeypatch.setattr(app.state.store, "_atomic_replace", fail)
        rejected = post(client, original, operation, **payload)
        assert rejected.status_code == 503 and read(app, original) == original
        monkeypatch.setattr(app.state.store, "_atomic_replace", write)
        assert post(client, original, operation, **payload).status_code == 200
