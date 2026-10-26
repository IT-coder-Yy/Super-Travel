"""Credential-free MCP Streamable HTTP adapter for the desktop backend."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import math
import re
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


class ToolError(Exception):
    def __init__(self, code: str, retryable: bool, message: str):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.message = message


class McpClient:
    def __init__(self, url: str, *, timeout: float = 30.0):
        try:
            parsed = urlsplit(url)
            local = parsed.hostname == "localhost"
            if not local:
                try:
                    local = ipaddress.ip_address(parsed.hostname or "").is_loopback
                except ValueError:
                    pass
            valid = parsed.hostname and (parsed.scheme == "https" or parsed.scheme == "http" and local)
            valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            valid = valid and math.isfinite(timeout) and timeout > 0
            parsed.port  # Validate malformed ports before opening the connection.
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ToolError("tool_configuration", False, "工具地址需要 HTTPS，本机开发可使用回环 HTTP；地址中不能包含密钥。")
        self.url = url
        self.timeout = timeout

    @asynccontextmanager
    async def _connect(self):
        try:
            async with asyncio.timeout(self.timeout), httpx2.AsyncClient(
                timeout=self.timeout, trust_env=False, follow_redirects=False,
            ) as http:
                transport = streamable_http_client(self.url, http_client=http)
                async with Client(transport, read_timeout_seconds=self.timeout) as client:
                    yield client
        except asyncio.CancelledError:
            raise
        except Exception as error:
            while isinstance(error, ExceptionGroup) and error.exceptions:
                error = error.exceptions[0]
            if isinstance(error, ToolError):
                raise error from None
            if isinstance(error, (TimeoutError, httpx2.TimeoutException)):
                raise ToolError("tool_timeout", True, "工具服务响应超时，请稍后重试。") from None
            if isinstance(error, httpx2.HTTPStatusError):
                status = error.response.status_code
                raise ToolError("tool_service", status == 429 or status >= 500, "工具服务拒绝请求，请检查服务配置或稍后重试。") from None
            if isinstance(error, MCPError):
                raise ToolError("tool_protocol", False, "工具服务无法处理本次请求，请检查工具名称与参数。") from None
            if isinstance(error, (httpx2.HTTPError, OSError)):
                raise ToolError("tool_connection", True, "无法连接工具服务，请检查地址与网络。") from None
            raise ToolError("tool_response", False, "工具服务返回格式不正确，请检查服务。") from None

    async def list_tools(self) -> list[dict]:
        async with self._connect() as client:
            tools, cursor = [], None
            while True:
                page = await client.list_tools(cursor=cursor)
                for tool in page.tools:
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name):
                        raise ToolError("tool_response", False, "工具服务返回了不兼容的工具名称。")
                    tools.append({"name": tool.name, "description": tool.description or "", "inputSchema": tool.input_schema})
                cursor = page.next_cursor
                if not cursor:
                    return tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(arguments, dict):
            raise ToolError("tool_arguments", False, "工具名称或参数格式不正确。")
        async with self._connect() as client:
            result = await client.call_tool(name, arguments)
            # _meta is application-only context and must not enter a model prompt.
            return result.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"meta"})
