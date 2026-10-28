"""Public tool relay: only tool arguments leave the desktop, never model credentials."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import ipaddress
import json

import httpx

from backend.external_tools import ALLOWED, SERVICES
from backend.mcp_client import ToolError


ERRORS = {
    "tool_configuration": "开发者工具服务尚未就绪，请稍后再试。",
    "tool_arguments": "工具名称或参数不正确，请调整查询。",
    "tool_quota": "外部服务额度不足，请使用其他可用工具或稍后再试。",
    "tool_rate_limit": "共享工具服务已达到调用上限，请稍后再试。",
    "tool_auth": "外部服务凭据或权限不可用，请由开发者检查。",
    "tool_timeout": "共享工具服务响应超时，请稍后重试。",
    "tool_connection": "暂时无法连接共享工具服务，请检查网络。",
    "tool_result": "外部服务未完成查询，请调整参数或换用其他工具。",
    "tool_service": "共享工具服务暂不可用，请稍后重试。",
    "tool_response": "共享工具服务返回格式不正确。",
    "tool_request_too_large": "工具请求过大，请缩短查询条件。",
    "tool_protocol": "外部工具服务无法处理本次请求。",
    "tool_redirect": "外部工具服务地址异常，请由开发者检查。",
}


class SharedTools:
    configured = True

    def __init__(self, url: str, *, transport=None):
        try:
            parsed = httpx.URL(url)
            local = parsed.host == "localhost"
            if not local:
                try:
                    local = ipaddress.ip_address(parsed.host).is_loopback
                except ValueError:
                    pass
            valid = parsed.host and (parsed.scheme == "https" or parsed.scheme == "http" and local)
            valid = valid and len(url) <= 2048 and not (parsed.userinfo or parsed.query or parsed.fragment or any(c.isspace() for c in url))
        except (httpx.InvalidURL, TypeError, ValueError):
            valid = False
        if not valid:
            raise ToolError("tool_configuration", False, "共享工具服务地址必须使用无凭据的 HTTPS；仅本机测试允许回环 HTTP。")
        self.url, self._transport = str(parsed).rstrip("/"), transport
        self._catalog, self._providers = None, []
        self._calls = {}
        self._lock = asyncio.Lock()

    def status(self):
        return [{**deepcopy(item), "lastCall": deepcopy(self._calls.get(item["id"]))} for item in self._providers]

    def _providers_from(self, data):
        providers = data.get("providers")
        if not isinstance(providers, list) or len(providers) > len(SERVICES):
            raise ToolError("tool_response", False, ERRORS["tool_response"])
        states, seen = [], set()
        for item in providers:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] not in SERVICES or item["id"] in seen
                    or type(item.get("configured")) is not bool or item.get("connection") not in ("unchecked", "connected", "error")
                    or type(item.get("toolCount")) is not int or not 0 <= item["toolCount"] <= 100
                    or not isinstance(item.get("message", ""), str)):
                raise ToolError("tool_response", False, ERRORS["tool_response"])
            identifier = item["id"]
            seen.add(identifier)
            label, kind, _ = SERVICES[identifier]
            # Global relay history belongs to other users; retain only this client's call status.
            states.append({"id": identifier, "label": label, "kind": kind, "configured": item["configured"],
                           "connection": item["connection"], "toolCount": item["toolCount"], "message": item.get("message", "")[:500], "lastCall": None})
        self._providers = states

    async def _request(self, method, path, body=None):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=8), trust_env=False,
                                         follow_redirects=False, transport=self._transport) as client:
                async with client.stream(method, self.url + path, json=body) as response:
                    if response.is_redirect:
                        raise ToolError("tool_redirect", False, "共享工具服务返回重定向，请由开发者检查地址。")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 2 * 1024 * 1024:
                            raise ToolError("tool_response", False, ERRORS["tool_response"])
                    try:
                        data = json.loads(content)
                    except (ValueError, UnicodeDecodeError):
                        data = None
                    if not response.is_success:
                        error = data.get("error", {}) if isinstance(data, dict) else {}
                        code = error.get("code") if isinstance(error, dict) else None
                        if response.status_code == 429:
                            code = "tool_quota" if code == "tool_quota" else "tool_rate_limit"
                        elif not isinstance(code, str) or code not in ERRORS:
                            code = "tool_service"
                        retryable = code in ("tool_timeout", "tool_connection") or code == "tool_service" and response.status_code >= 500
                        raise ToolError(code, retryable, ERRORS[code])
                    if not isinstance(data, dict):
                        raise ToolError("tool_response", False, ERRORS["tool_response"])
                    return data
        except httpx.TimeoutException:
            raise ToolError("tool_timeout", True, ERRORS["tool_timeout"]) from None
        except httpx.HTTPError:
            raise ToolError("tool_connection", True, ERRORS["tool_connection"]) from None

    async def read_status(self):
        self._providers_from(await self._request("GET", "/v1/status"))
        return self.status()

    async def refresh(self):
        async with self._lock:
            data = await self._request("GET", "/v1/tools")
            catalog = data.get("tools")
            allowed = {name if name.startswith(provider + "_") else provider + "_" + name
                       for provider, names in ALLOWED.items() for name in names}
            if not isinstance(catalog, list) or len(catalog) > len(allowed):
                raise ToolError("tool_response", False, ERRORS["tool_response"])
            result, seen = [], set()
            for item in catalog:
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or item["name"] not in allowed or item["name"] in seen
                        or not isinstance(item.get("description", ""), str) or not isinstance(item.get("inputSchema"), dict)):
                    raise ToolError("tool_response", False, ERRORS["tool_response"])
                seen.add(item["name"])
                result.append({"name": item["name"], "description": item.get("description", ""), "inputSchema": item["inputSchema"]})
            self._providers_from(data)
            self._catalog = result
        return {"connected": any(item["connection"] == "connected" for item in self._providers),
                "toolCount": len(result), "providers": self.status()}

    async def list_tools(self):
        if self._catalog is None:
            await self.refresh()
        return deepcopy(self._catalog)

    async def call_tool(self, name, arguments):
        if not isinstance(arguments, dict) or name not in {item["name"] for item in await self.list_tools()}:
            raise ToolError("tool_arguments", False, ERRORS["tool_arguments"])
        provider = name.split("_", 1)[0]
        try:
            data = await self._request("POST", "/v1/call", {"name": name, "arguments": arguments})
            result = data.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("content", []), list):
                raise ToolError("tool_response", False, ERRORS["tool_response"])
            self._providers_from(data)
        except ToolError as error:
            self._calls[provider] = {"status": {"tool_quota": "quota", "tool_auth": "auth"}.get(error.code, "error"), "message": error.message}
            raise
        empty = isinstance(result.get("structuredContent"), dict) and result["structuredContent"].get("status") == "empty"
        self._calls[provider] = {"status": "empty" if empty else "ok", "message": "查询完成，没有找到结果。" if empty else "最近一次查询成功。"}
        return result
