"""Taxi fixtures are offline; live evidence remains local and is not a billing claim."""
import asyncio
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest

from supertravel_server.trip_routes import _taxi_estimates, query_route
from supertravel_server.trips import IncomingRoute, validate_result
from .test_trip_routes import RouteMaps, research_pair
from .test_trip_api import app, save
from .test_exports import saved
from .test_transit_details import baidu_path, transit_itinerary


def taxi_root():
    return {"routes": [baidu_path(6), baidu_path(999)], "taxi": {
        "detail": [{"desc": "白天", "total_price": 39}, {"desc": "夜间", "total_price": "46.29"}], "remark": "仅供参考，费用以实际行驶为准。"}}


@pytest.mark.parametrize("value", [None, "", [], {}, True, False, -1, "NaN", "Infinity", float("nan"), "1.001", 1000000.01])
def test_unknown_or_invalid_amount_is_never_free(value):
    root = taxi_root()
    root["taxi"]["detail"] = [{"total_price": value}]
    assert _taxi_estimates(root, "baidu") == []
    assert _taxi_estimates({"taxi_cost": value, "paths": [{"tolls": "30"}]}, "amap") == []


def test_times_and_remarks_are_preserved_without_summing_or_fabricating_conditions():
    estimates = _taxi_estimates(taxi_root(), "baidu")
    assert [item["amountFen"] for item in estimates] == [3900, 4629]
    assert [item["label"] for item in estimates] == ["白天", "夜间"]
    assert all(item["kind"] == "estimate" and item["note"] == "仅供参考，费用以实际行驶为准。" for item in estimates)
    assert _taxi_estimates({"taxi_cost": "38", "paths": [{"tolls": "100"}]}, "amap") == [{"amountFen": 3800, "currency": "CNY", "kind": "estimate"}]
    root = taxi_root()
    root["taxi"]["detail"][0]["desc"] = "长" * 101
    root["taxi"]["remark"] = "长" * 501
    assert "label" not in _taxi_estimates(root, "baidu")[0] and "note" not in _taxi_estimates(root, "baidu")[0]
    for bad in (None, {}, [None], [{}], [{"total_price": 1}] * 9):
        root["taxi"]["detail"] = bad
        assert _taxi_estimates(root, "baidu") == []


def test_one_call_and_one_selected_payload_supply_metrics_fare_and_taxi_cache():
    async def run():
        wrong = taxi_root()
        wrong["routes"][0]["duration"] = None
        wrong["taxi"]["detail"][0]["total_price"] = 999
        result = {"structuredContent": {"status": 0, "result": wrong},
                  "content": [{"type": "text", "text": json.dumps({"status": 0, "result": taxi_root()})}]}
        tools, research, places, _ = await research_pair(RouteMaps(result=result))
        route = await research.route(places[0]["id"], places[1]["id"], "transit")
        assert route["durationMinutes"] == 42 and route["transit"]["fare"]["amountFen"] == 600
        assert route["taxiEstimates"] == _taxi_estimates(taxi_root(), "baidu")
        IncomingRoute.model_validate(route)
        route["taxiEstimates"][0]["amountFen"] = 1
        cached = await research.route(places[0]["id"], places[1]["id"], "transit")
        assert cached["taxiEstimates"][0]["amountFen"] == 3900 and len(tools.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["driving", "transit", "walking"])
def test_amap_route_root_amount_is_separate_from_tolls_and_transit_cost(mode):
    class Tools:
        async def list_tools(self):
            return [{"name": "amap_maps_direction_" + ("transit_integrated" if mode == "transit" else mode), "inputSchema": {"type": "object"}}]
        async def call_tool(self, name, arguments):
            return {"structuredContent": {"status": "1", "route": {"taxi_cost": "38", "transits" if mode == "transit" else "paths": [
                {"distance": "12000", "duration": "1800", "cost": "6", "tolls": "0"}]}}}
    origin = {"id": "amap:start", "provider": "amap", "sourceLocation": {"latitude": 30.2, "longitude": 120.1, "coordinateSystem": "gcj02"}}
    route = asyncio.run(query_route(Tools(), origin, {**origin, "id": "amap:end"}, mode, "杭州"))
    IncomingRoute.model_validate(route)
    if mode == "walking":
        assert "taxiEstimates" not in route
    else:
        assert route["taxiEstimates"] == [{"amountFen": 3800, "currency": "CNY", "kind": "estimate"}]
        if mode == "transit": assert route["transit"]["fare"]["amountFen"] == 600


@pytest.mark.parametrize("field,value", [("amountFen", 1), ("label", "伪造时段"), ("note", "伪造计价条件")])
def test_client_cannot_forge_any_taxi_evidence(app, field, value):
    with TestClient(app) as client:
        doc = saved(client, transit_itinerary())
        data = deepcopy(doc["data"])
        data["draft"], data["draftTrip"] = deepcopy(data["days"]), deepcopy(data["trip"])
        data["draft"][0]["activities"][1]["incomingRoute"]["taxiEstimates"][0][field] = value
        response = save(client, doc, data)
        assert response.status_code == 422 and response.json()["detail"]["code"] == "trip_evidence"


def test_old_null_and_missing_estimates_remain_valid_and_bad_evidence_is_rejected():
    real = transit_itinerary()
    route = real["days"][0]["activities"][1]["incomingRoute"]
    for value in (None, []):
        route["taxiEstimates"] = value
        validate_result(real)
    route.pop("taxiEstimates")
    validate_result(real)
    for amount in (-1, True, 1.1, 100000001):
        route["taxiEstimates"] = [{"amountFen": amount, "currency": "CNY", "kind": "estimate"}]
        with pytest.raises(ValueError): validate_result(real)
    route["taxiEstimates"] = []
    route.pop("transit")
    for patch in ({"mode": "walking"}, {"status": "unknown", "durationMinutes": None, "distanceMeters": None}):
        with pytest.raises(ValueError): IncomingRoute.model_validate({**route, **patch})


@pytest.mark.parametrize("failure", [False, True])
def test_refresh_replaces_stale_taxi_including_failure_without_changing_user_costs(failure):
    from datetime import datetime, timedelta, timezone
    from supertravel_server.mcp_client import ToolError
    from supertravel_server.trip_api import refresh_routes
    async def run():
        real = transit_itinerary()
        real["days"][0]["activities"] = real["days"][0]["activities"][:2]
        _, _, places, _ = await research_pair()
        for activity, place in zip(real["days"][0]["activities"], places):
            activity["place"] = place
        route = real["days"][0]["activities"][1]["incomingRoute"]
        route.update(fromPlaceId=places[0]["id"], toPlaceId=places[1]["id"])
        route["queriedAt"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        costs = [deepcopy(a.get("cost")) for a in real["days"][0]["activities"]]
        root = taxi_root()
        root["taxi"]["detail"] = [{"total_price": 40, "desc": "全天"}]
        tools = RouteMaps(result={"structuredContent": {"status": 0, "result": root}},
                          error=ToolError("tool_connection", True, "offline fixture") if failure else None)
        await refresh_routes(tools, real["days"], "杭州", "transit")
        refreshed = real["days"][0]["activities"][1]["incomingRoute"]
        assert len(tools.calls) == 1
        if failure:
            assert refreshed["status"] == "unknown" and "taxiEstimates" not in refreshed
        else:
            assert refreshed["taxiEstimates"][0]["amountFen"] == 4000 and len(refreshed["taxiEstimates"]) == 1
        assert [a.get("cost") for a in real["days"][0]["activities"]] == costs
    asyncio.run(run())


@pytest.mark.parametrize("value,phrase", [(None, "出租车参考价未查询"), ([], "暂无可用出租车估算"),
    ([{"amountFen": 0, "currency": "CNY", "kind": "estimate"}], "约 ¥0.00（仅供参考，以实际结果为准）")])
def test_taxi_export_preserves_zero_and_distinguishes_missing_estimates(app, value, phrase):
    from .test_exports import export
    real = transit_itinerary()
    real["days"][0]["activities"][1]["incomingRoute"]["taxiEstimates"] = value
    with TestClient(app) as client:
        body = export(client, saved(client, real)).json()["markdown"]
        assert phrase in body and "¥39.00" not in body and "¥12.34" in body


@pytest.mark.parametrize("value", [0, "0.00"])
def test_valid_zero_estimate_is_preserved_for_both_providers(value):
    root = taxi_root()
    root["taxi"]["detail"] = [{"total_price": value}]
    for estimates in (_taxi_estimates(root, "baidu"), _taxi_estimates({"taxi_cost": value}, "amap")):
        assert len(estimates) == 1 and estimates[0]["amountFen"] == 0
        real = transit_itinerary()
        real["days"][0]["activities"][1]["incomingRoute"]["taxiEstimates"] = estimates
        validate_result(real)
