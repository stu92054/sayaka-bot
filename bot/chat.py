"""訊息處理流程：記錄歷史 → 判斷觸發 → 冷卻 → 呼叫模型 → 產生回覆。"""
from __future__ import annotations

import logging
import random
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable

import openai

from .config import Config
from .history import HistoryManager, strip_leading_stamp
from .llm import BudgetExceeded, ContentFiltered, LLMClient, LLMError
from .lore import Lore
from .memories import RECALL_GUIDE, Memories, build_recall_tool
from .persona import Persona
from .replies import Replies
from .text import split_message
from .tools import WEB_TOOLS_GUIDE, Tool, ToolRunner, suppress_embeds
from .triggers import Cooldown, Trigger, detect_trigger, is_ignored
from .vision import attach_images, is_image, load_images, supports_vision

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
# 比對 bot 名字時去掉的結尾稱呼（已正規化：片假名轉平假名、小寫）
NAME_HONORIFICS = ("さん", "ちゃん", "さま", "様", "せんぱい", "先輩", "醬", "san", "chan", "sama")


def normalize_name(name: str) -> str:
    """比對暱稱用的正規化：全形轉半形、不分大小寫、片假名轉平假名、去掉空白與符號、去掉結尾的稱呼。

    「サヤカ」「村野 沙耶香」「さやかさん」「SAYAKA-chan」會分別變成「さやか」「村野沙耶香」「さやか」「sayaka」。
    """
    s = unicodedata.normalize("NFKC", name).casefold()
    s = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)
    s = "".join(c for c in s if unicodedata.category(c)[0] in "LN")
    stripped = True
    while stripped:
        stripped = False
        for h in NAME_HONORIFICS:
            if s.endswith(h) and len(s) > len(h):
                s = s[: -len(h)]
                stripped = True
    return s


def display_author(name: str, config: Config) -> str:
    """顯示名稱可以自己改：撞名 bot 或像「SYSTEM」「系統管理員」的，在 prompt 裡標成一般成員。

    bot 的名字（BOT_DISPLAY_NAME、BOT_ALIASES、BOT_NAMES）用正規化後完整比對，
    中日文、片假名、羅馬拼音、加上「さん」等寫法都算撞名；「沙耶香推」這種只是包含名字的暱稱不算。
    """
    key = name.strip().casefold()
    reserved = {n.casefold() for n in RESERVED_AUTHOR_NAMES}
    bot_names = {normalize_name(n) for n in (config.bot_display_name, *config.bot_aliases, *config.bot_names)}
    bot_names.discard("")
    if key in reserved or key.startswith(("system", "系統")) or normalize_name(name) in bot_names:
        return f"{name}（成員暱稱）"
    return name


def describe_attachments(attachments: list[Any]) -> str:
    """附件在歷史紀錄裡的文字：圖片寫 [圖片:檔名]，其他檔案寫 [附件:檔名]。"""
    return " ".join(f"[{'圖片' if is_image(a) else '附件'}:{getattr(a, 'filename', '檔案')}]" for a in attachments)


def message_text(message: Any) -> str:
    text = (getattr(message, "clean_content", None) or getattr(message, "content", "") or "").strip()
    attachments = getattr(message, "attachments", None) or []
    if attachments:
        text = (text + " " if text else "") + describe_attachments(attachments)
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
        memories: Memories | None = None,
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
        self.memories = memories
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

        # 插話判斷只看文字；要回覆時才下載這則訊息的圖片
        model = self.config.model_default
        images = await self._images_for(getattr(message, "attachments", None) or [], model)
        runner = None
        if self.tools_enabled:
            # 插話不查資料（max_calls=0）：仍帶工具定義，prompt 開頭才和其他聊天一致、命中快取
            runner = self._tool_runner(message.author.id, content, prompt,
                                       max_calls=0 if trigger is Trigger.INTERJECT else None)
        return await self.generate(channel_id, prompt, trigger, model=model, tool_runner=runner, images=images)

    async def generate(self, channel_id: int, prompt: list[dict[str, str]], trigger: Trigger,
                       *, model: str, purpose: str = "chat", max_tokens: int | None = None,
                       thinking: str | None = None, total_timeout: float | None = None,
                       reasoning_effort: str | None = None, call_timeout: float | None = None,
                       tool_runner: ToolRunner | None = None, images: list[str] | None = None) -> Reply:
        """images：要附在最後一則訊息的圖片（data URL）。圖片被 API 拒絕時改用純文字重試一次。"""
        max_tokens = max_tokens or self.config.max_reply_tokens

        async def ask(messages: list[dict[str, Any]]) -> str:
            if tool_runner is not None:
                return await self.llm.chat_with_tools(messages, tool_runner, model=model, max_tokens=max_tokens,
                                                      purpose=purpose, max_rounds=tool_runner.max_calls)
            return await self.llm.chat(messages, model=model, max_tokens=max_tokens,
                                       purpose=purpose, thinking=thinking, total_timeout=total_timeout,
                                       reasoning_effort=reasoning_effort, call_timeout=call_timeout)

        try:
            try:
                # 圖片只在送出前附上；工具迴圈接在後面的訊息不受影響，圖片一直留在 user 訊息裡
                text = await ask(attach_images(prompt, images or [], self.config.vision_detail))
            except LLMError as exc:
                if not images or not isinstance(exc.__cause__, openai.BadRequestError):
                    raise
                log.warning("圖片被 API 拒絕（%s），改用純文字重試", exc)
                text = await ask(prompt)
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

    async def deep(self, channel_id: int, author: str, question: str, attachments: list[Any] | None = None) -> Reply:
        author = display_author(author, self.config)
        attachments = attachments or []
        content = f"{question} {describe_attachments(attachments)}" if attachments else question
        prompt = self.history.build_prompt(channel_id, self.persona.text, author, content, self._lore_text(),
                                           now=self._now())
        self.history.add(channel_id, author, content)
        images = await self._images_for(attachments, self.config.model_deep)
        # 思考模式的推理內容也算在 max_tokens 內，所以 /deep 給較大的上限；
        # 總時限要短於 Discord 互動的 15 分鐘，失敗時才來得及回覆錯誤訊息。
        # 思考模式、強度、單次逾時都明確指定，不靠模型名稱判斷（MODEL_DEEP 可以和一般聊天同一個模型）
        return await self.generate(channel_id, prompt, Trigger.MENTION, model=self.config.model_deep,
                                   purpose="deep", max_tokens=self.config.deep_max_tokens,
                                   thinking=self.config.thinking_deep,
                                   reasoning_effort=self.config.reasoning_effort_deep,
                                   call_timeout=self.config.deep_api_timeout_seconds,
                                   total_timeout=self.config.deep_total_timeout_seconds, images=images)

    async def _images_for(self, attachments: list[Any], model: str) -> list[str]:
        if not attachments or not any(is_image(a) for a in attachments):
            return []
        if not supports_vision(model, self.config):
            # 不支援看圖的模型收到圖片不會報錯，只會說看不到，所以乾脆不送
            log.info("模型 %s 不支援看圖（或 VISION_ENABLED=false），圖片只以檔名附上", model)
            return []
        return await load_images(attachments, self.config)

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
                                               tools=[t.spec() for t in self.tools] if self.tools_enabled else None)
        except (BudgetExceeded, LLMError, ContentFiltered):
            return False
        threshold = self.config.interject_threshold
        log.info("插話判斷（頻道 %s）：p=%.2f，門檻 %.2f → %s", channel_id, p, threshold,
                 "插話" if p >= threshold else "不插話")
        return p >= threshold

    @property
    def recall_enabled(self) -> bool:
        return self.memories is not None and self.memories.enabled

    @property
    def tools(self) -> list[Tool]:
        """這次可用的工具：回憶（有回憶卡時）＋查資料（WEB_TOOLS_ENABLED 時）。順序固定，不影響快取。"""
        tools = [build_recall_tool(self.memories)] if self.recall_enabled else []
        if self.config.web_tools_enabled:
            tools += self.web_tools
        return tools

    @property
    def tools_enabled(self) -> bool:
        return bool(self.tools)

    def _with_tool_guide(self, prompt: list[dict[str, str]]) -> list[dict[str, str]]:
        """啟用工具時，在開頭的 system 訊息（原作資料、人設）之後加上工具說明。內容固定，不影響快取。"""
        guides = []
        if self.recall_enabled:
            guides.append(RECALL_GUIDE)
        if self.config.web_tools_enabled and self.web_tools:
            guides.append(WEB_TOOLS_GUIDE)
        if not guides:
            return prompt
        i = 0
        while i < len(prompt) and prompt[i]["role"] == "system":
            i += 1
        return prompt[:i] + [{"role": "system", "content": "\n\n".join(guides)}] + prompt[i:]

    def _tool_runner(self, user_id: int, question: str, prompt: list[dict[str, str]],
                     *, max_calls: int | None = None) -> ToolRunner:
        day = self.llm.today()
        limit = self.config.web_daily_limit_per_user
        storage = self.history.storage
        runner = ToolRunner(
            tools=self.tools,
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
