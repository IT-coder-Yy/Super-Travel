import asyncio
import json

import httpx
import pytest

from backend import agent_service
from backend.external_tools import ExternalTools
from backend.mcp_client import McpClient, ToolError
from backend.shared_tools import SharedTools


PROVIDERS = [{"id": "baidu", "label": "百度地图", "kind": "mcp", "configured": True,
              "connection": "connected", "toolCount": 1, "message": "已连接", "lastCall": {"status": "ok", "message": "其他用户查询"}}]
TOOL = {"name": "baidu_map_search_places", "description": "地点查询", "inputSchema": {"type": "object"}}


@pytest.mark.parametrize("url", ["http://example.test", "https://secret@example.test", "https://example.test/?key=secret", "https://example.test/#secret", "not a url"])
def test_relay_requires_clean_https_address(url):
    with pytest.raises(ToolError):
        SharedTools(url)


def test_relay_sends_only_tool_arguments_and_keeps_call_status_local(monkeypatch):
    monkeypatch.setenv("STA_MODEL_API_KEY", "model-secret-never-forward")
    monkeypatch.setenv("STA_BACKEND_TOKEN", "local-token-never-forward")
    seen = []
    async def handler(request):
        assert not request.headers.get("Authorization")
        assert "secret-never-forward" not in request.content.decode()
        assert "local-token-never-forward" not in str(request.headers)
        seen.append((request.method, request.url.path))
        if request.url.path == "/v1/call":
            assert json.loads(request.content) == {"name": TOOL["name"], "arguments": {"query": "西湖", "region": "杭州"}}
            return httpx.Response(200, json={"result": {"content": [{"type": "text", "text": "固定测试地点"}]}, "providers": PROVIDERS})
        return httpx.Response(200, json={"tools": [TOOL], "providers": PROVIDERS})
    async def run():
        client = SharedTools("http://127.0.0.1:18999", transport=httpx.MockTransport(handler))
        assert client.configured
        assert (await client.read_status())[0]["lastCall"] is None
        assert await client.list_tools() == [TOOL]
        assert await client.list_tools() == [TOOL]
        assert "固定测试地点" in str(await client.call_tool(TOOL["name"], {"query": "西湖", "region": "杭州"}))
        assert client.status()[0]["lastCall"]["status"] == "ok"
        assert "其他用户" not in str(client.status())
        assert (await client.read_status())[0]["lastCall"]["status"] == "ok"
    asyncio.run(run())
    assert seen == [("GET", "/v1/status"), ("GET", "/v1/tools"), ("POST", "/v1/call"), ("GET", "/v1/status")]


@pytest.mark.parametrize("status,body,code,retryable", [
    (429, {"error": {"code": "tool_rate_limit", "message": "secret-response", "retryable": True}}, "tool_rate_limit", False),
    (503, {"error": {"code": "unexpected", "message": "secret-response"}}, "tool_service", True),
    (502, {"error": {"code": "tool_quota", "message": "secret-response"}}, "tool_quota", False),
    (429, {"error": {"code": "tool_quota", "message": "secret-response"}}, "tool_quota", False),
    (200, [], "tool_response", False),
])
def test_errors_are_safe_and_rate_limits_not_retried(status, body, code, retryable):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)
    async def run():
        client = SharedTools("https://relay.example.test", transport=httpx.MockTransport(handler))
        with pytest.raises(ToolError) as caught:
            await client.list_tools()
        assert (caught.value.code, caught.value.retryable) == (code, retryable)
        assert "secret-response" not in str(caught.value)
    asyncio.run(run())
    assert len(calls) == 1


def test_redirects_unknown_tools_and_oversized_responses_are_rejected():
    async def run():
        for response in [httpx.Response(302, headers={"Location": "https://other.example.test"}),
                         httpx.Response(200, json={"tools": [{**TOOL, "name": "arbitrary_fetch"}], "providers": PROVIDERS}),
                         httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))]:
            client = SharedTools("https://relay.example.test", transport=httpx.MockTransport(lambda _: response))
            with pytest.raises(ToolError):
                await client.list_tools()
    asyncio.run(run())


def test_configuration_selects_shared_service_without_reading_provider_credentials(monkeypatch):
    for name in ("STA_MODEL_API_KEY", "STA_MCP_URL", "STA_SHARED_TOOLS_URL"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(agent_service.AgentService(None, None).tools, ExternalTools)
    monkeypatch.setenv("STA_SHARED_TOOLS_URL", "https://relay.example.test")
    service = agent_service.AgentService(None, None)
    assert isinstance(service.tools, SharedTools) and service.public_settings()["toolsConfigured"]
    monkeypatch.setenv("STA_MCP_URL", "https://custom.example.test/mcp")
    assert isinstance(agent_service.AgentService(None, None).tools, McpClient)


def test_settings_endpoints_report_safe_shared_service_failure(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    monkeypatch.delenv("STA_SHARED_TOOLS_URL", raising=False)
    monkeypatch.delenv("STA_MCP_URL", raising=False)
    app = create_app(tmp_path, "local-test-token")
    app.state.agents.tools = SharedTools("https://relay.example.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(503, json={"error": {"code": "tool_configuration", "message": "private-server-detail"}})))
    with TestClient(app, headers={"Authorization": "Bearer local-test-token"}) as client:
        for method, path in [("GET", "/api/tools/status"), ("POST", "/api/tools/check")]:
            response = client.request(method, path)
            assert response.status_code == 502 and response.json()["detail"]["code"] == "tool_configuration"
            assert "private-server-detail" not in response.text


def test_trip_research_uses_shared_http_service_without_client_provider_keys(monkeypatch):
    from backend.external_tools import KEY_NAMES
    from backend.shared_tools_server import create_app
    from backend.trips import TripResearch, TripRequest
    for name in KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    class MapFixture:
        async def list_tools(self):
            return [{"name": "map_search_places", "description": "固定地图接口", "inputSchema": {
                "type": "object", "properties": {"query": {"type": "string"}, "region": {"type": "string"}}, "required": ["query", "region"]}}]
        async def call_tool(self, name, arguments):
            assert name == "map_search_places" and arguments == {"query": "固定公园", "region": "杭州"}
            return {"content": [{"type": "text", "text": json.dumps({"status": 0, "results": [{"uid": "fixture", "name": "固定公园", "city": "杭州市", "address": "固定测试地址", "location": {"lng": 120.15, "lat": 30.25}}]})}]}
    async def run():
        tools = ExternalTools({"BAIDU_MAP_API_KEY": "server-fixture-key"})
        tools.clients["baidu"] = MapFixture()
        client = SharedTools("http://127.0.0.1", transport=httpx.ASGITransport(app=create_app(tools)))
        research = TripResearch(client, "杭州")
        places = await research.search("固定公园")
        assert len(places) == 1 and places[0]["id"] == "baidu:fixture"
        trip = research.submit({"summary": "固定测试", "days": [{"day": 1, "title": "一天", "subtitle": "固定测试", "activities": [{"place_id": places[0]["id"], "time": "09:00", "duration_minutes": 60, "description": "固定测试"}]}]}, TripRequest(destination="杭州", days=1))
        assert trip["days"][0]["activities"][0]["place"]["coordinateSystem"] == "wgs84"
        assert "server-fixture-key" not in json.dumps(trip)
        assert client.status()[1]["lastCall"]["status"] == "ok"
    asyncio.run(run())
