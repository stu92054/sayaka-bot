"""觸發判斷與冷卻。純邏輯，方便以 mock 物件測試。"""
from __future__ import annotations

import random
import time
from enum import Enum
from typing import Any, Callable

from .config import Config


class Trigger(str, Enum):
    MENTION = "mention"
    REPLY = "reply"
    DM = "dm"
    NAME = "name"
    INTERJECT = "interject"


def is_reply_to(message: Any, user_id: int) -> bool:
    ref = getattr(message, "reference", None)
    if ref is None:
        return False
    resolved = getattr(ref, "resolved", None)
    author = getattr(resolved, "author", None)
    return author is not None and getattr(author, "id", None) == user_id


def is_mention_of(message: Any, user_id: int) -> bool:
    if any(getattr(u, "id", None) == user_id for u in (getattr(message, "mentions", None) or [])):
        return True
    # bot 加入伺服器時 Discord 會建立同名的身分組；@ 選單裡兩者長得一樣，選到身分組也算是 @她
    return any(getattr(getattr(r, "tags", None), "bot_id", None) == user_id
               for r in (getattr(message, "role_mentions", None) or []))


def contains_name(content: str, names: tuple[str, ...]) -> bool:
    lowered = content.casefold()
    return any(n.casefold() in lowered for n in names if n)


def is_ignored(message: Any, config: Config) -> bool:
    """一律忽略：bot 訊息、不在允許清單的伺服器。"""
    if getattr(message.author, "bot", False):
        return True
    guild = getattr(message, "guild", None)
    if guild is not None and guild.id not in config.allowed_guild_ids:
        return True
    return False


def detect_trigger(
    message: Any,
    bot_user_id: int,
    config: Config,
    *,
    dm_allowed: bool = False,
    rng: Callable[[], float] = random.random,
) -> Trigger | None:
    """判斷這則訊息是否該觸發回應（不含冷卻）。"""
    if is_ignored(message, config):
        return None
    if getattr(message, "guild", None) is None:
        return Trigger.DM if dm_allowed else None
    if is_mention_of(message, bot_user_id):
        return Trigger.MENTION
    if is_reply_to(message, bot_user_id):
        return Trigger.REPLY
    if contains_name(message.content or "", config.bot_names):
        return Trigger.NAME
    rate = interject_rate(message.content or "", config)
    if rate > 0 and rng() < rate:
        return Trigger.INTERJECT
    return None


def interject_rate(content: str, config: Config) -> float:
    """一般訊息被抽中、進入插話判斷的機率；聊到指定話題時提高。"""
    rate = config.random_interject_rate
    if config.interject_keywords and contains_name(content, config.interject_keywords):
        rate = max(rate, config.interject_keyword_rate)
    return rate


class Cooldown:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._last: dict[int, float] = {}

    def remaining(self, user_id: int) -> float:
        last = self._last.get(user_id)
        if last is None:
            return 0.0
        return max(0.0, self.seconds - (self._clock() - last))

    def try_acquire(self, user_id: int) -> bool:
        """不在冷卻中就記下時間並回傳 True。"""
        if self.seconds <= 0:
            return True
        if self.remaining(user_id) > 0:
            return False
        self._last[user_id] = self._clock()
        return True
