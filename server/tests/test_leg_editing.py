"""Offline editing of a mixed itinerary with repeated places on different days."""
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from supertravel_server import candidates
from supertravel_server.candidates import as_candidate
from supertravel_server.mcp_client import ToolError
from supertravel_server.trip_api import clear_changed_routes
from .test_candidates import insert, park, slots
from .test_leg_transport import set_mode
from .test_place_selection import SearchTools, select as replace_place, token
from .test_route_options import OptionsTools, select
from .test_trip_api import AUTH, app, save
from .test_trip_mutations import activity, document, move, post, read


def mixed_draft(client):
    tools = client.app.state.agents.tools = OptionsTools()

    def setup(data):
        data["trip"]["transportMode"] = "driving"
        first, second = [day["activities"] for day in data["days"]]
        first.extend([activity("e", "15:00", previous="c"), activity("f", "17:00", previous="e")])
        for index, stop in enumerate(first):
            stop["place"]["sourceLocation"] = {"latitude": 30.25, "longitude": 120.1 + index / 10, "coordinateSystem": "bd09ll"}
            if index:
                stop["incomingRoute"]["mode"] = "driving"
        first[2]["cost"] = {"amountFen": 0, "currency": "CNY", "source": "user"}
        second[:] = deepcopy(first[:3])
        for stop, identifier, time in zip(second, ("u", "v", "w"), ("08:00", "12:00", "16:00")):
            stop.update(id=identifier, time=time)
        data["candidates"] = [as_candidate(activity("spare", "18:00"), "杭州")]
        data["favorites"] = ["b", "c", "f", "v", "spare"]

    pending = document(client, draft=False, change=setup)
    for target, index in (("b", 1), ("e", 2), ("f", 1), ("v", 2), ("w", 1)):
        pending = select(client, set_mode(client, pending, target, "transit"), index=index, target=target)
    pending = set_mode(client, pending, "c", "walking")
    applied = post(client, pending, "apply")
    assert applied.status_code == 200, applied.text
    formal = applied.json()
    data = deepcopy(formal["data"])
    data.update(draft=deepcopy(data["days"]), draftTrip=deepcopy(data["trip"]), draftCandidates=deepcopy(data["candidates"]))
    saved = save(client, formal, data)
    assert saved.status_code == 200, saved.text
    assert len(tools.calls) == 6
    return saved.json(), tools


def stops(doc):
    return {item["id"]: item for day in doc["data"]["draft"] for item in day["activities"]}


def clear_leg(stop):
    stop.pop("incomingTransportOverride", None)
    stop.pop("incomingRoute", None)


@pytest.mark.parametrize("cross_day", [False, True])
def test_reorder_keeps_only_surviving_edges_and_all_activity_data(app, cross_day):
    with TestClient(app) as client:
        original, tools = mixed_draft(client)
        assert move(client, original, activity_id="b", target_day=2).json()["detail"]["code"] == "place_duplicate"
        assert read(app, original) == original

        expected = deepcopy(original["data"])
        first, second = [day["activities"] for day in expected["draft"]]
        if cross_day:
            moved = first.pop(3)
            second.insert(1, moved)
            moved["time"] = "10:00"
            for target in (moved, first[-1], second[2]):
                clear_leg(target)
            expected["selectedDay"] = 2
            result = move(client, original, activity_id="e", target_day=2, target_index=1, time="10:00")
        else:
            first.append(first.pop(0))
            first[-1]["time"] = "19:00"
            clear_leg(first[0])
            result = move(client, original, activity_id="a", target_day=1, target_index=4, time="19:00")
        assert result.status_code == 200, result.text
        assert result.json()["data"] == expected
        assert len(tools.calls) == 6
        applied = post(client, result.json(), "apply")
        assert applied.status_code == 200, applied.text
        new_targets = {"f", "e", "v"} if cross_day else {"a"}
        for day, expected_day in zip(applied.json()["data"]["days"], expected["draft"]):
            for stop, previous in zip(day["activities"], expected_day["activities"]):
                if stop["id"] in new_targets:
                    assert stop == {**previous, "incomingRoute": stop["incomingRoute"]}
                    assert stop["incomingRoute"]["status"] == "verified" and stop["incomingRoute"]["mode"] == "driving"
                else:
                    assert stop == previous
        assert len(tools.calls) == 6 + len(new_targets)


@pytest.mark.parametrize("operation", ["select", "resolve"])
@pytest.mark.parametrize("same_poi", [False, True])
def test_change_place_only_invalidates_the_two_legs_at_this_visit(app, operation, same_poi):
    with TestClient(app) as client:
        original, routes = mixed_draft(client)
        search = app.state.agents.tools = SearchTools()
        row = search.rows[0]
        row.update(uid="b" if same_poi else "replacement", name="固定地点b" if same_poi else "新地点")
        search.rows = [row]
        if operation == "select":
            ticket = token(client, original)
            assert read(app, original) == original
            response = replace_place(client, original, ticket, activity_id="b")
        else:
            response = client.post(f"/api/conversations/{original['id']}/resolve-place", headers=AUTH,
                                   json={"expected_revision": original["revision"], "activity_id": "b", "query": row["name"]})
        assert response.status_code == 200, response.text
        after = response.json()
        expected = deepcopy(original["data"])
        modified, next_stop = expected["draft"][0]["activities"][1:3]
        clear_leg(modified)
        clear_leg(next_stop)
        place = stops(after)["b"]["place"]
        assert place["id"] == "baidu:" + row["uid"] and place != stops(original)["b"]["place"]
        modified.update(place=place, title=row["name"])
        if not same_poi:
            modified.pop("cost")
        assert after["data"] == expected
        assert len(search.calls) == 1 and len(routes.calls) == 6
        app.state.agents.tools = routes
        applied = post(client, after, "apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["data"]["days"][1] == original["data"]["days"][1]
        # Both queries must use this visit's provider coordinates, even with the same POI on day 2.
        calls = [arguments for _, arguments in routes.calls[6:]]
        assert calls == [
            {"origin": "30.250000,120.100000", "destination": "30.250000,120.150000", "model": "driving"},
            {"origin": "30.250000,120.150000", "destination": "30.250000,120.300000", "model": "driving"},
        ]


@pytest.mark.parametrize("available", [True, False])
def test_park_preview_and_insert_keep_the_ticket_routes_and_both_outer_choices(app, monkeypatch, available):
    with TestClient(app) as client:
        original, tools = mixed_draft(client)
        parked = park(client, original, "c")
        by_id = stops(parked)
        expected_e = deepcopy(stops(original)["e"])
        clear_leg(expected_e)
        assert by_id["e"] == expected_e
        assert parked["data"]["draftCandidates"] == original["data"]["candidates"] + [as_candidate(stops(original)["c"], "杭州")]
        assert parked["data"]["favorites"] == original["data"]["favorites"] and len(tools.calls) == 6
        selected = select(client, set_mode(client, parked, "e", "transit"), target="e", index=2)
        if not available:
            tools.error = ToolError("tool_quota", False, "offline quota")
        evidence = {}
        query_route = candidates.query_route

        async def record(tools, origin, target, mode, city):
            assert mode == "driving"
            key = origin["id"], target["id"]
            assert key not in evidence
            result = await query_route(tools, origin, target, mode, city)
            evidence[key] = deepcopy(result)
            return result

        monkeypatch.setattr(candidates, "query_route", record)
        preview = slots(client, selected, candidate_id="c")
        assert preview.status_code == 200, preview.text
        option = next(option for option in preview.json()["options"] if option["position"] == 2)
        assert len(evidence) == 8
        assert len(option["warnings"]) == (0 if available else 2)
        assert read(app, selected) == selected
        assert insert(client, selected, option, "08:00").json()["detail"]["code"] == "candidate_time_invalid"
        assert read(app, selected) == selected
        inserted = insert(client, selected, option, "13:00")
        assert inserted.status_code == 200, inserted.text
        data = inserted.json()["data"]
        expected = deepcopy(original["data"])
        c, e = expected["draft"][0]["activities"][2:4]
        for stop, pair in ((c, ("baidu:b", "baidu:c")), (e, ("baidu:c", "baidu:e"))):
            clear_leg(stop)
            assert evidence[pair]["status"] == ("verified" if available else "unknown")
            stop["incomingRoute"] = evidence[pair]
        assert data == expected
        assert c["cost"]["amountFen"] == 0 and e.get("cost") is None
        assert expected["draft"][0]["activities"][1]["incomingRoute"]["transit"]["fare"]["amountFen"] == 0
        before_apply = len(tools.calls)
        assert before_apply == 7 + len(evidence)
        applied = post(client, inserted.json(), "apply")
        assert applied.status_code == 200, applied.text
        days = applied.json()["data"]["days"]
        if not available:
            for position in (2, 3):
                fresh = days[0]["activities"][position]["incomingRoute"]
                assert fresh["status"] == "unknown" and fresh.get("durationMinutes") is None
                assert "额度" in fresh["reason"]
                # Applying explicitly retries an unknown leg and records the new query time.
                expected["draft"][0]["activities"][position]["incomingRoute"]["queriedAt"] = fresh["queriedAt"]
        assert days == expected["draft"]
        assert len(tools.calls) == before_apply + (0 if available else 2)


def test_an_adjacent_pair_moved_together_to_a_new_day_does_not_recover_its_choices(app):
    with TestClient(app) as client:
        original, tools = mixed_draft(client)
        data = deepcopy(original["data"])
        first, second = data["draft"]
        moved = first["activities"][2:4]
        del first["activities"][2:4]
        data["draft"].append({"day": 3, "title": "第三天", "subtitle": "离线夹具", "activities": moved})
        assert save(client, original, data).status_code == 422
        clear_changed_routes(data["draft"], original["data"]["draft"])
        assert all("incomingTransportOverride" not in stop and "incomingRoute" not in stop for stop in [*moved, first["activities"][-1]])
        assert second == original["data"]["draft"][1]
        saved = save(client, original, data)
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"] == data and len(tools.calls) == 6


def test_deleting_a_stop_counts_pending_choices_and_only_clears_its_neighbor(app):
    with TestClient(app) as client:
        original, tools = mixed_draft(client)
        pending = set_mode(client, original, "e", "walking")
        assert "incomingRoute" not in stops(pending)["e"]
        preview = post(client, pending, "activities/delete-preview", activity_id="e")
        assert preview.json()["affected_routes"] == 2 and read(app, pending) == pending
        deleted = post(client, pending, "activities/delete", activity_id="e")
        assert deleted.status_code == 200, deleted.text
        expected = deepcopy(pending["data"])
        first = expected["draft"][0]["activities"]
        first.pop(3)
        clear_leg(first[-1])
        assert deleted.json()["data"] == expected and len(tools.calls) == 6


def test_autosave_cannot_reuse_an_ordinary_route_on_another_day_with_the_same_places(app):
    with TestClient(app) as client:
        original, _ = mixed_draft(client)
        # Day 2 visits the same pair B -> C under different activity IDs.
        copied = stops(original)["c"]["incomingRoute"]
        draft = set_mode(client, original, "w", "walking")
        data = deepcopy(draft["data"])
        data["draft"][1]["activities"][2]["incomingRoute"] = copied
        rejected = save(client, draft, data)
        assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "route_unverified"
        assert read(app, draft) == draft
