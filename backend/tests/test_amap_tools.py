import asyncio
import json
import logging

import httpx2
import pytest

from backend.amap_tools import AmapTools, TOOLS
from backend.external_tools import ExternalTools
from backend.mcp_client import McpClient, ToolError
from backend.trip_routes import query_route


def test_rest_catalog_survives_mcp_failure_and_deduplicates(monkeypatch):
    async def unavailable(_self):
        raise ToolError("tool_timeout", True, "连接超时")
    monkeypatch.setattr(McpClient, "list_tools", unavailable)
    tools = AmapTools("fixture-key")
    assert asyncio.run(tools.list_tools()) == TOOLS
    assert tools.mcp_connected is False
    async def available(_self):
        return [*TOOLS, {"name": "maps_weather"}]
    monkeypatch.setattr(McpClient, "list_tools", available)
    catalog = asyncio.run(tools.list_tools())
    assert len(catalog) == 5 and tools.mcp_connected is True
    assert len({row["name"] for row in catalog}) == 5


def test_rest_search_preserves_poi_coordinates_and_never_leaks_key(caplog):
    seen = []
    key = "private-fixture+key"
    payload = {"status": "1", "pois": [{"id": "abc", "name": "断桥残雪", "location": "120.148427,30.258031"}], "echo": key}
    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json=payload)
    tools = AmapTools(key, transport=httpx2.MockTransport(handler))
    with caplog.at_level(logging.INFO, logger="httpx2"):
        result = asyncio.run(tools.call_tool("maps_text_search", {"keywords": "断桥残雪", "city": "杭州", "citylimit": True}))
    assert len(seen) == 1
    assert seen[0].url.host == "restapi.amap.com" and seen[0].url.path == "/v3/place/text"
    assert seen[0].url.params["citylimit"] == "true" and seen[0].url.params["key"] == key
    assert result["structuredContent"]["pois"][0]["location"] == "120.148427,30.258031"
    assert key not in json.dumps(result) and key not in caplog.text and "private-fixture%2Bkey" not in caplog.text


@pytest.mark.parametrize("mode,path", [("walking", "/v3/direction/walking"), ("driving", "/v3/direction/driving"), ("transit", "/v3/direction/transit/integrated")])
def test_trip_routes_use_original_gcj02_and_parse_rest_metrics(monkeypatch, mode, path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json={"status": "1", "route": {
            "transits" if mode == "transit" else "paths": [{"duration": "167", "distance": "209"}]}})
    async def no_mcp(_self):
        raise ToolError("tool_quota", False, "MCP quota exhausted")
    monkeypatch.setattr(McpClient, "list_tools", no_mcp)
    tools = ExternalTools({"AMAP_MAPS_API_KEY": "fixture-key"})
    tools.clients["amap"] = AmapTools("fixture-key", transport=httpx2.MockTransport(handler))
    source = {"id": "amap:a", "provider": "amap", "latitude": 0, "longitude": 0,
              "sourceLocation": {"coordinateSystem": "gcj02", "longitude": 116.434307, "latitude": 39.90909}}
    target = {**source, "id": "amap:b", "sourceLocation": {"coordinateSystem": "gcj02", "longitude": 116.434446, "latitude": 39.90816}}
    route = asyncio.run(query_route(tools, source, target, mode, "北京"))
    assert route["status"] == "verified" and route["durationMinutes"] == 3 and route["distanceMeters"] == 209
    assert len(calls) == 1 and calls[0].url.path == path
    assert calls[0].url.params["origin"] == "116.434307,39.909090"
    assert calls[0].url.params["destination"] == "116.434446,39.908160"
    if mode == "transit":
        assert calls[0].url.params["city"] == calls[0].url.params["cityd"] == "北京"


def test_business_quota_is_reported_without_retry_or_fallback(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json={"status": "0", "info": "USER_DAILY_QUERY_OVER_LIMIT", "infocode": "10044"})
    async def no_mcp(_self):
        raise ToolError("tool_timeout", True, "连接超时")
    monkeypatch.setattr(McpClient, "list_tools", no_mcp)
    tools = ExternalTools({"AMAP_MAPS_API_KEY": "fixture-key"})
    tools.clients["amap"] = AmapTools("fixture-key", transport=httpx2.MockTransport(handler))
    with pytest.raises(ToolError) as error:
        asyncio.run(tools.call_tool("amap_maps_text_search", {"keywords": "断桥残雪"}))
    assert error.value.code == "tool_quota" and len(calls) == 1
    assert tools.status()[0]["lastCall"]["status"] == "quota"


@pytest.mark.parametrize("status,data,code", [(302, {}, "tool_redirect"), (401, {}, "tool_auth"), (429, {}, "tool_rate_limit"), (500, {}, "tool_service"), (200, [], "tool_response")])
def test_http_failures_are_sanitized_and_redirects_not_followed(status, data, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx2.Response(status, json=data, headers={"Location": "https://untrusted.invalid/leak"})
    tools = AmapTools("private-key", transport=httpx2.MockTransport(handler))
    with pytest.raises(ToolError) as error:
        asyncio.run(tools.call_tool("maps_text_search", {"keywords": "杭州"}))
    assert error.value.code == code and len(calls) == 1
    assert "private-key" not in str(error.value)


def test_credentials_and_destination_cannot_be_overridden_by_arguments():
    def unexpected(_request):
        pytest.fail("Invalid arguments must not reach HTTP")
    tools = AmapTools("private-key", transport=httpx2.MockTransport(unexpected))
    with pytest.raises(ToolError) as error:
        asyncio.run(tools.call_tool("maps_text_search", {"keywords": "杭州", "key": "injected"}))
    assert error.value.code == "tool_arguments"


def test_other_amap_tools_still_use_mcp(monkeypatch):
    seen = []
    async def call(_self, name, arguments):
        seen.append((name, arguments))
        return {"isError": False, "content": []}
    monkeypatch.setattr(McpClient, "call_tool", call)
    asyncio.run(AmapTools("fixture-key").call_tool("maps_weather", {"city": "杭州"}))
    assert seen == [("maps_weather", {"city": "杭州"})]
