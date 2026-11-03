"""訊息處理流程：記錄歷史 → 判斷觸發 → 冷卻 → 呼叫模型 → 產生回覆。"""
from __future__ import annotations

import logging
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .history import HistoryManager, strip_leading_stamp
from .llm import BudgetExceeded, ContentFiltered, LLMClient, LLMError
from .lore import Lore
from .persona import Persona
from .replies import Replies
from .text import split_message
from .tools import WEB_TOOLS_GUIDE, Tool, ToolRunner, suppress_embeds
from .triggers import Cooldown, Trigger, detect_trigger, is_ignored

log = logging.getLogger(__name__)

# 插話判斷：沿用和一般聊天相同的 prompt（原作資料、人設、對話紀錄），開頭可命中快取，
# 判斷模型也知道角色的個性。只在最後一則訊息後面加上這段問題，讓模型回答一個字。
INTERJECT_JUDGE_SUFFIX = (
    "\n\n（以上是群組目前的對話，沒有人叫你。請判斷：以你的個性，現在主動加入這段對話是否自然，"
    "而且你真的有話想說？只回答一個字：是 或 否。）"
)
HOUR = 3600.0
# 插話判斷含重試的總時限（秒）。判斷只要一個字，正常幾秒內就回來；
# DeepSeek 排隊時與其等上幾分鐘（話題早就過了），不如放棄這次插話
INTERJECT_JUDGE_TIMEOUT = 30.0


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
        clock: Callable[[], float] = time.monotonic,
        web_tools: list[Tool] | None = None,
    ):
        self.config = config
        self.llm = llm
        self.history = history
        self.persona = persona
        self.cooldown = cooldown or Cooldown(config.user_cooldown_seconds)
        self.rng = rng
        self.replies = replies or Replies()
        self.lore = lore
        self.clock = clock
        self.web_tools = list(web_tools or [])
        # 插話控制（記憶體內，重啟後歸零）：各頻道她上次發言的時間、最近一小時的插話時間
        self._last_spoke: dict[int, float] = {}
        self._interjections: dict[int, deque[float]] = defaultdict(deque)

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
            prompt = self.history.build_prompt(channel_id, self.persona.text, author, content, self._lore_text(),
                                               now=self._now())
            prompt = self._with_tool_guide(prompt)
        self.history.add(channel_id, author, content)

        if trigger is None:
            return None
        if self.cooldown.remaining(message.author.id) > 0:
            log.debug("使用者 %s 冷卻中", message.author.id)
            return None
        if trigger is Trigger.INTERJECT:
            # 先檢查頻道限制（不花錢），通過才呼叫判斷模型
            if not self.interject_allowed(channel_id):
                return None
            if not await self._worth_interjecting(channel_id, prompt):
                return None
        if not self.cooldown.try_acquire(message.author.id):
            return None

        runner = None
        if self.tools_enabled:
            # 插話不查資料（max_calls=0）：仍帶工具定義，prompt 開頭才和其他聊天一致、命中快取
            runner = self._tool_runner(message.author.id, content, prompt,
                                       max_calls=0 if trigger is Trigger.INTERJECT else None)
        return await self.generate(channel_id, prompt, trigger, model=self.config.model_default, tool_runner=runner)

    async def generate(self, channel_id: int, prompt: list[dict[str, str]], trigger: Trigger,
                       *, model: str, purpose: str = "chat", max_tokens: int | None = None,
                       thinking: str | None = None, total_timeout: float | None = None,
                       reasoning_effort: str | None = None, call_timeout: float | None = None,
                       tool_runner: ToolRunner | None = None) -> Reply:
        max_tokens = max_tokens or self.config.max_reply_tokens
        try:
            if tool_runner is not None:
                text = await self.llm.chat_with_tools(prompt, tool_runner, model=model, max_tokens=max_tokens,
                                                      purpose=purpose, max_rounds=tool_runner.max_calls)
            else:
                text = await self.llm.chat(prompt, model=model, max_tokens=max_tokens,
                                           purpose=purpose, thinking=thinking, total_timeout=total_timeout,
                                           reasoning_effort=reasoning_effort, call_timeout=call_timeout)
        except BudgetExceeded:
            return Reply([self.replies["budget_exceeded"]], trigger, recorded=False)
        except LLMError:
            return Reply([self.replies["error"]], trigger, recorded=False)
        except ContentFiltered:
            return Reply([self.replies["content_filter"]], trigger, recorded=False)
        text = strip_leading_stamp(text)
        if tool_runner is not None and tool_runner.used:
            text = suppress_embeds(text)
        if not text:
            return Reply([self.replies["error"]], trigger, recorded=False)
        self.history.add(channel_id, self.bot_display_name, text, is_bot=True)
        now = self.clock()
        self._last_spoke[channel_id] = now
        if trigger is Trigger.INTERJECT:
            self._interjections[channel_id].append(now)
        return Reply(split_message(text), trigger, recorded=True)

    async def deep(self, channel_id: int, author: str, question: str) -> Reply:
        author = display_author(author, self.config)
        prompt = self.history.build_prompt(channel_id, self.persona.text, author, question, self._lore_text(),
                                           now=self._now())
        self.history.add(channel_id, author, question)
        # 思考模式的推理內容也算在 max_tokens 內，所以 /deep 給較大的上限；
        # 總時限要短於 Discord 互動的 15 分鐘，失敗時才來得及回覆錯誤訊息。
        # 思考模式、強度、單次逾時都明確指定，不靠模型名稱判斷（MODEL_DEEP 可以和一般聊天同一個模型）
        return await self.generate(channel_id, prompt, Trigger.MENTION, model=self.config.model_deep,
                                   purpose="deep", max_tokens=self.config.deep_max_tokens,
                                   thinking=self.config.thinking_deep,
                                   reasoning_effort=self.config.reasoning_effort_deep,
                                   call_timeout=self.config.deep_api_timeout_seconds,
                                   total_timeout=self.config.deep_total_timeout_seconds)

    def interject_allowed(self, channel_id: int) -> bool:
        """頻道層級的插話限制：她上次發言後的冷卻、每小時上限。"""
        now = self.clock()
        cooldown = self.config.interject_channel_cooldown_minutes * 60
        last = self._last_spoke.get(channel_id)
        if last is not None and now - last < cooldown:
            log.debug("頻道 %s 插話冷卻中", channel_id)
            return False
        limit = self.config.interject_max_per_hour
        if limit > 0:
            recent = self._interjections[channel_id]
            while recent and now - recent[0] >= HOUR:
                recent.popleft()
            if len(recent) >= limit:
                log.info("頻道 %s 這一小時已插話 %d 次，達到上限", channel_id, len(recent))
                return False
        return True

    async def _worth_interjecting(self, channel_id: int, prompt: list[dict[str, str]] | None) -> bool:
        if not prompt:
            return False
        if self.llm.over_budget():
            return False
        judge = [dict(m) for m in prompt]
        judge[-1]["content"] += INTERJECT_JUDGE_SUFFIX
        try:
            p = await self.llm.yes_probability(judge, model=self.config.model_default, purpose="interject_judge",
                                               total_timeout=INTERJECT_JUDGE_TIMEOUT,
                                               tools=[t.spec() for t in self.web_tools] if self.tools_enabled else None)
        except (BudgetExceeded, LLMError, ContentFiltered):
            return False
        threshold = self.config.interject_threshold
        log.info("插話判斷（頻道 %s）：p=%.2f，門檻 %.2f → %s", channel_id, p, threshold,
                 "插話" if p >= threshold else "不插話")
        return p >= threshold

    @property
    def tools_enabled(self) -> bool:
        return self.config.web_tools_enabled and bool(self.web_tools)

    def _with_tool_guide(self, prompt: list[dict[str, str]]) -> list[dict[str, str]]:
        """啟用工具時，在開頭的 system 訊息（原作資料、人設）之後加上工具說明。內容固定，不影響快取。"""
        if not self.tools_enabled:
            return prompt
        i = 0
        while i < len(prompt) and prompt[i]["role"] == "system":
            i += 1
        return prompt[:i] + [{"role": "system", "content": WEB_TOOLS_GUIDE}] + prompt[i:]

    def _tool_runner(self, user_id: int, question: str, prompt: list[dict[str, str]],
                     *, max_calls: int | None = None) -> ToolRunner:
        day = self.llm.today()
        limit = self.config.web_daily_limit_per_user
        storage = self.history.storage
        runner = ToolRunner(
            tools=self.web_tools,
            max_calls=self.config.web_max_tool_calls_per_reply if max_calls is None else max_calls,
            question=question,
            has_quota=lambda: limit <= 0 or storage.tool_uses_on(day, user_id) < limit,
            on_use=lambda name: storage.record_tool_use(day, user_id, name),
        )
        # 只有對話中出現過的網址可以讀取（成員的發言與摘要；不含 system 訊息）
        for m in prompt:
            if m["role"] == "user":
                runner.allow_urls_from(m["content"])
        return runner

    def _now(self):
        """要附在 prompt 的現在時間（BOT_TIMEZONE）；SHOW_CURRENT_TIME=false 時不附。"""
        return self.llm.now() if self.config.show_current_time else None

    def _lore_text(self) -> str | None:
        return self.lore.text if self.lore is not None and self.lore.enabled else None

    @property
    def bot_display_name(self) -> str:
        """歷史紀錄與摘要中 bot 的名字（BOT_NAMES 可能是空的，所以另外設定）。"""
        return self.config.bot_display_name
