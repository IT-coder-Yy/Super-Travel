"""UI-only fake model/tools. Runs the real API, AgentSession and JSON store without paid services."""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
for name in ("STA_MODEL_API_KEY", "STA_MODEL_NAME", "STA_MODEL_BASE_URL", "STA_MCP_URL"):
    os.environ.pop(name, None)

from backend import agent_service, main
from backend.model import ModelReply


def reply(content=None, name=None, arguments=None):
    message = {"role": "assistant", "content": content}
    if name:
        message["tool_calls"] = [{"id": "fixture-call", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]
    return ModelReply.model_validate({"message": message, "finish_reason": "tool_calls" if name else "stop"})


class FixtureModel:
    def __init__(self, config):
        self.config = config

    async def chat(self, messages, tools=None, **_kwargs):
        if tools and tools[0]["function"]["name"] == "route_request":
            text = json.loads(messages[-1]["content"])["request"]
            kind, content, steps = "plan", "固定测试计划已开始。", ["查询杭州天气（固定测试工具）"]
            if "你好" in text:
                kind, content, steps = "answer", "固定测试回复：你好，旅行者。", []
            elif "进度" in text:
                kind, content, steps = "progress", "查看已有任务进度。", []
            elif "缺少" in text:
                kind, content, steps = "clarification", "你准备查询哪个城市？", []
            return reply(name="route_request", arguments={"kind": kind, "content": content, "steps": steps, "replace_active": False})
        if tools:
            if messages[-1]["role"] == "tool":
                return reply("固定测试查询已完成；这不是实时天气数据。")
            return reply(name="fixture_weather", arguments={"city": "杭州"})
        return reply("OK")


class FixtureTools:
    def __init__(self):
        self.pending = []
        self.cancelled = 0
        self.finished = 0

    async def list_tools(self):
        return [{"name": "fixture_weather", "description": "固定的离线测试天气工具，不提供真实资料", "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": False}}]

    async def call_tool(self, _name, _arguments):
        gate = asyncio.Event()
        self.pending.append(gate)
        try:
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
            self.finished += 1
            return {"content": [{"type": "text", "text": "FIXTURE_LATE_RESULT_MUST_NOT_APPEAR"}], "isError": False}
        finally:
            self.pending.remove(gate)


agent_service.ModelClient = FixtureModel
original_create_app = main.create_app
storage_errors = []
original_replace = main.ConversationStore._atomic_replace
original_mutate = main.ConversationStore.mutate


def diagnosed_replace(path, payload):
    try:
        return original_replace(path, payload)
    except OSError as error:
        storage_errors.append({"type": type(error).__name__, "errno": error.errno, "winerror": getattr(error, "winerror", None), "file": path.name})
        raise


def diagnosed_mutate(store, identifier, change):
    try:
        return original_mutate(store, identifier, change)
    except main.StoreError as error:
        cause = error.__cause__
        storage_errors.append({"type": type(error).__name__, "code": error.detail["code"],
                               "cause": type(cause).__name__ if cause else None,
                               "errno": getattr(cause, "errno", None), "winerror": getattr(cause, "winerror", None),
                               "file": Path(cause.filename).name if cause and getattr(cause, "filename", None) else None})
        raise


main.ConversationStore._atomic_replace = staticmethod(diagnosed_replace)
main.ConversationStore.mutate = diagnosed_mutate


def create_app(data_dir, token):
    app = original_create_app(data_dir, token)
    fixture = FixtureTools()
    app.state.agents.tools = fixture
    app.state.agents.tools_url = "https://fixture.invalid/mcp"

    @app.get("/api/test-fixture/state")
    async def state():
        return {"pending": len(fixture.pending), "cancelled": fixture.cancelled, "finished": fixture.finished,
                "storage_errors": storage_errors,
                "sessions": [{"status": session.snapshot()["status"], "error": session.snapshot()["error"], "task_done": session._task.done() if session._task else None} for session in app.state.agents.sessions.values()]}

    @app.post("/api/test-fixture/release")
    async def release():
        for gate in fixture.pending:
            gate.set()
        return {"released": len(fixture.pending)}

    return app


main.create_app = create_app
main.ALLOWED_ORIGINS.append(os.environ["STA_TEST_FRONTEND_ORIGIN"])
raise SystemExit(main.main())
