import asyncio
from copy import deepcopy
import json
from urllib.parse import quote, quote_plus

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.external_tools import KEY_NAMES
from backend.mcp_client import ToolError
from backend.shared_tools_server import create_app


NAME = "baidu_map_search_places"
QUERY = {"name": NAME, "arguments": {"query": "杭州西湖"}}
SECRET = "fixture/private+key=shared"


class FakeTools:
    def __init__(self):
        self.lists, self.calls = 0, []
        self.refreshes = 0
        self.failure = None
        self.started = self.release = None
        self.catalog = [{"name": NAME, "description": "固定地图查询测试", "inputSchema": {
            "type": "object", "properties": {"query": {"type": "string", "minLength": 1},
                "radius": {"type": "number", "minimum": 0, "maximum": 1000}},
            "required": ["query"], "additionalProperties": False,
        }}]
        self.providers = [{"id": "baidu", "label": "百度地图", "kind": "mcp", "configured": True,
            "connection": "unchecked", "toolCount": 1, "message": "固定状态", "lastCall": None}]
        self.result = {"content": [{"type": "text", "text": "固定查询结果"}], "isError": False}

    def status(self):
        return deepcopy(self.providers)

    async def list_tools(self):
        self.lists += 1
        return deepcopy(self.catalog)

    async def refresh(self):
        self.refreshes += 1

    async def call_tool(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if self.started is not None:
            self.started.set()
            await self.release.wait()
        if self.failure:
            raise self.failure
        return deepcopy(self.result)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in KEY_NAMES:
        monkeypatch.delenv(name, raising=False)


def assert_error(response):
    assert response.status_code >= 400
    error = response.json()["error"]
    assert set(error) == {"code", "retryable", "message"}
    assert isinstance(error["code"], str) and isinstance(error["message"], str)
    assert type(error["retryable"]) is bool
    return error


def test_health_and_status_are_local_then_catalog_and_call_use_existing_adapter():
    tools, now = FakeTools(), [120.0]
    with TestClient(create_app(tools, clock=lambda: now[0])) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert client.get("/v1/status").json() == {"providers": tools.providers}
        assert tools.lists == 0 and tools.calls == []
        catalog = client.get("/v1/tools")
        assert catalog.status_code == 200 and catalog.json() == {"tools": tools.catalog, "providers": tools.providers}
        assert client.get("/v1/tools").json() == catalog.json()
        response = client.post("/v1/call", json=QUERY)
        assert response.status_code == 200
        assert response.json() == {"result": tools.result, "providers": tools.providers}
        assert tools.calls == [(NAME, QUERY["arguments"])]
        assert tools.lists == 1 and tools.refreshes == 0
        # An unavailable provider catalog stays cached, then can recover after the TTL.
        available, tools.catalog = tools.catalog, []
        now[0] += 301
        assert client.get("/v1/tools").json()["tools"] == []
        assert tools.lists == 2 and tools.refreshes == 1
        tools.catalog = available
        assert client.get("/v1/tools").json()["tools"] == []
        assert tools.lists == 2
        now[0] += 301
        assert client.get("/v1/tools").json()["tools"] == available
        assert tools.lists == 3 and tools.refreshes == 2


def test_unknown_tools_and_malformed_parameters_never_reach_upstream():
    tools = FakeTools()
    tools.catalog[0]["inputSchema"].pop("additionalProperties")
    payloads = [
        {"name": "https://untrusted.invalid/proxy", "arguments": {}},
        {"name": "baidu_map_mark", "arguments": {}},
        {"name": NAME, "arguments": {"query": 123}},
        {"name": NAME, "arguments": {"query": "杭州", "headers": {"X-API-KEY": "forged"}}},
        {**QUERY, "url": "https://untrusted.invalid/proxy"},
        {"name": NAME, "arguments": []},
    ]
    with TestClient(create_app(tools)) as client:
        for payload in payloads:
            assert_error(client.post("/v1/call", json=payload))
        assert tools.calls == []


def test_oversized_and_invalid_json_are_rejected_without_echoing_request():
    tools = FakeTools()
    with TestClient(create_app(tools)) as client:
        response = client.post("/v1/call", content=b"x" * (16 * 1024 + 1), headers={"Content-Type": "application/json"})
        assert response.status_code == 413
        assert_error(response)
        assert_error(client.post("/v1/call", content=json.dumps(QUERY), headers={"Content-Type": "text/plain"}))
        for raw in [b'{"private":"request-secret",', b'{"name":"baidu_map_search_places","arguments":{"query":"x","radius":NaN}}']:
            response = client.post("/v1/call", content=raw, headers={"Content-Type": "application/json"})
            assert_error(response)
            assert "request-secret" not in response.text and "NaN" not in response.text
        assert tools.calls == []


def test_missing_provider_keys_keep_health_and_status_available_without_discovery(monkeypatch):
    async def forbidden(_self):
        pytest.fail("Unconfigured service must not discover upstream tools")
    monkeypatch.setattr("backend.external_tools.ExternalTools.list_tools", forbidden)
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        status = client.get("/v1/status")
        assert status.status_code == 200
        assert all(not provider["configured"] for provider in status.json()["providers"])
        response = client.get("/v1/tools")
        assert response.status_code == 503
        assert assert_error(response)["code"] == "tool_configuration"


def test_forged_forwarded_ip_cannot_bypass_minute_limit_and_new_window_recovers():
    tools, now = FakeTools(), [120.0]
    with TestClient(create_app(tools, per_minute=1, daily_limit=10, clock=lambda: now[0])) as client:
        assert client.post("/v1/call", json=QUERY, headers={"X-Forwarded-For": "203.0.113.10"}).status_code == 200
        response = client.post("/v1/call", json=QUERY, headers={"X-Forwarded-For": "203.0.113.20"})
        assert response.status_code == 429
        assert_error(response)
        assert len(tools.calls) == 1
        now[0] += 61
        assert client.post("/v1/call", json=QUERY).status_code == 200
        assert len(tools.calls) == 2


def test_daily_budget_is_shared_between_real_client_addresses_and_resets_at_utc_day():
    async def scenario():
        tools, now = FakeTools(), [86400.0]
        app = create_app(tools, per_minute=10, daily_limit=1, clock=lambda: now[0])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.0.2.10", 1234)), base_url="http://fixture") as first:
            assert (await first.post("/v1/call", json=QUERY)).status_code == 200
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.0.2.20", 1234)), base_url="http://fixture") as second:
            response = await second.post("/v1/call", json=QUERY)
            assert response.status_code == 429
            assert_error(response)
            assert len(tools.calls) == 1
            now[0] += 86400
            assert (await second.post("/v1/call", json=QUERY)).status_code == 200
        assert len(tools.calls) == 2
    asyncio.run(scenario())


def test_concurrent_call_is_rejected_and_cancellation_releases_capacity():
    async def scenario():
        tools = FakeTools()
        tools.started, tools.release = asyncio.Event(), asyncio.Event()
        app = create_app(tools, per_minute=10, daily_limit=10, max_concurrent=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            pending = asyncio.create_task(client.post("/v1/call", json=QUERY))
            await asyncio.wait_for(tools.started.wait(), 2)
            try:
                response = await client.post("/v1/call", json=QUERY)
                assert response.status_code == 429
                assert_error(response)
                assert len(tools.calls) == 1
            finally:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                tools.release.set()
            assert (await client.post("/v1/call", json=QUERY)).status_code == 200
            assert len(tools.calls) == 2
    asyncio.run(scenario())


def test_upstream_errors_are_fixed_and_success_payloads_redact_configured_secret(monkeypatch, caplog):
    monkeypatch.setenv("BAIDU_MAP_API_KEY", SECRET)
    tools = FakeTools()
    tools.catalog[0]["description"] = SECRET
    tools.providers[0]["message"] = quote(SECRET, safe="")
    tools.providers[0]["lastCall"] = {"status": "ok", "message": "OTHER_CLIENT_PRIVATE_QUERY"}
    tools.result["content"][0]["text"] = SECRET + " " + quote_plus(SECRET, safe="")
    with TestClient(create_app(tools), raise_server_exceptions=False) as client:
        responses = [client.get("/v1/status"), client.get("/v1/tools"), client.post("/v1/call", json=QUERY)]
        assert all(response.status_code == 200 for response in responses)
        assert all("[REDACTED]" in response.text for response in responses)
        assert all(response.json()["providers"][0]["lastCall"] is None for response in responses)
        for failure in [ToolError("tool_auth", False, "unexpected provider detail " + SECRET), RuntimeError("unexpected error " + SECRET)]:
            tools.failure = failure
            response = client.post("/v1/call", json=QUERY)
            error = assert_error(response)
            assert "unexpected" not in error["message"]
            responses.append(response)
        combined = " ".join(response.text for response in responses) + caplog.text
        assert "OTHER_CLIENT_PRIVATE_QUERY" not in combined
        assert all(value not in combined for value in (SECRET, quote(SECRET, safe=""), quote_plus(SECRET, safe="")))
