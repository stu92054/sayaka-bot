"""插話控制：logprobs 機率門檻、頻道插話冷卻、每小時上限、話題加權。"""
import logging
import math
from types import SimpleNamespace

import pytest

from bot.config import Config
from bot.chat import INTERJECT_JUDGE_TIMEOUT
from bot.llm import LLMError, yes_probability_from
from bot.triggers import Trigger, detect_trigger, interject_rate

from .conftest import BOT_ID, HANG, make_config, make_message

CH, OTHER_CH = 500, 501


def choice(top=None, content=""):
    logprobs = None
    if top is not None:
        cands = [SimpleNamespace(token=t, logprob=math.log(p)) for t, p in top]
        logprobs = SimpleNamespace(content=[SimpleNamespace(token=cands[0].token, logprob=cands[0].logprob,
                                                            top_logprobs=cands)])
    return SimpleNamespace(message=SimpleNamespace(content=content), logprobs=logprobs)


# --- 從 logprobs 算機率 ---

def test_probability_from_logprobs():
    c = choice([("是", 0.6), ("否", 0.3), ("嗯", 0.1)])
    assert yes_probability_from(c, ("是",), ("否",)) == pytest.approx(0.6 / 0.9)


def test_probability_tokens_with_spaces():
    c = choice([(" 否", 0.8), (" 是", 0.2)])
    assert yes_probability_from(c, ("是",), ("否",)) == pytest.approx(0.2)


def test_probability_only_yes_candidate():
    assert yes_probability_from(choice([("是", 0.4), ("好", 0.6)]), ("是",), ("否",)) == pytest.approx(1.0)


def test_probability_falls_back_to_text():
    assert yes_probability_from(choice(None, "是"), ("是",), ("否",)) == 1.0
    assert yes_probability_from(choice(None, "否"), ("是",), ("否",)) == 0.0
    assert yes_probability_from(choice(None, "早安！"), ("是",), ("否",)) == 0.0
    # 候選裡沒有是／否（例如 token 被切成位元組）→ 看文字
    assert yes_probability_from(choice([("\\xe6", 0.9)], "是"), ("是",), ("否",)) == 1.0


async def test_yes_probability_request(make_service, fake_openai):
    svc = make_service()
    fake_openai.completions.replies = [{"top": [("是", 0.7), ("否", 0.3)], "finish": "length"}]
    p = await svc.llm.yes_probability([{"role": "user", "content": "?"}], purpose="interject_judge")
    assert p == pytest.approx(0.7)
    call = fake_openai.completions.calls[0]
    assert call["max_tokens"] == 1 and call["logprobs"] is True and call["top_logprobs"] == 10
    assert call["extra_body"] == {"thinking": {"type": "disabled"}} and call["temperature"] == 0


async def test_yes_probability_length_not_warned(make_service, fake_openai, caplog):
    svc = make_service()
    fake_openai.completions.replies = [{"top": [("是", 1.0)], "finish": "length"}]
    with caplog.at_level(logging.WARNING):
        await svc.llm.yes_probability([{"role": "user", "content": "?"}])
    assert "max_tokens 上限" not in caplog.text  # 只要一個 token，被截斷是正常的


# --- 門檻 ---

def interject_service(make_service, clock, **cfg):
    cfg.setdefault("random_interject_rate", 0.5)  # conftest 的 rng 固定回 0.0 → 一定被抽中
    svc = make_service(**cfg)
    svc.clock = clock
    return svc


@pytest.mark.parametrize("p_yes,expected", [(0.69, False), (0.7, True), (0.9, True)])
async def test_threshold(make_service, fake_openai, clock, caplog, p_yes, expected):
    svc = interject_service(make_service, clock)
    fake_openai.completions.replies = [{"top": [("是", p_yes), ("否", 1 - p_yes)]}, "插嘴內容"]
    with caplog.at_level(logging.INFO):
        reply = await svc.handle_message(make_message("今天好累"), BOT_ID)
    assert (reply is not None) is expected
    assert f"p={p_yes:.2f}" in caplog.text


async def test_custom_threshold(make_service, fake_openai, clock):
    svc = interject_service(make_service, clock, interject_threshold=0.5)
    fake_openai.completions.replies = [{"top": [("是", 0.55), ("否", 0.45)]}, "插嘴內容"]
    assert await svc.handle_message(make_message("今天好累"), BOT_ID)


# --- 頻道插話冷卻 ---

async def test_channel_cooldown_after_she_speaks(make_service, fake_openai, clock):
    svc = interject_service(make_service, clock, interject_channel_cooldown_minutes=10)
    await svc.handle_message(make_message("早安", mentions=[BOT_ID]), BOT_ID)  # 被 @ 而回覆
    calls = len(fake_openai.completions.calls)
    clock.t += 9 * 60
    assert await svc.handle_message(make_message("今天好累", author_id=2), BOT_ID) is None
    assert len(fake_openai.completions.calls) == calls  # 冷卻中，連判斷都不呼叫
    # 其他頻道不受影響
    fake_openai.completions.replies = ["是", "插嘴"]
    assert await svc.handle_message(make_message("今天好累", author_id=3, channel_id=OTHER_CH), BOT_ID)
    # 10 分鐘後可以插話
    clock.t += 60
    fake_openai.completions.replies = ["是", "插嘴"]
    assert (await svc.handle_message(make_message("今天好累", author_id=4), BOT_ID)).trigger is Trigger.INTERJECT


async def test_mentions_ignore_interject_limits(make_service, fake_openai, clock):
    svc = interject_service(make_service, clock, interject_max_per_hour=1)
    fake_openai.completions.replies = ["是", "插嘴"]
    assert await svc.handle_message(make_message("今天好累"), BOT_ID)
    clock.t += 10
    assert await svc.handle_message(make_message("沙耶香さん？", mentions=[BOT_ID]), BOT_ID)


async def test_judge_no_does_not_start_channel_cooldown(make_service, fake_openai, clock):
    svc = interject_service(make_service, clock)
    fake_openai.completions.replies = ["否", "是", "插嘴"]
    assert await svc.handle_message(make_message("今天好累", author_id=1), BOT_ID) is None
    assert await svc.handle_message(make_message("好想睡", author_id=2), BOT_ID)


# --- 每小時上限 ---

async def test_hourly_cap(make_service, fake_openai, clock):
    svc = interject_service(make_service, clock, interject_channel_cooldown_minutes=0, interject_max_per_hour=2)
    fake_openai.completions.default = "是"
    for i in range(2):
        clock.t += 60
        assert await svc.handle_message(make_message(f"m{i}", author_id=10 + i), BOT_ID)
    calls = len(fake_openai.completions.calls)
    clock.t += 60
    assert await svc.handle_message(make_message("m2", author_id=20), BOT_ID) is None
    assert len(fake_openai.completions.calls) == calls  # 達上限，不呼叫判斷
    clock.t += 3600  # 一小時後重新計算
    assert await svc.handle_message(make_message("m3", author_id=21), BOT_ID)


async def test_hourly_cap_zero_means_unlimited(make_service, fake_openai, clock):
    svc = interject_service(make_service, clock, interject_channel_cooldown_minutes=0, interject_max_per_hour=0)
    fake_openai.completions.default = "是"
    for i in range(6):
        clock.t += 60
        assert await svc.handle_message(make_message(f"m{i}", author_id=10 + i), BOT_ID)


# --- 話題加權 ---

def test_keyword_rate():
    cfg = make_config(random_interject_rate=0.02, interject_keywords=("便當", "滑冰"), interject_keyword_rate=0.3)
    assert interject_rate("今天的便當是燉菜", cfg) == 0.3
    assert interject_rate("今天好累", cfg) == 0.02
    assert detect_trigger(make_message("週末去滑冰"), BOT_ID, cfg, rng=lambda: 0.2) is Trigger.INTERJECT
    assert detect_trigger(make_message("今天好累"), BOT_ID, cfg, rng=lambda: 0.2) is None


def test_keywords_only_mode():
    """RANDOM_INTERJECT_RATE=0 時，只在聊到指定話題時才可能插話。"""
    cfg = make_config(random_interject_rate=0, interject_keywords=("便當",), interject_keyword_rate=0.3)
    assert detect_trigger(make_message("便當"), BOT_ID, cfg, rng=lambda: 0.1) is Trigger.INTERJECT
    assert detect_trigger(make_message("天氣"), BOT_ID, cfg, rng=lambda: 0.0) is None


def test_base_rate_higher_than_keyword_rate():
    cfg = make_config(random_interject_rate=0.5, interject_keywords=("便當",), interject_keyword_rate=0.3)
    assert interject_rate("便當", cfg) == 0.5


# --- 設定 ---

def test_config_defaults_and_parse():
    cfg = Config.from_env({})
    assert cfg.random_interject_rate == 0  # 插話預設關閉
    assert cfg.interject_keywords == () and cfg.interject_keyword_rate == 0.3
    assert cfg.interject_threshold == 0.7
    assert cfg.interject_channel_cooldown_minutes == 10 and cfg.interject_max_per_hour == 3
    cfg = Config.from_env({
        "INTERJECT_KEYWORDS": "便當, 滑冰", "INTERJECT_KEYWORD_RATE": "0.5", "INTERJECT_THRESHOLD": "0.8",
        "INTERJECT_CHANNEL_COOLDOWN_MINUTES": "30", "INTERJECT_MAX_PER_HOUR": "1",
    })
    assert cfg.interject_keywords == ("便當", "滑冰") and cfg.interject_keyword_rate == 0.5
    assert cfg.interject_threshold == 0.8
    assert cfg.interject_channel_cooldown_minutes == 30 and cfg.interject_max_per_hour == 1


def test_config_validation():
    problems = Config.from_env({"INTERJECT_THRESHOLD": "1.5", "INTERJECT_KEYWORD_RATE": "-1"}).validate()
    assert any("INTERJECT_THRESHOLD" in p for p in problems)
    assert any("INTERJECT_KEYWORD_RATE" in p for p in problems)


# --- 與逾時處理的整合（判斷也經過 _complete） ---

async def test_judge_hanging_request_times_out_and_retries(make_service, fake_openai):
    svc = make_service(api_timeout_seconds=0.01)
    fake_openai.completions.replies = [HANG, {"top": [("是", 0.9), ("否", 0.1)]}]
    p = await svc.llm.yes_probability([{"role": "user", "content": "?"}])
    assert p == pytest.approx(0.9)
    assert len(fake_openai.completions.calls) == 2


async def test_judge_server_error_body_is_retried(make_service, fake_openai):
    svc = make_service()
    queue_timeout = SimpleNamespace(choices=None, usage=None, error={"message": "unable to start processing"})
    fake_openai.completions.replies = [queue_timeout, {"top": [("否", 1.0)]}]
    assert await svc.llm.yes_probability([{"role": "user", "content": "?"}]) == 0.0
    assert len(fake_openai.completions.calls) == 2


async def test_judge_total_timeout(make_service, fake_openai):
    svc = make_service(api_timeout_seconds=5)
    fake_openai.completions.default = HANG
    with pytest.raises(LLMError):
        await svc.llm.yes_probability([{"role": "user", "content": "?"}], total_timeout=0.05)
    assert len(fake_openai.completions.calls) == 1


async def test_interject_gives_up_when_judge_hangs(make_service, fake_openai, clock, monkeypatch):
    import bot.chat
    monkeypatch.setattr(bot.chat, "INTERJECT_JUDGE_TIMEOUT", 0.05)
    svc = interject_service(make_service, clock, api_timeout_seconds=5)
    fake_openai.completions.default = HANG
    assert await svc.handle_message(make_message("今天好累"), BOT_ID) is None
    assert len(fake_openai.completions.calls) == 1  # 沒有重試到 3 次


def test_judge_timeout_is_short():
    assert INTERJECT_JUDGE_TIMEOUT <= 60
