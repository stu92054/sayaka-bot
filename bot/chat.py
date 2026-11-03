"""訊息處理流程：記錄歷史 → 判斷觸發 → 冷卻 → 呼叫模型 → 產生回覆。"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .history import HistoryManager
from .llm import BudgetExceeded, ContentFiltered, LLMClient, LLMError
from .lore import Lore
from .persona import Persona
from .replies import Replies
from .text import split_message
from .triggers import Cooldown, Trigger, detect_trigger, is_ignored

log = logging.getLogger(__name__)

INTERJECT_JUDGE_PROMPT = (
    "你是一個判斷器。以下是 Discord 群組的對話，最後一行是最新訊息。"
    "請判斷一個個性鮮明、愛吐槽的群組成員現在插話是否自然且有趣。"
    "只回答「是」或「否」，不要解釋。"
)


@dataclass
class Reply:
    chunks: list[str]
    trigger: Trigger
    recorded: bool  # 是否為模型產生（會寫入歷史）


RESERVED_AUTHOR_NAMES = ("system", "系統", "系統管理員", "管理員", "admin", "assistant", "bot")


def display_author(name: str, config: Config) -> str:
    """顯示名稱可以自己改：撞名 bot 或像「SYSTEM」「系統管理員」的，在 prompt 裡標成一般成員。"""
    key = name.strip().casefold()
    reserved = {n.casefold() for n in (config.bot_display_name, *config.bot_names, *RESERVED_AUTHOR_NAMES)}
    if key in reserved or key.startswith(("system", "系統")):
        return f"{name}（成員暱稱）"
    return name


def message_text(message: Any) -> str:
    text = (getattr(message, "clean_content", None) or getattr(message, "content", "") or "").strip()
    attachments = getattr(message, "attachments", None) or []
    names = [getattr(a, "filename", "檔案") for a in attachments]
    if names:
        text = (text + " " if text else "") + " ".join(f"[附件:{n}]" for n in names)
    return text


class ChatService:
    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        history: HistoryManager,
        persona: Persona,
        cooldown: Cooldown | None = None,
        rng: Callable[[], float] = random.random,
        replies: Replies | None = None,
        lore: Lore | None = None,
    ):
        self.config = config
        self.llm = llm
        self.history = history
        self.persona = persona
        self.cooldown = cooldown or Cooldown(config.user_cooldown_seconds)
        self.rng = rng
        self.replies = replies or Replies()
        self.lore = lore

    async def handle_message(self, message: Any, bot_user_id: int, *, dm_allowed: bool = False) -> Reply | None:
        if is_ignored(message, self.config):
            return None
        if message.guild is None and not dm_allowed:
            return None

        channel_id = message.channel.id
        author = display_author(message.author.display_name, self.config)
        content = message_text(message)
        if not content:
            return None

        trigger = detect_trigger(message, bot_user_id, self.config, dm_allowed=dm_allowed, rng=self.rng)

        # 先以「目前為止的歷史」組 prompt，再把當前訊息寫入歷史
        prompt = None
        if trigger is not None:
            prompt = self.history.build_prompt(channel_id, self.persona.text, author, content, self._lore_text())
        self.history.add(channel_id, author, content)

        if trigger is None:
            return None
        if self.cooldown.remaining(message.author.id) > 0:
            log.debug("使用者 %s 冷卻中", message.author.id)
            return None
        if trigger is Trigger.INTERJECT and not await self._worth_interjecting(prompt):
            return None
        if not self.cooldown.try_acquire(message.author.id):
            return None

        return await self.generate(channel_id, prompt, trigger, model=self.config.model_default)

    async def generate(self, channel_id: int, prompt: list[dict[str, str]], trigger: Trigger,
                       *, model: str, purpose: str = "chat", max_tokens: int | None = None,
                       thinking: str | None = None, total_timeout: float | None = None) -> Reply:
        try:
            text = await self.llm.chat(prompt, model=model, max_tokens=max_tokens or self.config.max_reply_tokens,
                                       purpose=purpose, thinking=thinking, total_timeout=total_timeout)
        except BudgetExceeded:
            return Reply([self.replies["budget_exceeded"]], trigger, recorded=False)
        except LLMError:
            return Reply([self.replies["error"]], trigger, recorded=False)
        except ContentFiltered:
            return Reply([self.replies["content_filter"]], trigger, recorded=False)
        if not text:
            return Reply([self.replies["error"]], trigger, recorded=False)
        self.history.add(channel_id, self.bot_display_name, text, is_bot=True)
        return Reply(split_message(text), trigger, recorded=True)

    async def deep(self, channel_id: int, author: str, question: str) -> Reply:
        author = display_author(author, self.config)
        prompt = self.history.build_prompt(channel_id, self.persona.text, author, question, self._lore_text())
        self.history.add(channel_id, author, question)
        # 思考模式的推理內容也算在 max_tokens 內，所以 /deep 給較大的上限；
        # 總時限要短於 Discord 互動的 15 分鐘，失敗時才來得及回覆錯誤訊息
        return await self.generate(channel_id, prompt, Trigger.MENTION, model=self.config.model_deep,
                                   purpose="deep", max_tokens=self.config.deep_max_tokens,
                                   thinking=self.config.thinking_deep,
                                   total_timeout=self.config.deep_total_timeout_seconds)

    async def _worth_interjecting(self, prompt: list[dict[str, str]] | None) -> bool:
        if not prompt:
            return False
        if self.llm.over_budget():
            return False
        transcript = "\n".join(m["content"] for m in prompt[1:] if m["role"] != "system")[-3000:]
        try:
            answer = await self.llm.chat(
                [
                    {"role": "system", "content": INTERJECT_JUDGE_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                model=self.config.model_default,
                max_tokens=5,
                purpose="interject_judge",
                temperature=0,
                thinking="disabled",  # 只要一個字，不能讓推理吃掉 max_tokens
            )
        except (BudgetExceeded, LLMError, ContentFiltered):
            return False
        return answer.strip().startswith("是")

    def _lore_text(self) -> str | None:
        return self.lore.text if self.lore is not None and self.lore.enabled else None

    @property
    def bot_display_name(self) -> str:
        """歷史紀錄與摘要中 bot 的名字（BOT_NAMES 可能是空的，所以另外設定）。"""
        return self.config.bot_display_name
