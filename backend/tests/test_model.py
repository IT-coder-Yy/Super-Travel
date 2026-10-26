import asyncio
import json
from pathlib import Path
import sys
import traceback

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend import model
SECRET = "test-key-do-not-display"
CONFIG = model.ModelConfig("https://dashscope.aliyuncs.com/compatible-mode/v1/", SECRET, "test-model")
MESSAGES = [{"role": "user", "content": "你好"}]


def completion(message=None, finish_reason="stop"):
    return {"choices": [{"message": message or {"role": "assistant", "content": "你好！"}, "finish_reason": finish_reason}]}


def test_chat_uses_explicit_host_and_does_not_log_key(caplog):
    def respond(request):
        assert str(request.url) == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        assert json.loads(request.content) == {"model": "test-model", "messages": MESSAGES, "stream": False, "max_completion_tokens": 32, "enable_thinking": False}
        return httpx.Response(200, json=completion())

    reply = asyncio.run(model.ModelClient(CONFIG, transport=httpx.MockTransport(respond)).chat(MESSAGES, max_completion_tokens=32, enable_thinking=False))
    assert reply.message.content == "你好！"
    assert reply.message.tool_calls is None
    assert reply.finish_reason == "stop"
    assert SECRET not in repr(CONFIG) + caplog.text


def test_tool_call_and_tool_result_round_trip():
    tools = [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    assistant = {"role": "assistant", "content": None, "reasoning_content": "tool context", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "weather", "arguments": '{"city":"杭州"}'}}]}
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["tools"] == tools
        return httpx.Response(200, json=completion(assistant, "tool_calls") if len(requests) == 1 else completion())

    async def run():
        client = model.ModelClient(CONFIG, transport=httpx.MockTransport(respond))
        reply = await client.chat(MESSAGES, tools)
        call = reply.message.tool_calls[0]
        assert call.id == "call_1" and call.function.name == "weather"
        assert json.loads(call.function.arguments) == {"city": "杭州"}
        await client.chat([*MESSAGES, reply.message.model_dump(exclude_none=True), {"role": "tool", "tool_call_id": call.id, "content": "晴"}], tools)

    asyncio.run(run())
    assert requests[1]["messages"][1]["reasoning_content"] == assistant["reasoning_content"]
    assert requests[1]["messages"][1]["tool_calls"] == assistant["tool_calls"]
    assert requests[1]["messages"][2] == {"role": "tool", "tool_call_id": "call_1", "content": "晴"}


@pytest.mark.parametrize("status,code", [(401, "model_authentication"), (403, "model_authentication"), (429, "model_rate_limit"), (400, "model_request"), (500, "model_service"), (307, "model_service")])
def test_http_errors_are_sanitized_and_never_redirect(status, code, caplog):
    hosts = []

    def respond(request):
        hosts.append(request.url.host)
        return httpx.Response(status, text=f"Authorization: Bearer {SECRET}; private user text", headers={"Location": "https://another.example/steal"})

    with pytest.raises(model.ModelError) as error:
        asyncio.run(model.ModelClient(CONFIG, transport=httpx.MockTransport(respond)).chat(MESSAGES))
    assert error.value.code == code and error.value.status == status
    assert hosts == ["dashscope.aliyuncs.com"]
    diagnostic = "".join(traceback.format_exception(error.value)) + caplog.text
    assert SECRET not in diagnostic and "private user text" not in diagnostic


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout, "model_timeout"), (httpx.ConnectError, "model_connection")])
def test_transport_errors_do_not_echo_credentials(exception, code):
    def respond(request):
        raise exception(f"failed with {SECRET}", request=request)

    with pytest.raises(model.ModelError) as error:
        asyncio.run(model.ModelClient(CONFIG, transport=httpx.MockTransport(respond)).chat(MESSAGES))
    assert error.value.code == code
    assert SECRET not in "".join(traceback.format_exception(error.value))


def test_total_deadline_and_cancellation_close_transport():
    async def run(cancel):
        started = asyncio.Event()
        closed = asyncio.Event()

        async def respond(request):
            started.set()
            await asyncio.Event().wait()

        class Transport(httpx.MockTransport):
            async def aclose(self):
                closed.set()
                await super().aclose()

        config = model.ModelConfig(CONFIG.base_url, SECRET, CONFIG.model, timeout_seconds=0.03 if not cancel else 10)
        task = asyncio.create_task(model.ModelClient(config, transport=Transport(respond)).chat(MESSAGES))
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(model.ModelError) as error:
                await task
            assert error.value.code == "model_timeout"
        assert closed.is_set()

    asyncio.run(run(False))
    asyncio.run(run(True))


@pytest.mark.parametrize("payload", [{}, {"choices": []}, completion({"role": "assistant", "content": 3}), completion({"role": "assistant", "content": None}), completion({"role": "assistant", "tool_calls": [{"type": "function"}]})])
def test_bad_provider_response_is_safe(payload):
    with pytest.raises(model.ModelError) as error:
        asyncio.run(model.ModelClient(CONFIG, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))).chat(MESSAGES))
    assert error.value.code == "model_response"


def test_truncation_remains_visible_to_caller():
    reply = asyncio.run(model.ModelClient(CONFIG, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=completion({"role": "assistant", "content": None}, "length")))).chat(MESSAGES))
    assert reply.finish_reason == "length" and reply.message.content is None


@pytest.mark.parametrize("url", ["http://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/v1?api_key=secret", "https://example.com/v1#secret", "missing-host"])
def test_invalid_configuration_never_echoes_url(url):
    with pytest.raises(model.ModelError) as error:
        model.ModelConfig(url, SECRET, "test-model")
    assert error.value.code == "model_configuration"
    assert url not in str(error.value)
