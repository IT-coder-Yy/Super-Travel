import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import agent_service
from backend.external_tools import KEY_NAMES, ExternalTools
from backend.main import create_app
from backend.shared_tools import SharedTools
from backend.tests.test_agent_api import FakeModel, FakeSession, state


TOKEN = "tools-settings-fixture-token"
AUTH = {"Authorization": "Bearer " + TOKEN}
DEFAULT = {"baseUrl": "http://127.0.0.1:8080", "mode": "shared"}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ("STA_MODEL_API_KEY", "STA_MODEL_BASE_URL", "STA_MODEL_NAME", "STA_MCP_URL", "STA_SHARED_TOOLS_URL", *KEY_NAMES):
        monkeypatch.delenv(name, raising=False)


def test_saved_tool_address_survives_restart_overrides_environment_and_keeps_model_private(tmp_path, monkeypatch):
    monkeypatch.setenv("STA_MCP_URL", "https://legacy.example.test/mcp")
    monkeypatch.setenv("STA_SHARED_TOOLS_URL", "https://built-in.example.test")
    app = create_app(tmp_path, TOKEN)
    with TestClient(app, headers=AUTH) as client:
        assert client.get("/api/tools/settings").json() == {"baseUrl": "https://legacy.example.test/mcp", "mode": "mcp"}
        assert client.post("/api/model/configure", json={"base_url": "https://model.example.test/v1", "model": "fixture", "api_key": "model-key-fixture-only"}).status_code == 200
        model = app.state.agents.model
        response = client.post("/api/tools/configure", json={"base_url": "http://127.0.0.1:8080/"})
        assert response.status_code == 200 and response.json() == DEFAULT
        assert app.state.agents.model is model
        assert app.state.agents.model.config.api_key == "model-key-fixture-only"
        assert isinstance(app.state.agents.tools, SharedTools)
        assert json.loads((tmp_path / "tools.json").read_bytes()) == {"base_url": DEFAULT["baseUrl"]}
        assert "model-key-fixture-only" not in response.text + (tmp_path / "tools.json").read_text()
    with TestClient(create_app(tmp_path, TOKEN), headers=AUTH) as restarted:
        assert restarted.get("/api/tools/settings").json() == DEFAULT


def test_defaults_use_local_keys_then_shared_environment_then_saved_url(tmp_path, monkeypatch):
    assert agent_service.AgentService(None, None).public_tools_settings() == DEFAULT
    monkeypatch.setenv("BAIDU_MAP_API_KEY", "fixture-map-key")
    local = agent_service.AgentService(None, None)
    assert isinstance(local.tools, ExternalTools) and local.public_tools_settings()["mode"] == "direct"
    monkeypatch.setenv("STA_SHARED_TOOLS_URL", "https://relay.example.test")
    service = agent_service.AgentService(None, None)
    assert isinstance(service.tools, SharedTools) and service.public_tools_settings()["baseUrl"] == "https://relay.example.test"
    (tmp_path / "tools.json").write_text('{"base_url":"https://saved.example.test"}', encoding="utf-8")
    assert create_app(tmp_path, TOKEN).state.agents.public_tools_settings() == {"baseUrl": "https://saved.example.test", "mode": "shared"}


def test_invalid_address_does_not_change_saved_config_or_current_connection(tmp_path):
    app = create_app(tmp_path, TOKEN)
    with TestClient(app, headers=AUTH) as client:
        assert client.post("/api/tools/configure", json={"base_url": "https://relay.example.test"}).status_code == 200
        before, tools = (tmp_path / "tools.json").read_bytes(), app.state.agents.tools
        for url in ("http://public.example.test", "https://password@example.test", "https://example.test?key=private", "https://example.test/#private"):
            response = client.post("/api/tools/configure", json={"base_url": url})
            assert response.status_code == 502 and response.json()["detail"]["code"] == "tool_configuration"
            assert "private" not in response.text and "password" not in response.text
            assert (tmp_path / "tools.json").read_bytes() == before and app.state.agents.tools is tools
        assert client.post("/api/tools/configure", json={"base_url": "https://relay.example.test", "api_key": "forbidden"}).status_code == 422


@pytest.mark.parametrize("raw", [b"invalid json", b'{"base_url":"https://secret@example.test"}'])
def test_corrupt_config_does_not_block_startup_and_explicit_repair_keeps_original(tmp_path, raw):
    (tmp_path / "tools.json").write_bytes(raw)
    with TestClient(create_app(tmp_path, TOKEN), headers=AUTH) as client:
        settings = client.get("/api/tools/settings").json()
        assert settings["baseUrl"] == DEFAULT["baseUrl"] and settings["mode"] == "shared" and settings["error"]
        assert "secret" not in str(settings) and (tmp_path / "tools.json").read_bytes() == raw
        response = client.post("/api/tools/configure", json={"base_url": "https://repaired.example.test"})
        assert response.status_code == 200 and "error" not in response.json()
        assert (tmp_path / "tools.json.previous").read_bytes() == raw
    with TestClient(create_app(tmp_path, TOKEN), headers=AUTH) as restarted:
        assert restarted.get("/api/tools/settings").json() == {"baseUrl": "https://repaired.example.test", "mode": "shared"}


def test_write_failure_retains_current_tools_and_previous_file(tmp_path, monkeypatch):
    app = create_app(tmp_path, TOKEN)
    with TestClient(app, headers=AUTH) as client:
        assert client.post("/api/tools/configure", json={"base_url": "https://before.example.test"}).status_code == 200
        tools, before = app.state.agents.tools, (tmp_path / "tools.json").read_bytes()
        write = app.state.store._atomic_replace
        def failing_write(path, content):
            if path.name == "tools.json":
                raise OSError("private disk detail")
            write(path, content)
        monkeypatch.setattr(app.state.store, "_atomic_replace", failing_write)
        response = client.post("/api/tools/configure", json={"base_url": "https://after.example.test"})
        assert response.status_code == 502 and response.json()["detail"]["code"] == "tools_save_failed"
        assert "private disk detail" not in response.text
        assert app.state.agents.tools is tools and (tmp_path / "tools.json").read_bytes() == before
        assert client.get("/api/tools/settings").json()["baseUrl"] == "https://before.example.test"


def test_switching_tools_retires_old_session_and_blocks_its_late_result(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_service, "ModelClient", FakeModel)
    monkeypatch.setattr(agent_service, "AgentSession", FakeSession)
    monkeypatch.setattr(FakeSession, "block", True)
    monkeypatch.setattr(FakeSession, "late_on_cancel", True)
    app = create_app(tmp_path, TOKEN)
    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture", headers=AUTH) as client:
            await client.post("/api/model/configure", json={"base_url": "https://model.example.test/v1", "model": "fixture", "api_key": "fixture-model-key"})
            document = (await client.post("/api/conversations", json={"title": "切换服务测试", "data": {"messages": []}})).json()
            identifier = document["id"]
            await client.post(f"/api/conversations/{identifier}/messages", json={"message_id": "3f71ddc9-4930-4738-a03d-e90cb34a326f", "content": "规划测试"})
            old = app.state.agents.sessions[UUID(identifier)]
            await asyncio.wait_for(old.started.wait(), 1)
            response = await client.post("/api/tools/configure", json={"base_url": "https://next.example.test"})
            assert response.status_code == 200
            assert old.closed and not app.state.agents.turns and not app.state.agents.sessions
            await old.on_state(state("completed", "旧服务的迟到结果"))
            saved = (await client.get(f"/api/conversations/{identifier}")).json()["data"]
            assert not saved["agentPending"] and saved["agent"]["status"] == "interrupted"
            assert [message["content"] for message in saved["messages"]] == ["规划测试"]
            new = app.state.agents.session(UUID(identifier))
            assert new is not old and new.tools is app.state.agents.tools
            assert new.tools.url == "https://next.example.test"
    asyncio.run(scenario())
