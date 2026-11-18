"""Phase 4 offline acceptance across route selection and existing mutations."""
from copy import deepcopy
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from .test_candidates import slots, insert
from .test_route_options import OptionsTools, select
from .test_trip_api import app, save
from .test_trip_mutations import post, read
from .test_trip_transport import setup_document


def activities(document, snapshot="days"):
    return {item["id"]: item for day in document["data"][snapshot] for item in day["activities"]}


@pytest.mark.parametrize("mutation", ["cross_day_move", "candidate_insert"])
def test_selected_route_survives_only_unchanged_adjacency_and_restore(app, mutation):
    tools = app.state.agents.tools = OptionsTools()
    with TestClient(app) as client:
        original = setup_document(client, mode="transit")
        def prepare_candidate(data):
            for field in ("candidates", "draftCandidates"):
                data[field][0]["duration"] = "约 15 分钟"
                data[field][0]["place"]["sourceLocation"] = deepcopy(data["days"][0]["activities"][0]["place"]["sourceLocation"])
        original = app.state.store.mutate(UUID(original["id"]), prepare_candidate).model_dump(mode="json")
        pending = select(client, select(client, original, 1, "b"), 2, "c")
        response = post(client, pending, "apply")
        assert response.status_code == 200, response.text
        formal = response.json()
        old = activities(formal)
        assert old["b"]["incomingRoute"]["durationMinutes"] == 20
        assert old["c"]["incomingRoute"]["durationMinutes"] == 30
        assert len(tools.calls) == 2

        if mutation == "cross_day_move":
            data = deepcopy(formal["data"])
            data.update(draft=deepcopy(data["days"]), draftTrip=deepcopy(data["trip"]), draftCandidates=deepcopy(data["candidates"]))
            saved = save(client, formal, data)
            assert saved.status_code == 200, saved.text
            response = post(client, saved.json(), "activities/move", activity_id="a", target_day=2, target_index=1, time="12:00")
            assert response.status_code == 200, response.text
            pending = response.json()
            assert [[item["id"] for item in day["activities"]] for day in pending["data"]["draft"]] == [["b", "c"], ["d", "a"]]
            assert "incomingRoute" not in activities(pending, "draft")["b"]
            assert len(tools.calls) == 2
        else:
            result = slots(client, formal, candidate_id="candidate", duration=15)
            assert result.status_code == 200, result.text
            option = next(item for item in result.json()["options"] if item["position"] == 1)
            assert (option["earliest_start"], option["latest_start"]) == ("10:15", "10:30")
            assert len(tools.calls) == 8  # Two choices, then the six possible candidate-neighbor legs.
            response = insert(client, formal, option)
            assert response.status_code == 200, response.text
            pending = response.json()
            current = activities(pending, "draft")
            assert [item["id"] for item in pending["data"]["draft"][0]["activities"]] == ["a", "candidate", "b", "c"]
            assert current["b"]["incomingRoute"]["fromPlaceId"] == "baidu:candidate"
            assert "selection" not in current["b"]["incomingRoute"]
            assert current["candidate"]["description"] == formal["data"]["candidates"][0]["description"]
            assert current["candidate"]["cost"]["amountFen"] == 0
            assert pending["data"]["draftCandidates"] == []
            assert len(tools.calls) == 8

        assert pending["data"]["days"] == formal["data"]["days"]
        assert activities(pending, "draft")["c"]["incomingRoute"] == old["c"]["incomingRoute"]
        response = post(client, pending, "apply")
        assert response.status_code == 200, response.text
        applied = response.json()
        assert len(tools.calls) == (3 if mutation == "cross_day_move" else 8)
        current = activities(applied)
        assert current["c"]["incomingRoute"] == old["c"]["incomingRoute"]
        assert applied["data"]["favorites"] == formal["data"]["favorites"]
        for identifier, item in old.items():
            for field in ("id", "description", "cost", "place"):
                assert current[identifier].get(field) == item.get(field)
        assert current["b"]["cost"]["amountFen"] == 2500
        assert read(app, applied) == applied
        restored = post(client, applied, "restore")
        assert restored.status_code == 200, restored.text
        for field in ("days", "trip", "candidates", "favorites"):
            assert restored.json()["data"][field] == formal["data"][field]
        assert len(tools.calls) == (3 if mutation == "cross_day_move" else 8)
