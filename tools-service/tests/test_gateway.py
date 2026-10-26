import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from mcp.types import CallToolRequestParams, CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gateway

KEYS = {"AMAP_MAPS_API_KEY": "fake-amap-secret", "TAVILY_API_KEY": "fake-tavily-secret"}
SCHEMA = {"type": "object", "properties": {"keywords": {"type": "string"}}, "required": ["keywords"]}


def prepared_gateway(client):
    result = gateway.Gateway(KEYS)
    result.clients["amap"] = client
    result.tools["amap_maps_text_search"] = Tool(name="amap_maps_text_search", input_schema=SCHEMA)
    result.routes["amap_maps_text_search"] = ("amap", "maps_text_search")
    return result


def test_env_loader_ignores_other_secrets_and_keeps_old_file_unchanged(tmp_path):
    env = tmp_path / ".env"
    raw = 'AMAP_MAPS_API_KEY="fake-amap-secret"\nTAVILY_API_KEY=fake-tavily-secret # comment\nMODEL_KEY=never-read\nFILESYSTEM_PATH=/private\n'
    env.write_text(raw, encoding="utf-8")
    assert gateway.load_keys(env) == KEYS
    assert env.read_text(encoding="utf-8") == raw
    env.write_text("AMAP_MAPS_API_KEY=only-one", encoding="utf-8")
    with pytest.raises(ValueError, match="凭据"):
        gateway.load_keys(env)


def test_only_selected_provider_key_enters_each_process_and_schema_is_preserved(monkeypatch):
    parameters = []

    def transport(params, errlog):
        parameters.append(params)
        return params

    class FakeClient:
        def __init__(self, params, **kwargs):
            self.params = params

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def list_tools(self):
            names = ["maps_text_search", "filesystem_read"] if "AMAP_MAPS_API_KEY" in self.params.env else ["tavily_search", "tavily_research"]
            return SimpleNamespace(tools=[Tool(name=name, input_schema=SCHEMA) for name in names])

    monkeypatch.setattr(gateway, "stdio_client", transport)
    monkeypatch.setattr(gateway, "Client", FakeClient)
    monkeypatch.setattr(gateway.shutil, "which", lambda _: "node")
    monkeypatch.setattr(Path, "is_file", lambda _: True)

    async def run():
        instance = gateway.Gateway(KEYS)
        async with instance.lifespan(None):
            assert set(instance.tools) == {"amap_maps_text_search", "tavily_search"}
            assert instance.tools["amap_maps_text_search"].input_schema == SCHEMA

    asyncio.run(run())
    assert [item.env for item in parameters] == [{"AMAP_MAPS_API_KEY": KEYS["AMAP_MAPS_API_KEY"]}, {"TAVILY_API_KEY": KEYS["TAVILY_API_KEY"]}]
    assert all(item.command == "node" and len(item.args) == 1 for item in parameters)
    assert all(Path(item.cwd).name.startswith(".runtime-") for item in parameters)


def test_error_redaction_and_parameter_rejection():
    calls = []

    class Provider:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return CallToolResult(content=[TextContent(type="text", text="error key=" + KEYS["AMAP_MAPS_API_KEY"])],
                                  structured_content={"echo": KEYS["TAVILY_API_KEY"]}, is_error=True)

    async def run():
        instance = prepared_gateway(Provider())
        missing = await instance.call_tool(None, CallToolRequestParams(name="filesystem_read", arguments={}))
        invalid = await instance.call_tool(None, CallToolRequestParams(name="amap_maps_text_search", arguments={"keywords": 12}))
        result = await instance.call_tool(None, CallToolRequestParams(name="amap_maps_text_search", arguments={"keywords": "西湖"}))
        assert missing.is_error and invalid.is_error and result.is_error
        wire = json.dumps(result.model_dump(mode="json"))
        assert all(secret not in wire for secret in KEYS.values())
        assert "[REDACTED]" in wire

    asyncio.run(run())
    assert calls == [("maps_text_search", {"keywords": "西湖"})]


def test_redaction_handles_json_escaping_and_url_encoding():
    secret = 'test-"/secret'
    result = gateway.redact({"content": [secret, "https://example.test/?key=" + quote(secret, safe="")]}, {"KEY": secret})
    assert result == {"content": ["[REDACTED]", "https://example.test/?key=[REDACTED]"]}


def test_provider_exception_is_safe_and_cancellation_propagates():
    class Provider:
        async def call_tool(self, *args):
            raise RuntimeError(KEYS["AMAP_MAPS_API_KEY"])

    class CancelledProvider:
        async def call_tool(self, *args):
            raise asyncio.CancelledError

    async def run():
        params = CallToolRequestParams(name="amap_maps_text_search", arguments={"keywords": "西湖"})
        result = await prepared_gateway(Provider()).call_tool(None, params)
        assert result.is_error and KEYS["AMAP_MAPS_API_KEY"] not in result.model_dump_json()
        with pytest.raises(asyncio.CancelledError):
            await prepared_gateway(CancelledProvider()).call_tool(None, params)

    asyncio.run(run())
