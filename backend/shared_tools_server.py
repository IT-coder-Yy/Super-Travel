"""Small public read-only relay; provider credentials stay on the server."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
import time
from urllib.parse import quote, quote_plus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jsonschema import SchemaError, ValidationError as SchemaValidationError, validate
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException

from backend.external_tools import ExternalTools, KEY_NAMES
from backend.mcp_client import ToolError, _CredentialFilter


ERRORS = {
    "tool_arguments": (422, False, "工具名称或参数不正确，请从可用工具中选择并检查查询条件。"),
    "tool_request_too_large": (413, False, "工具请求过大，请缩短查询条件。"),
    "tool_rate_limit": (429, False, "共享工具请求达到限额，请稍后再试。"),
    "tool_quota": (429, False, "外部服务额度不足，请稍后再试或使用其他可用工具。"),
    "tool_auth": (502, False, "外部服务鉴权失败，请由开发者检查服务配置。"),
    "tool_configuration": (503, False, "开发者尚未配置可用的共享工具服务。"),
    "tool_timeout": (504, True, "工具服务响应超时，请稍后重试。"),
    "tool_connection": (502, True, "暂时无法连接外部工具服务，请稍后重试。"),
    "tool_service": (502, True, "外部工具服务暂不可用，请稍后重试。"),
    "tool_protocol": (502, False, "外部工具服务无法处理本次请求。"),
    "tool_redirect": (502, False, "外部工具服务地址异常，请由开发者检查。"),
    "tool_result": (502, False, "外部工具服务未完成查询，请调整条件或换用其他工具。"),
    "tool_response": (502, False, "外部工具服务返回异常，请稍后重试。"),
}


def failure(code, *, status=None, retryable=None):
    code = code if code in ERRORS else "tool_response"
    default_status, default_retry, message = ERRORS[code]
    return JSONResponse({"error": {"code": code, "retryable": default_retry if retryable is None else bool(retryable),
                                   "message": message}}, status_code=status or default_status)


def setting(value, name, default):
    try:
        value = int(os.environ.get(name, str(default))) if value is None else value
        if type(value) is not int or value < 1:
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise ValueError("共享工具限额必须设置为正整数。") from None


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    arguments: dict


class Limits:
    def __init__(self, per_minute, daily_limit, max_concurrent, clock):
        self.per_minute, self.daily_limit, self.max_concurrent, self.clock = per_minute, daily_limit, max_concurrent, clock
        self.minute = self.day = None
        self.clients, self.total, self.active = {}, 0, 0

    @contextmanager
    def call(self, address):
        # shortcut: one process only; restart resets budgets. Scale with a shared limiter before adding workers.
        now = self.clock()
        minute, day = int(now // 60), int(now // 86400)
        if day != self.day:
            self.day, self.total = day, 0
        if minute != self.minute:
            self.minute, self.clients = minute, {}
        count = self.clients.get(address, 0)
        if self.active >= self.max_concurrent or self.total >= self.daily_limit or count >= self.per_minute:
            raise ToolError("tool_rate_limit", False, "")
        self.clients[address], self.total, self.active = count + 1, self.total + 1, self.active + 1
        try:
            yield
        finally:
            self.active -= 1


def create_app(tools=None, *, per_minute=None, daily_limit=None, max_concurrent=None, clock=time.time):
    keys = {name: value for name in KEY_NAMES if (value := os.environ.get(name, "").strip())}
    service = tools if tools is not None else ExternalTools(keys)
    redact = _CredentialFilter(tuple(sorted({encoded for value in keys.values()
        for encoded in (value, quote(value, safe=""), quote_plus(value, safe=""))}, key=len, reverse=True))).redact
    limits = Limits(setting(per_minute, "STA_TOOLS_PER_IP_MINUTE", 20), setting(daily_limit, "STA_TOOLS_DAILY_LIMIT", 1000),
                    setting(max_concurrent, "STA_TOOLS_MAX_CONCURRENT", 4), clock)
    app = FastAPI(title="SuperTravel shared tools", docs_url=None, redoc_url=None, openapi_url=None)
    catalog_lock, catalog, expires = asyncio.Lock(), None, 0

    def providers():
        # Shared state must not disclose another client's recent tool activity.
        return redact([{**provider, "lastCall": None} for provider in service.status()])

    async def tools_catalog():
        nonlocal catalog, expires
        if not getattr(service, "configured", True):
            raise ToolError("tool_configuration", False, "")
        async with catalog_lock:
            if catalog is None or clock() >= expires:
                if catalog is not None and hasattr(service, "refresh"):
                    await service.refresh()
                catalog = await service.list_tools()
                expires = clock() + 300
            return catalog

    @app.middleware("http")
    async def safe_errors(request, call_next):
        try:
            return await call_next(request)
        except Exception:
            # Do not let Uvicorn log upstream exception details containing credentials.
            return failure("tool_response")

    @app.exception_handler(ToolError)
    async def tool_error(_request, error):
        return failure(error.code, retryable=error.retryable if error.code in ERRORS else None)

    @app.exception_handler(HTTPException)
    async def http_error(_request, error):
        return failure("tool_arguments", status=error.status_code)

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/v1/status")
    async def status():
        return {"providers": providers()}

    @app.get("/v1/tools")
    async def list_tools():
        return {"tools": redact(await tools_catalog()), "providers": providers()}

    @app.post("/v1/call")
    async def call_tool(request: Request):
        # Uvicorn may resolve proxy headers only from explicitly trusted proxy addresses.
        with limits.call(request.client.host if request.client else "unknown"):
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ToolError("tool_arguments", False, "")
            raw = bytearray()
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 16 * 1024:
                        raise ToolError("tool_request_too_large", False, "")
            try:
                data = json.loads(raw)
                json.dumps(data, allow_nan=False)
                call = Call.model_validate(data)
            except (ValueError, TypeError, ValidationError, RecursionError):
                raise ToolError("tool_arguments", False, "") from None
            definition = next((row for row in await tools_catalog() if row["name"] == call.name), None)
            if definition is None:
                raise ToolError("tool_arguments", False, "")
            try:
                # Provider schemas sometimes omit additionalProperties; never accept extra transport/credential fields.
                validate(call.arguments, {**definition["inputSchema"], "additionalProperties": False})
            except SchemaValidationError:
                raise ToolError("tool_arguments", False, "") from None
            except SchemaError:
                raise ToolError("tool_response", False, "") from None
            result = await service.call_tool(call.name, call.arguments)
            return {"result": redact(result), "providers": providers()}

    return app
