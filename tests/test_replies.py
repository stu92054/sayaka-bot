"""replies.toml：固定訊息外部化。"""
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.commands import reload_persona_and_replies
from bot.llm import LLMClient
from bot.main import PersonaBot
from bot.persona import Persona
from bot.replies import DEFAULT_REPLIES, Replies

from .conftest import BOT_ID, TAIPEI, make_config, make_message

ROOT = Path(__file__).resolve().parent.parent

# 原本寫死在程式裡的大小姐口吻
OLD_CHARACTER_WORDS = ("本小姐", "大小姐", "哼")


def write(tmp_path, text, name="replies.toml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_are_neutral():
    for key, value in DEFAULT_REPLIES.items():
        assert value.strip(), key
        assert not any(w in value for w in OLD_CHARACTER_WORDS), (key, value)
        assert "沙耶香" not in value, (key, value)


def test_no_character_lines_left_in_code():
    for path in (ROOT / "bot").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for w in ("本小姐", "大小姐"):
            assert w not in text, f"{path.name} 還有「{w}」"


def test_shipped_replies_toml_loads_cleanly():
    r = Replies(ROOT / "characters/sayaka/replies.toml")
    assert r.warnings == []
    assert r["budget_exceeded"] == "今天已經說了很多話了……我們明天再繼續吧。"
    assert r["error"] == "對不起，我這邊好像出了點問題。請稍等一下再跟我說話。"
    assert r["content_filter"] == "這個話題……請容我不回答。"
    assert r["not_admin"] == "不好意思，這個指令只有管理員才能使用。"
    assert r["not_allowed"] == "對不起，我在這裡沒辦法回應。"
    assert r["cooldown"] == "請稍等一下，一個一個來。"
    assert r["reset"] == "好的，剛才的對話我已經整理乾淨了。"
    assert r["deep_description"] == "請村野さやか認真思考後回答一個問題（使用深度模型）"


def sayaka_persona(scene="discord"):
    from bot.config import Config
    c = Config.from_env({"CHARACTER_DIR": str(ROOT / "characters/sayaka")})
    return Persona(c.persona_path, names=(c.bot_display_name, *c.bot_aliases), scene=scene)


def test_shipped_persona_is_sayaka():
    p = sayaka_persona()
    assert "村野沙耶香" in p.base
    assert "大小姐" not in p.text and "本小姐" not in p.text
    assert "## 不要做的事" in p.base and "## 對話格式" in p.text


def test_persona_holds_only_the_character():
    """persona.md 只寫角色本身；在哪裡聊、對方是誰都在情境與 audience，換情境時不會互相蓋過。"""
    for key in ("sayaka", "kosuzu", "tsuzuri", "kozue", "ginko", "kaho"):
        base = (ROOT / f"characters/{key}/persona.md").read_text(encoding="utf-8")
        for word in ("Discord", "群組成員", "/deep", "@everyone", "## 對話格式", "台灣"):
            assert word not in base, (key, word)
    discord, web = sayaka_persona().text, sayaka_persona("web").text
    assert "Discord" in discord and "/deep" in discord and "台灣的蓮之空粉絲" in discord
    assert "Discord" not in web and "/deep" not in web and "一對一" in web
    group = sayaka_persona().compose("group", "使用者「KO」的身分：教練", others="小鈴", who="KO",
                                     situation="LINE 群組聊天", time="跟現實一樣", play="一般聊天")
    assert "LINE 群組" in group and "Discord" not in group and "教練" in group and "{" not in group
    # 拆分後曾經漏掉「顯示名稱就是對方的名字」，角色改叫範例裡的 KO 或說不知道對方叫什麼
    for text in (discord, web, group):
        assert "顯示名稱就是對方的名字" in text


def test_persona_handles_japanese_name_and_collisions():
    text = sayaka_persona().text
    # 改用日文名後，回覆開頭也不能加上日文名字
    assert "「村野さやか:」" in text and "「さやか:」" in text
    # 暱稱剛好和她同名的成員：照常稱呼、不戒備
    assert "不要把「（成員暱稱）」說出來" in text and "不要因此對他特別戒備" in text


def test_missing_file_uses_defaults_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        r = Replies(tmp_path / "nope.toml")
    assert r["error"] == DEFAULT_REPLIES["error"]
    assert any("找不到" in w for w in r.warnings)
    assert "找不到" in caplog.text


def test_missing_keys_use_defaults_with_warning(tmp_path, caplog):
    p = write(tmp_path, 'error = "自訂錯誤"\n')
    with caplog.at_level(logging.WARNING):
        r = Replies(p)
    assert r["error"] == "自訂錯誤"
    assert r["reset"] == DEFAULT_REPLIES["reset"]
    assert any("缺少 reset" in w for w in r.warnings)
    assert "缺少 reset" in caplog.text


def test_invalid_values_and_unknown_keys(tmp_path):
    p = write(tmp_path, 'error = 123\nreset = "   "\ntypo_key = "x"\n')
    r = Replies(p)
    assert r["error"] == DEFAULT_REPLIES["error"] and r["reset"] == DEFAULT_REPLIES["reset"]
    assert any("error 必須是非空字串" in w for w in r.warnings)
    assert any("未知的項目 typo_key" in w for w in r.warnings)


def test_long_description_truncated(tmp_path):
    p = write(tmp_path, f'deep_description = "{"長" * 150}"\n')
    r = Replies(p)
    assert len(r["deep_description"]) == 100
    assert any("截斷" in w for w in r.warnings)


def test_broken_toml_at_startup_uses_defaults(tmp_path):
    r = Replies(write(tmp_path, "error = 沒有引號\n"))
    assert r["error"] == DEFAULT_REPLIES["error"]
    assert any("TOMLDecodeError" in w for w in r.warnings)


def test_broken_toml_on_reload_keeps_previous(tmp_path):
    p = write(tmp_path, 'error = "第一版"\n')
    r = Replies(p)
    p.write_text("error = 壞掉了\n", encoding="utf-8")
    warnings = r.reload()
    assert r["error"] == "第一版"
    assert any("保留目前的訊息" in w for w in warnings)


def test_reload_picks_up_changes(tmp_path):
    p = write(tmp_path, 'error = "舊"\n')
    r = Replies(p)
    p.write_text('error = "新"\n', encoding="utf-8")
    r.reload()
    assert r["error"] == "新"
    assert r.error == "新"  # 屬性存取


async def test_chat_uses_replies(make_service, fake_openai, storage, tmp_path):
    svc = make_service(daily_token_budget=10)
    svc.replies = Replies(write(tmp_path, 'budget_exceeded = "明天再說"\nerror = "壞了"\ncontent_filter = "不回答"\n'))
    storage.record_usage(svc.llm.today(), "deepseek-flash", "chat", 10, 0, 0, 10)
    reply = await svc.handle_message(make_message("大小姐"), BOT_ID)
    assert reply.chunks == ["明天再說"]


async def test_chat_error_and_filter_replies(make_service, fake_openai, tmp_path, clock):
    svc = make_service()
    svc.replies = Replies(write(tmp_path, 'error = "壞了"\ncontent_filter = "不回答"\n'))
    fake_openai.completions.replies = [("", "content_filter")]
    assert (await svc.handle_message(make_message("大小姐"), BOT_ID)).chunks == ["不回答"]
    clock.t += 10
    fake_openai.completions.default = RuntimeError("down")
    assert (await svc.handle_message(make_message("大小姐"), BOT_ID)).chunks == ["壞了"]


# --- 與 Discord client / slash commands 的整合 ---

def make_bot(storage, fake_openai, persona_file, replies=None, **cfg):
    config = make_config(**cfg)
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI))
    return PersonaBot(config, storage, llm, Persona(persona_file), replies)


def make_interaction(user_id=1, guild_id=111):
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
        user=SimpleNamespace(id=user_id, display_name="KO", name="KO"),
        channel_id=500,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


async def test_command_descriptions_come_from_replies(storage, fake_openai, persona_file):
    bot = make_bot(storage, fake_openai, persona_file, Replies(ROOT / "characters/sayaka/replies.toml"))
    try:
        assert bot.tree.get_command("deep").description == "請村野さやか認真思考後回答一個問題（使用深度模型）"
        assert bot.tree.get_command("reset").description == "清除本頻道的對話歷史與摘要（管理員）"
    finally:
        await bot.close()


async def test_command_replies_come_from_replies(storage, fake_openai, persona_file):
    bot = make_bot(storage, fake_openai, persona_file, Replies(ROOT / "characters/sayaka/replies.toml"))
    try:
        # 非管理員用 /reset
        it = make_interaction(user_id=1)
        await bot.tree.get_command("reset").callback(it)
        it.response.send_message.assert_awaited_once_with("不好意思，這個指令只有管理員才能使用。", ephemeral=True)
        # 不在允許的伺服器
        it = make_interaction(guild_id=999)
        await bot.tree.get_command("deep").callback(it, question="hi")
        it.response.send_message.assert_awaited_once_with("對不起，我在這裡沒辦法回應。", ephemeral=True)
        # 管理員 /reset
        it = make_interaction(user_id=7)
        await bot.tree.get_command("reset").callback(it)
        it.response.send_message.assert_awaited_once_with("好的，剛才的對話我已經整理乾淨了。", ephemeral=True)
        # /deep 冷卻中
        it = make_interaction(user_id=2)
        await bot.tree.get_command("deep").callback(it, question="第一題")
        it = make_interaction(user_id=2)
        await bot.tree.get_command("deep").callback(it, question="第二題")
        it.response.send_message.assert_awaited_once_with("請稍等一下，一個一個來。", ephemeral=True)
    finally:
        await bot.close()


async def test_persona_reload_reloads_both(storage, fake_openai, persona_file, tmp_path):
    rp = write(tmp_path, 'error = "舊"\n')
    bot = make_bot(storage, fake_openai, persona_file, Replies(rp))
    try:
        persona_file.write_text("新人設", encoding="utf-8")
        rp.write_text('error = "新"\n', encoding="utf-8")
        it = make_interaction(user_id=7)
        await bot.tree.get_command("persona_reload").callback(it)
        msg = it.response.send_message.await_args.args[0]
        assert "已重新載入人設（3 字）" in msg
        assert "缺少" in msg  # 其他項目缺少的警告會回報給管理員
        assert "重啟" in msg
        assert bot.persona.base == "新人設"
        assert bot.replies["error"] == "新" and bot.chat.replies["error"] == "新"
    finally:
        await bot.close()


async def test_persona_reload_failure_keeps_old_persona(storage, fake_openai, persona_file, tmp_path):
    bot = make_bot(storage, fake_openai, persona_file, Replies(ROOT / "characters/sayaka/replies.toml"))
    try:
        persona_file.write_text("", encoding="utf-8")
        msg = reload_persona_and_replies(bot)
        assert "人設讀取失敗" in msg and "已重新載入固定訊息" in msg
        assert bot.persona.base == "你是傲嬌大小姐。"
    finally:
        await bot.close()
