import asyncio
from copy import deepcopy
import json

import pytest

from backend.trips import Place, TripRequest, TripResearch, to_wgs84, validate_result
from backend.mcp_client import ToolError
from backend.tests.test_agents import FakeModel, finish, reply, route, session_for


# A public POI field subset from the successful Baidu MCP shape probe; no raw log or credential.
BAIDU_POI = {"uid": "69750ad0eca032122fb96d67", "name": "断桥残雪", "address": "杭州市西湖区北山街", "city": "杭州市",
             "location": {"lat": 30.264614117876146, "lng": 120.15826108679033}}
BAIDU_RESULT = {"isError": False, "content": [{"type": "text", "text": json.dumps({"status": 0, "results": [BAIDU_POI]})}]}
PLACE_ID = "baidu:" + BAIDU_POI["uid"]


class Maps:
    def __init__(self, result=None, names=None):
        self.result = result or BAIDU_RESULT
        self.names = names if names is not None else ["baidu_map_search_places", "amap_maps_text_search"]
        self.calls = []

    async def list_tools(self):
        return [{"name": name, "inputSchema": {"type": "object"}} for name in self.names]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return deepcopy(self.result)


def submitted(place_id=PLACE_ID):
    return {"summary": "固定测试行程；时间为建议，营业和预约待确认。", "days": [{"day": 1, "title": "西湖漫步", "subtitle": "湖畔的一天",
        "activities": [{"place_id": place_id, "time": "09:00", "duration_minutes": 60, "description": "沿湖慢行；开放情况出发前确认。"}]}]}


def trip_route(steps=None, **constraints):
    return reply(name="route_request", arguments={"kind": "plan", "content": "先查真实地点再生成。", "steps": steps or ["查询并提交一天行程"],
                                                  "trip": {"destination": "杭州", "days": 1, **constraints}})


def execute_trip(messages):
    if messages[-1]["role"] == "tool":
        return reply(name="submit_trip", arguments=submitted())
    return reply(name="search_trip_places", arguments={"query": "断桥残雪"})


def known_trip():
    async def build():
        research = TripResearch(Maps(), "杭州")
        await research.search("断桥残雪")
        return research.submit(submitted(), TripRequest(destination="杭州", days=1))
    return asyncio.run(build())


def test_baidu_search_pins_city_normalizes_observed_result_and_ignores_unusable_rows():
    async def run():
        wrong_city = {**BAIDU_POI, "uid": "other", "city": "北京市"}
        malformed = {**BAIDU_POI, "uid": "bad", "location": {"lat": True, "lng": 120}}
        result = {"content": [{"type": "text", "text": json.dumps({"status": 0, "results": [wrong_city, malformed, BAIDU_POI, BAIDU_POI]})}]}
        tools = Maps(result)
        research = TripResearch(tools, "杭州")
        places = await research.search("断桥残雪")
        assert len(places) == 1 and places[0]["id"] == PLACE_ID
        assert places[0]["longitude"] != BAIDU_POI["location"]["lng"]
        assert places[0]["coordinateSystem"] == "wgs84"
        assert set(places[0]) == {"id", "name", "address", "latitude", "longitude", "coordinateSystem", "provider"}
        assert tools.calls == [("baidu_map_search_places", {"query": "断桥残雪", "region": "杭州"})]
    asyncio.run(run())


def test_amap_search_uses_official_fields_and_falls_back_only_when_baidu_is_absent():
    async def run():
        tools = Maps({"structuredContent": {"pois": [{"id": "B00001", "name": "天安门", "cityname": "北京市", "location": "116.404,39.915", "address": []}]}}, ["amap_maps_text_search"])
        research = TripResearch(tools, "北京")
        places = await research.search("天安门")
        assert places[0]["id"] == "amap:B00001" and places[0]["address"] == ""
        assert tools.calls == [("amap_maps_text_search", {"keywords": "天安门", "city": "北京"})]
        assert places[0]["longitude"] == pytest.approx(116.3977555, abs=0.0000002)
        assert places[0]["latitude"] == pytest.approx(39.9135957, abs=0.0000002)
    asyncio.run(run())


def test_coordinate_conversion_matches_original_implementation_reference_points():
    assert to_wgs84(116.41036949371029, 39.92133699351021, "baidu") == pytest.approx((116.3977555, 39.9135961), abs=.000001)
    assert to_wgs84(-74.006, 40.7128, "amap") == (-74.006, 40.7128)


def test_exhausted_provider_is_not_called_again_in_same_trip_but_new_trip_can_retry():
    async def run():
        class QuotaMaps(Maps):
            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                if name.startswith("baidu"):
                    raise ToolError("tool_quota", False, "固定测试额度不足")
                return {"structuredContent": {"pois": [{"id": "B00001", "name": "断桥残雪", "cityname": "杭州市", "location": "120.15,30.26", "address": "北山街"}]}}
        tools = QuotaMaps()
        research = TripResearch(tools, "杭州")
        with pytest.raises(ToolError):
            await research.search("断桥")
        with pytest.raises(ToolError):
            await research.search("断桥", "baidu")
        assert len(tools.calls) == 1
        assert (await research.search("断桥"))[0]["provider"] == "amap"
        assert len(tools.calls) == 2
        with pytest.raises(ToolError):
            await TripResearch(tools, "杭州").search("断桥")
        assert len(tools.calls) == 3
    asyncio.run(run())


@pytest.mark.parametrize("count", [2, 4])
def test_requested_three_stops_rejects_fewer_or_more_even_when_places_are_verified(count):
    existing = known_trip()
    research = TripResearch(Maps(), "杭州", existing["days"])
    value = submitted()
    value["days"][0]["activities"] = []
    for index in range(4):
        place = deepcopy(existing["days"][0]["activities"][0]["place"])
        place.update(id=f"baidu:fixture-{index}", name=f"固定测试地点{index}")
        research.places[place["id"]] = place
        value["days"][0]["activities"].append({"place_id": place["id"], "time": f"{9 + index:02d}:00", "duration_minutes": 30, "description": "固定测试活动"})
    invalid = deepcopy(value)
    invalid["days"][0]["activities"] = invalid["days"][0]["activities"][:count]
    request = TripRequest(destination="杭州", days=1, stops_per_day=3)
    with pytest.raises(ValueError, match="恰好安排 3"):
        research.submit(invalid, request)
    value["days"][0]["activities"] = value["days"][0]["activities"][:3]
    assert len(research.submit(value, request)["days"][0]["activities"]) == 3


def test_locked_map_provider_rejects_other_service_and_never_falls_back_after_quota():
    async def run():
        class QuotaMaps(Maps):
            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                raise ToolError("tool_quota", False, "固定测试额度不足")
        tools = QuotaMaps()
        research = TripResearch(tools, "杭州", map_provider="baidu")
        with pytest.raises(ToolError, match="已限定"):
            await research.search("断桥", "amap")
        assert not tools.calls
        with pytest.raises(ToolError):
            await research.search("断桥")
        with pytest.raises(ToolError):
            await research.search("断桥")
        assert len(tools.calls) == 1 and tools.calls[0][0] == "baidu_map_search_places"
    asyncio.run(run())


@pytest.mark.parametrize("provider", [None, "baidu"])
def test_trip_catalog_never_offers_raw_coordinate_tools_and_honors_only_map(provider):
    async def run():
        captured = []
        class CapturingModel(FakeModel):
            async def chat(self, messages, tools=None, **kwargs):
                if tools and tools[0]["function"]["name"] != "route_request":
                    captured.append({tool["function"]["name"] for tool in tools})
                return await super().chat(messages, tools, **kwargs)
        names = ["baidu_map_search_places", "amap_maps_text_search", "amap_maps_direction_walking", "baidu_map_directions", "baidu_map_weather", "amap_maps_weather", "serper_search"]
        tools = Maps(names=names)
        session, _ = session_for(CapturingModel([trip_route(map_provider=provider, stops_per_day=1)], execute_trip), tools)
        await session.chat("杭州一天一个景点，按指定供应商查询")
        assert (await finish(session))["status"] == "completed"
        expected = {"search_trip_places", "submit_trip"} | ({"amap_maps_weather", "serper_search"} if provider is None else set())
        assert captured and all(names == expected for names in captured)
        assert tools.calls == [("baidu_map_search_places", {"query": "断桥残雪", "region": "杭州"})]
    asyncio.run(run())


@pytest.mark.parametrize("mutation", [
    lambda value: value["days"][0]["activities"][0].update(place_id="baidu:invented"),
    lambda value: value["days"][0]["activities"][0].update(latitude=30),
    lambda value: value["days"][0].update(day=2),
    lambda value: value["days"][0]["activities"][0].update(time="25:00"),
    lambda value: value["days"][0]["activities"].append(deepcopy(value["days"][0]["activities"][0])),
    lambda value: value["days"][0]["activities"][0].update(time="23:30", duration_minutes=60),
])
def test_submission_rejects_unverified_or_invalid_itinerary(mutation):
    trip = known_trip()
    research = TripResearch(Maps(), "杭州", trip["days"])
    value = submitted()
    mutation(value)
    with pytest.raises(ValueError):
        research.submit(value, TripRequest(destination="杭州", days=1))


@pytest.mark.parametrize("mutation", [
    lambda value: value["days"][0]["activities"][0].update(title="另一个未经定位的地点"),
    lambda value: value["days"][0]["activities"][0]["place"].update(latitude=float("nan")),
    lambda value: value["days"][0]["activities"][0]["place"].update(provider="amap"),
    lambda value: value["days"][0]["activities"][0].pop("place"),
    lambda value: value["days"][0].update(activities=[]),
])
def test_saved_trip_validator_rejects_false_place_bindings_and_corrupt_data(mutation):
    value = known_trip()
    mutation(value)
    with pytest.raises(ValueError):
        validate_result(value)


def test_trip_execution_publishes_only_verified_structured_result():
    async def run():
        tools = Maps()
        session, events = session_for(FakeModel([trip_route()], execute_trip), tools)
        await session.chat("杭州一天，安排断桥残雪")
        state = await finish(session)
        assert state["status"] == "completed" and state["tool_calls"] == 1
        result = validate_result(state["trip_result"])
        assert result["trip"]["destination"] == "杭州"
        assert result["days"][0]["activities"][0]["title"] == BAIDU_POI["name"]
        assert all("trip_result" not in event for event in events[:-1])
        assert len(tools.calls) == 1
    asyncio.run(run())


def test_existing_trip_can_change_time_without_new_queries_and_preserves_original():
    original = known_trip()
    async def run():
        modified = submitted()
        modified["days"][0]["activities"][0]["time"] = "10:00"
        model = FakeModel([trip_route()], lambda messages: reply(name="submit_trip", arguments=modified))
        tools = Maps(names=[])
        session, _ = session_for(model, tools)
        await session.chat("把出发时间改到十点", trip_context=original)
        state = await finish(session)
        assert state["status"] == "completed" and state["tool_calls"] == 0 and not tools.calls
        assert state["trip_result"]["days"][0]["activities"][0]["time"] == "10:00"
        assert original["days"][0]["activities"][0]["time"] == "09:00"
        assert json.loads(model.planner_calls[0][-1]["content"])["current_trip"] == original
    asyncio.run(run())


def test_empty_map_result_cannot_become_successful_trip():
    async def run():
        tools = Maps({"content": [{"type": "text", "text": '{"status":0,"results":[]}'}]})
        session, events = session_for(FakeModel([trip_route(), route("clarification", "没有找到地点，请提供更明确的名称。")], execute_trip), tools)
        await session.chat("生成行程")
        state = await finish(session)
        assert state["status"] == "needs_input" and "trip_result" not in state
        assert all(event["status"] != "completed" for event in events)
        assert len(tools.calls) == 1
    asyncio.run(run())


def test_cancelled_search_cannot_publish_a_late_trip():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class SlowMaps(Maps):
            async def call_tool(self, name, arguments):
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                return BAIDU_RESULT
        session, events = session_for(FakeModel([trip_route()], execute_trip), SlowMaps())
        await session.chat("杭州一天")
        await started.wait()
        await session.cancel()
        count = len(events)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await session._task
        assert session.snapshot()["status"] == "cancelled" and "trip_result" not in session.snapshot()
        assert len(events) == count
    asyncio.run(run())
