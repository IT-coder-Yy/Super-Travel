"""Development-only MCP gateway; provider credentials stay in this process."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from urllib.parse import quote

from jsonschema import ValidationError, validate
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.types import CallToolResult, ListToolsResult, TextContent

ROOT = Path(__file__).resolve().parent
PROVIDERS = {
    "amap": ("AMAP_MAPS_API_KEY", "@amap/amap-maps-mcp-server"),
    "tavily": ("TAVILY_API_KEY", "tavily-mcp"),
}
# Only the read-only travel tools needed by the first stage are exposed.
ALLOWED = {
    "amap": {"maps_regeocode", "maps_geo", "maps_weather", "maps_bicycling", "maps_direction_walking",
             "maps_direction_driving", "maps_direction_transit_integrated", "maps_distance",
             "maps_text_search", "maps_around_search", "maps_search_detail"},
    "tavily": {"tavily_search", "tavily_extract"},
}


def load_keys(env_file: Path | None) -> dict[str, str]:
    names = {spec[0] for spec in PROVIDERS.values()}
    if env_file is None:
        values = {name: os.environ.get(name, "") for name in names}
    else:
        values = {}
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator and name.strip() in names:
                value = value.strip()
                if value.startswith(("'", '"')) and value.endswith(value[0]):
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].strip()
                values[name.strip()] = value
    if any(not values.get(name) for name in names):
        raise ValueError("工具服务缺少高德或 Tavily 凭据，请检查指定的环境文件。")
    return values


def redact(data, keys: dict[str, str]):
    # Providers sometimes return the failed request URL, including its API key.
    if isinstance(data, str):
        for secret in keys.values():
            for value in {secret, quote(secret, safe="")}:
                data = data.replace(value, "[REDACTED]")
    elif isinstance(data, list):
        data = [redact(item, keys) for item in data]
    elif isinstance(data, dict):
        data = {redact(key, keys): redact(value, keys) for key, value in data.items()}
    return data


def failure(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


class Gateway:
    def __init__(self, keys: dict[str, str]):
        self.keys = keys
        self.clients = {}
        self.tools = {}
        self.routes = {}

    @asynccontextmanager
    async def lifespan(self, server):
        node = shutil.which("node")
        if not node:
            raise RuntimeError("工具服务需要 Node.js 22.14 或更高版本。")
        async with AsyncExitStack() as stack:
            stderr = stack.enter_context(open(os.devnull, "w"))
            # Tavily calls dotenv.config(); an empty cwd prevents it reading extra credentials.
            cwd = stack.enter_context(TemporaryDirectory(prefix=".runtime-", dir=ROOT))
            for provider, (key_name, package) in PROVIDERS.items():
                script = ROOT / "node_modules" / package / "build" / "index.js"
                if not script.is_file():
                    raise RuntimeError("请先在 tools-service 目录运行 npm ci。")
                parameters = StdioServerParameters(command=node, args=[str(script)],
                                                   env={key_name: self.keys[key_name]}, cwd=cwd)
                client = await stack.enter_async_context(Client(stdio_client(parameters, errlog=stderr), read_timeout_seconds=25))
                self.clients[provider] = client
                result = await client.list_tools()
                for tool in result.tools:
                    if tool.name not in ALLOWED[provider]:
                        continue
                    name = tool.name if tool.name.startswith(provider + "_") else provider + "_" + tool.name
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in self.tools:
                        raise RuntimeError("工具服务的工具名称不兼容。")
                    wire = redact(tool.model_dump(mode="json", by_alias=True, exclude_none=True), self.keys)
                    wire["name"] = name
                    self.tools[name] = type(tool).model_validate(wire)
                    self.routes[name] = (provider, tool.name)
            yield self

    async def list_tools(self, ctx, params):
        return ListToolsResult(tools=list(self.tools.values()))

    async def call_tool(self, ctx, params):
        if params.name not in self.tools:
            return failure("工具不存在或未开放，请重新选择已列出的工具。")
        try:
            validate(params.arguments or {}, self.tools[params.name].input_schema)
        except ValidationError:
            return failure("工具参数不符合 schema，请检查必填字段与类型后修改计划。")
        provider, name = self.routes[params.name]
        try:
            async with asyncio.timeout(25):
                result = await self.clients[provider].call_tool(name, params.arguments or {})
            wire = result.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"meta"})
            return CallToolResult.model_validate(redact(wire, self.keys))
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return failure("上游工具响应超时，请稍后重试或调整计划。")
        except Exception:
            return failure("上游工具调用失败，请检查工具服务配置或调整计划。")


def create_server(keys: dict[str, str]) -> Server:
    gateway = Gateway(keys)
    return Server("SuperTravel Tools", version="0.1.0", lifespan=gateway.lifespan,
                  on_list_tools=gateway.list_tools, on_call_tool=gateway.call_tool)
