from bot.llm import BUDGET_EXCEEDED_REPLY, ERROR_REPLY
from bot.triggers import Trigger

from .conftest import BOT_ID, OTHER_GUILD_ID, make_message


async def test_mention_gets_reply_and_both_sides_recorded(make_service, fake_openai, storage):
    svc = make_service()
    reply = await svc.handle_message(make_message("大小姐早安"), BOT_ID)
    assert reply.trigger is Trigger.NAME
    assert reply.chunks == [fake_openai.completions.default]
    call = fake_openai.completions.calls[0]
    assert call["model"] == "deepseek-flash"
    assert call["max_tokens"] == 600
    assert call["messages"][0] == {"role": "system", "content": "你是傲嬌大小姐。"}
    assert call["messages"][-1] == {"role": "user", "content": "KO: 大小姐早安"}
    recent = storage.recent_messages(500, 10)
    assert [(m.content, m.is_bot) for m in recent] == [("大小姐早安", False), (fake_openai.completions.default, True)]


async def test_untriggered_message_recorded_but_not_answered(make_service, fake_openai, storage):
    svc = make_service()
    assert await svc.handle_message(make_message("今天好累", author_name="Amy"), BOT_ID) is None
    assert fake_openai.completions.calls == []
    assert storage.recent_messages(500, 10)[0].content == "今天好累"
    await svc.handle_message(make_message("大小姐", mentions=[BOT_ID]), BOT_ID)
    msgs = fake_openai.completions.calls[0]["messages"]
    assert msgs[-1]["content"] == "Amy: 今天好累\nKO: 大小姐"


async def test_bot_and_foreign_guild_messages_not_recorded(make_service, storage):
    svc = make_service()
    assert await svc.handle_message(make_message("大小姐", bot=True), BOT_ID) is None
    assert await svc.handle_message(make_message("大小姐", guild_id=OTHER_GUILD_ID), BOT_ID) is None
    assert storage.count_messages(500) == 0


async def test_cooldown_blocks_second_trigger(make_service, fake_openai, clock):
    svc = make_service()
    assert await svc.handle_message(make_message("大小姐"), BOT_ID)
    assert await svc.handle_message(make_message("大小姐？"), BOT_ID) is None
    assert len(fake_openai.completions.calls) == 1
    # 其他人不受影響
    assert await svc.handle_message(make_message("大小姐", author_id=2), BOT_ID)
    clock.t += 5
    assert await svc.handle_message(make_message("大小姐"), BOT_ID)


async def test_budget_exceeded_returns_fixed_message(make_service, fake_openai, storage):
    svc = make_service(daily_token_budget=50)
    storage.record_usage(svc.llm.today(), "deepseek-flash", "chat", 50, 0, 0, 50)
    reply = await svc.handle_message(make_message("大小姐"), BOT_ID)
    assert reply.chunks == [BUDGET_EXCEEDED_REPLY]
    assert not reply.recorded
    assert fake_openai.completions.calls == []
    assert all(not m.is_bot for m in storage.recent_messages(500, 10))


async def test_budget_reached_mid_day(make_service, fake_openai, clock):
    svc = make_service(daily_token_budget=200)  # 每次呼叫 120 tokens
    assert (await svc.handle_message(make_message("大小姐", author_id=1), BOT_ID)).recorded
    assert (await svc.handle_message(make_message("大小姐", author_id=2), BOT_ID)).recorded
    reply = await svc.handle_message(make_message("大小姐", author_id=3), BOT_ID)
    assert reply.chunks == [BUDGET_EXCEEDED_REPLY]
    assert len(fake_openai.completions.calls) == 2


async def test_api_failure_returns_fixed_message(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.default = RuntimeError("boom")
    reply = await svc.handle_message(make_message("大小姐"), BOT_ID)
    assert reply.chunks == [ERROR_REPLY]
    assert len(fake_openai.completions.calls) == 3


async def test_long_reply_is_split(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.default = "哼" * 4100
    reply = await svc.handle_message(make_message("大小姐"), BOT_ID)
    assert len(reply.chunks) == 3 and all(len(c) <= 2000 for c in reply.chunks)


async def test_dm(make_service, fake_openai):
    svc = make_service()
    assert await svc.handle_message(make_message("hi", guild_id=None), BOT_ID, dm_allowed=False) is None
    reply = await svc.handle_message(make_message("hi", guild_id=None), BOT_ID, dm_allowed=True)
    assert reply.trigger is Trigger.DM


async def test_interject_judge_no(make_service, fake_openai):
    svc = make_service(random_interject_rate=0.5)  # rng 固定回 0.0
    fake_openai.completions.replies = ["否"]
    assert await svc.handle_message(make_message("今天好累"), BOT_ID) is None
    assert len(fake_openai.completions.calls) == 1
    assert fake_openai.completions.calls[0]["max_tokens"] == 5
    assert fake_openai.completions.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    # 判斷為否不消耗冷卻
    assert svc.cooldown.remaining(1) == 0


async def test_interject_judge_yes(make_service, fake_openai):
    svc = make_service(random_interject_rate=0.5)
    fake_openai.completions.replies = ["是", "插嘴內容"]
    reply = await svc.handle_message(make_message("今天好累"), BOT_ID)
    assert reply.trigger is Trigger.INTERJECT and reply.chunks == ["插嘴內容"]
    assert [c["model"] for c in fake_openai.completions.calls] == ["deepseek-flash", "deepseek-flash"]


async def test_deep_uses_deep_model(make_service, fake_openai, storage):
    svc = make_service()
    reply = await svc.deep(500, "KO", "為什麼天空是藍的")
    assert fake_openai.completions.calls[0]["model"] == "deepseek-v4-pro"
    assert reply.recorded
    assert storage.count_messages(500) == 2


async def test_attachments_are_described(make_service, storage):
    svc = make_service()
    await svc.handle_message(make_message("", attachments=["cat.png"]), BOT_ID)
    assert storage.recent_messages(500, 1)[0].content == "[附件:cat.png]"


async def test_persona_reload(make_service, fake_openai, persona_file):
    svc = make_service()
    persona_file.write_text("新的人設", encoding="utf-8")
    svc.persona.reload()
    await svc.handle_message(make_message("大小姐"), BOT_ID)
    assert fake_openai.completions.calls[0]["messages"][0]["content"] == "新的人設"
