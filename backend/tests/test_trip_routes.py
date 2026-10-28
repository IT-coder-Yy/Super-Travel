import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from backend.mcp_client import ToolError
from backend.trip_api import refresh_routes
from backend.trips import ActivityCost, TripRequest, TripResearch, validate_result
from backend.tests.test_trips import BAIDU_POI, Maps, submitted


class RouteMaps(Maps):
    def __init__(self, *, result=None, error=None, mode="walking"):
        super().__init__()
        self.route_result = result or {"structuredContent": {"status": 0, "result": {"routes": [{"distance": 842, "duration": 721}]}}}
        self.error, self.mode = error, mode

    async def list_tools(self):
        return [*await super().list_tools(), {"name": "baidu_map_directions", "inputSchema": {
            "type": "object", "required": ["origin", "destination"], "properties": {
                "origin": {"type": "string"}, "destination": {"type": "string"}, "model": {"type": "string"}}, "additionalProperties": False}}]

    async def call_tool(self, name, arguments):
        if name == "baidu_map_directions":
            self.calls.append((name, arguments))
            if self.error:
                raise self.error
            return deepcopy(self.route_result)
        result = deepcopy(self.result)
        import json
        data = json.loads(result["content"][0]["text"])
        data["results"].append({**BAIDU_POI, "uid": "second", "name": "白堤", "location": {"lat": 30.258174, "lng": 120.153455}})
        result["content"][0]["text"] = json.dumps(data)
        return result


async def research_pair(tools=None):
    tools = tools or RouteMaps()
    research = TripResearch(tools, "杭州")
    places = await research.search("断桥白堤")
    value = submitted(places[0]["id"])
    value["days"][0]["activities"].append({"place_id": places[1]["id"], "time": "10:30", "duration_minutes": 60, "description": "步行抵达白堤"})
    return tools, research, places, value


def test_baidu_route_uses_source_bd09_not_display_wgs84_and_rounds_seconds_up():
    async def run():
        tools, research, places, value = await research_pair()
        route = await research.route(places[0]["id"], places[1]["id"])
        assert tools.calls[-1] == ("baidu_map_directions", {"origin": "30.264614,120.158261", "destination": "30.258174,120.153455", "model": "walking"})
        assert route["status"] == "verified" and route["durationMinutes"] == 13 and route["distanceMeters"] == 842
        assert route["provider"] == "baidu"
        assert await research.route(places[0]["id"], places[1]["id"]) == route
        assert len(tools.calls) == 1  # The fixture records route calls, not POI calls.
        result = research.submit(value, TripRequest(destination="杭州", days=1))
        assert result["days"][0]["activities"][1]["incomingRoute"] == route
        value["days"][0]["activities"][1]["time"] = "10:12"
        with pytest.raises(ValueError, match="至少需要 13 分钟.*只预留 12 分钟"):
            research.submit(value, TripRequest(destination="杭州", days=1))
    asyncio.run(run())


@pytest.mark.parametrize("error", [ToolError("tool_quota", False, "must not echo credential"), ToolError("tool_connection", True, "must not echo credential"), TimeoutError()])
def test_route_failure_is_explicit_unknown_and_never_has_zero_metrics(error):
    async def run():
        tools, research, places, value = await research_pair(RouteMaps(error=error))
        route = await research.route(places[0]["id"], places[1]["id"])
        assert route["status"] == "unknown" and "durationMinutes" not in route
        assert "credential" not in str(route)
        result = research.submit(value, TripRequest(destination="杭州", days=1))
        assert result["days"][0]["activities"][1]["incomingRoute"] == route
        assert len(tools.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("duration", [True, "nan", float("inf"), -1, 0, 200000])
def test_malformed_provider_duration_stays_unknown(duration):
    async def run():
        tools = RouteMaps(result={"structuredContent": {"status": 0, "result": {"routes": [{"distance": 842, "duration": duration}]}}})
        _, research, places, _ = await research_pair(tools)
        result = await research.route(places[0]["id"], places[1]["id"])
        assert result["status"] == "unknown" and "durationMinutes" not in result
    asyncio.run(run())


def test_missing_source_or_mixed_provider_never_sends_display_coordinates():
    async def run():
        tools, research, places, value = await research_pair()
        research.places[places[0]["id"]].pop("sourceLocation")
        result = await research.route(places[0]["id"], places[1]["id"])
        assert result["status"] == "unknown" and not tools.calls
        submitted_trip = research.submit(value, TripRequest(destination="杭州", days=1, transport_mode="transit"))
        missing = submitted_trip["days"][0]["activities"][1]["incomingRoute"]
        assert missing["status"] == "unknown" and missing["mode"] == "transit" and "queriedAt" not in missing
    asyncio.run(run())


def test_cost_is_only_user_integer_fen_and_is_preserved_across_note_edit():
    async def run():
        _, research, places, value = await research_pair()
        result = research.submit(value, TripRequest(destination="杭州", days=1))
        assert "cost" not in result["days"][0]["activities"][0]
        result["days"][0]["activities"][0]["cost"] = {"amountFen": 0, "currency": "CNY", "source": "user"}
        next_research = TripResearch(Maps(), "杭州", result["days"])
        changed = next_research.submit(value, TripRequest(destination="杭州", days=1))
        assert changed["days"][0]["activities"][0]["cost"]["amountFen"] == 0
        for bad in [-1, 1.5, True, 100000001]:
            with pytest.raises(ValueError):
                ActivityCost(amountFen=bad)
        with pytest.raises(ValueError):
            ActivityCost(amountFen=1, source="provider")
        value["days"][0]["activities"][0]["cost"] = {"amountFen": 1}
        with pytest.raises(ValueError):
            research.submit(value, TripRequest(destination="杭州", days=1))
    asyncio.run(run())


def test_refresh_queries_changed_transport_and_expired_evidence_then_checks_gap():
    async def run():
        tools, research, places, value = await research_pair()
        await research.route(places[0]["id"], places[1]["id"])
        result = research.submit(value, TripRequest(destination="杭州", days=1))
        days = result["days"]
        await refresh_routes(tools, days, "杭州", "driving")
        result["trip"]["transportMode"] = "driving"
        assert tools.calls[-1][1]["model"] == "driving"
        route = days[0]["activities"][1]["incomingRoute"]
        route["queriedAt"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        count = len(tools.calls)
        await refresh_routes(tools, days, "杭州", "driving")
        assert len(tools.calls) == count + 1
        validate_result(result)
    asyncio.run(run())


def test_refreshing_source_coordinates_invalidates_same_id_route_cache():
    async def run():
        tools, research, places, _ = await research_pair()
        await research.route(places[0]["id"], places[1]["id"])
        import json
        payload = json.loads(tools.result["content"][0]["text"])
        payload["results"][0]["location"]["lat"] += .01
        tools.result["content"][0]["text"] = json.dumps(payload)
        await research.search("断桥白堤")
        assert not research.routes
        await research.route(places[0]["id"], places[1]["id"])
        assert tools.calls[-1][1]["origin"] == "30.274614,120.158261"
    asyncio.run(run())


def test_reorder_clears_previous_leg_and_queries_new_direction():
    async def run():
        tools, research, places, value = await research_pair()
        await research.route(places[0]["id"], places[1]["id"])
        result = research.submit(value, TripRequest(destination="杭州", days=1))
        activities = result["days"][0]["activities"]
        activities.reverse()
        activities[0]["time"], activities[1]["time"] = "09:00", "10:30"
        await refresh_routes(tools, result["days"], "杭州")
        assert "incomingRoute" not in activities[0]
        assert activities[1]["incomingRoute"]["fromPlaceId"] == places[1]["id"]
        assert tools.calls[-1][1]["origin"] == "30.258174,120.153455"
        validate_result(result)
    asyncio.run(run())


@pytest.mark.parametrize("count", [1, 3, 7])
def test_days_bind_each_incoming_leg_without_carrying_route_across_nights(count):
    async def run():
        _, research, places, value = await research_pair()
        await research.route(places[0]["id"], places[1]["id"])
        value["days"] = [{**deepcopy(value["days"][0]), "day": number} for number in range(1, count + 1)]
        result = research.submit(value, TripRequest(destination="杭州", days=count))
        for day in result["days"]:
            assert "incomingRoute" not in day["activities"][0]
            assert day["activities"][1]["incomingRoute"]["status"] == "verified"
        assert len({activity["id"] for day in result["days"] for activity in day["activities"]}) == count * 2
    asyncio.run(run())
