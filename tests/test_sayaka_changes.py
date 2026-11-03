"""名字觸發可關閉、BOT_DISPLAY_NAME、/deep 不 ping 人、不可重試錯誤。"""
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import openai
import pytest

from bot.config import Config
from bot.llm import LLMClient, LLMError
from bot.main import PersonaBot
from bot.persona import Persona
from bot.storage import Storage
from bot.triggers import Trigger, detect_trigger

from .conftest import BOT_ID, TAIPEI, make_config, make_message

# --- 名字觸發 ---

def test_bot_names_default_empty():
    assert Config.from_env({}).bot_names == ()


@pytest.mark.parametrize("raw", ["", "   ", "          # 逗號分隔", "# 說明"])
def test_bot_names_blank_means_disabled(raw):
    assert Config.from_env({"BOT_NAMES": raw}).bot_names == ()


def test_name_trigger_disabled_when_empty():
    cfg = make_config(bot_names=())
    assert detect_trigger(make_message("沙耶香さん今天的 live 好棒"), BOT_ID, cfg, rng=lambda: 0.99) is None
    assert detect_trigger(make_message("さやか"), BOT_ID, cfg, rng=lambda: 0.99) is None
    # @mention、回覆、私訊照常觸發
    assert detect_trigger(make_message("hi", mentions=[BOT_ID]), BOT_ID, cfg) is Trigger.MENTION
    assert detect_trigger(make_message("hi", reply_to_author_id=BOT_ID), BOT_ID, cfg) is Trigger.REPLY
    assert detect_trigger(make_message("hi", guild_id=None), BOT_ID, cfg, dm_allowed=True) is Trigger.DM


def test_custom_nickname_still_triggers():
    cfg = Config.from_env({"BOT_NAMES": "小鐵人", "ALLOWED_GUILD_IDS": "111"})
    assert detect_trigger(make_message("小鐵人在嗎"), BOT_ID, cfg, rng=lambda: 0.99) is Trigger.NAME


def test_bot_display_name_config():
    assert Config.from_env({}).bot_display_name == "沙耶香"
    assert Config.from_env({"BOT_DISPLAY_NAME": "さやか"}).bot_display_name == "さやか"


async def test_bot_display_name_used_in_history_and_summary(make_service, fake_openai, storage):
    svc = make_service(bot_names=(), bot_display_name="沙耶香", history_max_messages=2)
    await svc.handle_message(make_message("早安", mentions=[BOT_ID]), BOT_ID)
    bot_msgs = [m for m in storage.recent_messages(500, 10) if m.is_bot]
    assert [m.author_name for m in bot_msgs] == ["沙耶香"]
    svc.history.add(500, "KO", "再一則")
    fake_openai.completions.replies = ["摘要"]
    await svc.history.maybe_summarize(500)
    assert "沙耶香（bot）:" in fake_openai.completions.calls[-1]["messages"][1]["content"]


# --- /deep 不替使用者 ping 人 ---

async def test_deep_never_allows_mentions(storage, fake_openai, persona_file):
    config = make_config()
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI))
    bot = PersonaBot(config, storage, llm, Persona(persona_file))
    fake_openai.completions.default = "好的，我來回答 <@456>。"
    it = SimpleNamespace(
        guild=SimpleNamespace(id=111),
        user=SimpleNamespace(id=1, display_name="KO", name="KO"),
        channel_id=500,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    try:
        await bot.tree.get_command("deep").callback(it, question="<@123> 你覺得呢")
        assert it.followup.send.await_count >= 1
        first = it.followup.send.await_args_list[0]
        assert "<@123>" in first.args[0]  # 引用了含提及的原文
        for call in it.followup.send.await_args_list:
            am = call.kwargs["allowed_mentions"]
            assert am.users is False and am.roles is False and am.everyone is False and am.replied_user is False
            assert am.to_dict() == {"parse": []}
    finally:
        await bot.close()


# --- 不可重試的錯誤 ---

REQ = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")


def status_error(cls, code):
    return cls("boom sk-secret-deepseek-key", response=httpx2.Response(code, request=REQ), body=None)


@pytest.mark.parametrize("cls,code", [
    (openai.AuthenticationError, 401),
    (openai.PermissionDeniedError, 403),
    (openai.BadRequestError, 400),
    (openai.NotFoundError, 404),
    (openai.UnprocessableEntityError, 422),
])
async def test_non_retryable_errors_fail_fast(make_service, fake_openai, caplog, cls, code):
    svc = make_service()
    fake_openai.completions.default = status_error(cls, code)
    with caplog.at_level(logging.WARNING), pytest.raises(LLMError) as ei:
        await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert len(fake_openai.completions.calls) == 1
    assert svc.llm.sleeps == []
    assert cls.__name__ in str(ei.value) and str(code) in str(ei.value)
    assert cls.__name__ in caplog.text and f"HTTP {code}" in caplog.text
    assert "sk-secret-deepseek-key" not in caplog.text  # 不印出錯誤訊息內容（可能帶 key）


@pytest.mark.parametrize("exc", [
    status_error(openai.RateLimitError, 429),
    status_error(openai.InternalServerError, 500),
    status_error(openai.InternalServerError, 503),
    openai.APIConnectionError(request=REQ),
    openai.APITimeoutError(request=REQ),
])
async def test_retryable_errors_are_retried(make_service, fake_openai, exc):
    svc = make_service()
    fake_openai.completions.replies = [exc, exc, "成功"]
    assert await svc.llm.chat([{"role": "user", "content": "hi"}]) == "成功"
    assert len(fake_openai.completions.calls) == 3
    assert svc.llm.sleeps == [1.0, 2.0]


async def test_non_retryable_error_gives_error_reply(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.default = status_error(openai.AuthenticationError, 401)
    reply = await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == [svc.replies["error"]]
    assert len(fake_openai.completions.calls) == 1


def test_sdk_builtin_retries_disabled():
    cfg = make_config()
    llm = LLMClient(cfg, Storage(":memory:"))
    assert llm.client.max_retries == 0


# --- 一般回覆也不允許提及 ---

class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_normal_replies_never_allow_mentions(storage, fake_openai, persona_file):
    config = make_config()
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI))
    bot = PersonaBot(config, storage, llm, Persona(persona_file))
    bot._connection.user = SimpleNamespace(id=BOT_ID)  # 模擬已登入
    # 模型照著使用者要求輸出提及，而且回覆長到需要分段
    fake_openai.completions.default = "<@123> <@&999> @everyone " + "好" * 2100
    channel = SimpleNamespace(id=500, typing=lambda: _Typing(), send=AsyncMock(), fetch_message=AsyncMock())
    msg = make_message("請幫我叫 <@123>", mentions=[BOT_ID])
    msg.channel = channel
    msg.reply = AsyncMock()
    try:
        await bot.on_message(msg)
        msg.reply.assert_awaited_once()
        assert msg.reply.await_args.kwargs["mention_author"] is False
        calls = [msg.reply.await_args] + list(channel.send.await_args_list)
        assert len(calls) == 2  # 第一段用 reply，第二段用 channel.send
        for call in calls:
            assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    finally:
        await bot.close()


async def test_client_default_allowed_mentions_is_none(storage, fake_openai, persona_file):
    bot = PersonaBot(make_config(), storage, LLMClient(make_config(), storage, client=fake_openai),
                     Persona(persona_file))
    try:
        am = bot.allowed_mentions
        assert am.to_dict() == {"parse": []} and am.replied_user is False
    finally:
        await bot.close()
