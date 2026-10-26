"""Actual MCP/HTTP client against deterministic transports; no provider credentials."""
import asyncio
import json
import logging
from pathlib import Path
import sys
import traceback
from urllib.parse import quote, quote_plus

import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend import mcp_client

SECRET = "fixture-key/a+b c"
TOKEN = "fixture-bearer-private"
BASE = "https://maps.example/mcp"


def mock_http(monkeypatch, handler):
    original = httpx2.AsyncClient
    clients = []

    def create(**kwargs):
        client = original(transport=httpx2.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(mcp_client.httpx2, "AsyncClient", create)
    return clients


def respond(request):
    payload = json.loads(request.content)
    if payload["method"] == "server/discover":
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": payload["id"],
                                         "error": {"code": -32601, "message": "Use legacy handshake"}})
    if payload["method"] == "initialize":
        result = {"protocolVersion": payload["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture", "version": "1"}}
    elif payload["method"] == "notifications/initialized":
        return httpx2.Response(202)
    elif payload["method"] == "tools/list":
        result = {"tools": [{"name": "weather", "description": "Weather " + SECRET,
                             "inputSchema": {"type": "object", "description": quote(SECRET, safe="")},
                             "_meta": {"hidden": SECRET}}]}
    elif payload["method"] == "tools/call":
        result = {"content": [{"type": "text", "text": "Weather " + TOKEN + " " + quote_plus(SECRET, safe=""),
                               "_meta": {"hidden": "application-context"}}],
                  "_meta": {"private": SECRET}, "isError": False}
    else:
        raise AssertionError(payload["method"])
    return httpx2.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})


def test_auth_only_reaches_configured_endpoint_and_results_and_logs_are_redacted(monkeypatch, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "maps.example" and request.url.path == "/mcp"
        assert dict(request.url.params) == {"key": SECRET}
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        return respond(request)

    clients = mock_http(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    before = {name: tuple(logging.getLogger(name).filters) for name in ("httpx2", "mcp.client.streamable_http")}

    async def run():
        client = mcp_client.McpClient(BASE, headers={"Authorization": "Bearer " + TOKEN}, auth_query={"key": SECRET})
        assert client.url == BASE and SECRET not in repr(client) and TOKEN not in repr(client)
        tools = await client.list_tools()
        assert tools == [{"name": "weather", "description": "Weather [REDACTED]", "inputSchema": {"type": "object", "description": "[REDACTED]"}}]
        result = await client.call_tool("weather", {})
        assert result["content"][0]["text"] == "Weather [REDACTED] [REDACTED]"
        assert "_meta" not in json.dumps(result)

    asyncio.run(run())
    assert requests and all(client.is_closed for client in clients)
    assert "HTTP Request:" in caplog.text  # Diagnostic logging remains enabled.
    for value in (SECRET, TOKEN, quote(SECRET, safe=""), quote_plus(SECRET, safe="")):
        assert value not in caplog.text
    assert before == {name: tuple(logging.getLogger(name).filters) for name in before}


@pytest.mark.parametrize("destination", ["https://other.example/steal", "https://maps.example/moved"])
def test_redirect_never_forwards_auth_even_to_same_origin(monkeypatch, destination, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(307, headers={"Location": destination}, text=SECRET)

    clients = mock_http(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(mcp_client.ToolError) as error:
        asyncio.run(mcp_client.McpClient(BASE, auth_query={"ak": SECRET}).list_tools())
    assert len(requests) == 1 and requests[0].url.host == "maps.example" and requests[0].url.path == "/mcp"
    assert not error.value.retryable
    assert SECRET not in caplog.text + "".join(traceback.format_exception(error.value))
    assert all(client.is_closed for client in clients)


def test_provider_exception_and_traceback_are_redacted_and_filters_removed(monkeypatch, caplog):
    def handler(request):
        raise httpx2.ConnectError("provider failed " + SECRET + " " + quote_plus(SECRET, safe=""), request=request)

    clients = mock_http(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(mcp_client.ToolError) as error:
        asyncio.run(mcp_client.McpClient(BASE, auth_query={"key": SECRET}).list_tools())
    diagnostic = caplog.text + "".join(traceback.format_exception(error.value))
    assert SECRET not in diagnostic and quote_plus(SECRET, safe="") not in diagnostic
    assert all(client.is_closed for client in clients)
    assert not any(isinstance(item, mcp_client._CredentialFilter) for item in logging.getLogger("httpx2").filters)


@pytest.mark.parametrize("url,kwargs", [
    (BASE + "?key=" + SECRET, {}),
    (BASE, {"headers": {"Authorization": "bad\r\nheader"}}),
    (BASE, {"headers": {"Host": "other.example"}}),
    (BASE, {"headers": {123: TOKEN}}),
    (BASE, {"auth_query": {"key": ""}}),
    (BASE, {"auth_query": {"key": 123}}),
])
def test_invalid_configuration_never_echoes_supplied_values(url, kwargs):
    with pytest.raises(mcp_client.ToolError) as error:
        mcp_client.McpClient(url, **kwargs)
    assert error.value.code == "tool_configuration"
    assert SECRET not in "".join(traceback.format_exception(error.value))


def test_cancellation_closes_http_and_removes_secret_filters(monkeypatch):
    async def run():
        started = asyncio.Event()

        async def handler(request):
            started.set()
            await asyncio.Event().wait()

        clients = mock_http(monkeypatch, handler)
        task = asyncio.create_task(mcp_client.McpClient(BASE, auth_query={"key": SECRET}).list_tools())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(client.is_closed for client in clients)
        assert not any(isinstance(item, mcp_client._CredentialFilter) for item in logging.getLogger("httpx2").filters)

    asyncio.run(run())
