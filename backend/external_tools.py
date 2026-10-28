"""Official provider adapters shared by local development and the hosted relay."""
from __future__ import annotations

import asyncio
import json
import os

from backend.mcp_client import McpClient, ToolError
from backend.amap_tools import AmapTools
from backend.search_tools import SearchTools


KEY_NAMES = ("AMAP_MAPS_API_KEY", "BAIDU_MAP_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY")
SERVICES = {
    "amap": ("高德地图", "api", "AMAP_MAPS_API_KEY"),
    "baidu": ("百度地图", "mcp", "BAIDU_MAP_API_KEY"),
    "tavily": ("Tavily", "mcp", "TAVILY_API_KEY"),
    "serper": ("Serper", "api", "SERPER_API_KEY"),
    "xhs": ("小红书公开内容", "public_search", "SERPER_API_KEY"),
}
ALLOWED = {
    "amap": {"maps_regeocode", "maps_geo", "maps_weather", "maps_direction_bicycling", "maps_direction_walking",
             "maps_direction_driving", "maps_direction_transit_integrated", "maps_distance",
             "maps_text_search", "maps_around_search", "maps_search_detail"},
    "baidu": {"map_geocode", "map_reverse_geocode", "map_search_places", "map_place_details",
              "map_directions_matrix", "map_directions", "map_weather", "map_road_traffic",
              "map_search_pro", "map_district_search", "map_geoconv"},
    "tavily": {"tavily_search", "tavily_extract"},
    "serper": {"serper_search"},
    "xhs": {"xhs_search_public"},
}


def result_error(provider: str, result: dict) -> ToolError | None:
    """MCP success envelopes can still contain a provider's business error."""
    errors = []
    blocks = list(result.get("content", []))
    if isinstance(result.get("structuredContent"), dict):
        blocks.append({"type": "text", "text": json.dumps(result["structuredContent"], ensure_ascii=False)})
    for block in blocks:
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            continue
        text = block["text"]
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        failed = result.get("isError") is True or text.lower().startswith(("error:", "error "))
        if isinstance(payload, dict):
            failed = failed or bool(payload.get("error"))
            if provider == "amap":
                failed = failed or str(payload.get("status", "1")) == "0"
            elif provider == "baidu" and "status" in payload:
                failed = failed or str(payload["status"]) != "0"
        if failed:
            errors.append(text.lower())
    if not errors and result.get("isError") is not True:
        return None
    error_text = " ".join(errors)
    if any(word in error_text for word in ("quota", "usage limit", "query_over_limit", "daily limit", "monthly limit", "额度", "配额")):
        return ToolError("tool_quota", False, "此工具的服务额度不足。配置已保留，额度恢复后可以重试；本次请使用其他可用工具。")
    if any(word in error_text for word in ("invalid key", "invalid_user_key", "unauthorized", "permission", "权限", "鉴权")):
        return ToolError("tool_auth", False, "外部服务未接受当前凭据或权限，请由开发者检查服务配置。")
    return ToolError("tool_result", False, "外部服务未完成此次查询，请检查参数或换用其他可用工具。")


class ExternalTools:
    def __init__(self, keys: dict[str, str] | None = None):
        keys = keys if keys is not None else {name: os.environ.get(name, "") for name in KEY_NAMES}
        self.clients, configuration_errors = {}, {}
        # Keep credential destinations fixed; a configurable host must not receive shared keys by mistake.
        factories = {
            "amap": lambda: AmapTools(keys["AMAP_MAPS_API_KEY"]),
            "baidu": lambda: McpClient("https://mcp.map.baidu.com/mcp", auth_query={"ak": keys["BAIDU_MAP_API_KEY"]}),
            "tavily": lambda: McpClient("https://mcp.tavily.com/mcp/", headers={"Authorization": "Bearer " + keys["TAVILY_API_KEY"]}),
            "serper": lambda: SearchTools(keys["SERPER_API_KEY"]),
            "xhs": lambda: SearchTools(keys["SERPER_API_KEY"]),
        }
        for identifier, factory in factories.items():
            if keys.get(SERVICES[identifier][2]):
                try:
                    self.clients[identifier] = factory()
                except ToolError as error:
                    configuration_errors[identifier] = error.message
        self._states = {
            identifier: {"id": identifier, "label": label, "kind": kind, "configured": bool(keys.get(key)),
                         "connection": "error" if identifier in configuration_errors else "unchecked", "toolCount": 0,
                         "message": configuration_errors.get(identifier, "" if identifier in self.clients else "开发者尚未配置此服务。"), "lastCall": None}
            for identifier, (label, kind, key) in SERVICES.items()
        }
        self._catalog: list[dict] = []
        self._routes: dict[str, tuple[str, str]] = {}
        self._loaded = False
        self._discovery_lock = asyncio.Lock()

    @property
    def configured(self):
        return any(state["configured"] for state in self._states.values())

    def status(self):
        return [{**state, "lastCall": dict(state["lastCall"]) if state["lastCall"] else None} for state in self._states.values()]

    async def refresh(self):
        async with self._discovery_lock:
            await self._discover()
        return {"connected": any(state["connection"] == "connected" for state in self._states.values()),
                "toolCount": len(self._catalog), "providers": self.status()}

    async def _discover(self):
        identifiers = list(self.clients)
        results = await asyncio.gather(*(self.clients[name].list_tools() for name in identifiers), return_exceptions=True)
        catalog, routes = [], {}
        for provider, result in zip(identifiers, results):
            state = self._states[provider]
            state["toolCount"] = 0
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                state.update(connection="error", message=result.message if isinstance(result, ToolError) else "外部服务暂时无法连接，请稍后检查。")
                continue
            if provider == "amap" and isinstance(self.clients[provider], AmapTools):
                state.update(connection="connected" if self.clients[provider].mcp_connected else "unchecked",
                             message="地点和步行、驾车、公交算路使用高德官方 API，其余工具使用 MCP。目录可用不代表查询成功。")
            elif provider in ("serper", "xhs"):
                state["message"] = "公开搜索索引，不包含登录后笔记内容。" if provider == "xhs" else "通过官方 Serper 接口搜索公开网页，实际可用性见最近调用。"
            else:
                state.update(connection="connected", message="工具列表已连接；不代表所有查询均有可用额度。")
            for tool in result:
                raw_name = tool["name"]
                if raw_name not in ALLOWED[provider]:
                    continue
                name = raw_name if raw_name.startswith(provider + "_") else provider + "_" + raw_name
                catalog.append({**tool, "name": name})
                routes[name] = (provider, raw_name)
                state["toolCount"] += 1
        self._catalog, self._routes, self._loaded = catalog, routes, True

    async def list_tools(self):
        if not self._loaded:
            async with self._discovery_lock:
                if not self._loaded:
                    await self._discover()
        return list(self._catalog)

    async def call_tool(self, name, arguments):
        await self.list_tools()
        if name not in self._routes:
            raise ToolError("tool_arguments", False, "工具不存在或未连接，请从当前可用工具中选择。")
        provider, raw_name = self._routes[name]
        state = self._states[provider]
        try:
            result = await self.clients[provider].call_tool(raw_name, arguments)
            error = result_error(provider, result)
            if error:
                raise error
        except asyncio.CancelledError:
            raise
        except ToolError as error:
            status = {"tool_quota": "quota", "tool_auth": "auth"}.get(error.code, "error")
            if error.code in ("tool_quota", "tool_rate_limit", "tool_result", "tool_service"):
                state["connection"] = "connected"
            elif error.code in ("tool_auth", "tool_timeout", "tool_connection", "tool_protocol", "tool_redirect"):
                state["connection"] = "error"
            state["lastCall"] = {"status": status, "message": error.message}
            raise
        except Exception:
            state["lastCall"] = {"status": "error", "message": "外部服务未能完成查询，请稍后重试。"}
            raise ToolError("tool_response", False, state["lastCall"]["message"]) from None
        empty = isinstance(result.get("structuredContent"), dict) and result["structuredContent"].get("status") == "empty"
        state["connection"] = "connected"
        state["lastCall"] = {"status": "empty" if empty else "ok", "message": "查询完成，没有找到结果。" if empty else "最近一次查询成功。"}
        return result
