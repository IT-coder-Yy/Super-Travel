import asyncio
import json
import logging
from pathlib import Path
import sys
import traceback
from urllib.parse import quote

import httpx
import pytest

from supertravel_server.search_tools import SearchTools
from supertravel_server.mcp_client import ToolError
from .test_agents import FakeModel, FakeTools, finish, reply, route, session_for


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


@pytest.mark.parametrize("has_hit", [True, False])
def test_public_search_plan_returns_only_indexed_results_without_creating_trip(has_hit):
    query = "杭州西湖"
    sample = {"title": "固定测试笔记", "link": "https://www.xiaohongshu.com/explore/fixture", "snippet": "固定测试摘要"}
    sent = []

    def provider(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"organic": [sample] if has_hit else []})

    def execute(messages):
        if messages[-1]["role"] != "tool":
            return reply(name="xhs_search_public", arguments={"query": query, "count": 1})
        result = json.loads(messages[-1]["content"])["structuredContent"]
        return reply(result["results"][0]["url"] if result["results"] else result["notice"])

    async def scenario():
        model = FakeModel([route(steps=["搜索小红书公开笔记"])], execute)
        session, _ = session_for(model, SearchTools(SECRET, transport=httpx.MockTransport(provider)))
        result = await session.chat("请在小红书上找杭州西湖的公开笔记，不用做行程。")
        assert result["kind"] == "started"
        assert "xhs_search_public" in model.planner_calls[0][0]["content"]
        state = await finish(session)
        assert state["status"] == "completed" and "trip_result" not in state
        assert (sample["link"] if has_hit else "未找到可用公开结果") in state["content"]
        assert sent == [{"q": "site:xiaohongshu.com 杭州西湖", "num": 1}]

    asyncio.run(scenario())


@pytest.mark.parametrize(("request_text", "include_links"), [
    ("请把标题和链接给我。", True),
    ("列出旅途资料，附上出处。", True),
    ("请注明来源。", True),
    ("只要摘要，不用注明来源。", False),
    ("Please list the titles and URLs.", True),
    ("给我标题，不要附上链接。", False),
    ("摘要即可，不需要标题和链接。", False),
    ("只说查到几条。", False),
])
def test_chat_preserves_verified_search_pairs_when_summary_omits_them(request_text, include_links):
    base = "https://www.xiaohongshu.com/explore/"
    first, second = base + "park", base + "park?day=2"
    organic = [
        {"title": "公园步道", "link": first, "snippet": "固定索引摘要"},
        {"title": "次日步道", "link": second},
        {"title": "重复结果", "link": first},
        {"title": "不属于本站", "link": "https://example.com/park"},
    ]
    calls = []

    def provider(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"organic": organic})

    def execute(messages):
        if messages[-1]["role"] != "tool":
            return reply(name="xhs_search_public", arguments={"query": "旅行步道"})
        return reply("找到了两条公开结果。")

    def summarize(messages):
        sources = json.loads(messages[-1]["content"])["completed_steps"][0]["search_sources"]
        assert [(v["title"], v["url"]) for v in sources] == [("公园步道", first), ("次日步道", second)]
        assert sources[0]["snippet"] == "固定索引摘要"
        return reply("有两条结果。" + (f"次日步道 — {second}" if include_links else ""))

    async def run():
        model = FakeModel([route(steps=["查一次公开索引"])], execute, summarize)
        session, _ = session_for(model, SearchTools(SECRET, transport=httpx.MockTransport(provider)))
        await session.chat(request_text)
        result = await finish(session)
        assert result["status"] == "completed" and len(calls) == len(model.summary_calls) == 1
        content = result["content"]
        if include_links:
            assert f"公园步道 — {first}" in content and content.count(second) == 1
        else:
            assert content == "有两条结果。"
        assert "example.com" not in content and "重复结果" not in content

    asyncio.run(run())


def test_citations_respect_an_executor_selection_and_keep_title_bound_to_its_url():
    from supertravel_server.agents import _with_search_links

    a, b = "https://www.xiaohongshu.com/explore/a", "https://www.xiaohongshu.com/explore/b"
    step = {"content": f"选择乙的出处 {b}", "search_sources": [{"title": "甲", "url": a}, {"title": "乙", "url": b}]}
    one = _with_search_links("选乙。", [step], "选一篇并给链接")
    assert b in one and a not in one
    swapped = f"[乙]({a})、[甲]({b})"
    result = _with_search_links(swapped, [step], "每条都要标题和链接")
    assert result == f"[乙]({b})、[甲]({a})"
    paired = f"[甲]({a})、[乙]({b})"
    assert _with_search_links(paired, [step, step], "列出全部链接") == paired
    generic = _with_search_links("分享如下。", [step], "给我标题和链接")
    assert generic.endswith(f"甲 — {a}\n乙 — {b}")


def test_one_citation_can_be_identified_by_unique_title_without_a_url():
    from supertravel_server.agents import _with_search_links

    rows = [{"title": "孤山指南", "url": "https://example.org/a"}, {"title": "西湖路线", "url": "https://example.org/b"}]
    request = "只选一篇并给出标题和链接"
    step = {"content": "选中《孤山指南》。", "search_sources": rows}
    content = _with_search_links("也比较过西湖路线。", [step], request)
    assert "孤山指南 — https://example.org/a" in content and "https://example.org/b" not in content
    step["search_sources"] = [dict(row, title="孤山指南") for row in rows]
    assert _with_search_links("旅行参考。", [step], request) == "旅行参考。"
    step["content"] = "选中 https://example.org/b"
    assert _with_search_links("旅行参考。", [step], request).endswith("孤山指南 — https://example.org/b")


@pytest.mark.parametrize("text_only", [False, True])
def test_tavily_search_citations_survive_an_incomplete_summary(text_only):
    payload = {"results": [
        {"title": "孤山指南", "url": "https://example.org/gushan", "content": "孤山步行导览"},
        {"title": "西湖路线", "url": "https://example.org/lake", "content": "西湖北线"},
    ]}

    async def respond(name, arguments):
        assert name == "tavily_search"
        result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
        if not text_only:
            result["structuredContent"] = payload
        return result

    def execute(messages):
        return reply("有两条旅行资料。") if messages[-1]["role"] == "tool" else reply(name="tavily_search", arguments={"query": "杭州"})

    async def run():
        tools = FakeTools(respond)
        tools.tools = [{"name": "tavily_search", "inputSchema": {"type": "object"}}]
        session, _ = session_for(FakeModel([route(steps=["用 Tavily 搜索"])], execute), tools)
        await session.chat("用 Tavily 查询，并附上标题和链接。")
        state = await finish(session)
        assert state["status"] == "completed" and len(tools.calls) == 1
        for row in payload["results"]:
            assert f"{row['title']} — {row['url']}" in state["content"]
        assert state["results"][0]["search_sources"][0]["snippet"] == "孤山步行导览"
    asyncio.run(run())


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
