"""Use official Amap REST for trip essentials; keep other tools on MCP."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import httpx2
from jsonschema import ValidationError, validate

from backend.mcp_client import McpClient, ToolError, _CredentialFilter


ROUTES = {
    "maps_text_search": ("/v3/place/text", {"keywords": {"type": "string", "minLength": 1},
        "city": {"type": "string"}, "citylimit": {"type": "boolean"}}, ["keywords"], "搜索真实地点，返回高德原始 GCJ-02 坐标。"),
    "maps_direction_walking": ("/v3/direction/walking", {}, ["origin", "destination"], "查询步行路线的时间与距离。"),
    "maps_direction_driving": ("/v3/direction/driving", {}, ["origin", "destination"], "查询驾车路线的时间与距离。"),
    "maps_direction_transit_integrated": ("/v3/direction/transit/integrated", {}, ["origin", "destination", "city", "cityd"], "查询公共交通路线的时间与距离。"),
}
TOOLS = [{"name": name, "description": "高德官方 Web API：" + description,
          "inputSchema": {"type": "object", "properties": properties or {
              key: {"type": "string", "minLength": 1,
                    "description": "高德 GCJ-02 经度,纬度，小数点不超过 6 位。" if key in ("origin", "destination") else "城市名称或城市编码。"}
              for key in required}, "required": required, "additionalProperties": False}}
         for name, (_, properties, required, description) in ROUTES.items()]


class AmapTools(McpClient):
    def __init__(self, api_key: str, *, transport=None):
        super().__init__("https://mcp.amap.com/mcp", auth_query={"key": api_key})
        self._api_key, self._transport, self.mcp_connected = api_key, transport, False

    async def list_tools(self):
        # REST essentials remain available even if MCP discovery fails or its quota is exhausted.
        try:
            others = await super().list_tools()
            self.mcp_connected = True
        except ToolError:
            others, self.mcp_connected = [], False
        return deepcopy(TOOLS) + [tool for tool in others if tool["name"] not in ROUTES]

    async def call_tool(self, name, arguments):
        if name not in ROUTES:
            return await super().call_tool(name, arguments)
        schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == name)
        try:
            validate(arguments, schema)
            if any(not arguments[key].strip() for key in schema["required"]):
                raise ValueError
        except (ValidationError, TypeError, ValueError):
            raise ToolError("tool_arguments", False, "高德查询参数不正确，请检查地点、城市或坐标。") from None
        params = {key: str(value).lower() if type(value) is bool else value for key, value in arguments.items()}
        params.update(key=self._api_key, output="JSON")
        credentials = _CredentialFilter(self._secrets)
        try:
            with credentials.logs():
                async with asyncio.timeout(10), httpx2.AsyncClient(timeout=10, follow_redirects=False,
                        trust_env=False, transport=self._transport) as client:
                    response = await client.get("https://restapi.amap.com" + ROUTES[name][0], params=params)
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx2.TimeoutException):
            raise ToolError("tool_timeout", True, "高德查询超时，请稍后重试。") from None
        except httpx2.HTTPError:
            raise ToolError("tool_connection", True, "无法连接高德官方服务，请检查网络。") from None
        if response.is_redirect:
            raise ToolError("tool_redirect", False, "高德服务返回了非预期重定向。")
        if response.status_code in (401, 403):
            raise ToolError("tool_auth", False, "高德凭据或服务权限不可用，请由开发者检查。")
        if response.status_code == 429:
            raise ToolError("tool_rate_limit", False, "高德请求受到限流，请稍后重试。")
        if not response.is_success:
            raise ToolError("tool_service", response.status_code >= 500, "高德官方服务暂不可用。")
        try:
            if len(response.content) > 2 * 1024 * 1024:
                raise ValueError
            data = response.json()
            if not isinstance(data, dict) or str(data.get("status")) not in ("0", "1"):
                raise ValueError
        except ValueError:
            raise ToolError("tool_response", False, "高德官方服务返回格式不正确。") from None
        data = credentials.redact(data)
        return {"isError": str(data["status"]) == "0", "structuredContent": data,
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
