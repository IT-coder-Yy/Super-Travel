"""MCP Streamable HTTP adapter; provider credentials never leave the transport."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import ipaddress
import logging
import math
import re
from urllib.parse import quote, quote_plus, urlsplit

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


class _CredentialFilter(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = secrets

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
        elif isinstance(value, list):
            value = [self.redact(item) for item in value]
        elif isinstance(value, dict):
            value = {self.redact(key): self.redact(item) for key, item in value.items() if key != "_meta"}
        return value

    def filter(self, record):
        record.msg, record.args = self.redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True

    @contextmanager
    def logs(self):
        # These SDK loggers print request URLs, headers or provider exceptions.
        names = {"httpx2", "client"} | {name for name in list(logging.Logger.manager.loggerDict)
                                        if name.startswith(("mcp.client.", "mcp.shared.", "httpcore2."))}
        loggers = [logging.getLogger(name) for name in names] if self.secrets else []
        for logger in loggers:
            logger.addFilter(self)
        try:
            yield
        finally:
            for logger in loggers:
                logger.removeFilter(self)


class McpClient:
    def __init__(self, url: str, *, timeout: float = 30.0,
                 headers: dict[str, str] | None = None, auth_query: dict[str, str] | None = None):
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
            headers, auth_query = dict(headers or {}), dict(auth_query or {})
            valid = valid and all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", name)
                                  and isinstance(value, str) and value and not any(ord(c) < 32 or ord(c) == 127 for c in value)
                                  for name, value in [*headers.items(), *auth_query.items()])
            valid = valid and not any(name.lower() == "host" for name in headers)
            if valid:
                httpx2.Headers(headers)  # Reject invalid header encodings before contacting a server.
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ToolError("tool_configuration", False, "工具地址需要 HTTPS，本机开发可使用回环 HTTP；地址中不能包含密钥。")
        self.url = url
        self.timeout = timeout
        self._headers, self._auth_query = headers, auth_query
        values = [*auth_query.values(), *headers.values()]
        values += [value.split(" ", 1)[1] for name, value in headers.items()
                   if name.lower() == "authorization" and " " in value]
        self._secrets = tuple(sorted({encoded for value in values if value
                                     for encoded in (value, quote(value, safe=""), quote_plus(value, safe=""))}, key=len, reverse=True))

    @staticmethod
    async def _reject_redirect(response):
        # The MCP SDK follows same-origin redirects independently of httpx's setting.
        if 300 <= response.status_code < 400:
            raise ToolError("tool_redirect", False, "工具服务返回重定向，请检查服务地址。")

    @asynccontextmanager
    async def _connect(self):
        try:
            with _CredentialFilter(self._secrets).logs():
                async with asyncio.timeout(self.timeout), httpx2.AsyncClient(
                    timeout=self.timeout, trust_env=False, follow_redirects=False,
                    headers=self._headers, params=self._auth_query,
                    event_hooks={"response": [self._reject_redirect]},
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
        redact = _CredentialFilter(self._secrets).redact
        async with self._connect() as client:
            tools, cursor = [], None
            while True:
                page = await client.list_tools(cursor=cursor)
                for tool in page.tools:
                    name = redact(tool.name)
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                        raise ToolError("tool_response", False, "工具服务返回了不兼容的工具名称。")
                    tools.append(redact({"name": name, "description": tool.description or "", "inputSchema": tool.input_schema}))
                cursor = page.next_cursor
                if not cursor:
                    return tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(arguments, dict):
            raise ToolError("tool_arguments", False, "工具名称或参数格式不正确。")
        async with self._connect() as client:
            result = await client.call_tool(name, arguments)
            # _meta is application-only context and must not enter a model prompt.
            return _CredentialFilter(self._secrets).redact(result.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"meta"}))
