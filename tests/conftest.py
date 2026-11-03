"""共用 fixture：假的 DeepSeek client、假的 Discord 訊息。全程不連網。"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest

from bot.chat import ChatService
from bot.config import Config
from bot.history import HistoryManager
from bot.llm import LLMClient
from bot.persona import Persona
from bot.storage import Storage
from bot.triggers import Cooldown

TAIPEI = ZoneInfo("Asia/Taipei")
GUILD_ID = 111
OTHER_GUILD_ID = 999
BOT_ID = 42
ADMIN_ID = 7


HANG = object()  # 模擬 DeepSeek 排隊時一直保持連線、遲遲不回應


class FakeCompletions:
    def __init__(self):
        self.calls: list[dict] = []
        # str、(str, finish_reason)、dict（含 logprobs）、Exception、HANG 或 SimpleNamespace（原樣回傳的回應物件），
        # 依序取用；空了就回 default
        self.replies: list = []
        self.default = "哼，才不是特地回你的。"
        self.usage = dict(prompt_tokens=100, completion_tokens=20,
                          prompt_cache_hit_tokens=60, prompt_cache_miss_tokens=40)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.replies.pop(0) if self.replies else self.default
        if isinstance(item, Exception):
            raise item
        if item is HANG:
            await asyncio.Event().wait()
        if isinstance(item, SimpleNamespace):
            return item
        finish = "stop"
        logprobs = None
        if isinstance(item, tuple):  # (content, finish_reason)
            item, finish = item
        elif isinstance(item, dict):  # {"content", "finish", "top": [(token, 機率), ...]}（第一個 token 的候選）
            top = [SimpleNamespace(token=t, logprob=math.log(p)) for t, p in item.get("top", [])]
            logprobs = SimpleNamespace(content=[SimpleNamespace(
                token=top[0].token if top else "", logprob=top[0].logprob if top else 0.0, top_logprobs=top)])
            finish = item.get("finish", "stop")
            item = item.get("content", top[0].token if top else "")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item), finish_reason=finish,
                                     logprobs=logprobs)],
            usage=SimpleNamespace(**self.usage),
        )


class FakeOpenAI:
    def __init__(self):
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_config(**overrides) -> Config:
    base = dict(
        discord_token="discord-secret-token",
        deepseek_api_key="sk-secret-deepseek-key",
        allowed_guild_ids=frozenset({GUILD_ID}),
        admin_user_ids=frozenset({ADMIN_ID}),
        bot_names=("大小姐", "Ojou"),
        history_max_messages=6,
        daily_token_budget=10_000,
        user_cooldown_seconds=5,
        max_reply_tokens=600,
        random_interject_rate=0.0,
    )
    base.update(overrides)
    return Config(**base)


_msg_counter = 0


def make_message(content="hello", *, author_id=1, author_name="KO", bot=False, guild_id=GUILD_ID,
                 channel_id=500, mentions=(), reply_to_author_id=None, attachments=()):
    global _msg_counter
    _msg_counter += 1
    author = SimpleNamespace(id=author_id, bot=bot, display_name=author_name, name=author_name)
    reference = None
    if reply_to_author_id is not None:
        reference = SimpleNamespace(message_id=1, resolved=SimpleNamespace(author=SimpleNamespace(id=reply_to_author_id)))
    return SimpleNamespace(
        id=_msg_counter,
        content=content,
        clean_content=content,
        author=author,
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
        channel=SimpleNamespace(id=channel_id),
        mentions=[SimpleNamespace(id=m) for m in mentions],
        reference=reference,
        attachments=[SimpleNamespace(filename=f) for f in attachments],
    )


@pytest.fixture
def fake_openai():
    return FakeOpenAI()


@pytest.fixture
def storage():
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture
def persona_file(tmp_path):
    p = tmp_path / "persona.md"
    p.write_text("你是傲嬌大小姐。", encoding="utf-8")
    return p


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_service(storage, fake_openai, persona_file, clock):
    def factory(now=None, **overrides):
        overrides_now = now
        config = make_config(**overrides)
        sleeps: list[float] = []

        async def fake_sleep(d):
            sleeps.append(d)

        when = overrides_now or datetime(2026, 11, 3, 12, 30, tzinfo=TAIPEI)  # 週二午休：空閒時段
        llm = LLMClient(config, storage, client=fake_openai, sleep=fake_sleep, now=lambda: when)
        llm.sleeps = sleeps
        history = HistoryManager(storage, llm, config.history_max_messages)
        persona = Persona(persona_file)
        svc = ChatService(config, llm, history, persona, Cooldown(config.user_cooldown_seconds, clock=clock),
                          rng=lambda: 0.0)
        return svc
    return factory
