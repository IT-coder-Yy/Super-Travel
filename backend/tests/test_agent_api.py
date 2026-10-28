import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from uuid import UUID, uuid4

import httpx
import pytest

from backend import agent_service
from backend.agents import AgentSession as RealAgentSession
from backend.main import create_app
from backend.model import ModelReply


TOKEN = "agent-api-test-token"
SECRET = "test-key-never-send-to-a-real-provider"
BASE = "https://model.example/v1"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeModel:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.error = None

    async def chat(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        if self.error:
            raise self.error
        return {"content": "OK"}


class FakeSession:
    block = False
    late_on_cancel = False

    def __init__(self, model, tools, on_state):
        self.model, self.tools, self.on_state = model, tools, on_state
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.close_started, self.close_release = asyncio.Event(), asyncio.Event()
        self.block_close = False
        self.calls = []
        self.closed = False
        self.error = None

    def snapshot(self):
        return state("idle", "", run_id=None)

    async def chat(self, text, *, history, trip_context=None):
        self.calls.append((text, deepcopy(history)))
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
        except asyncio.CancelledError:
            if not self.late_on_cancel:
                raise
            await self.on_state(state("completed", "迟到的执行结果"))
            return {"content": "迟到的规划回复"}
        if self.error:
            raise self.error
        return {"content": f"已回答：{text}"}

    async def aclose(self):
        self.close_started.set()
        if self.block_close:
            await self.close_release.wait()
        self.closed = True


class FakeTools:
    def __init__(self, error=None):
        self.error = error

    async def list_tools(self):
        if self.error:
            raise self.error
        return [{"name": "weather", "description": "天气", "inputSchema": {"type": "object"}}]


def state(status, content="任务状态", run_id="test-run"):
    return {"run_id": run_id, "status": status, "content": content, "plan": ["查询"], "results": [], "error": None}


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ("STA_MODEL_BASE_URL", "STA_MODEL_NAME", "STA_MODEL_API_KEY", "STA_MCP_URL", "STA_SHARED_TOOLS_URL", "AMAP_MAPS_API_KEY", "BAIDU_MAP_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agent_service, "ModelClient", FakeModel)
    monkeypatch.setattr(agent_service, "AgentSession", FakeSession)
    monkeypatch.setattr(FakeSession, "block", False)
    monkeypatch.setattr(FakeSession, "late_on_cancel", False)
    return create_app(tmp_path / "用户 会话", TOKEN)


@asynccontextmanager
async def running(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
            yield client


async def configure(client, *, base_url=BASE, key=SECRET):
    response = await client.post("/api/model/configure", json={"base_url": base_url, "model": "fake-model", "api_key": key})
    assert response.status_code == 200, response.text
    return response


async def create(client, title="杭州旅行"):
    response = await client.post("/api/conversations", json={"title": title, "data": {"messages": [], "formal": {"city": title}, "draft": {"note": "保留"}}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def send(client, identifier, content="你好", message_id=None):
    return await client.post(f"/api/conversations/{identifier}/messages", json={"message_id": str(message_id or uuid4()), "content": content})


async def settled(client, identifier):
    for _ in range(100):
        response = await client.get(f"/api/conversations/{identifier}")
        assert response.status_code == 200
        if not response.json()["data"].get("agentPending"):
            return response.json()
        await asyncio.sleep(0.005)
    raise AssertionError("fake conversation did not finish")


def test_configuration_stays_in_memory_and_key_retention_is_bound_to_host(app):
    async def scenario():
        async with running(app) as client:
            assert (await configure(client)).json() == {"configured": True}
            model = app.state.agents.model
            assert not model.calls  # Saving settings never makes a billable probe.
            public = (await client.get("/api/model/settings")).json()
            assert public["hasKey"] is True and public["baseUrl"] == BASE
            assert SECRET not in json.dumps(public)
            assert not list(app.state.store.root.rglob("*.json"))
            await configure(client, base_url="https://MODEL.example:443/v1/", key=None)
            assert app.state.agents.model.config.api_key == SECRET
            rejected = await client.post("/api/model/configure", json={"base_url": "https://other.example/v1", "model": "x", "api_key": None})
            assert rejected.status_code == 502 and SECRET not in rejected.text
            assert app.state.agents.base_url == BASE
            assert (await configure(client, base_url="https://other.example/v1", key="")).json() == {"configured": False}
            assert (await client.get("/api/model/settings")).json()["hasKey"] is False
            assert (await client.post("/api/model/check")).status_code == 409
            identifier = await create(client)
            assert (await send(client, identifier)).status_code == 409
    asyncio.run(scenario())


def test_message_is_accepted_immediately_and_idempotent_with_json_polling(app, monkeypatch):
    monkeypatch.setattr(FakeSession, "block", True)
    async def scenario():
        async with running(app) as client:
            await configure(client)
            identifier, message_id = await create(client), uuid4()
            first, repeated = await asyncio.gather(send(client, identifier, message_id=message_id), send(client, identifier, message_id=message_id))
            assert first.status_code == repeated.status_code == 202
            assert first.json()["data"]["agentPending"] is True
            assert first.json() == repeated.json()
            session = app.state.agents.sessions[UUID(identifier)]
            await asyncio.wait_for(session.started.wait(), 1)
            assert len(session.calls) == 1
            assert (await send(client, identifier, "不同内容", message_id)).status_code == 409
            assert (await send(client, identifier, "第二条消息")).status_code == 409
            session.release.set()
            result = await settled(client, identifier)
            messages = result["data"]["messages"]
            assert [(m["role"], m["content"]) for m in messages] == [("user", "你好"), ("assistant", "已回答：你好")]
            assert result["data"]["draft"] == {"note": "保留"}
            assert (await send(client, identifier, message_id=message_id)).json() == result
            stored = (app.state.store.conversations / f"{identifier}.json").read_text(encoding="utf-8")
            assert json.loads(stored) == result and SECRET not in stored
            await configure(client, key="")
            assert (await send(client, identifier, message_id=message_id)).json() == result
    asyncio.run(scenario())


def test_conversations_keep_separate_sessions_and_histories(app):
    async def scenario():
        async with running(app) as client:
            await configure(client)
            first, second = await create(client, "杭州"), await create(client, "苏州")
            assert (await send(client, first, "杭州需求")).status_code == 202
            assert (await send(client, second, "苏州需求")).status_code == 202
            await settled(client, first)
            await settled(client, second)
            await send(client, first, "继续杭州")
            await send(client, second, "继续苏州")
            a, b = await settled(client, first), await settled(client, second)
            sessions = app.state.agents.sessions
            assert sessions[UUID(first)] is not sessions[UUID(second)]
            assert "苏州" not in json.dumps(sessions[UUID(first)].calls, ensure_ascii=False)
            assert "杭州" not in json.dumps(sessions[UUID(second)].calls, ensure_ascii=False)
            assert len(sessions[UUID(first)].calls[-1][1]) == 2
            assert a["data"]["formal"]["city"] == "杭州" and b["data"]["formal"]["city"] == "苏州"
    asyncio.run(scenario())


def test_progress_and_followup_questions_are_durable_without_duplicate_answers(app):
    async def scenario():
        async with running(app) as client:
            await configure(client)
            identifier = await create(client)
            await send(client, identifier)
            await settled(client, identifier)
            session = app.state.agents.sessions[UUID(identifier)]
            for phase in ("running", "replanning", "needs_input", "completed", "completed"):
                await session.on_state(state(phase, f"状态：{phase}"))
                document = (await client.get(f"/api/conversations/{identifier}")).json()
                assert document["data"]["agent"]["status"] == phase
            messages = document["data"]["messages"]
            assert sum(item["id"] == "run-test-run-completed" for item in messages) == 1
            assert any(item["content"] == "状态：needs_input" for item in messages)
            assert document["data"]["draft"] == {"note": "保留"}
    asyncio.run(scenario())


@pytest.mark.parametrize("phase,pending,expected", [("running", False, "interrupted"), ("replanning", False, "interrupted"), ("idle", True, "interrupted"), ("completed", False, "completed")])
def test_startup_marks_unfinished_work_interrupted(app, phase, pending, expected):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
            response = await client.post("/api/conversations", json={"title": "重启", "data": {"messages": [], "agent": state(phase), "agentPending": pending}})
            identifier = response.json()["id"]
        async with running(app) as client:
            document = (await client.get(f"/api/conversations/{identifier}")).json()
            assert document["data"]["agent"]["status"] == expected
            assert not document["data"]["agentPending"]
            assert not app.state.agents.sessions and not app.state.agents.turns
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["cancel", "stop"])
def test_cancel_and_shutdown_drop_late_turn_and_state_writes(app, monkeypatch, action):
    monkeypatch.setattr(FakeSession, "block", True)
    monkeypatch.setattr(FakeSession, "late_on_cancel", True)
    async def scenario():
        async with running(app) as client:
            await configure(client)
            identifier = await create(client)
            await send(client, identifier)
            session = app.state.agents.sessions[UUID(identifier)]
            await asyncio.wait_for(session.started.wait(), 1)
            if action == "cancel":
                response = await client.post(f"/api/conversations/{identifier}/cancel")
                assert response.status_code == 200
            else:
                await app.state.agents.stop()
            result = await settled(client, identifier)
            assert result["data"]["agent"]["status"] == ("cancelled" if action == "cancel" else "interrupted")
            assert len(result["data"]["messages"]) == 1
            assert session.closed and not app.state.agents.turns and not app.state.agents.sessions
            await session.on_state(state("completed", "再次迟到的执行结果"))
            assert (await client.get(f"/api/conversations/{identifier}")).json() == result
            if action == "stop":
                assert (await send(client, identifier, "新请求")).status_code == 503
    asyncio.run(scenario())


def test_concurrent_reconfigure_and_send_never_reuse_a_key_for_another_host(app, monkeypatch):
    monkeypatch.setattr(FakeSession, "block", True)
    async def scenario():
        async with running(app) as client:
            await configure(client)
            old_id, new_id = await create(client), await create(client)
            await send(client, old_id)
            session = app.state.agents.sessions[UUID(old_id)]
            await asyncio.wait_for(session.started.wait(), 1)
            session.block_close = True
            replacing = asyncio.create_task(client.post("/api/model/configure", json={"base_url": "https://new.example/v1", "model": "new-model", "api_key": "new-test-key"}))
            await asyncio.wait_for(session.close_started.wait(), 1)
            stale_preserve = asyncio.create_task(client.post("/api/model/configure", json={"base_url": BASE, "model": "old-model", "api_key": None}))
            sending = asyncio.create_task(send(client, new_id, "新地址请求"))
            await asyncio.sleep(0)
            assert not stale_preserve.done() and not sending.done()
            session.close_release.set()
            changed, stale, accepted = await asyncio.gather(replacing, stale_preserve, sending)
            assert changed.status_code == 200 and stale.status_code == 502 and accepted.status_code == 202
            new_session = app.state.agents.sessions[UUID(new_id)]
            assert new_session.model.config.base_url == "https://new.example/v1"
            assert new_session.model.config.api_key == "new-test-key"
            new_session.release.set()
            await settled(client, new_id)
    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["discard", "recover"])
def test_discard_invalidates_sessions_even_if_conversation_json_is_broken(app, monkeypatch, action):
    monkeypatch.setattr(FakeSession, "block", True)
    monkeypatch.setattr(FakeSession, "late_on_cancel", True)
    async def scenario():
        async with running(app) as client:
            await configure(client)
            identifier = await create(client)
            await send(client, identifier)
            session = app.state.agents.sessions[UUID(identifier)]
            await asyncio.wait_for(session.started.wait(), 1)
            path = app.state.store.conversations / f"{identifier}.json"
            path.write_bytes(b"{broken")
            if action == "discard":
                await app.state.agents.discard(UUID(identifier))
                expected = b"{broken"
            else:
                options = (await client.get("/api/conversations/recovery")).json()
                option = next(item for item in options if item["id"] == identifier)
                response = await client.post(f"/api/conversations/{identifier}/recover", json={"token": option["token"]})
                assert response.status_code == 200
                expected = path.read_bytes()
                assert response.json() == json.loads(expected)
                originals = list((app.state.store.root / "recovered-originals").glob("*.json"))
                assert len(originals) == 1 and originals[0].read_bytes() == b"{broken"
            await session.on_state(state("completed", "不能覆盖恢复内容"))
            assert path.read_bytes() == expected
            assert session.closed and not app.state.agents.sessions and not app.state.agents.turns
    asyncio.run(scenario())


def test_failed_turn_does_not_persist_provider_secret_or_leave_pending(app, monkeypatch):
    monkeypatch.setattr(FakeSession, "block", True)
    async def scenario():
        async with running(app) as client:
            await configure(client)
            identifier = await create(client)
            await send(client, identifier)
            session = app.state.agents.sessions[UUID(identifier)]
            session.error = agent_service.ModelError("model_service", f"provider echoed {SECRET}")
            session.release.set()
            result = await settled(client, identifier)
            assert len(result["data"]["messages"]) == 2
            assert "本次对话未完成" in result["data"]["messages"][-1]["content"]
            for path in app.state.store.root.rglob("*.json"):
                assert SECRET not in path.read_text(encoding="utf-8")
    asyncio.run(scenario())


def test_recovered_active_backup_is_marked_interrupted(app, monkeypatch):
    monkeypatch.setattr(FakeSession, "block", True)
    async def scenario():
        async with running(app) as client:
            await configure(client)
            identifier = await create(client)
            await send(client, identifier)
            session = app.state.agents.sessions[UUID(identifier)]
            await asyncio.wait_for(session.started.wait(), 1)
            await session.on_state(state("running"))
            # The previous revision now contains an accepted, unfinished turn.
            backup = app.state.store.backups / f"{identifier}.json"
            assert json.loads(backup.read_bytes())["data"]["agentPending"] is True
            path = app.state.store.conversations / f"{identifier}.json"
            path.write_bytes(b"{broken")
            option = (await client.get("/api/conversations/recovery")).json()[0]
            response = await client.post(f"/api/conversations/{identifier}/recover", json={"token": option["token"]})
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["agentPending"] is False
            assert data["agent"]["status"] == "interrupted"
            assert session.closed and not app.state.agents.turns
    asyncio.run(scenario())


def test_check_endpoints_are_explicit_and_sanitize_provider_errors(app):
    async def scenario():
        async with running(app) as client:
            await configure(client)
            model = app.state.agents.model
            assert (await client.post("/api/model/check")).json() == {"connected": True}
            assert model.calls[0][1] == {"max_completion_tokens": 32, "enable_thinking": False}
            model.error = RuntimeError(f"Authorization: Bearer {SECRET}")
            failed = await client.post("/api/model/check")
            assert failed.status_code == 502 and SECRET not in failed.text
            assert (await client.post("/api/tools/check")).status_code == 409
            app.state.agents.tools_url = "https://tools.example/mcp"
            app.state.agents.tools = FakeTools()
            assert (await client.post("/api/tools/check")).json() == {"connected": True, "toolCount": 1}
            app.state.agents.tools = FakeTools(RuntimeError(SECRET))
            failed = await client.post("/api/tools/check")
            assert failed.status_code == 502 and SECRET not in failed.text
            invalid = await client.post("/api/model/configure", json={"base_url": BASE, "model": "x", "api_key": SECRET, "unexpected": SECRET})
            assert invalid.status_code == 422 and SECRET not in invalid.text
    asyncio.run(scenario())


def test_progress_save_failure_is_visible_without_changing_disk_or_executing_again(app, monkeypatch):
    class PlanModel(FakeModel):
        async def chat(self, messages, tools=None, **kwargs):
            self.calls.append(deepcopy(messages))
            planner = tools[0]["function"]["name"] == "route_request"
            name = "route_request" if planner else "weather"
            arguments = {"kind": "plan", "content": "开始查询", "steps": ["查询天气"]} if planner else {}
            return ModelReply.model_validate({"message": {"role": "assistant", "tool_calls": [
                {"id": "test-call", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
            ]}, "finish_reason": "tool_calls"})

    class CountingTools(FakeTools):
        calls = 0
        async def call_tool(self, _name, _arguments):
            self.calls += 1
            return {"content": [{"type": "text", "text": "测试天气"}]}

    monkeypatch.setattr(agent_service, "AgentSession", RealAgentSession)
    monkeypatch.setattr(agent_service, "ModelClient", PlanModel)
    async def scenario():
        async with running(app) as client:
            await configure(client)
            tools = app.state.agents.tools = CountingTools()
            identifier, message_id = await create(client), uuid4()
            path = app.state.store.conversations / f"{identifier}.json"
            original_save, failed_bytes = app.state.store._save, []
            def fail_tool_progress(document, previous=None):
                if document.data.get("agent", {}).get("tool_calls") == 1:
                    failed_bytes.append(path.read_bytes())
                    raise app.state.agents.error_type(503, "save_failed", f"injected failure {SECRET}")
                return original_save(document, previous)
            monkeypatch.setattr(app.state.store, "_save", fail_tool_progress)
            assert (await send(client, identifier, "查询杭州天气", message_id)).status_code == 202
            for _ in range(100):
                response = await client.get(f"/api/conversations/{identifier}")
                if response.json()["data"].get("agent", {}).get("status") == "failed":
                    break
                await asyncio.sleep(0.005)
            else:
                raise AssertionError("The API kept exposing a disk-only running state")
            result = response.json()
            assert len(failed_bytes) == 1 and path.read_bytes() == failed_bytes[0]
            disk = json.loads(failed_bytes[0])
            assert disk["data"]["agent"]["status"] == "running"
            assert result["revision"] == disk["revision"] and result["updated_at"] == disk["updated_at"]
            assert result["data"]["agent"]["persisted"] is False and result["data"]["agentPending"] is False
            assert result["data"]["agent"]["error"] == agent_service.SAVE_ERROR
            assert SECRET not in response.text
            assert len(app.state.agents.model.calls) == 2 and tools.calls == 0
            for _ in range(3):
                assert (await client.get(f"/api/conversations/{identifier}")).json() == result
            assert (await send(client, identifier, "查询杭州天气", message_id)).json() == result
            assert len(app.state.agents.model.calls) == 2 and path.read_bytes() == failed_bytes[0]
            monkeypatch.setattr(app.state.store, "_save", original_save)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["reply", "pending", "both"])
def test_turn_write_failures_do_not_escape_or_leave_the_api_pending(app, monkeypatch, failure):
    monkeypatch.setattr(FakeSession, "block", True)
    async def scenario():
        async with running(app) as client:
            await configure(client)
            identifier, message_id = await create(client), uuid4()
            await send(client, identifier, "测试写失败", message_id)
            key = UUID(identifier)
            session, turn = app.state.agents.sessions[key], app.state.agents.turns[key]
            await asyncio.wait_for(session.started.wait(), 1)
            path = app.state.store.conversations / f"{identifier}.json"
            baseline, original_save = path.read_bytes(), app.state.store._save
            def fail_selected_write(document, previous=None):
                writing_reply = document.data.get("agentPending") and document.data["messages"][-1]["role"] == "assistant"
                if failure == "both" or failure == "reply" and writing_reply or failure == "pending" and not document.data.get("agentPending"):
                    raise app.state.agents.error_type(503, "save_failed", f"injected failure {SECRET}")
                return original_save(document, previous)
            monkeypatch.setattr(app.state.store, "_save", fail_selected_write)
            session.release.set()
            assert await asyncio.wait_for(turn, 1) is None
            assert turn.exception() is None and key not in app.state.agents.turns
            disk = path.read_bytes()
            result = (await client.get(f"/api/conversations/{identifier}")).json()
            assert result["data"]["agentPending"] is False and result["data"]["agent"]["status"] == "failed"
            assert result["data"]["agent"]["persisted"] is False and SECRET not in json.dumps(result)
            assert (await send(client, identifier, "测试写失败", message_id)).json() == result
            assert len(session.calls) == 1 and path.read_bytes() == disk
            if failure == "both":
                assert disk == baseline
            monkeypatch.setattr(app.state.store, "_save", original_save)
            await send(client, identifier, "重新发送新消息")
            await settled(client, identifier)
            assert key not in app.state.agents.failed_turns
    asyncio.run(scenario())


def test_failed_snapshot_from_another_run_does_not_replace_disk_state(app, monkeypatch):
    async def scenario():
        async with running(app) as client:
            await configure(client)
            identifier = await create(client)
            await send(client, identifier)
            await settled(client, identifier)
            session = app.state.agents.sessions[UUID(identifier)]
            saved = app.state.store.mutate(UUID(identifier), lambda data: data.update(agent=state("running", run_id="new-run")))
            monkeypatch.setattr(session, "snapshot", lambda: state("failed", run_id="old-run"))
            result = (await client.get(f"/api/conversations/{identifier}")).json()
            assert result == saved.model_dump(mode="json")
    asyncio.run(scenario())
