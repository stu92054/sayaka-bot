from datetime import datetime

import pytest

from bot.storage import Storage

from .conftest import TAIPEI

from types import SimpleNamespace

from bot.llm import BudgetExceeded, ContentFiltered, LLMError


async def test_records_usage_including_cache_hits(make_service, fake_openai, storage):
    svc = make_service()
    out = await svc.llm.chat([{"role": "user", "content": "hi"}], purpose="chat")
    assert out == fake_openai.completions.default
    rows = svc.llm.usage_today()
    assert len(rows) == 1
    r = rows[0]
    assert (r.prompt_tokens, r.completion_tokens, r.cache_hit_tokens, r.cache_miss_tokens) == (100, 20, 60, 40)
    assert svc.llm.tokens_today() == 120


async def test_retries_with_exponential_backoff(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.replies = [RuntimeError("1"), RuntimeError("2"), "成功"]
    assert await svc.llm.chat([{"role": "user", "content": "hi"}]) == "成功"
    assert svc.llm.sleeps == [1.0, 2.0]
    assert len(fake_openai.completions.calls) == 3


async def test_gives_up_after_three_attempts(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.default = RuntimeError("down")
    with pytest.raises(LLMError):
        await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert len(fake_openai.completions.calls) == 3
    assert svc.llm.tokens_today() == 0


async def test_budget_exceeded_blocks_api_call(make_service, fake_openai, storage):
    svc = make_service(daily_token_budget=100)
    storage.record_usage(svc.llm.today(), "deepseek-v4-flash", "chat", 90, 10, 0, 90)
    with pytest.raises(BudgetExceeded):
        await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert fake_openai.completions.calls == []


async def test_budget_counts_only_today(make_service, fake_openai, storage):
    svc = make_service(daily_token_budget=100)
    storage.record_usage("2026-11-02", "deepseek-v4-flash", "chat", 999, 999, 0, 999)
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert len(fake_openai.completions.calls) == 1


async def test_cost_offpeak_is_half(make_service, fake_openai):
    fake_openai.completions.usage = dict(prompt_tokens=2_000_000, completion_tokens=1_000_000,
                                         prompt_cache_hit_tokens=1_000_000, prompt_cache_miss_tokens=1_000_000)
    svc = make_service()  # 週二 12:30 → 空閒
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    # flash 高峰：命中 0.04 + 未命中 2 + 輸出 8 = 10.04 元，空閒減半
    assert svc.llm.estimate_cost(svc.llm.usage_today()) == pytest.approx(5.02)


async def test_cost_peak_full_price_for_pro(make_service, fake_openai):
    fake_openai.completions.usage = dict(prompt_tokens=1_000_000, completion_tokens=1_000_000,
                                         prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1_000_000)
    svc = make_service(now=datetime(2026, 11, 3, 10, 0, tzinfo=TAIPEI))  # 週二 10:00 → 高峰
    await svc.llm.chat([{"role": "user", "content": "hi"}], model="deepseek-v4-pro")
    assert svc.llm.estimate_cost(svc.llm.usage_today()) == pytest.approx(9.0 + 27.0)


def test_peak_hours(make_service):
    cfg = make_service().config
    bj = lambda *a: datetime(*a, tzinfo=TAIPEI)  # 台北與北京同為 UTC+8
    assert cfg.is_peak(bj(2026, 11, 3, 9, 0))
    assert cfg.is_peak(bj(2026, 11, 3, 11, 59))
    assert not cfg.is_peak(bj(2026, 11, 3, 12, 0))
    assert cfg.is_peak(bj(2026, 11, 3, 14, 0))
    assert not cfg.is_peak(bj(2026, 11, 3, 18, 0))
    assert not cfg.is_peak(bj(2026, 11, 7, 10, 0))  # 週六
    assert not cfg.is_peak(bj(2026, 11, 3, 8, 59))


def test_holidays_are_offpeak():
    from datetime import date
    from .conftest import make_config
    cfg = make_config(cn_holidays=frozenset({date(2026, 10, 1)}))
    assert not cfg.is_peak(datetime(2026, 10, 1, 10, 0, tzinfo=TAIPEI))


def test_legacy_flash_name_uses_flash_price():
    from .conftest import make_config
    cfg = make_config()
    assert cfg.price_for("deepseek-v4-flash") == cfg.price_for("deepseek-flash")


async def test_thinking_disabled_for_chat_enabled_for_deep(make_service, fake_openai):
    svc = make_service()
    await svc.llm.chat([{"role": "user", "content": "hi"}], temperature=0)
    call = fake_openai.completions.calls[-1]
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert call["temperature"] == 0
    await svc.deep(500, "KO", "難題")
    call = fake_openai.completions.calls[-1]
    assert call["model"] == "deepseek-v4-pro"
    assert call["extra_body"] == {"thinking": {"type": "enabled"}}
    assert call["max_tokens"] == 8000
    assert call["reasoning_effort"] == "high"
    assert "temperature" not in call


async def test_no_reasoning_effort_when_thinking_disabled(make_service, fake_openai):
    svc = make_service()
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in fake_openai.completions.calls[-1]


async def test_effort_configurable_and_omittable(make_service, fake_openai):
    svc = make_service(thinking_default="enabled", reasoning_effort_default="max")
    await svc.llm.chat([{"role": "user", "content": "hi"}], temperature=0)
    call = fake_openai.completions.calls[-1]
    assert call["reasoning_effort"] == "max" and "temperature" not in call
    svc = make_service(thinking_default="enabled", reasoning_effort_default="omit")
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in fake_openai.completions.calls[-1]


def test_invalid_effort_rejected():
    from bot.config import Config
    for bad in ("turbo", "medium", "none"):
        with pytest.raises(ValueError):
            Config.from_env({"REASONING_EFFORT_DEEP": bad})


async def test_thinking_omit_sends_no_extra_body(make_service, fake_openai):
    svc = make_service(thinking_default="omit")
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert "extra_body" not in fake_openai.completions.calls[-1]


def test_storage_migrates_old_usage_table(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, created_at REAL NOT NULL, "
                 "model TEXT NOT NULL, purpose TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER "
                 "NOT NULL, cache_hit_tokens INTEGER NOT NULL, cache_miss_tokens INTEGER NOT NULL)")
    conn.commit(); conn.close()
    s = Storage(db)
    s.record_usage("2026-11-03", "deepseek-flash", "chat", 1, 1, 0, 1, cost=0.5)
    assert s.usage_by_model("2026-11-03")[0].cost == 0.5
    s.close()


async def test_interrupted_finish_reason_is_retried(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.replies = [("", "insufficient_system_resource"), ("半", "aborted"), "完整回答"]
    assert await svc.llm.chat([{"role": "user", "content": "hi"}]) == "完整回答"
    assert len(fake_openai.completions.calls) == 3
    assert svc.llm.sleeps == [1.0, 2.0]
    # 被打斷的呼叫仍有消耗 token，照樣記錄
    assert svc.llm.usage_today()[0].calls == 3


async def test_content_filter_raises(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.replies = [("", "content_filter")]
    with pytest.raises(ContentFiltered):
        await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert len(fake_openai.completions.calls) == 1  # 不重試


async def test_length_returns_partial_content(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.replies = [("說到一半", "length")]
    assert await svc.llm.chat([{"role": "user", "content": "hi"}]) == "說到一半"


async def test_records_reasoning_tokens_and_cached_tokens_fallback(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.usage = dict(
        prompt_tokens=100, completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=30),
    )
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    r = svc.llm.usage_today()[0]
    assert (r.cache_hit_tokens, r.cache_miss_tokens, r.reasoning_tokens) == (80, 20, 30)
