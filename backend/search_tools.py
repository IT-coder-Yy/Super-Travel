"""Direct Serper searches, including clearly labelled public Xiaohongshu indexing."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
from urllib.parse import quote, urlsplit

import httpx
from jsonschema import ValidationError, validate

from backend.mcp_client import ToolError


ENDPOINT = "https://google.serper.dev/search"
SCHEMA = {"type": "object", "properties": {
    "query": {"type": "string", "minLength": 1, "maxLength": 500},
    "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
}, "required": ["query"], "additionalProperties": False}
TOOLS = [
    {"name": "serper_search", "description": "通过 Serper 搜索公开网页，返回链接和搜索摘要，不代表已读取全文。", "inputSchema": SCHEMA},
    {"name": "xhs_search_public", "description": "通过 Serper 检索小红书公开索引，返回公开链接和搜索摘要；不是小红书官方 MCP，不读取登录内容或完整笔记。", "inputSchema": SCHEMA},
]


def _failure(status: int, data) -> ToolError:
    # Classify the provider response without exposing its request/header echoes.
    message = " ".join(str(data.get(key, "")) for key in ("message", "error")).lower() if isinstance(data, dict) else ""
    if status == 402 or any(word in message for word in ("credit", "quota", "balance", "payment")):
        return ToolError("tool_quota", False, "Serper 额度不足；配置仍保留，请恢复额度后再试。")
    if status in (401, 403):
        return ToolError("tool_auth", False, "Serper 鉴权失败，请检查 API Key 与服务权限。")
    if status == 429:
        return ToolError("tool_rate_limit", False, "Serper 请求受到限流，请稍后再试。")
    if status in (400, 404, 422):
        return ToolError("tool_arguments", False, "Serper 拒绝了查询参数，请调整搜索条件。")
    return ToolError("tool_service", status >= 500, "Serper 搜索暂不可用，请稍后再试。")


class SearchTools:
    def __init__(self, api_key: str, *, timeout: float = 25.0, transport: httpx.AsyncBaseTransport | None = None):
        if not isinstance(api_key, str) or len(api_key) > 8192:
            raise ToolError("tool_configuration", False, "Serper API Key 格式不正确。")
        if any(c.isspace() or ord(c) < 32 or ord(c) > 126 for c in api_key):
            raise ToolError("tool_configuration", False, "Serper API Key 格式不正确。")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ToolError("tool_configuration", False, "搜索超时时间必须为正数。")
        self._api_key, self.timeout, self._transport = api_key, timeout, transport

    async def list_tools(self) -> list[dict]:
        return deepcopy(TOOLS)

    def _redact(self, value):
        if isinstance(value, str) and self._api_key:
            for secret in {self._api_key, quote(self._api_key, safe="")}:
                value = value.replace(secret, "[REDACTED]")
        elif isinstance(value, list):
            value = [self._redact(item) for item in value]
        elif isinstance(value, dict):
            value = {self._redact(key): self._redact(item) for key, item in value.items()}
        return value

    async def call_tool(self, name: str, arguments: dict) -> dict:
        try:
            if name not in {tool["name"] for tool in TOOLS} or not isinstance(arguments, dict):
                raise ValueError
            validate(arguments, SCHEMA)
            query = arguments["query"].strip()
            if not query:
                raise ValueError
        except (ValueError, TypeError, ValidationError):
            raise ToolError("tool_arguments", False, "搜索工具名称或参数不正确，请提供查询词及 1～10 条结果数量。") from None
        if not self._api_key:
            raise ToolError("tool_configuration", False, "Serper 尚未配置 API Key；小红书公开搜索也需要此配置。")
        xhs = name == "xhs_search_public"
        count = arguments.get("count", 5)
        try:
            async with asyncio.timeout(self.timeout), httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(10, self.timeout)),
                follow_redirects=False, trust_env=False, transport=self._transport,
            ) as client:
                response = await client.post(ENDPOINT, headers={"X-API-KEY": self._api_key},
                    json={"q": f"site:xiaohongshu.com {query}" if xhs else query, "num": count})
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ToolError("tool_timeout", True, "Serper 搜索超时，请稍后重试。") from None
        except httpx.HTTPError:
            raise ToolError("tool_connection", True, "无法连接 Serper 搜索，请检查网络。") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if not response.is_success:
            raise _failure(response.status_code, data)
        if not isinstance(data, dict) or not isinstance(data.get("organic", []), list):
            raise ToolError("tool_response", False, "Serper 搜索返回格式不正确。")
        status = data.get("statusCode")
        if data.get("error") or data.get("message") and "organic" not in data or type(status) is int and status >= 400:
            raise _failure(status if type(status) is int and status >= 400 else 200, data)
        results, seen = [], set()
        for row in data.get("organic", []):
            if not isinstance(row, dict) or not isinstance(row.get("link"), str):
                continue
            url = row["link"]
            try:
                parsed = urlsplit(url)
                host = (parsed.hostname or "").lower()
                if parsed.scheme != "https" or not host or parsed.username or parsed.password or "\\" in url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
                    continue
                parsed.port  # Reject malformed URL ports without making a request to the result.
                if xhs and not (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")):
                    continue
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)
            results.append({"title": row.get("title", "")[:500] if isinstance(row.get("title"), str) else "",
                            "url": url, "snippet": row.get("snippet", "")[:2000] if isinstance(row.get("snippet"), str) else ""})
            if len(results) == count:
                break
        payload = self._redact({"provider": "serper", "source": "xiaohongshu_public_index" if xhs else "web_search",
            "query": query, "status": "ok" if results else "empty", "results": results,
            "notice": ("仅为 Serper 收录的小红书公开链接与搜索摘要，未读取登录内容或完整笔记。" if xhs else "搜索摘要不等同于已读取网页全文。") +
                      (" 本次未找到可用公开结果。" if not results else "")})
        return {"isError": False, "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload}
