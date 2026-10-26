import asyncio
import json
import logging
from pathlib import Path
import sys
import traceback
from urllib.parse import quote

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.search_tools import SearchTools
from backend.mcp_client import ToolError


SECRET = "fake-key/with+reserved=characters"


def run_search(response, name="serper_search", arguments=None):
    return asyncio.run(SearchTools(SECRET, transport=httpx.MockTransport(lambda _request: response)).call_tool(name, arguments or {"query": "杭州旅行"}))


def test_catalog_is_available_without_credentials_and_is_not_mutable():
    async def scenario():
        tools = SearchTools("")
        catalog = await tools.list_tools()
        assert [tool["name"] for tool in catalog] == ["serper_search", "xhs_search_public"]
        assert "不是小红书官方 MCP" in catalog[1]["description"]
        catalog[0]["inputSchema"]["properties"]["count"]["maximum"] = 100
        assert (await tools.list_tools())[0]["inputSchema"]["properties"]["count"]["maximum"] == 10
        with pytest.raises(ToolError) as error:
            await tools.call_tool("serper_search", {"query": "杭州"})
        assert error.value.code == "tool_configuration" and not error.value.retryable
    asyncio.run(scenario())


def test_only_serper_receives_key_and_output_is_sanitized(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("HTTPS_PROXY", "http://invalid-proxy.invalid:1234")
    hosts = []
    def respond(request):
        hosts.append(request.url.host)
        assert str(request.url) == "https://google.serper.dev/search"
        assert request.headers["X-API-KEY"] == SECRET
        assert json.loads(request.content) == {"q": "杭州旅行", "num": 2}
        return httpx.Response(200, json={"organic": [
            {"title": SECRET, "link": "https://example.com/travel?key=" + quote(SECRET, safe=""), "snippet": "摘要 " + SECRET},
            {"title": "非网页", "link": "javascript:alert(1)", "snippet": "不该返回"},
        ]})
    adapter = SearchTools(SECRET, transport=httpx.MockTransport(respond))
    result = asyncio.run(adapter.call_tool("serper_search", {"query": " 杭州旅行 ", "count": 2}))
    assert hosts == ["google.serper.dev"]
    assert result["isError"] is False and result["structuredContent"]["status"] == "ok"
    assert len(result["structuredContent"]["results"]) == 1
    assert "[REDACTED]" in json.dumps(result)
    assert SECRET not in json.dumps(result) + repr(adapter) + caplog.text
    assert quote(SECRET, safe="") not in json.dumps(result)
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_xhs_public_index_restricts_query_and_rejects_lookalike_urls():
    def respond(request):
        assert json.loads(request.content)["q"] == "site:xiaohongshu.com 杭州旅行"
        return httpx.Response(200, json={"organic": [
            {"title": "欺骗后缀", "link": "https://xiaohongshu.com.evil.example/explore/1"},
            {"title": "欺骗前缀", "link": "https://notxiaohongshu.com/explore/1"},
            {"title": "浏览器会把反斜杠解析为路径", "link": "https://evil.example\\.xiaohongshu.com/explore/1"},
            {"title": "非HTTPS", "link": "http://www.xiaohongshu.com/explore/1"},
            {"title": "嵌入账号", "link": "https://user@www.xiaohongshu.com/explore/1"},
            {"title": "端口错误", "link": "https://www.xiaohongshu.com:no/explore/1"},
            {"title": "公开笔记", "link": "https://www.xiaohongshu.com/explore/1", "snippet": "搜索摘要"},
            {"title": "重复链接", "link": "https://www.xiaohongshu.com/explore/1"},
            {"title": "主域名", "link": "https://xiaohongshu.com/explore/2"},
        ]})
    result = asyncio.run(SearchTools(SECRET, transport=httpx.MockTransport(respond)).call_tool("xhs_search_public", {"query": "杭州旅行"}))
    data = result["structuredContent"]
    assert data["provider"] == "serper" and data["source"] == "xiaohongshu_public_index"
    assert [row["title"] for row in data["results"]] == ["公开笔记", "主域名"]
    assert "未读取登录内容或完整笔记" in data["notice"]


@pytest.mark.parametrize("data", [{"organic": []}, {"searchParameters": {}}, {"organic": [{"link": "http://example.com"}]}])
def test_empty_results_remain_empty_instead_of_claiming_documents(data):
    result = run_search(httpx.Response(200, json=data))
    assert result["isError"] is False
    assert result["structuredContent"]["status"] == "empty"
    assert result["structuredContent"]["results"] == []
    assert "未找到" in result["structuredContent"]["notice"]


@pytest.mark.parametrize("arguments", [{"query": " "}, {"query": "x" * 501}, {"query": "杭州", "count": 0}, {"query": "杭州", "count": 11}, {"query": "杭州", "count": True}, {"query": "杭州", "url": "https://evil.example"}, {"query": 123}, []])
def test_invalid_arguments_never_send_a_request(arguments):
    def forbidden(_request):
        pytest.fail("Invalid arguments must not reach the provider")
    with pytest.raises(ToolError) as error:
        asyncio.run(SearchTools(SECRET, transport=httpx.MockTransport(forbidden)).call_tool("serper_search", arguments))
    assert error.value.code == "tool_arguments" and not error.value.retryable


def test_unknown_tool_is_not_an_arbitrary_endpoint():
    with pytest.raises(ToolError) as error:
        run_search(httpx.Response(200, json={"organic": []}), name="https://evil.example")
    assert error.value.code == "tool_arguments"


def test_header_injection_and_invalid_timeout_are_rejected_without_exposing_key():
    for key, timeout in [(SECRET + "\r\nInjected: yes", 25), (SECRET, float("inf")), (SECRET, True), (SECRET, 0)]:
        with pytest.raises(ToolError) as error:
            SearchTools(key, timeout=timeout)
        assert error.value.code == "tool_configuration"
        assert SECRET not in str(error.value)


@pytest.mark.parametrize("status,code,retryable", [(401, "tool_auth", False), (403, "tool_auth", False), (402, "tool_quota", False), (429, "tool_rate_limit", False), (503, "tool_service", True), (307, "tool_service", False)])
def test_http_failures_are_classified_without_secret_or_body_leaks(status, code, retryable, caplog):
    hosts = []
    def respond(request):
        hosts.append(request.url.host)
        return httpx.Response(status, text=SECRET + " private body", headers={"Location": "https://evil.example/steal"})
    with pytest.raises(ToolError) as error:
        asyncio.run(SearchTools(SECRET, transport=httpx.MockTransport(respond)).call_tool("serper_search", {"query": "杭州"}))
    assert hosts == ["google.serper.dev"]
    assert error.value.code == code and error.value.retryable is retryable
    diagnostic = "".join(traceback.format_exception(error.value)) + caplog.text
    assert SECRET not in diagnostic and "private body" not in diagnostic


@pytest.mark.parametrize("status,data,code", [(403, {"message": "Not enough credits"}, "tool_quota"), (200, {"error": "quota exceeded"}, "tool_quota"), (200, {"statusCode": 401, "message": "Invalid API key"}, "tool_auth")])
def test_business_errors_do_not_become_successful_empty_results(status, data, code):
    with pytest.raises(ToolError) as error:
        run_search(httpx.Response(status, json=data))
    assert error.value.code == code and not error.value.retryable


@pytest.mark.parametrize("data", [[], {"organic": "not a list"}])
def test_malformed_response_is_not_a_success(data):
    with pytest.raises(ToolError) as error:
        run_search(httpx.Response(200, json=data))
    assert error.value.code == "tool_response"


class HangingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False

    async def handle_async_request(self, request):
        self.started.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def test_cancellation_closes_the_connection_and_propagates():
    async def scenario():
        transport = HangingTransport()
        task = asyncio.create_task(SearchTools(SECRET, transport=transport).call_tool("serper_search", {"query": "杭州"}))
        await transport.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport.closed
    asyncio.run(scenario())


def test_timeout_closes_connection_without_exposing_configuration():
    async def scenario():
        transport = HangingTransport()
        with pytest.raises(ToolError) as error:
            await SearchTools(SECRET, timeout=0.01, transport=transport).call_tool("serper_search", {"query": "杭州"})
        assert error.value.code == "tool_timeout" and error.value.retryable
        assert transport.closed
        assert SECRET not in "".join(traceback.format_exception(error.value))
    asyncio.run(scenario())
