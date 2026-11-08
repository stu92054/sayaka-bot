"""查資料工具：DeepSeek Search／Fetch 客戶端、工具框架、工具迴圈、與聊天流程的整合。"""
import json
import logging
from datetime import datetime

import pytest

from bot.commands import format_usage
from bot.config import Config
from bot.llm import BudgetExceeded, LLMClient
from bot.main import PersonaBot
from bot.persona import Persona
from bot.tools import (TOOL_RESULT_NOTE, WEB_TOOLS_GUIDE, Tool, ToolRunner, build_web_tools, extract_urls,
                       suppress_embeds, valid_fetch_url)
from bot.web import DeepSeekWeb, WebError

from .conftest import BOT_ID, TAIPEI, make_config, make_message

CH = 500
KEY = "sk-secret-deepseek-key"
SEARCH_OK = {"results": [
    {"title": "蓮之空 6th Live 公告", "url": "https://example.com/live6", "snippet": "短", "summary": "6th Live 將於 2027 年舉辦。",
     "site_name": "Example", "date": "2026-10-01T00:00:00+00:00", "rerank_score": 2.1, "authority": 3},
    {"title": "其他", "url": "https://example.org/b", "summary": "", "snippet": "片段", "site_name": None, "date": None,
     "rerank_score": 1.0, "authority": -1},
]}


class FakeWebAPI:
    """模擬 /v1/tools/search 與 /v1/tools/fetch。responses: kind → [(status, data) 或 Exception, ...]"""

    def __init__(self, **responses):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, dict, dict]] = []

    async def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers))
        kind = url.rsplit("/", 1)[-1]
        item = self.responses[kind].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_web(api, billed=None, **kw):
    async def no_sleep(d):
        pass
    return DeepSeekWeb(KEY, post=api, sleep=no_sleep, on_billable=(billed.append if billed is not None else None), **kw)


# --- DeepSeek Search／Fetch 客戶端 ---

async def test_search_request_and_parse():
    api, billed = FakeWebAPI(search=[(200, SEARCH_OK)]), []
    results = await make_web(api, billed).search("  蓮之空 6th Live  " + "x" * 200, count=5, ori_question="問" * 200)
    url, body, headers = api.calls[0]
    assert url == "https://api.deepseek.com/v1/tools/search"
    assert headers == {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    assert len(body["query"]) == 128 and body["query"].startswith("蓮之空")
    assert body["count"] == 5 and body["need_content"] is False and len(body["ori_question"]) == 128
    assert [r.url for r in results] == ["https://example.com/live6", "https://example.org/b"]
    assert results[1].summary == "片段"  # 沒有 summary 時用 snippet
    assert billed == ["search"]


async def test_empty_search_not_billed():
    api, billed = FakeWebAPI(search=[(200, {"results": []})]), []
    assert await make_web(api, billed).search("找不到的東西") == []
    assert billed == []


async def test_fetch_billing_only_with_content():
    api, billed = FakeWebAPI(fetch=[(200, {"title": "T", "content": "# 內文"}), (200, {"title": "", "content": " "})]), []
    web = make_web(api, billed)
    assert (await web.fetch("https://example.com/a")).content == "# 內文"
    await web.fetch("https://example.com/b")
    assert billed == ["fetch"]
    assert api.calls[0][0] == "https://api.deepseek.com/v1/tools/fetch" and api.calls[0][1] == {"url": "https://example.com/a"}


@pytest.mark.parametrize("status", [408, 429, 500, 504])
async def test_retryable_statuses(status):
    api = FakeWebAPI(search=[(status, None), (200, SEARCH_OK)])
    assert len(await make_web(api).search("q")) == 2
    assert len(api.calls) == 2


async def test_timeout_and_network_errors_retried_then_fail():
    api = FakeWebAPI(search=[OSError("down"), OSError("down")])
    with pytest.raises(WebError, match="暫時沒有回應"):
        await make_web(api).search("q")
    assert len(api.calls) == 2


@pytest.mark.parametrize("kind,status,msg", [
    ("search", 402, "額度不足"), ("search", 401, "無法使用"), ("search", 400, "格式不正確"),
    ("fetch", 403, "無法讀取"), ("fetch", 404, "無法讀取"),
])
async def test_non_retryable_statuses(kind, status, msg):
    api = FakeWebAPI(**{kind: [(status, {"error": {"message": "x"}})]})
    web = make_web(api)
    with pytest.raises(WebError, match=msg):
        await (web.search("q") if kind == "search" else web.fetch("https://example.com"))
    assert len(api.calls) == 1


async def test_web_errors_do_not_log_key(caplog):
    api = FakeWebAPI(search=[(401, {"error": {"message": "Authentication Fails"}})])
    with caplog.at_level(logging.WARNING), pytest.raises(WebError):
        await make_web(api).search("q")
    assert "HTTP 401" in caplog.text and KEY not in caplog.text


# --- 網址工具 ---

def test_extract_urls():
    urls = extract_urls("看這個 https://example.com/a?x=1。還有（https://example.org/b/）跟 <https://x.tw>")
    assert urls == {"https://example.com/a?x=1", "https://example.org/b", "https://x.tw"}


@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a", True), ("http://example.com", True),
    ("ftp://example.com", False), ("https://user:pw@example.com", False), ("https://", False), ("", False),
    ("https://example.com/" + "a" * 9000, False),
])
def test_valid_fetch_url(url, ok):
    assert valid_fetch_url(url) is ok


def test_suppress_embeds():
    assert suppress_embeds("來源：https://example.com/a 跟 <https://b.com>") == "來源：<https://example.com/a> 跟 <https://b.com>"
    assert suppress_embeds("[連結](https://c.com)") == "[連結](https://c.com)"


# --- 工具框架 ---

def echo_tool(name="echo", quota=True):
    async def handler(args, runner):
        return f"echo:{args.get('x')}"
    return Tool(name, "說明", {"type": "object", "properties": {}}, handler, counts_toward_quota=quota)


async def test_runner_basics():
    used = []
    runner = ToolRunner([echo_tool()], max_calls=2, on_use=used.append)
    assert runner.specs()[0]["function"]["name"] == "echo"
    assert await runner.run("echo", '{"x": 1}') == "echo:1"
    assert await runner.run("nope", "{}") == "（沒有名為 nope 的工具）"
    assert await runner.run("echo", "{壞掉") == "（工具參數格式錯誤）"
    assert await runner.run("echo", "{}") == "echo:None"
    assert "查詢次數已用完" in await runner.run("echo", "{}")  # 超過 max_calls
    assert used == ["echo", "echo"]


async def test_runner_quota():
    runner = ToolRunner([echo_tool()], has_quota=lambda: False)
    assert "今天的查詢次數已用完" in await runner.run("echo", "{}")
    free = ToolRunner([echo_tool(quota=False)], has_quota=lambda: False)
    assert await free.run("echo", "{}") == "echo:None"  # 不計額度的工具不受影響


async def test_runner_handles_errors():
    async def boom(args, runner):
        raise RuntimeError("x")

    async def web_fail(args, runner):
        raise WebError("查詢服務的額度不足")
    runner = ToolRunner([Tool("boom", "", {}, boom), Tool("web", "", {}, web_fail)])
    assert await runner.run("boom", "{}") == "（工具執行失敗）"
    assert await runner.run("web", "{}") == "（查詢失敗：查詢服務的額度不足）"


# --- 查資料工具 ---

def web_tools(api, **kw):
    return build_web_tools(make_web(api), **kw)


async def test_web_search_tool_formats_and_allows_result_urls():
    runner = ToolRunner(web_tools(FakeWebAPI(search=[(200, SEARCH_OK)])), question="6th Live 什麼時候？")
    out = await runner.run("web_search", json.dumps({"query": "蓮之空 6th Live"}))
    assert out.startswith(TOOL_RESULT_NOTE)
    assert "[1] 蓮之空 6th Live 公告（Example，2026-10-01）" in out and "網址：https://example.com/live6" in out
    assert runner.url_allowed("https://example.com/live6")


async def test_fetch_only_allowed_urls():
    api = FakeWebAPI(fetch=[(200, {"title": "公告", "content": "內文 https://evil.example/next"})])
    runner = ToolRunner(web_tools(api))
    runner.allow_urls_from("KO: 幫我看 https://example.com/news。")
    # 對話中沒出現過的網址、或在網址後面夾帶資料，都不能讀（避免資料外洩）
    assert "只能讀取" in await runner.run("fetch_page", json.dumps({"url": "https://evil.example/?q=對話內容"}))
    assert "只能讀取" in await runner.run("fetch_page", json.dumps({"url": "https://example.com/news?leak=1"}))
    assert api.calls == []
    out = await runner.run("fetch_page", json.dumps({"url": "https://example.com/news"}))
    assert "標題：公告" in out and out.startswith(TOOL_RESULT_NOTE)
    # 網頁內文裡的連結不會加進可讀清單
    assert not runner.url_allowed("https://evil.example/next")


async def test_fetch_rejects_bad_url_without_calling_api():
    api = FakeWebAPI(fetch=[])
    runner = ToolRunner(web_tools(api))
    assert "網址格式不正確" in await runner.run("fetch_page", json.dumps({"url": "file:///etc/passwd"}))
    assert api.calls == []


async def test_tool_calls_and_refusals_are_logged(caplog):
    api = FakeWebAPI(search=[(200, SEARCH_OK)], fetch=[(200, {"title": "公告", "content": "內文"})])
    runner = ToolRunner(web_tools(api), max_calls=3)
    runner.allow_urls_from("KO: 幫我看 https://example.com/news")
    with caplog.at_level(logging.INFO, logger="bot.tools"):
        await runner.run("web_search", json.dumps({"query": "蓮之空 6th Live"}))
        await runner.run("fetch_page", json.dumps({"url": "https://github.com/x/y"}))
        await runner.run("fetch_page", json.dumps({"url": "https://example.com/news"}))
        await runner.run("fetch_page", json.dumps({"url": "https://example.com/news"}))  # 次數用完
    text = caplog.text
    assert "工具 web_search（第 1/3 次）：蓮之空 6th Live" in text and "搜尋「蓮之空 6th Live」：2 筆結果" in text
    assert "讀取網頁被拒：網址沒有在對話或搜尋結果中出現過（https://github.com/x/y）" in text
    assert "讀取網頁 https://example.com/news：2 字" in text
    assert "工具 fetch_page 呼叫被拒：這次回覆已用完 3 次查詢" in text


async def test_web_error_is_logged(caplog):
    runner = ToolRunner(web_tools(FakeWebAPI(search=[(500, {"error": "boom"})] * 3)))
    with caplog.at_level(logging.WARNING, logger="bot.tools"):
        out = await runner.run("web_search", json.dumps({"query": "x"}))
    assert out.startswith("（查詢失敗") and "工具 web_search 查詢失敗" in caplog.text


async def test_fetch_truncates_long_pages():
    api = FakeWebAPI(fetch=[(200, {"title": "長文", "content": "字" * 100})])
    runner = ToolRunner(web_tools(api, fetch_max_chars=10))
    runner.allow_urls_from("https://example.com/long")
    out = await runner.run("fetch_page", json.dumps({"url": "https://example.com/long"}))
    assert "字" * 10 + "\n（內容過長，只顯示前 10 字）" in out and "字" * 11 not in out


# --- 工具迴圈 ---

async def test_chat_with_tools_loop(make_service, fake_openai):
    svc = make_service()
    runner = ToolRunner(web_tools(FakeWebAPI(search=[(200, SEARCH_OK)])), max_calls=3)
    fake_openai.completions.replies = [
        {"content": None, "tool_calls": [("web_search", {"query": "蓮之空 6th Live"})]},
        "查到了，6th Live 預定 2027 年舉辦喔。",
    ]
    text = await svc.llm.chat_with_tools([{"role": "user", "content": "6th Live？"}], runner, max_rounds=3)
    assert text == "查到了，6th Live 預定 2027 年舉辦喔。"
    first, second = fake_openai.completions.calls
    assert first["tools"] == runner.specs() and first["tool_choice"] == "auto"
    assert first["extra_body"] == {"thinking": {"type": "disabled"}}
    assistant, tool = second["messages"][-2:]
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["function"]["name"] == "web_search"
    assert tool == {"role": "tool", "tool_call_id": "call_0", "content": tool["content"]}
    assert "蓮之空 6th Live 公告" in tool["content"]


async def test_chat_with_tools_forces_answer_after_max_rounds(make_service, fake_openai):
    svc = make_service()
    runner = ToolRunner([echo_tool()], max_calls=5)
    fake_openai.completions.replies = [{"tool_calls": [("echo", {})]}, {"tool_calls": [("echo", {})]}, "最終回答"]
    assert await svc.llm.chat_with_tools([{"role": "user", "content": "?"}], runner, max_rounds=2) == "最終回答"
    assert [c["tool_choice"] for c in fake_openai.completions.calls] == ["auto", "auto", "none"]


async def test_chat_with_tools_checks_budget_each_round(make_service, fake_openai, storage):
    svc = make_service(daily_cost_budget=0.5, daily_token_budget=0)

    async def spend(args, runner):
        storage.record_usage(svc.llm.today(), "deepseek-search", "web_search", 0, 0, 0, 0, cost=1.0)
        return "結果"
    runner = ToolRunner([Tool("spend", "", {}, spend)])
    fake_openai.completions.replies = [{"tool_calls": [("spend", {})]}, "不會走到這裡"]
    with pytest.raises(BudgetExceeded):
        await svc.llm.chat_with_tools([{"role": "user", "content": "?"}], runner)
    assert len(fake_openai.completions.calls) == 1


# --- 費用 ---

@pytest.mark.parametrize("currency,price", [("USD", 0.00075), ("CNY", 0.005)])
def test_web_request_cost(storage, fake_openai, currency, price):
    llm = LLMClient(make_config(price_currency=currency), storage, client=fake_openai)
    llm.record_web_request("search")
    llm.record_web_request("fetch")
    assert llm.cost_today() == pytest.approx(2 * price)
    assert {r.model: r.calls for r in llm.usage_today()} == {"deepseek-fetch": 1, "deepseek-search": 1}


# --- 與聊天流程整合 ---

def tool_service(make_service, api, **cfg):
    cfg.setdefault("web_tools_enabled", True)
    svc = make_service(**cfg)
    web = DeepSeekWeb(KEY, post=api, on_billable=svc.llm.record_web_request)
    svc.web_tools = build_web_tools(web)
    return svc


async def test_tools_off_by_default(make_service, fake_openai):
    assert Config.from_env({}).web_tools_enabled is False
    svc = make_service()
    await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    call = fake_openai.completions.calls[0]
    assert "tools" not in call and all(m["content"] != WEB_TOOLS_GUIDE for m in call["messages"])


async def test_tools_need_both_flag_and_tools(make_service, fake_openai):
    svc = make_service(web_tools_enabled=False)
    svc.web_tools = build_web_tools(make_web(FakeWebAPI()))
    await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    assert "tools" not in fake_openai.completions.calls[0]


async def test_full_search_flow(make_service, fake_openai, storage):
    svc = tool_service(make_service, FakeWebAPI(search=[(200, SEARCH_OK)]))
    fake_openai.completions.replies = [
        {"tool_calls": [("web_search", {"query": "蓮之空 6th Live"})]},
        "查到了！6th Live 預定 2027 年，來源：https://example.com/live6",
    ]
    reply = await svc.handle_message(make_message("幫我查 6th Live 什麼時候", mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == ["查到了！6th Live 預定 2027 年，來源：<https://example.com/live6>"]
    first = fake_openai.completions.calls[0]["messages"]
    guide_at = [m["content"] for m in first].index(WEB_TOOLS_GUIDE)
    assert all(m["role"] == "system" for m in first[:guide_at + 1])  # 工具說明緊接在人設之後
    assert first[guide_at - 1]["content"].startswith("你是傲嬌大小姐。\n\n")
    search = next(r for r in svc.llm.usage_today() if r.model == "deepseek-search")
    assert search.calls == 1 and search.cost == pytest.approx(0.00075)  # 搜尋費用記入每日預算
    assert storage.tool_uses_on(svc.llm.today(), 1) == 1
    assert storage.recent_messages(CH, 10)[-1].content.endswith("<https://example.com/live6>")


async def test_url_from_conversation_can_be_fetched(make_service, fake_openai):
    api = FakeWebAPI(fetch=[(200, {"title": "公告", "content": "活動延期到 12 月。"})])
    svc = tool_service(make_service, api)
    fake_openai.completions.replies = [{"tool_calls": [("fetch_page", {"url": "https://example.com/news"})]}, "延期到 12 月了。"]
    reply = await svc.handle_message(make_message("這篇在說什麼？https://example.com/news", mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == ["延期到 12 月了。"]
    assert len(api.calls) == 1


async def test_daily_limit_per_user(make_service, fake_openai, storage, clock):
    api = FakeWebAPI(search=[(200, SEARCH_OK)])
    svc = tool_service(make_service, api, web_daily_limit_per_user=1)
    fake_openai.completions.replies = [{"tool_calls": [("web_search", {"query": "a"})]}, "好的",
                                       {"tool_calls": [("web_search", {"query": "b"})]}, "今天查不了了"]
    await svc.handle_message(make_message("查 a", mentions=[BOT_ID]), BOT_ID)
    clock.t += 10
    await svc.handle_message(make_message("查 b", mentions=[BOT_ID]), BOT_ID)
    assert len(api.calls) == 1
    assert "今天的查詢次數已用完" in fake_openai.completions.calls[-1]["messages"][-1]["content"]


async def test_interject_reply_cannot_use_tools(make_service, fake_openai, clock):
    svc = tool_service(make_service, FakeWebAPI(), random_interject_rate=0.5)
    svc.clock = clock
    fake_openai.completions.replies = ["是", "插嘴"]
    reply = await svc.handle_message(make_message("今天好累"), BOT_ID)
    judge, answer = fake_openai.completions.calls
    assert reply.chunks == ["插嘴"]
    # 判斷與回覆都帶工具定義但禁止呼叫，prompt 開頭和一般聊天一致（命中快取）
    assert judge["tools"] == answer["tools"] and judge["tool_choice"] == answer["tool_choice"] == "none"
    assert judge["messages"][:-1] == answer["messages"][:-1]


async def test_deep_does_not_use_tools(make_service, fake_openai):
    svc = tool_service(make_service, FakeWebAPI())
    await svc.deep(CH, "KO", "幫我查最新消息")
    call = fake_openai.completions.calls[0]
    assert "tools" not in call and call["extra_body"] == {"thinking": {"type": "enabled"}}


async def test_usage_shows_web_requests(storage, fake_openai, persona_file):
    config = make_config(daily_cost_budget=1.0, daily_token_budget=0)
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI))
    bot = PersonaBot(config, storage, llm, Persona(persona_file))
    try:
        llm.record_web_request("search")
        llm.record_web_request("search")
        llm.record_web_request("fetch")
        text = format_usage(bot)
        assert "- 搜尋：2 次，約 $0.0015" in text and "- 讀取網頁：1 次，約 $0.0008" in text
    finally:
        await bot.close()


def test_config_web_settings():
    cfg = Config.from_env({"WEB_TOOLS_ENABLED": "true", "WEB_MAX_TOOL_CALLS_PER_REPLY": "2",
                           "WEB_DAILY_LIMIT_PER_USER": "5", "WEB_SEARCH_RESULTS": "8", "WEB_FETCH_MAX_CHARS": "3000"})
    assert cfg.web_tools_enabled and cfg.web_max_tool_calls_per_reply == 2 and cfg.web_daily_limit_per_user == 5
    assert cfg.web_search_results == 8 and cfg.web_fetch_max_chars == 3000
    assert any("WEB_SEARCH_RESULTS" in p for p in Config.from_env({"WEB_SEARCH_RESULTS": "99"}).validate())


async def test_bot_passes_web_tools(storage, fake_openai, persona_file):
    tools = build_web_tools(make_web(FakeWebAPI()))
    config = make_config(web_tools_enabled=True)
    bot = PersonaBot(config, storage, LLMClient(config, storage, client=fake_openai), Persona(persona_file),
                     web_tools=tools)
    try:
        assert bot.chat.tools_enabled and bot.chat.web_tools == tools
    finally:
        await bot.close()
