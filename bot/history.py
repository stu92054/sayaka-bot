"""頻道歷史、摘要壓縮、prompt 組裝。"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from .llm import BudgetExceeded, ContentFiltered, LLMClient, LLMError
from .storage import Storage, StoredMessage

log = logging.getLogger(__name__)

SUMMARY_SYSTEM_PROMPT = (
    "你是對話紀錄整理員。請把提供的 Discord 群組對話壓縮成一段精簡的中文摘要，"
    "保留：誰說了什麼重要的事、大家的偏好或約定、尚未結束的話題。"
    "若有「先前的摘要」，請把它與新對話合併成一段新的摘要。"
    "只輸出摘要本身，不超過 400 字。"
)


def format_line(author_name: str, content: str) -> str:
    return f"{author_name}: {content}"


def merge_consecutive(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """合併相鄰同角色的訊息（部分模型不接受連續的 user/assistant）。system 不合併。"""
    merged: list[dict[str, str]] = []
    for m in messages:
        if merged and m["role"] != "system" and merged[-1]["role"] == m["role"]:
            merged[-1] = {"role": m["role"], "content": merged[-1]["content"] + "\n" + m["content"]}
        else:
            merged.append(dict(m))
    return merged


def build_prompt(
    persona: str,
    summary: str | None,
    recent: list[StoredMessage],
    current_author: str,
    current_content: str,
    lore: str | None = None,
) -> list[dict[str, str]]:
    """順序固定：原作資料 → persona 全文 → 頻道摘要 → 近期訊息 → 當前訊息。

    原作資料與 persona 都放在最前面且內容不變，讓 DeepSeek 的前綴快取能命中。
    """
    messages: list[dict[str, str]] = []
    if lore:
        messages.append({"role": "system", "content": lore})
    messages.append({"role": "system", "content": persona})
    if summary:
        messages.append({"role": "system", "content": f"先前發生過的事（摘要）：\n{summary}"})
    for m in recent:
        if m.is_bot:
            messages.append({"role": "assistant", "content": m.content})
        else:
            messages.append({"role": "user", "content": format_line(m.author_name, m.content)})
    messages.append({"role": "user", "content": format_line(current_author, current_content)})
    return merge_consecutive(messages)


class HistoryManager:
    def __init__(self, storage: Storage, llm: LLMClient, max_messages: int):
        self.storage = storage
        self.llm = llm
        self.max_messages = max_messages
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def add(self, channel_id: int, author_name: str, content: str, is_bot: bool = False) -> None:
        self.storage.add_message(channel_id, author_name, content, is_bot)

    def recent(self, channel_id: int) -> list[StoredMessage]:
        return self.storage.recent_messages(channel_id, self.max_messages)

    def summary(self, channel_id: int) -> str | None:
        return self.storage.get_summary(channel_id)

    def reset(self, channel_id: int) -> None:
        self.storage.clear_channel(channel_id)

    def build_prompt(self, channel_id: int, persona: str, author: str, content: str,
                     lore: str | None = None) -> list[dict[str, str]]:
        return build_prompt(persona, self.summary(channel_id), self.recent(channel_id), author, content, lore)

    def needs_summary(self, channel_id: int) -> bool:
        return self.storage.count_messages(channel_id) > self.max_messages

    async def maybe_summarize(self, channel_id: int) -> bool:
        """超過上限時，把較舊的訊息壓成摘要。回傳是否有更新摘要。

        為了不要每多一則就呼叫一次 API，一次會把訊息壓到剩下一半上限，
        摘要因此不常變動，也有利前綴快取。
        """
        async with self._locks[channel_id]:
            count = self.storage.count_messages(channel_id)
            if count <= self.max_messages:
                return False
            keep = max(self.max_messages // 2, 1)
            old = self.storage.oldest_messages(channel_id, count - keep)
            if not old:
                return False
            prev = self.storage.get_summary(channel_id)
            transcript = "\n".join(
                format_line(m.author_name + ("（bot）" if m.is_bot else ""), m.content) for m in old
            )
            user_content = (f"先前的摘要：\n{prev}\n\n" if prev else "") + f"新的對話：\n{transcript}"
            try:
                new_summary = await self.llm.chat(
                    [
                        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    model=self.llm.config.model_default,
                    max_tokens=600,
                    purpose="summary",
                    thinking="disabled",
                )
            except (BudgetExceeded, LLMError, ContentFiltered) as exc:
                log.warning("頻道 %s 摘要失敗：%s", channel_id, type(exc).__name__)
                self._hard_trim(channel_id, count)
                return False
            if not new_summary:
                self._hard_trim(channel_id, count)
                return False
            self.storage.set_summary(channel_id, new_summary)
            self.storage.delete_messages_upto(channel_id, old[-1].id)
            log.info("頻道 %s 已壓縮 %d 則訊息成摘要", channel_id, len(old))
            return True

    def _hard_trim(self, channel_id: int, count: int) -> None:
        """摘要失敗時避免無限累積：超過兩倍上限就直接丟掉最舊的。"""
        limit = self.max_messages * 2
        if count > limit:
            old = self.storage.oldest_messages(channel_id, count - limit)
            if old:
                self.storage.delete_messages_upto(channel_id, old[-1].id)
