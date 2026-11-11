import asyncio
import json
import logging

import httpx2
import pytest

from supertravel_server.amap_tools import AmapTools, TOOLS
from supertravel_server.external_tools import ExternalTools
from supertravel_server.mcp_client import McpClient, ToolError
from supertravel_server.trip_routes import query_route


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


@pytest.mark.parametrize("code", ["tool_connection", "tool_timeout"])
def test_mcp_discovery_retries_transient_failure_once_and_recovers(monkeypatch, code):
    calls = []
    async def intermittent(_self):
        calls.append(1)
        if len(calls) == 1:
            raise ToolError(code, True, "临时连接失败")
        return [{"name": "maps_weather"}]
    monkeypatch.setattr(McpClient, "list_tools", intermittent)
    tools = AmapTools("fixture-key")
    catalog = asyncio.run(tools.list_tools())
    assert len(calls) == 2 and len(catalog) == 5 and tools.mcp_connected and tools.mcp_error is None


@pytest.mark.parametrize("code", ["tool_auth", "tool_quota", "tool_protocol", "tool_service"])
def test_mcp_discovery_does_not_retry_nonconnection_errors_and_reports_degradation(monkeypatch, code):
    calls = []
    async def failed(_self):
        calls.append(1)
        raise ToolError(code, True, "已脱敏的失败原因")
    monkeypatch.setattr(McpClient, "list_tools", failed)
    service = ExternalTools({"AMAP_MAPS_API_KEY": "fixture-key"})
    catalog = asyncio.run(service.list_tools())
    assert len(calls) == 1 and len(catalog) == 4
    status = next(item for item in service.status() if item["id"] == "amap")
    assert "基础地图 API" in status["message"] and "MCP 工具清单未连接" in status["message"]
    assert status["connection"] == "unchecked"


def test_discovery_timeout_retries_but_caller_cancellation_does_not(monkeypatch):
    calls = []
    async def slow(_self):
        calls.append(1)
        if len(calls) == 1: await asyncio.Event().wait()
        return [{"name": "maps_weather"}]
    monkeypatch.setattr(McpClient, "list_tools", slow)
    tools = AmapTools("fixture-key")
    tools.timeout = 0.04
    async def run():
        assert len(await tools.list_tools()) == 5 and len(calls) == 2
        calls.clear()
        task = asyncio.create_task(tools.list_tools())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert len(calls) == 1
    asyncio.run(run())


def test_explicit_refresh_recovers_mcp_catalog_after_bounded_failure(monkeypatch):
    calls = []
    async def returns_later(_self):
        calls.append(1)
        if len(calls) <= 2: raise ToolError("tool_connection", True, "临时网络错误")
        return [{"name": "maps_weather"}]
    monkeypatch.setattr(McpClient, "list_tools", returns_later)
    service = ExternalTools({"AMAP_MAPS_API_KEY": "fixture-key"})
    async def run():
        assert len(await service.list_tools()) == 4 and len(calls) == 2
        assert len(await service.list_tools()) == 4 and len(calls) == 2
        refreshed = await service.refresh()
        assert len(calls) == 3 and refreshed["toolCount"] == 5
        status = next(item for item in refreshed["providers"] if item["id"] == "amap")
        assert status["connection"] == "connected" and "未连接" not in status["message"]
    asyncio.run(run())
