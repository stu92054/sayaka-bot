"""計價幣別：預設美元（英文版價目表），可切換人民幣。"""
import logging
from datetime import datetime

import pytest

from bot.commands import format_usage
from bot.config import PRICE_TABLES, Config, ModelPrice
from bot.llm import BudgetExceeded, LLMClient
from bot.main import PersonaBot
from bot.persona import Persona
from bot.storage import Storage

from .conftest import TAIPEI, make_config

PEAK = datetime(2026, 11, 3, 10, 0, tzinfo=TAIPEI)      # 週二 10:00（UTC 02:00）
OFFPEAK = datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI)  # 週二 12:30（UTC 04:30）
M = 1_000_000


def test_default_currency_is_usd():
    cfg = Config.from_env({})
    assert cfg.price_currency == "USD" and cfg.currency_symbol == "$"
    assert cfg.daily_cost_budget == 1.0


def test_usd_price_table_matches_official():
    usd = PRICE_TABLES["USD"]
    assert usd["deepseek-flash"] == ModelPrice(0.006, 0.3, 1.2)
    assert usd["deepseek-v4-pro"] == ModelPrice(0.044, 1.32, 3.96)
    assert usd["deepseek-v4-flash"] == usd["deepseek-flash"]  # 舊名稱以 flash 計價


def test_cny_still_available():
    cfg = Config.from_env({"PRICE_CURRENCY": "cny"})
    assert cfg.price_currency == "CNY" and cfg.currency_symbol == "¥"
    assert cfg.price_for("deepseek-flash") == ModelPrice(0.04, 2.0, 8.0)


def test_invalid_currency_rejected():
    with pytest.raises(ValueError):
        Config.from_env({"PRICE_CURRENCY": "JPY"})


def test_unknown_model_uses_flash_price_of_current_currency():
    assert make_config().price_for("some-new-model") == PRICE_TABLES["USD"]["deepseek-flash"]
    assert make_config(price_currency="CNY").price_for("x") == PRICE_TABLES["CNY"]["deepseek-flash"]


def test_price_table_override_applies_to_current_currency():
    cfg = Config.from_env({"PRICE_TABLE": "deepseek-flash:0.01:0.5:2"})
    assert cfg.price_for("deepseek-flash") == ModelPrice(0.01, 0.5, 2.0)
    assert cfg.price_for("deepseek-v4-pro") == PRICE_TABLES["USD"]["deepseek-v4-pro"]


@pytest.mark.parametrize("model,when,expected", [
    # 命中 1M + 未命中 1M + 輸出 1M
    ("deepseek-flash", PEAK, 0.006 + 0.3 + 1.2),
    ("deepseek-flash", OFFPEAK, (0.006 + 0.3 + 1.2) / 2),
    ("deepseek-v4-pro", PEAK, 0.044 + 1.32 + 3.96),
    ("deepseek-v4-pro", OFFPEAK, (0.044 + 1.32 + 3.96) / 2),
])
def test_usd_call_cost(model, when, expected):
    assert make_config().call_cost(model, M, M, M, when) == pytest.approx(expected)


async def test_recorded_cost_and_log_use_usd(make_service, fake_openai, storage, caplog):
    fake_openai.completions.usage = dict(prompt_tokens=2 * M, completion_tokens=M,
                                         prompt_cache_hit_tokens=M, prompt_cache_miss_tokens=M)
    svc = make_service(now=PEAK)
    with caplog.at_level(logging.INFO):
        await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert svc.llm.cost_today() == pytest.approx(1.506)
    assert "$1.506000（高峰）" in caplog.text


async def test_budget_only_counts_current_currency(make_service, fake_openai, storage):
    svc = make_service(daily_cost_budget=1.0, daily_token_budget=0)
    day = svc.llm.today()
    # 之前用人民幣計算的 5 元，不能當成 5 美元
    storage.record_usage(day, "deepseek-flash", "chat", 1, 1, 0, 1, cost=5.0, currency="CNY")
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    storage.record_usage(day, "deepseek-flash", "chat", 1, 1, 0, 1, cost=1.0, currency="USD")
    with pytest.raises(BudgetExceeded) as ei:
        await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert "$" in str(ei.value)


def test_old_rows_migrate_as_cny(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, created_at REAL NOT NULL, "
                 "model TEXT NOT NULL, purpose TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER "
                 "NOT NULL, cache_hit_tokens INTEGER NOT NULL, cache_miss_tokens INTEGER NOT NULL, "
                 "peak INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0, "
                 "reasoning_tokens INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO usage(day, created_at, model, purpose, prompt_tokens, completion_tokens, cache_hit_tokens, "
                 "cache_miss_tokens, cost) VALUES ('2026-11-03', 0, 'deepseek-flash', 'chat', 1, 1, 0, 1, 3.0)")
    conn.commit()
    conn.close()
    s = Storage(db)
    assert s.cost_on("2026-11-03", "CNY") == 3.0
    assert s.cost_on("2026-11-03", "USD") == 0.0
    s.close()


async def test_usage_command_shows_usd(storage, fake_openai, persona_file):
    config = make_config(daily_cost_budget=1.0, daily_token_budget=0)
    llm = LLMClient(config, storage, client=fake_openai, now=lambda: OFFPEAK)
    bot = PersonaBot(config, storage, llm, Persona(persona_file))
    try:
        storage.record_usage(llm.today(), "deepseek-flash", "chat", 100, 10, 50, 50, cost=0.25, currency="USD")
        storage.record_usage(llm.today(), "deepseek-flash", "chat", 100, 10, 50, 50, cost=9.0, currency="CNY")
        text = format_usage(bot)
        assert "費用：約 $0.2500 / 上限 $1（25.0%）" in text
        assert "約 $0.2500" in text and "¥" not in text
        assert "USD 價目表" in text
        assert "總 tokens：220" in text  # token 數照常全部加總
    finally:
        await bot.close()
