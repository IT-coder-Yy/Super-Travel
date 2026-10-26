import asyncio
from contextlib import asynccontextmanager
import traceback

import httpx2
import pytest
from mcp import Client as SDKClient
from mcp.server import Server
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from backend import mcp_client


@pytest.mark.parametrize("url", ["http://remote.example/mcp", "https://key@remote.example/mcp", "https://remote.example/mcp?key=secret", "https://remote.example/#secret", "file:///mcp"])
def test_configuration_rejects_credentials_and_insecure_remote(url):
    with pytest.raises(mcp_client.ToolError) as caught:
        mcp_client.McpClient(url)
    assert caught.value.code == "tool_configuration" and not caught.value.retryable
    assert "secret" not in str(caught.value)


def test_official_sdk_schema_and_error_result_round_trip(monkeypatch):
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}

    async def list_tools(ctx, params):
        return ListToolsResult(tools=[Tool(name="amap_weather", description="Weather", input_schema=schema)])

    async def call_tool(ctx, params):
        assert params.arguments == {"city": "杭州"}
        return CallToolResult(content=[TextContent(type="text", text="quota exhausted")], is_error=True, _meta={"private": "not-for-model"})

    server = Server("test", on_list_tools=list_tools, on_call_tool=call_tool)
    monkeypatch.setattr(mcp_client, "Client", lambda *args, **kwargs: SDKClient(server))

    async def run():
        client = mcp_client.McpClient("http://127.0.0.1:8766/mcp")
        assert await client.list_tools() == [{"name": "amap_weather", "description": "Weather", "inputSchema": schema}]
        result = await client.call_tool("amap_weather", {"city": "杭州"})
        assert result["isError"] and result["content"][0]["text"] == "quota exhausted"
        assert "_meta" not in result

    asyncio.run(run())


@pytest.mark.parametrize("error,code,retryable", [
    (httpx2.ConnectError("secret-key"), "tool_connection", True),
    (httpx2.ReadTimeout("secret-key"), "tool_timeout", True),
    (MCPError(-32602, "secret-key"), "tool_protocol", False),
    (ValueError("secret-key"), "tool_response", False),
])
def test_errors_hide_provider_details(monkeypatch, error, code, retryable):
    @asynccontextmanager
    async def failing_client(*args, **kwargs):
        raise error
        yield

    monkeypatch.setattr(mcp_client, "Client", failing_client)
    with pytest.raises(mcp_client.ToolError) as caught:
        asyncio.run(mcp_client.McpClient("http://localhost:8766/mcp").list_tools())
    assert caught.value.code == code and caught.value.retryable == retryable
    assert "secret-key" not in "".join(traceback.format_exception(caught.value))


def test_cancel_closes_connection_and_never_returns_late_result(monkeypatch):
    closed = []

    class WaitingClient:
        async def list_tools(self, **kwargs):
            await asyncio.Event().wait()

    @asynccontextmanager
    async def waiting_client(*args, **kwargs):
        try:
            yield WaitingClient()
        finally:
            closed.append(True)

    monkeypatch.setattr(mcp_client, "Client", waiting_client)

    async def run():
        request = asyncio.create_task(mcp_client.McpClient("http://localhost:8766/mcp").list_tools())
        await asyncio.sleep(0.03)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert closed == [True]

    asyncio.run(run())


def test_real_sdk_cancels_an_inflight_tool(monkeypatch):
    cancelled = []

    async def call_tool(ctx, params):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    server = Server("cancellation-test", on_call_tool=call_tool)
    monkeypatch.setattr(mcp_client, "Client", lambda *args, **kwargs: SDKClient(server))

    async def run():
        task = asyncio.create_task(mcp_client.McpClient("http://localhost:8766/mcp").call_tool("wait", {}))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled == [True]

    asyncio.run(run())
