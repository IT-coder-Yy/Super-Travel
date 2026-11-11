"""Provider-shaped fixtures; no real map/model calls or billing claims."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from supertravel_server.trip_routes import _fare, _transit_details, query_route
from supertravel_server.trips import IncomingRoute, TripRequest, TripResearch, validate_result
from supertravel_server.trip_api import clear_changed_routes, refresh_routes
from .test_trip_routes import RouteMaps, research_pair
from .test_trip_api import AUTH, app, save
from .test_exports import export, itinerary, saved, file_state
from .test_pdf_exports import read_pdf, plain_text, compact


def baidu_path(price=29):
    return {"duration": 2496, "distance": 12248, "price": price, "steps": [
        [{"type": 5}],
        [{"type": 3, "vehicle": {"name": "地铁1号线", "type": 1, "start_name": "火车东站(B口)", "end_name": "龙翔桥站", "total_price": 0}}],
        [{"type": 5}],
        [{"type": 3, "vehicle": {"name": "观光巴士", "type": 10, "start_name": "胜利剧院站", "end_name": "净寺站", "total_price": 0}},
         {"type": 3, "vehicle": {"name": "同段备选不应累加", "type": 0}}],
        [{"type": 5}]]}


def amap_path(cost="3.0"):
    return {"duration": "7633", "distance": "15836", "cost": cost, "segments": [
        {"walking": {"distance": "1448"}, "bus": {"buslines": [
            {"name": "8212路", "type": "普通公交线路", "departure_stop": {"name": "前新片"}, "arrival_stop": {"name": "吴山广场北2"}},
            {"name": "同段备选不应累加", "type": "普通公交线路"}]}, "railway": {"via_stops": [], "alters": [], "spaces": []}},
        {"walking": {"distance": "3046"}, "bus": {"buslines": []}}]}


def wrapped(path, provider="baidu"):
    return {"structuredContent": {"status": 0, "result": {"routes": [path, baidu_path(999)]}}} if provider == "baidu" else {
        "structuredContent": {"status": "1", "route": {"transits": [path, amap_path("999")], "taxi_cost": "0"}}}


@pytest.mark.parametrize("provider,expected,transfers,minutes", [("baidu", 2900, 1, 42), ("amap", 300, 0, 128)])
def test_first_plan_and_first_line_share_metrics_and_fare(provider, expected, transfers, minutes):
    class Tools:
        async def list_tools(self):
            return [{"name": "baidu_map_directions" if provider == "baidu" else "amap_maps_direction_transit_integrated", "inputSchema": {"type": "object"}}]
        async def call_tool(self, name, arguments):
            return wrapped(baidu_path() if provider == "baidu" else amap_path(), provider)
    origin = {"id": provider + ":start", "provider": provider, "sourceLocation": {"latitude": 30.2, "longitude": 120.1, "coordinateSystem": "bd09ll" if provider == "baidu" else "gcj02"}}
    target = {**deepcopy(origin), "id": provider + ":end"}
    route = asyncio.run(query_route(Tools(), origin, target, "transit", "杭州"))
    assert route["durationMinutes"] == minutes
    assert route["transit"]["fare"] == {"amountFen": expected, "currency": "CNY"}
    assert route["transit"]["transferCount"] == transfers
    assert "同段备选" not in str(route) and "taxi_cost" not in str(route)
    IncomingRoute.model_validate(route)


@pytest.mark.parametrize("value", [None, "", [], {}, True, False, -1, "-0.01", "NaN", "Infinity", float("inf"), "1.001", 1000000.01, "1e999999999"])
def test_bad_total_price_is_unknown_without_losing_valid_route(value):
    async def run():
        tools, research, places, _ = await research_pair(RouteMaps(result=wrapped(baidu_path(value))))
        result = await research.route(places[0]["id"], places[1]["id"], "transit")
        assert result["status"] == "verified" and result["durationMinutes"] == 42
        assert "fare" not in result["transit"]
        assert result["transit"]["transferCount"] == 1
        IncomingRoute.model_validate(result)
    asyncio.run(run())


@pytest.mark.parametrize("value,expected", [(0, 0), ("0.00", 0), ("0.29", 29), (2.9, 290), ("3.00", 300), ("1000000", 100000000)])
def test_prices_are_decimal_integer_fen_including_zero_quotes(value, expected):
    assert _fare(value) == {"amountFen": expected, "currency": "CNY"}


@pytest.mark.parametrize("steps", [None, [], {}, [None], [[]], [{"type": 1}], [[{"type": 3, "vehicle": []}]], [[{"type": 5}]] * 33])
def test_missing_or_unsupported_steps_never_invent_transfers(steps):
    path = baidu_path()
    path["steps"] = steps
    result = _transit_details(path, "baidu")
    assert result["fare"]["amountFen"] == 2900 and "transferCount" not in result


def test_amap_railway_and_missing_station_and_no_bus():
    path = amap_path()
    path["segments"][0]["railway"]["name"] = "确有铁路段"
    assert "transferCount" not in _transit_details(path, "amap")
    path = amap_path()
    path["segments"][0]["bus"]["buslines"][0]["departure_stop"] = []
    result = _transit_details(path, "amap")
    assert result["transferCount"] == 0 and "departureStop" not in result["segments"][1]
    path = {"cost": "0", "segments": [{"walking": {"distance": "90"}, "bus": {"buslines": []}}]}
    assert _transit_details(path, "amap") == {"segments": [{"mode": "walking"}], "transferCount": 0, "fare": {"amountFen": 0, "currency": "CNY"}}


def transit_itinerary(price=29):
    result = itinerary()
    result["trip"]["transportMode"] = "transit"
    route = result["days"][0]["activities"][1]["incomingRoute"]
    route.update(mode="transit", transit=_transit_details(baidu_path(price), "baidu"))
    return result


@pytest.mark.parametrize("change", ["fare", "station", "transfers"])
def test_client_cannot_forge_any_transit_evidence(app, change):
    with TestClient(app) as client:
        doc = saved(client, transit_itinerary())
        data = deepcopy(doc["data"])
        data["draft"], data["draftTrip"] = deepcopy(data["days"]), deepcopy(data["trip"])
        transit = data["draft"][0]["activities"][1]["incomingRoute"]["transit"]
        if change == "fare": transit["fare"]["amountFen"] = 1
        elif change == "station": transit["segments"][1]["departureStop"] = "伪造上车站"
        else: transit["transferCount"] = 9
        response = save(client, doc, data)
        assert response.status_code == 422 and response.json()["detail"]["code"] == "trip_evidence"
        assert app.state.store.get(UUID(doc["id"])).data["draft"] is None


def test_apply_reload_restore_and_chat_research_preserve_details_and_cost(app, tmp_path):
    from supertravel_server.app import create_app
    with TestClient(app) as client:
        doc = saved(client, transit_itinerary())
        original = deepcopy(doc["data"])
        data = deepcopy(original)
        data["draft"], data["draftTrip"] = deepcopy(data["days"]), deepcopy(data["trip"])
        data["draft"][0]["activities"][1]["description"] = "只改备注"
        pending = save(client, doc, data).json()
        response = client.post(f"/api/conversations/{doc['id']}/trip/apply", headers=AUTH, json={"expected_revision": pending["revision"]})
        assert response.status_code == 200, response.text
        applied = response.json()
        before, after = original["days"][0]["activities"][1], applied["data"]["days"][0]["activities"][1]
        assert before["incomingRoute"] == after["incomingRoute"] and before["cost"] == after["cost"]
        assert applied["data"]["previousTrip"]["days"] == original["days"]
    with TestClient(create_app(tmp_path, "trip-api-fixture")) as client:
        loaded = client.get(f"/api/conversations/{doc['id']}", headers=AUTH).json()
        assert loaded == applied
        response = client.post(f"/api/conversations/{doc['id']}/trip/restore", headers=AUTH, json={"expected_revision": loaded["revision"]})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["days"] == original["days"]
    # The execution Agent's submit path reuses trusted route details without changing activity costs.
    research = TripResearch(RouteMaps(), "杭州", original["days"])
    submission = {"summary": "保留证据续改", "days": [{"day": day["day"], "title": day["title"], "subtitle": day["subtitle"],
        "activities": [{"activity_id": a["id"], "place_id": a["place"]["id"], "time": a["time"], "duration_minutes": 60, "description": a["description"]} for a in day["activities"]]} for day in original["days"]]}
    result = research.submit(submission, TripRequest(destination="杭州", days=2, transport_mode="transit"))
    assert result["days"][0]["activities"][1]["incomingRoute"] == before["incomingRoute"]
    assert result["days"][0]["activities"][1]["cost"] == before["cost"]


@pytest.mark.parametrize("price,phrase", [(29, "参考总票价：¥29.00"), (0, "参考总票价：¥0.00"), (None, "参考总票价未知")])
def test_markdown_pdf_share_formal_transit_details_and_keep_budget(app, monkeypatch, price, phrase):
    with TestClient(app) as client:
        real = transit_itinerary(price)
        # Long names exercise wrapping without hidden HTML or unescaped Markdown links.
        real["days"][0]["activities"][1]["incomingRoute"]["transit"]["segments"][1]["departureStop"] = "超长中文站名" * 12 + "[东出口](not-a-link)"
        doc = saved(client, real)
        before = file_state(app.state.store.root)
        monkeypatch.setattr("supertravel_server.trip_routes.query_route", lambda *a, **k: pytest.fail("export cannot query maps"))
        result = export(client, doc, format="pdf")
        assert result.status_code == 200, result.text
        body = result.json()
        assert "¥12.34" in body["markdown"] and "换乘 1 次" in body["markdown"] and phrase in body["markdown"]
        assert "同段备选" not in body["markdown"] and "供应商首方案" in body["markdown"]
        pdf_text = compact(plain_text(read_pdf(body)))
        for output in (body["markdown"], pdf_text):
            assert "是否免费待确认" not in output
            if price is not None:
                assert compact(phrase + "（仅供参考，以实际结果为准）") in compact(output)
        for expected in (phrase, "地铁1号线", "净寺站", "¥12.34", "交通参考价不计入活动费用小计"):
            assert compact(expected) in pdf_text
        assert file_state(app.state.store.root) == before


@pytest.mark.parametrize("bad", ["expired", "place", "mode"])
def test_old_or_mismatched_details_are_not_exported(app, bad):
    real = transit_itinerary()
    route = real["days"][0]["activities"][1]["incomingRoute"]
    if bad == "expired": route["queriedAt"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    elif bad == "place": route["fromPlaceId"] = "baidu:wrong"
    else: real["trip"]["transportMode"] = "walking"
    with TestClient(app) as client:
        result = export(client, saved(client, real)).json()["markdown"]
        assert "¥29.00" not in result and "地铁1号线" not in result and "待确认" in result


def test_changed_place_clears_entire_details_and_legacy_missing_details_is_valid():
    real = transit_itinerary()
    validate_result(real)
    real["days"][0]["activities"][0]["place"]["id"] = "baidu:replaced"
    clear_changed_routes(real["days"])
    assert "incomingRoute" not in real["days"][0]["activities"][1]
    real = transit_itinerary()
    route = real["days"][0]["activities"][1]["incomingRoute"]
    route.pop("transit")
    validate_result(real)
    route["transit"] = None
    validate_result(real)
    route["transit"] = _transit_details(baidu_path(), "baidu")
    route["mode"] = "walking"
    with pytest.raises(ValueError): validate_result(real)


def test_candidate_insert_keeps_new_transit_evidence_in_both_adjacent_legs(app, monkeypatch):
    from supertravel_server import candidates
    from .test_candidates import park, slots, insert
    from .test_trip_mutations import document, post
    async def query(tools, origin, target, mode, city):
        return {"fromPlaceId": origin["id"], "toPlaceId": target["id"], "mode": mode, "status": "verified", "durationMinutes": 13,
                "distanceMeters": 500, "provider": "baidu", "queriedAt": datetime.now(timezone.utc).isoformat(), "transit": _transit_details(baidu_path(), "baidu")}
    monkeypatch.setattr(candidates, "query_route", query)
    def use_transit(data):
        data["trip"]["transportMode"] = "transit"
        for day in data["days"]:
            for item in day["activities"]:
                if item.get("incomingRoute"): item["incomingRoute"]["mode"] = "transit"
    with TestClient(app) as client:
        pending = park(client, document(client, change=use_transit))
        formal = post(client, pending, "apply").json()
        option = next(item for item in slots(client, formal).json()["options"] if item["position"] == 1)
        response = insert(client, formal, option, "10:30")
        assert response.status_code == 200, response.text
        items = response.json()["data"]["draft"][0]["activities"]
        assert items[1]["incomingRoute"]["transit"] == items[2]["incomingRoute"]["transit"] == _transit_details(baidu_path(), "baidu")
        assert items[1]["cost"]["amountFen"] == 2500
        applied = post(client, response.json(), "apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["data"]["days"][0]["activities"][1]["incomingRoute"]["transit"] == items[1]["incomingRoute"]["transit"]
