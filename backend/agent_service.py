"""Connect the two-agent session to the existing per-conversation JSON store."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from uuid import UUID

from fastapi import APIRouter
import httpx
from pydantic import BaseModel, ConfigDict, Field

from backend.agents import AgentSession
from backend.mcp_client import McpClient, ToolError
from backend.external_tools import ExternalTools
from backend.shared_tools import SharedTools
from backend.model import ModelClient, ModelConfig, ModelError


SAVE_ERROR = "本次对话或进度未能完整保存，请检查本地数据目录。已有文件已保留。"
DEFAULT_TOOLS_URL = "http://127.0.0.1:8080"


class ConfigureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    base_url: str = Field(max_length=2048)
    model: str = Field(max_length=200)
    api_key: str | None = Field(default=None, max_length=8192, repr=False)


class ConfigureTools(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    base_url: str = Field(min_length=1, max_length=2048)


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    message_id: UUID = Field(strict=False)
    content: str = Field(min_length=1, max_length=4000)


class AgentService:
    def __init__(self, store, error_type):
        self.store, self.error_type = store, error_type
        self.model: ModelClient | None = None
        self.base_url = self._base_url(os.environ.get("STA_MODEL_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model_name = os.environ.get("STA_MODEL_NAME", "")
        key = os.environ.get("STA_MODEL_API_KEY", "")
        if key:
            self.model = ModelClient(ModelConfig(self.base_url, key, self.model_name))
        self.tools_url, self.tools_base_url, self.tools_mode = "", DEFAULT_TOOLS_URL, "shared"
        self.tools_settings_error = None
        self.tools_settings_path = store.root / "tools.json" if store is not None else None
        self.tools = self._initial_tools()
        self.sessions: dict[UUID, AgentSession] = {}
        self.turns: dict[UUID, asyncio.Task] = {}
        self.failed_turns: dict[UUID, str] = {}
        self.configuration_lock = asyncio.Lock()
        self.closed = False

    def _initial_tools(self):
        try:
            path = self.tools_settings_path
            if path is not None and path.exists():
                if path.is_symlink():
                    raise ValueError
                with path.open("rb") as handle:
                    raw = handle.read(4097)
                if len(raw) > 4096:
                    raise ValueError
                tools = SharedTools(ConfigureTools.model_validate_json(raw).base_url)
            elif url := os.environ.get("STA_MCP_URL", ""):
                tools = McpClient(url)
                self.tools_url, self.tools_base_url, self.tools_mode = tools.url, tools.url, "mcp"
                return tools
            elif url := os.environ.get("STA_SHARED_TOOLS_URL", ""):
                tools = SharedTools(url)
            else:
                direct = ExternalTools()
                if direct.configured:
                    self.tools_mode = "direct"
                    return direct
                tools = SharedTools(DEFAULT_TOOLS_URL)
            self.tools_base_url = tools.url
            return tools
        except (OSError, ValueError, ToolError):
            self.tools_settings_error = "工具服务配置无效或无法读取，原文件已保留。请在设置中重新保存有效地址。"
            return SharedTools(DEFAULT_TOOLS_URL)

    def public_tools_settings(self):
        settings = {"baseUrl": self.tools_base_url, "mode": self.tools_mode}
        if self.tools_settings_error:
            settings["error"] = self.tools_settings_error
        return settings

    async def configure_tools(self, payload: ConfigureTools):
        async with self.configuration_lock:
            if self.closed:
                raise ModelError("service_closed", "本地服务正在关闭，请重启应用。")
            try:
                candidate = SharedTools(payload.base_url)
            except ToolError as error:
                raise ModelError(error.code, error.message) from None
            if self.store is None or self.tools_settings_path is None:
                raise ModelError("tools_storage_unavailable", "当前运行没有本地数据目录，无法保存工具服务地址。")
            path = self.tools_settings_path
            encoded = json.dumps({"base_url": candidate.url}, ensure_ascii=False, indent=2).encode("utf-8")
            try:
                if path.exists():
                    self.store._atomic_replace(path.with_suffix(".json.previous"), self.store._read_bytes(path))
                self.store._atomic_replace(path, encoded)
            except (OSError, self.error_type):
                raise ModelError("tools_save_failed", "工具服务地址未完整保存，当前连接保持不变，原配置或备份已保留。请检查数据目录。") from None
            self.tools, self.tools_base_url, self.tools_mode = candidate, candidate.url, "shared"
            self.tools_url, self.tools_settings_error = "", None
            try:
                await self._retire(set(self.turns) | set(self.sessions))
            except Exception:
                # Ownership has already been invalidated and old tasks cancelled by _retire.
                raise ModelError("tools_switch_incomplete", "工具地址已保存，旧任务已停止，但任务状态未能完整保存；请重新打开会话。") from None
            return self.public_tools_settings()

    def public_settings(self):
        return {"baseUrl": self.base_url, "model": self.model_name, "hasKey": self.model is not None,
                "storageAvailable": False, "toolsConfigured": bool(self.tools_url) or getattr(self.tools, "configured", False)}

    def read(self, identifier):
        document = self.store.get(identifier)
        session = self.sessions.get(identifier)
        snapshot = session.snapshot() if session else None
        saved = document.data.get("agent")
        saved = saved if isinstance(saved, dict) else {}
        failed_run = (snapshot and snapshot.get("status") == "failed" and snapshot.get("run_id")
                      and isinstance(saved, dict) and snapshot["run_id"] == saved.get("run_id") and snapshot != saved)
        failed_message = self.failed_turns.get(identifier)
        last_user = next((item.get("id") for item in reversed(document.data.get("messages", [])) if item.get("role") == "user"), None) if failed_message else None
        failed_turn = failed_message is not None and failed_message == last_user
        if failed_run or failed_turn:
            # This is an explicitly unsaved runtime view; disk revision and contents stay intact.
            document = document.model_copy(deep=True)
            agent = snapshot if failed_run else saved if saved else snapshot
            document.data["agent"] = {**(agent or {}), "status": "failed", "error": SAVE_ERROR, "persisted": False}
            document.data["agentPending"] = identifier in self.turns
        return document

    @asynccontextmanager
    async def lifespan(self, _app):
        for summary in self.store.summaries():
            document = self.store.get(summary.id)
            if self._active(document.data):
                self._interrupt(summary.id)
        try:
            yield
        finally:
            await self.stop()

    @staticmethod
    def _active(data):
        agent = data.get("agent")
        return data.get("agentPending") or isinstance(agent, dict) and agent.get("status") in ("planning", "running", "replanning")

    def _interrupt(self, identifier, status="interrupted"):
        def change(data):
            data["agentPending"] = False
            previous = data.get("agent")
            data["agent"] = {**(previous if isinstance(previous, dict) else {}), "status": status,
                             "content": "任务已取消。" if status == "cancelled" else "上次任务已中断，可重新发送需求继续。"}
        self.store.mutate(identifier, change)

    async def _retire(self, identifiers, status="interrupted", *, persist=True):
        # Invalidate callback ownership before cancelling: late results must never write JSON.
        turns = [self.turns.pop(identifier) for identifier in identifiers if identifier in self.turns]
        sessions = [self.sessions.pop(identifier) for identifier in identifiers if identifier in self.sessions]
        for turn in turns:
            turn.cancel()
        try:
            if persist:
                for identifier in identifiers:
                    if self._active(self.store.get(identifier).data):
                        self._interrupt(identifier, status)
        finally:
            await asyncio.gather(*turns, *(session.aclose() for session in sessions), return_exceptions=True)

    async def discard(self, identifier):
        async with self.configuration_lock:
            self.failed_turns.pop(identifier, None)
            await self._retire({identifier}, persist=False)

    async def stop(self):
        async with self.configuration_lock:
            self.closed = True
            await self._retire(set(self.turns) | set(self.sessions))

    @staticmethod
    def _base_url(value):
        try:
            url = httpx.URL(value)
            if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment or any(c.isspace() for c in value):
                raise ValueError
            return str(url).rstrip("/")
        except (httpx.InvalidURL, ValueError):
            raise ModelError("model_configuration", "请输入不含账号、密码、查询参数的 HTTPS 模型服务地址。") from None

    async def configure(self, payload: ConfigureModel):
        async with self.configuration_lock:
            if self.closed:
                raise self.error_type(503, "service_closed", "本地服务正在关闭，请重启应用。")
            base_url = self._base_url(payload.base_url)
            key = payload.api_key
            if key is None and self.model:
                if base_url != self._base_url(self.model.config.base_url):
                    raise ModelError("model_configuration", "更换模型服务地址时必须重新填写 API Key。")
                key = self.model.config.api_key
            candidate = ModelClient(ModelConfig(base_url, key, payload.model)) if key else None
            await self._retire(set(self.turns) | set(self.sessions))
            self.model, self.base_url, self.model_name = candidate, base_url, payload.model
        return {"configured": candidate is not None}

    def session(self, identifier):
        if identifier not in self.sessions:
            async def on_state(state):
                if self.sessions.get(identifier) is not session:
                    return
                def change(data):
                    previous = data.get("agent") or {}
                    result = state.get("trip_result")
                    if state.get("status") == "completed" and result and not (previous.get("run_id") == state.get("run_id") and previous.get("status") == "completed"):
                        from backend.trips import validate_result
                        result = validate_result(result)
                        if data.get("trip"):
                            data.update(draft=result["days"], draftTrip=result["trip"])
                        else:
                            data.update(days=result["days"], trip=result["trip"], draft=None, draftTrip=None, selectedDay=1)
                    data["agent"] = state
                    if state.get("status") in ("completed", "needs_input") and state.get("content"):
                        self._append(data, f"run-{state['run_id']}-{state['status']}", "assistant", state["content"])
                self.store.mutate(identifier, change)
            session = AgentSession(self.model, self.tools, on_state)
            self.sessions[identifier] = session
        return self.sessions[identifier]

    @staticmethod
    def _append(data, identifier, role, content):
        messages = data.setdefault("messages", [])
        if not any(item.get("id") == identifier for item in messages):
            messages.append({"id": identifier, "role": role, "content": content})

    async def send(self, identifier, payload):
        async with self.configuration_lock:
            document = self.read(identifier)
            previous = next((item for item in document.data.get("messages", []) if item.get("id") == str(payload.message_id)), None)
            if previous:
                if previous.get("content") != payload.content:
                    raise self.error_type(409, "message_conflict", "这条消息标识已被使用。")
                return document
            if self.closed:
                raise self.error_type(503, "service_closed", "本地服务正在关闭，请重启应用。")
            if self.model is None:
                raise self.error_type(409, "model_unconfigured", "请先在设置中配置模型连接。")
            if identifier in self.turns:
                raise self.error_type(409, "message_pending", "规划 Agent 正在处理上一条消息，请稍候。")
            history = [{"role": item["role"], "content": item["content"]} for item in document.data.get("messages", [])
                       if item.get("role") in ("user", "assistant") and isinstance(item.get("content"), str)][-40:]
            def append(data):
                self._append(data, str(payload.message_id), "user", payload.content)
                data["agentPending"] = True
            self.store.mutate(identifier, append)
            self.failed_turns.pop(identifier, None)
            session = self.session(identifier)
            self.turns[identifier] = asyncio.create_task(self._turn(identifier, payload, session, history))
            return self.store.get(identifier)

    def _write_turn(self, identifier, message_id, change):
        try:
            self.store.mutate(identifier, change)
        except Exception:
            self.failed_turns[identifier] = str(message_id)

    async def _turn(self, identifier, payload, session, history):
        try:
            data = self.store.get(identifier).data
            meta = data.get("draftTrip") or data.get("trip")
            trip_context = {"days": data.get("draft") or data.get("days"), "trip": meta} if meta else None
            reply = await session.chat(payload.content, history=history, trip_context=trip_context)
            if self.turns.get(identifier) is asyncio.current_task() and reply.get("content"):
                self._write_turn(identifier, payload.message_id, lambda data: self._append(data, "reply-" + str(payload.message_id), "assistant", reply["content"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.turns.get(identifier) is asyncio.current_task():
                content = "本次对话未完成，请检查模型设置后重试。已保存的消息保持不变。"
                self._write_turn(identifier, payload.message_id, lambda data: self._append(data, "reply-" + str(payload.message_id), "assistant", content))
        finally:
            if self.turns.get(identifier) is asyncio.current_task():
                self._write_turn(identifier, payload.message_id, lambda data: data.update(agentPending=False))
                self.turns.pop(identifier, None)

    async def cancel(self, identifier):
        async with self.configuration_lock:
            self.store.get(identifier)
            await self._retire({identifier}, "cancelled")
            return self.store.get(identifier)

    def router(self):
        router = APIRouter(prefix="/api")

        @router.get("/tools/settings")
        async def tools_settings():
            return self.public_tools_settings()

        @router.post("/tools/configure")
        async def configure_tools(payload: ConfigureTools):
            return await self.configure_tools(payload)

        @router.get("/model/settings")
        async def settings():
            return self.public_settings()

        @router.post("/model/configure")
        async def configure(payload: ConfigureModel):
            return await self.configure(payload)

        @router.post("/model/check")
        async def check_model():
            async with self.configuration_lock:
                if self.model is None:
                    raise self.error_type(409, "model_unconfigured", "请先配置模型连接。")
                try:
                    await self.model.chat([{"role": "user", "content": "只回复 OK"}], max_completion_tokens=32, enable_thinking=False)
                except Exception:
                    raise ModelError("model_check_failed", "模型连接检查失败，请检查模型地址、名称、API Key 与网络。") from None
            return {"connected": True}

        @router.get("/tools/status")
        async def tool_status():
            if isinstance(self.tools, SharedTools):
                try:
                    return {"providers": await self.tools.read_status()}
                except ToolError as error:
                    raise ModelError(error.code, error.message) from None
            return {"providers": self.tools.status() if isinstance(self.tools, ExternalTools) else []}

        @router.post("/tools/check")
        async def check_tools():
            if isinstance(self.tools, (ExternalTools, SharedTools)):
                if not self.tools.configured:
                    raise self.error_type(409, "tools_unconfigured", "开发者尚未配置外部服务。")
                try:
                    return await self.tools.refresh()
                except ToolError as error:
                    raise ModelError(error.code, error.message) from None
            if not self.tools_url:
                raise self.error_type(409, "tools_unconfigured", "开发者工具服务尚未配置。")
            try:
                tools = await self.tools.list_tools()
            except Exception:
                raise ModelError("tools_check_failed", "工具服务连接失败，请检查开发者配置与网络。") from None
            return {"connected": True, "toolCount": len(tools)}

        @router.post("/conversations/{identifier}/messages", status_code=202)
        async def send(identifier: UUID, payload: SendMessage):
            return await self.send(identifier, payload)

        @router.post("/conversations/{identifier}/cancel")
        async def cancel(identifier: UUID):
            return await self.cancel(identifier)

        return router
