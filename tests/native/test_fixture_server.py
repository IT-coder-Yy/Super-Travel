"""Exercise fixture HTTPS and the unmodified production Agent/SharedTools contracts."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import ssl
import subprocess
import sys
import time

import httpx
import pytest

import fixture_server as fixture
from backend.agents import AgentSession, ROUTE_TOOL, Route
from backend.mcp_client import ToolError
from backend.model import ModelClient, ModelConfig, ModelReply
from backend.shared_tools import SharedTools
from backend.trips import TripResearch


@pytest.fixture(scope="module")
def live_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-fixture")
    fixture.prepare(root)
    process = subprocess.Popen([sys.executable, str(Path(fixture.__file__).resolve()), "--run", str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not (root / "ready.json").exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        ready = json.loads((root / "ready.json").read_text())
        yield root, ready
    finally:
        process.terminate()
        process.wait(timeout=10)


def model(root, ready):
    # Test the actual TLS connection without mutating the developer's CA bundle.
    transport = httpx.AsyncHTTPTransport(verify=ssl.create_default_context(cafile=root / "ca.pem"))
    return ModelClient(ModelConfig(ready["modelBaseUrl"], ready["apiKey"], ready["model"]), transport=transport)


def test_https_planner_and_simple_model_connection(live_fixture):
    root, ready = live_fixture

    async def check():
        for text, expected in (("你好", "answer"), ("我想旅行", "clarification"), ("进度", "progress"), ("生成固定行程，杭州两天", "plan")):
            reply = await model(root, ready).chat([{"role": "user", "content": json.dumps({"request": text})}], [ROUTE_TOOL])
            route = Route.model_validate_json(reply.message.tool_calls[0].function.arguments)
            assert route.kind == expected
        answer = await model(root, ready).chat([{"role": "user", "content": "connection probe"}])
        assert "固定" in answer.message.content
        ModelReply.model_validate(fixture.reply("固定验收")["choices"][0])

    asyncio.run(check())


def test_real_agent_search_route_submit_and_preserve_times(live_fixture):
    root, ready = live_fixture

    async def check():
        async def on_state(_state):
            pass
        session = AgentSession(model(root, ready), SharedTools(ready["toolsBaseUrl"]), on_state)
        try:
            started = await session.chat("生成固定行程，杭州两天")
            assert started["kind"] == "started"
            await asyncio.wait_for(session._task, timeout=10)
            state = session.snapshot()
            assert state["status"] == "completed", state["error"]
            trip = deepcopy(state["trip_result"])
            assert len(trip["days"]) == 2
            route = trip["days"][0]["activities"][1]["incomingRoute"]
            assert route["status"] == "verified" and route["durationMinutes"] == 20 and route["distanceMeters"] == 1000
            trip["days"][0]["activities"][0]["time"] = "08:30"
            await session.chat("调整固定行程，放慢节奏", trip_context=trip)
            await asyncio.wait_for(session._task, timeout=10)
            modified = session.snapshot()
            assert modified["status"] == "completed", modified["error"]
            assert modified["trip_result"]["days"][0]["activities"][0]["time"] == "08:30"
            assert "调整" in modified["trip_result"]["days"][0]["activities"][0]["description"]
        finally:
            await session.aclose()

    asyncio.run(check())


def test_quota_delay_and_counters_use_shared_tools_schema(live_fixture):
    _root, ready = live_fixture

    async def check():
        tools = SharedTools(ready["toolsBaseUrl"])
        assert (await tools.refresh())["toolCount"] == 2
        located = await TripResearch(tools, "杭州").search("固定测试花园")
        assert len(located) == 1 and located[0]["id"] == "baidu:fixture-garden"
        async with httpx.AsyncClient(trust_env=False) as client:
            await client.post(ready["toolsBaseUrl"] + "/test/mode", json={"mode": "quota"})
            with pytest.raises(ToolError, match="额度") as error:
                await tools.call_tool("baidu_map_search_places", {"query": "固定测试", "region": "杭州"})
            assert error.value.code == "tool_quota"
            await client.post(ready["toolsBaseUrl"] + "/test/mode", json={"mode": "delay"})
            started = time.monotonic()
            await tools.call_tool("baidu_map_search_places", {"query": "固定测试", "region": "杭州"})
            assert time.monotonic() - started >= 3.9
            await client.post(ready["toolsBaseUrl"] + "/test/mode", json={"mode": "ok"})
            status = (await client.get(ready["toolsBaseUrl"] + "/test/status")).json()
            assert status["fixture"] and status["modelCalls"] > 0 and status["searchCalls"] >= 2

    asyncio.run(check())


def test_trust_requires_ci_and_restores_only_mocked_certifi(tmp_path, monkeypatch):
    import certifi
    fixture.prepare(tmp_path)
    bundle = tmp_path / "original-certifi.pem"
    original = b"fixed existing CA bytes\n"
    bundle.write_bytes(original)
    monkeypatch.setattr(certifi, "where", lambda: str(bundle))
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(RuntimeError, match="CI=true"):
        fixture.change_trust(tmp_path)
    assert bundle.read_bytes() == original
    monkeypatch.setenv("CI", "true")
    fixture.change_trust(tmp_path)
    assert bundle.read_bytes() == original + b"\n" + (tmp_path / "ca.pem").read_bytes()
    fixture.change_trust(tmp_path, restore=True)
    assert bundle.read_bytes() == original and not (tmp_path / "certifi-backup.pem").exists()
