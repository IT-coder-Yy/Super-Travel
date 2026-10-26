"""Small, non-streaming DashScope Chat Completions adapter; it never executes tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ModelError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 60.0

    def __post_init__(self):
        try:
            url = httpx.URL(self.base_url)
            valid = url.scheme == "https" and url.host and not (url.userinfo or url.query or url.fragment)
        except httpx.InvalidURL:
            valid = False
        if not valid or not self.api_key.strip() or not self.model.strip():
            raise ModelError("model_configuration", "请填写 HTTPS 模型服务地址、模型名称与 API Key。")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ModelError("model_configuration", "模型超时时间必须为正数。")


class ToolFunction(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str = Field(min_length=1)
    # Keep the provider's JSON string for the next turn; the executor validates its schema.
    arguments: str


class ToolCall(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1)
    type: Literal["function"]
    function: ToolFunction


class AssistantMessage(BaseModel):
    model_config = ConfigDict(strict=True)
    role: Literal["assistant"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    reasoning_content: str | None = None


class ModelReply(BaseModel):
    model_config = ConfigDict(strict=True)
    message: AssistantMessage
    finish_reason: str = Field(min_length=1)


class ModelClient:
    def __init__(self, config: ModelConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_completion_tokens: int = 4096,
        enable_thinking: bool | None = None,
    ) -> ModelReply:
        if not messages or type(max_completion_tokens) is not int or max_completion_tokens < 1:
            raise ModelError("model_request", "模型请求需要消息和有效的输出长度限制。")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "max_completion_tokens": max_completion_tokens,
        }
        if tools:
            payload["tools"] = tools
        if enable_thinking is not None:
            payload["enable_thinking"] = enable_thinking
        try:
            # A cancelled task exits this context and closes the pending HTTP connection.
            async with asyncio.timeout(self.config.timeout_seconds), httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds, connect=min(10.0, self.config.timeout_seconds)),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self.config.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                    json=payload,
                )
        except (httpx.TimeoutException, TimeoutError):
            raise ModelError("model_timeout", "模型响应超时，请稍后重试。") from None
        except httpx.HTTPError:
            raise ModelError("model_connection", "无法连接模型服务，请检查网络与服务地址。") from None
        except (ValueError, TypeError):
            raise ModelError("model_request", "模型请求格式不正确。") from None
        if not response.is_success:
            status = response.status_code
            if status in (401, 403):
                raise ModelError("model_authentication", "模型鉴权失败，请检查 API Key 与模型访问权限。", status)
            if status == 429:
                raise ModelError("model_rate_limit", "模型服务限流或额度不足，请稍后重试并检查账户额度。", status)
            if status in (400, 404, 422):
                raise ModelError("model_request", "模型服务拒绝请求，请检查模型名称、服务地址与参数。", status)
            raise ModelError("model_service", "模型服务暂时不可用，请稍后重试。", status)
        try:
            reply = ModelReply.model_validate(response.json()["choices"][0])
            if not (reply.message.content or reply.message.tool_calls) and reply.finish_reason != "length":
                raise ValueError("empty response")
            return reply
        except (ValueError, TypeError, KeyError, IndexError, ValidationError):
            raise ModelError("model_response", "模型返回格式不完整，请重试。") from None
