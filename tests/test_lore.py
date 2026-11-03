"""原作資料常駐 prompt，以及以費用計算的每日預算。"""
from datetime import datetime
from pathlib import Path

import pytest

from bot.commands import format_usage, reload_persona_and_replies
from bot.config import Config
from bot.history import build_prompt
from bot.llm import BudgetExceeded, LLMClient
from bot.lore import LORE_INSTRUCTIONS, Lore
from bot.main import PersonaBot
from bot.persona import Persona
from bot.replies import Replies

from .conftest import BOT_ID, TAIPEI, make_config, make_message

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lore_dir(tmp_path):
    d = tmp_path / "lore"
    d.mkdir()
    (d / "02_b.md").write_text("# 人物關係\n綴理先輩", encoding="utf-8")
    (d / "01_a.md").write_text("# 個人設定\n生日 1 月 13 日", encoding="utf-8")
    return d


# --- 載入 ---

def test_shipped_lore_loads():
    lore = Lore(ROOT / "lore")
    assert lore.warnings == []
    assert lore.files == ["01_個人設定.md", "02_人物關係.md", "03_重要事件時間線.md"]
    assert lore.text.startswith(LORE_INSTRUCTIONS)
    assert "<lore>" in lore.text and lore.text.endswith("</lore>")
    assert "ビッグボイス選手権" in lore.text
    # 指向未收錄台本的參考、內部路徑、台本統計都已移除
    for junk in ("scripts/", "D:\\", "story_main", "raw_wiki", "語料統計"):
        assert junk not in lore.text, junk
    assert 10_000 < len(lore.text) < 16_000


def test_instructions_say_reference_only():
    assert "只在有人問到原作" in LORE_INSTRUCTIONS
    assert "不要主動背誦" in LORE_INSTRUCTIONS
    assert "不要編造" in LORE_INSTRUCTIONS
    assert "沙耶香" not in LORE_INSTRUCTIONS  # 說明保持中性，角色內容只在 lore/ 檔案裡


def test_files_sorted_and_wrapped(lore_dir):
    lore = Lore(lore_dir)
    assert lore.files == ["01_a.md", "02_b.md"]
    assert lore.text.index('<file name="01_a.md">') < lore.text.index('<file name="02_b.md">')
    assert "生日 1 月 13 日\n</file>" in lore.text


def test_missing_dir_disables_lore(tmp_path):
    lore = Lore(tmp_path / "nope")
    assert not lore.enabled and lore.text == ""
    assert any("找不到" in w for w in lore.warnings)


def test_empty_files_skipped(tmp_path):
    d = tmp_path / "lore"
    d.mkdir()
    (d / "a.md").write_text("   ", encoding="utf-8")
    (d / "b.txt").write_text("不是 md", encoding="utf-8")
    lore = Lore(d)
    assert not lore.enabled
    assert any("a.md 是空的" in w for w in lore.warnings)


def test_reload_picks_up_changes_and_keeps_old_on_failure(lore_dir):
    lore = Lore(lore_dir)
    (lore_dir / "03_c.md").write_text("新資料", encoding="utf-8")
    lore.reload()
    assert lore.files == ["01_a.md", "02_b.md", "03_c.md"]
    old = lore.text
    for p in lore_dir.iterdir():
        p.unlink()
    lore_dir.rmdir()
    warnings = lore.reload()
    assert lore.text == old
    assert any("保留目前的原作資料" in w for w in warnings)


def test_lore_dir_config():
    assert Config.from_env({}).lore_dir == Path("lore")
    assert Config.from_env({"LORE_DIR": "none"}).lore_dir is None
    assert Config.from_env({"LORE_DIR": "/data/lore"}).lore_dir == Path("/data/lore")


# --- prompt 順序與快取 ---

def test_prompt_order_lore_persona_summary():
    msgs = build_prompt("PERSONA", "摘要", [], "KO", "hi", lore="LORE")
    assert msgs[0] == {"role": "system", "content": "LORE"}
    assert msgs[1] == {"role": "system", "content": "PERSONA"}
    assert msgs[2]["role"] == "system" and "摘要" in msgs[2]["content"]
    assert msgs[-1] == {"role": "user", "content": "KO: hi"}


def test_prompt_without_lore_unchanged():
    assert build_prompt("PERSONA", None, [], "KO", "hi") == build_prompt("PERSONA", None, [], "KO", "hi", lore=None)
    assert build_prompt("PERSONA", None, [], "KO", "hi", lore="")[0]["content"] == "PERSONA"


async def test_chat_sends_lore_first_and_stable(make_service, fake_openai, lore_dir, clock):
    svc = make_service()
    svc.lore = Lore(lore_dir)
    await svc.handle_message(make_message("早安", mentions=[BOT_ID]), BOT_ID)
    clock.t += 10
    await svc.deep(500, "KO", "妳跟綴理先輩是怎麼認識的？")
    first, second = (c["messages"] for c in fake_openai.completions.calls)
    assert first[0]["content"] == svc.lore.text
    assert first[1]["content"] == "你是傲嬌大小姐。"
    # 前兩則（原作資料＋persona）每次都相同 → 前綴快取可命中
    assert first[:2] == second[:2]


async def test_interject_judge_does_not_get_lore(make_service, fake_openai, lore_dir):
    svc = make_service(random_interject_rate=0.5)
    svc.lore = Lore(lore_dir)
    fake_openai.completions.replies = ["否"]
    await svc.handle_message(make_message("今天好累"), BOT_ID)
    judge = fake_openai.completions.calls[0]["messages"]
    assert all("<lore>" not in m["content"] for m in judge)


async def test_summary_does_not_get_lore(make_service, fake_openai, lore_dir):
    svc = make_service(history_max_messages=2)
    svc.lore = Lore(lore_dir)
    for i in range(3):
        svc.history.add(500, "KO", f"m{i}")
    fake_openai.completions.replies = ["摘要"]
    await svc.history.maybe_summarize(500)
    assert all("<lore>" not in m["content"] for m in fake_openai.completions.calls[-1]["messages"])


# --- /persona_reload 一併重新載入原作資料 ---

async def test_persona_reload_reloads_lore(storage, fake_openai, persona_file, lore_dir):
    config = make_config()
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI))
    bot = PersonaBot(config, storage, llm, Persona(persona_file), Replies(ROOT / "replies.toml"), Lore(lore_dir))
    try:
        (lore_dir / "03_c.md").write_text("新資料", encoding="utf-8")
        msg = reload_persona_and_replies(bot)
        assert "已重新載入原作資料（3 個檔案" in msg
        assert bot.chat.lore.files == ["01_a.md", "02_b.md", "03_c.md"]
    finally:
        await bot.close()


# --- 以費用計算的每日預算 ---

def test_cost_budget_config():
    cfg = Config.from_env({})
    assert cfg.daily_cost_budget == 1.0 and cfg.daily_token_budget == 0
    cfg = Config.from_env({"DAILY_COST_BUDGET": "12.5", "DAILY_TOKEN_BUDGET": "3000000"})
    assert cfg.daily_cost_budget == 12.5 and cfg.daily_token_budget == 3_000_000


async def test_cost_budget_blocks_api(make_service, fake_openai, storage):
    svc = make_service(daily_cost_budget=1.0, daily_token_budget=0)
    storage.record_usage(svc.llm.today(), "deepseek-v4-pro", "deep", 10, 10, 0, 10, cost=0.6)
    await svc.llm.chat([{"role": "user", "content": "hi"}])  # ¥0.6 < ¥1，可以呼叫
    storage.record_usage(svc.llm.today(), "deepseek-v4-pro", "deep", 10, 10, 0, 10, cost=0.5)
    with pytest.raises(BudgetExceeded) as ei:
        await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert "費用" in str(ei.value)
    assert len(fake_openai.completions.calls) == 1


async def test_cost_budget_counts_only_today(make_service, fake_openai, storage):
    svc = make_service(daily_cost_budget=1.0, daily_token_budget=0)
    storage.record_usage("2026-11-02", "deepseek-v4-pro", "deep", 10, 10, 0, 10, cost=99)
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert len(fake_openai.completions.calls) == 1


async def test_cached_tokens_barely_count_against_cost_budget(make_service, fake_openai):
    # 每次 1 萬 tokens 的原作資料幾乎全部命中快取：空閒時段 flash 約 ¥0.0002
    fake_openai.completions.usage = dict(prompt_tokens=12_000, completion_tokens=100,
                                         prompt_cache_hit_tokens=11_800, prompt_cache_miss_tokens=200)
    svc = make_service(daily_cost_budget=0.01, daily_token_budget=0)
    for _ in range(5):
        await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert len(fake_openai.completions.calls) == 5
    assert svc.llm.cost_today() < 0.01


async def test_both_budgets_disabled(make_service, fake_openai, storage):
    svc = make_service(daily_cost_budget=0, daily_token_budget=0)
    storage.record_usage(svc.llm.today(), "deepseek-v4-pro", "deep", 10**9, 10**9, 0, 10**9, cost=10**6)
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert len(fake_openai.completions.calls) == 1


async def test_cost_budget_gives_budget_reply(make_service, fake_openai, storage):
    svc = make_service(daily_cost_budget=0.5, daily_token_budget=0)
    storage.record_usage(svc.llm.today(), "deepseek-flash", "chat", 1, 1, 0, 1, cost=0.5)
    reply = await svc.handle_message(make_message("hi", mentions=[BOT_ID]), BOT_ID)
    assert reply.chunks == [svc.replies["budget_exceeded"]]


async def test_usage_shows_cost_budget(storage, fake_openai, persona_file):
    config = make_config(daily_cost_budget=5.0, daily_token_budget=0)
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI))
    bot = PersonaBot(config, storage, llm, Persona(persona_file))
    try:
        storage.record_usage(llm.today(), "deepseek-flash", "chat", 100, 10, 50, 50, cost=1.25)
        text = format_usage(bot)
        assert "費用：約 ¥1.2500 / 上限 ¥5（25.0%）" in text
        assert "總 tokens：110" in text and "上限 0" not in text
    finally:
        await bot.close()
