import pytest

from bot.llm import BudgetExceeded, LLMError


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


def test_estimate_cost(make_service, storage):
    svc = make_service()
    storage.record_usage(svc.llm.today(), "deepseek-v4-flash", "chat", 2_000_000, 1_000_000, 1_000_000, 1_000_000)
    p = svc.config.price_for("deepseek-v4-flash")
    assert svc.llm.estimate_cost(svc.llm.usage_today()) == pytest.approx(p.cache_hit + p.cache_miss + p.output)
