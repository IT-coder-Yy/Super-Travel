import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend.external_tools import ExternalTools, KEY_NAMES, result_error
from backend.main import create_app
from backend.mcp_client import ToolError


class FakeClient:
    def __init__(self, names, failure=None):
        self.names, self.failure, self.lists, self.calls = names, failure, 0, []
        self.result = {"content": [{"type": "text", "text": "杭州天气测试结果"}], "isError": False}

    async def list_tools(self):
        self.lists += 1
        if self.failure:
            raise self.failure
        return [{"name": name, "description": "测试", "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}}} for name in self.names]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def configured():
    return ExternalTools({name: "fixture-" + name.lower() for name in KEY_NAMES})


def test_registry_namespaces_and_filters_read_only_tools_and_routes_original_schema():
    async def scenario():
        tools = configured()
        amap = FakeClient(["maps_weather", "maps_direction_bicycling", "maps_schema_personal_map"])
        baidu = FakeClient(["map_search_places", "map_mark"])
        tools.clients = {"amap": amap, "baidu": baidu}
        rows = await tools.list_tools()
        assert {row["name"] for row in rows} == {"amap_maps_weather", "amap_maps_direction_bicycling", "baidu_map_search_places"}
        assert rows[0]["inputSchema"]["properties"]["city"] == {"type": "string"}
        assert await tools.list_tools() == rows and amap.lists == 1
        await tools.call_tool("amap_maps_weather", {"city": "杭州"})
        assert amap.calls == [("maps_weather", {"city": "杭州"})]
        with pytest.raises(ToolError):
            await tools.call_tool("amap_maps_schema_personal_map", {})
        assert len(amap.calls) == 1
        assert tools.status()[0]["lastCall"]["status"] == "ok"
    asyncio.run(scenario())


def test_one_unreachable_provider_does_not_hide_other_tools_and_refresh_is_explicit():
    async def scenario():
        tools = configured()
        amap, baidu = FakeClient(["maps_weather"], ToolError("tool_timeout", True, "连接超时")), FakeClient(["map_weather"])
        tools.clients = {"amap": amap, "baidu": baidu}
        checked = await tools.refresh()
        assert checked["connected"] and checked["toolCount"] == 1
        assert checked["providers"][0]["connection"] == "error"
        await tools.list_tools()
        assert amap.lists == 1
        amap.failure = None
        assert (await tools.refresh())["toolCount"] == 2
        assert amap.lists == 2
    asyncio.run(scenario())


def test_quota_keeps_configuration_and_catalog_and_can_recover_without_reconfiguration():
    async def scenario():
        tools = configured()
        client = FakeClient(["maps_text_search", "maps_weather"])
        tools.clients = {"amap": client}
        client.result = {"isError": False, "content": [{"type": "text", "text": json.dumps({"status": "0", "info": "USER_DAILY_QUERY_OVER_LIMIT"})}]}
        with pytest.raises(ToolError) as error:
            await tools.call_tool("amap_maps_text_search", {"city": "杭州"})
        assert error.value.code == "tool_quota" and error.value.retryable is False
        assert len(client.calls) == 1  # No automatic repeat of a known quota failure.
        checked = await tools.refresh()
        assert checked["providers"][0]["configured"] is True and checked["toolCount"] == 2
        assert checked["providers"][0]["lastCall"]["status"] == "quota"
        client.result = {"isError": False, "content": [{"type": "text", "text": "真实结果已恢复"}]}
        await tools.call_tool("amap_maps_weather", {"city": "杭州"})
        assert tools.status()[0]["lastCall"]["status"] == "ok"
    asyncio.run(scenario())


def test_empty_and_search_text_about_quotas_are_not_provider_failures():
    result = {"isError": False, "content": [{"type": "text", "text": json.dumps({"results": [{"snippet": "服务的 daily limit 与 quota 介绍"}]})}]}
    assert result_error("tavily", result) is None
    assert result_error("baidu", {"isError": False, "structuredContent": {"status": 0, "results": []}}) is None
    assert result_error("amap", {"isError": False, "structuredContent": {"status": "0", "info": "INVALID_USER_KEY"}}).code == "tool_auth"


def test_static_search_catalog_is_not_mistaken_for_a_successful_provider_connection():
    async def scenario():
        tools = ExternalTools({"SERPER_API_KEY": "fixture-key"})
        client = FakeClient(["serper_search", "xhs_search_public"])
        tools.clients = {"serper": client, "xhs": client}
        checked = await tools.refresh()
        assert checked["connected"] is False and checked["toolCount"] == 2
        assert all(state["connection"] == "unchecked" for state in checked["providers"])
        client.result = {"isError": False, "content": [], "structuredContent": {"status": "empty", "results": []}}
        await tools.call_tool("xhs_search_public", {"query": "杭州"})
        assert tools.status()[-1]["connection"] == "connected"
        assert tools.status()[-1]["lastCall"]["status"] == "empty"
        assert (await tools.refresh())["providers"][-1]["lastCall"]["status"] == "empty"
    asyncio.run(scenario())


def test_missing_and_malformed_provider_keys_do_not_break_model_only_startup():
    tools = ExternalTools({})
    assert not tools.configured and all(not state["configured"] for state in tools.status())
    assert asyncio.run(tools.list_tools()) == []
    malformed = ExternalTools({"SERPER_API_KEY": "bad\r\nvalue", "AMAP_MAPS_API_KEY": "normal-test-key"})
    states = {item["id"]: item for item in malformed.status()}
    assert states["serper"]["configured"] and states["serper"]["connection"] == "error"
    assert states["amap"]["connection"] == "unchecked"
    assert "bad" not in json.dumps(malformed.status())


def test_status_is_local_only_and_does_not_expose_keys(tmp_path, monkeypatch):
    for key in KEY_NAMES:
        monkeypatch.setenv(key, "private-fixture-key")
    monkeypatch.delenv("STA_MCP_URL", raising=False)
    app = create_app(tmp_path, "fixture-token")
    with TestClient(app, headers={"Authorization": "Bearer fixture-token"}) as client:
        response = client.get("/api/tools/status")
        assert response.status_code == 200
        assert len(response.json()["providers"]) == 5
        assert all(row["configured"] and row["connection"] == "unchecked" for row in response.json()["providers"])
        assert "private-fixture-key" not in response.text
        assert "XHS_COOKIE" not in response.text
