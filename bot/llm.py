"""DeepSeek 呼叫：重試、預算檢查、用量紀錄。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from .config import Config
from .storage import Storage, UsageRow

log = logging.getLogger(__name__)

BUDGET_EXCEEDED_REPLY = "哼，本小姐今天說太多話了，明天再說吧。"
ERROR_REPLY = "……本小姐現在沒心情說話，等一下再來。（連線出了點問題）"


class BudgetExceeded(Exception):
    """今日 token 用量已達上限。"""


class LLMError(Exception):
    """重試後仍然失敗。"""


class LLMClient:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        client: Any | None = None,
        *,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.storage = storage
        self.client = client or AsyncOpenAI(api_key=config.deepseek_api_key, base_url=config.deepseek_base_url)
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._sleep = sleep
        tz = ZoneInfo(config.timezone)
        self._now = now or (lambda: datetime.now(tz))

    def today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def tokens_today(self) -> int:
        return self.storage.tokens_on(self.today())

    def over_budget(self) -> bool:
        return self.tokens_today() >= self.config.daily_token_budget

    def usage_today(self) -> list[UsageRow]:
        return self.storage.usage_by_model(self.today())

    def estimate_cost(self, rows: list[UsageRow]) -> float:
        total = 0.0
        for r in rows:
            p = self.config.price_for(r.model)
            total += (r.cache_hit_tokens * p.cache_hit + r.cache_miss_tokens * p.cache_miss
                      + r.completion_tokens * p.output) / 1_000_000
        return total

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        purpose: str = "chat",
        temperature: float | None = None,
    ) -> str:
        if self.over_budget():
            raise BudgetExceeded()
        model = model or self.config.model_default
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_reply_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature

        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self.client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - 任何 API/網路錯誤都要重試
                last_exc = exc
                log.warning("DeepSeek 呼叫失敗（第 %d/%d 次，%s）：%s",
                            attempt, self.max_attempts, purpose, type(exc).__name__)
                if attempt < self.max_attempts:
                    await self._sleep(self.base_delay * (2 ** (attempt - 1)))
                continue
            self._record(model, purpose, getattr(resp, "usage", None))
            try:
                content = resp.choices[0].message.content or ""
            except (AttributeError, IndexError):
                content = ""
            return content.strip()
        raise LLMError(f"DeepSeek 呼叫失敗：{type(last_exc).__name__}") from last_exc

    def _record(self, model: str, purpose: str, usage: Any) -> None:
        if usage is None:
            return
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        miss = int(miss) if miss is not None else max(prompt - hit, 0)
        self.storage.record_usage(self.today(), model, purpose, prompt, completion, hit, miss)
        log.info("用量 %s/%s：in=%d (hit=%d) out=%d", model, purpose, prompt, hit, completion)
