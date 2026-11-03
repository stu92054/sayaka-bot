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
CONTENT_FILTER_REPLY = "哼，這種話本小姐才不說呢。"

# finish_reason 為這些值時表示生成被打斷，視同失敗並重試
RETRYABLE_FINISH_REASONS = {"insufficient_system_resource", "aborted"}


class BudgetExceeded(Exception):
    """今日 token 用量已達上限。"""


class LLMError(Exception):
    """重試後仍然失敗。"""


class ContentFiltered(Exception):
    """輸出被 DeepSeek 內容過濾（finish_reason=content_filter）。"""


class _Interrupted(Exception):
    pass


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

    def now(self) -> datetime:
        return self._now()

    def today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def tokens_today(self) -> int:
        return self.storage.tokens_on(self.today())

    def over_budget(self) -> bool:
        return self.tokens_today() >= self.config.daily_token_budget

    def usage_today(self) -> list[UsageRow]:
        return self.storage.usage_by_model(self.today())

    def estimate_cost(self, rows: list[UsageRow]) -> float:
        """今日預估費用（元）。每次呼叫時已依高峰／空閒時段算好並存入資料庫。"""
        return sum(r.cost for r in rows)

    def _is_deep(self, model: str) -> bool:
        return model == self.config.model_deep and model != self.config.model_default

    def thinking_for(self, model: str) -> str:
        return self.config.thinking_deep if self._is_deep(model) else self.config.thinking_default

    def effort_for(self, model: str) -> str:
        return self.config.reasoning_effort_deep if self._is_deep(model) else self.config.reasoning_effort_default

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        purpose: str = "chat",
        temperature: float | None = None,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        if self.over_budget():
            raise BudgetExceeded()
        model = model or self.config.model_default
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_reply_tokens,
        }
        # 思考模式透過 extra_body 傳入（OpenAI SDK 沒有 thinking 參數）；模型預設是開啟
        thinking = thinking or self.thinking_for(model)
        if thinking != "omit":
            kwargs["extra_body"] = {"thinking": {"type": thinking}}
        if thinking != "disabled":
            effort = reasoning_effort or self.effort_for(model)
            if effort != "omit":
                kwargs["reasoning_effort"] = effort
        # 思考模式不支援 temperature（傳了不報錯但不生效），只在非思考模式送出
        if temperature is not None and thinking == "disabled":
            kwargs["temperature"] = temperature

        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self.client.chat.completions.create(**kwargs)
                self._record(model, purpose, getattr(resp, "usage", None))
                choice = resp.choices[0]
                finish = getattr(choice, "finish_reason", None)
                if finish in RETRYABLE_FINISH_REASONS:
                    raise _Interrupted(finish)
            except Exception as exc:  # noqa: BLE001 - 任何 API/網路錯誤都要重試
                last_exc = exc
                log.warning("DeepSeek 呼叫失敗（第 %d/%d 次，%s）：%s",
                            attempt, self.max_attempts, purpose,
                            f"finish_reason={exc}" if isinstance(exc, _Interrupted) else type(exc).__name__)
                if attempt < self.max_attempts:
                    await self._sleep(self.base_delay * (2 ** (attempt - 1)))
                continue
            if finish == "content_filter":
                raise ContentFiltered()
            # 思維鏈在 message.reasoning_content；沒帶 tools 時下一輪不需回傳，只取最終回答
            content = (getattr(choice.message, "content", None) or "").strip()
            if finish == "length":
                # 推理內容也算在 max_tokens 內，思考太久可能沒有留下最終回答
                log.warning("回覆達到 max_tokens 上限（%s，max_tokens=%s，最終回答 %d 字）",
                            purpose, kwargs["max_tokens"], len(content))
            return content
        raise LLMError(f"DeepSeek 呼叫失敗：{type(last_exc).__name__}") from last_exc

    def _record(self, model: str, purpose: str, usage: Any) -> None:
        if usage is None:
            return
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        if hit is None:  # 與 prompt_tokens_details.cached_tokens 相同
            hit = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
        hit = int(hit or 0)
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        miss = int(miss) if miss is not None else max(prompt - hit, 0)
        reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
        reasoning = int(reasoning or 0)
        now = self._now()
        peak = self.config.is_peak(now)
        cost = self.config.call_cost(model, hit, miss, completion, now)
        self.storage.record_usage(self.today(), model, purpose, prompt, completion, hit, miss, cost=cost, peak=peak,
                                  reasoning_tokens=reasoning)
        log.info("用量 %s/%s：in=%d (hit=%d) out=%d (推理 %d) ¥%.5f%s", model, purpose, prompt, hit, completion,
                 reasoning, cost, "（高峰）" if peak else "")
