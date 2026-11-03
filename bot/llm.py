"""DeepSeek 呼叫：重試、預算檢查、用量紀錄。"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import openai
from openai import AsyncOpenAI

from .config import Config
from .replies import DEFAULT_REPLIES
from .storage import Storage, UsageRow

log = logging.getLogger(__name__)

# 固定訊息改由 replies.toml 提供（見 bot/replies.py）；這裡只是中性預設值的別名
BUDGET_EXCEEDED_REPLY = DEFAULT_REPLIES["budget_exceeded"]
ERROR_REPLY = DEFAULT_REPLIES["error"]
CONTENT_FILTER_REPLY = DEFAULT_REPLIES["content_filter"]

# 這些錯誤重試也不會成功（key 錯誤、沒有權限、參數錯誤、模型不存在…），直接失敗
NON_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    openai.AuthenticationError,      # 401
    openai.PermissionDeniedError,    # 403
    openai.BadRequestError,          # 400
    openai.NotFoundError,            # 404
    openai.UnprocessableEntityError,  # 422
)


def describe_error(exc: BaseException) -> str:
    """給 log 用的錯誤描述：類別名稱與 HTTP 狀態碼，不含訊息內容（可能帶有 key 片段）。"""
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f"（HTTP {status}）" if status is not None else "")


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


class _ServerError(Exception):
    """HTTP 200 但回應裡沒有 choices。

    DeepSeek 忙碌時會先回 200、持續送換行保持連線，排隊逾時後才在內容裡放 {"error": {...}}；
    SDK 照樣解析成 ChatCompletion，只是 choices 為 None。
    """


def server_error_message(resp: Any) -> str:
    """取出伺服器在回應內容裡附的錯誤訊息（不含請求內容與 key，可以寫進 log）。"""
    error = getattr(resp, "error", None)
    if error is None:
        extra = getattr(resp, "model_extra", None) or {}
        error = extra.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error
    return str(error)[:200] if error else "回應沒有 choices"


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
        calendar: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.storage = storage
        # 關閉 SDK 內建重試（預設 2 次），統一由這裡的重試邏輯控制
        self.client = client or AsyncOpenAI(api_key=config.deepseek_api_key, base_url=config.deepseek_base_url,
                                            max_retries=0)
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self._sleep = sleep
        self._monotonic = monotonic
        self.calendar = calendar  # HolidayCalendar，判斷高峰時段用
        tz = ZoneInfo(config.timezone)
        self._now = now or (lambda: datetime.now(tz))

    def now(self) -> datetime:
        return self._now()

    def is_peak(self, when: datetime | None = None) -> bool:
        return self.config.is_peak(when or self._now(), self.calendar)

    def today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def tokens_today(self) -> int:
        return self.storage.tokens_on(self.today())

    def cost_today(self) -> float:
        return self.storage.cost_on(self.today(), self.config.price_currency)

    def budget_status(self) -> str | None:
        """超過預算時回傳原因，否則 None。兩種上限設為 0 時不限制。"""
        cost_budget = self.config.daily_cost_budget
        if cost_budget > 0:
            spent = self.cost_today()
            if spent >= cost_budget:
                return f"今日費用 {self.config.money(spent)} 已達上限 {self.config.money(cost_budget, 2)}"
        token_budget = self.config.daily_token_budget
        if token_budget > 0:
            used = self.tokens_today()
            if used >= token_budget:
                return f"今日 tokens {used:,} 已達上限 {token_budget:,}"
        return None

    def over_budget(self) -> bool:
        return self.budget_status() is not None

    def usage_today(self) -> list[UsageRow]:
        return self.storage.usage_by_model(self.today(), self.config.price_currency)

    def estimate_cost(self, rows: list[UsageRow]) -> float:
        """今日預估費用（幣別同 PRICE_CURRENCY）。每次呼叫時已依高峰／空閒時段算好並存入資料庫。"""
        return sum(r.cost for r in rows)

    def _is_deep(self, model: str) -> bool:
        return model == self.config.model_deep and model != self.config.model_default

    def thinking_for(self, model: str) -> str:
        return self.config.thinking_deep if self._is_deep(model) else self.config.thinking_default

    def effort_for(self, model: str) -> str:
        return self.config.reasoning_effort_deep if self._is_deep(model) else self.config.reasoning_effort_default

    def timeout_for(self, model: str) -> float:
        return self.config.deep_api_timeout_seconds if self._is_deep(model) else self.config.api_timeout_seconds

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
        total_timeout: float | None = None,
    ) -> str:
        """total_timeout：含重試與等待的總時限（秒）；None = 只受單次逾時與重試次數限制。"""
        choice = await self._complete(messages, model=model, max_tokens=max_tokens, purpose=purpose,
                                      temperature=temperature, thinking=thinking, reasoning_effort=reasoning_effort,
                                      total_timeout=total_timeout)
        # 思維鏈在 message.reasoning_content；沒帶 tools 時下一輪不需回傳，只取最終回答
        return (getattr(choice.message, "content", None) or "").strip()

    async def yes_probability(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        purpose: str = "judge",
        yes: tuple[str, ...] = ("是",),
        no: tuple[str, ...] = ("否",),
        total_timeout: float | None = None,
    ) -> float:
        """讓模型回答一個字（是／否），用 logprobs 算出回答「是」的機率（0～1）。

        沒有 logprobs 時退回看回答的文字：「是」= 1、其他 = 0。
        """
        choice = await self._complete(messages, model=model, max_tokens=1, purpose=purpose, temperature=0,
                                      thinking="disabled", logprobs=True, expect_truncation=True,
                                      total_timeout=total_timeout)
        return yes_probability_from(choice, yes, no)

    async def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None,
        max_tokens: int | None,
        purpose: str,
        temperature: float | None = None,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
        logprobs: bool = False,
        expect_truncation: bool = False,
        total_timeout: float | None = None,
    ) -> Any:
        """呼叫 API（含預算檢查、單次逾時、總時限、重試、用量紀錄），回傳 choices[0]。

        chat 與 yes_probability 都經過這裡，所以插話判斷也有逾時保護。
        """
        reason = self.budget_status()
        if reason is not None:
            log.warning("超過每日預算，不呼叫 API（%s）：%s", purpose, reason)
            raise BudgetExceeded(reason)
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
        if logprobs:
            kwargs["logprobs"] = True
            kwargs["top_logprobs"] = 10

        last_exc: Exception | None = None
        detail = ""
        started = self._monotonic()
        for attempt in range(1, self.max_attempts + 1):
            timeout = self.timeout_for(model)
            if total_timeout is not None:
                remaining = total_timeout - (self._monotonic() - started)
                if remaining <= 0:
                    log.warning("DeepSeek 呼叫超過總時限 %.0f 秒，不再重試（%s）", total_timeout, purpose)
                    break
                timeout = min(timeout, remaining)
            try:
                resp = await asyncio.wait_for(self.client.chat.completions.create(**kwargs), timeout)
                self._record(model, purpose, getattr(resp, "usage", None))
                if not getattr(resp, "choices", None):
                    raise _ServerError(server_error_message(resp))
                choice = resp.choices[0]
                finish = getattr(choice, "finish_reason", None)
                if finish in RETRYABLE_FINISH_REASONS:
                    raise _Interrupted(finish)
            except NON_RETRYABLE_ERRORS as exc:
                log.error("DeepSeek 呼叫失敗，不重試（%s）：%s", purpose, describe_error(exc))
                raise LLMError(f"DeepSeek 呼叫失敗：{describe_error(exc)}") from exc
            except Exception as exc:  # noqa: BLE001 - 網路錯誤、逾時、429、5xx 等都重試
                last_exc = exc
                if isinstance(exc, _Interrupted):
                    detail = f"finish_reason={exc}"
                elif isinstance(exc, _ServerError):
                    detail = f"伺服器回報錯誤：{exc}"
                elif isinstance(exc, asyncio.TimeoutError):
                    detail = f"逾時（{timeout:.0f} 秒）"
                else:
                    detail = describe_error(exc)
                log.warning("DeepSeek 呼叫失敗（第 %d/%d 次，%s）：%s", attempt, self.max_attempts, purpose, detail)
                if attempt < self.max_attempts:
                    await self._sleep(self.base_delay * (2 ** (attempt - 1)))
                continue
            if finish == "content_filter":
                raise ContentFiltered()
            if finish == "length" and not expect_truncation:
                # 推理內容也算在 max_tokens 內，思考太久可能沒有留下最終回答
                content = (getattr(choice.message, "content", None) or "").strip()
                log.warning("回覆達到 max_tokens 上限（%s，max_tokens=%s，最終回答 %d 字）",
                            purpose, kwargs["max_tokens"], len(content))
            return choice
        raise LLMError(f"DeepSeek 呼叫失敗：{detail if last_exc is not None else '超過總時限'}") from last_exc

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
        peak = self.config.is_peak(now, self.calendar)
        cost = self.config.call_cost(model, hit, miss, completion, now, self.calendar)
        self.storage.record_usage(self.today(), model, purpose, prompt, completion, hit, miss, cost=cost, peak=peak,
                                  reasoning_tokens=reasoning, currency=self.config.price_currency)
        log.info("用量 %s/%s：in=%d (hit=%d) out=%d (推理 %d) %s%s", model, purpose, prompt, hit, completion,
                 reasoning, self.config.money(cost, 6), "（高峰）" if peak else "")


def yes_probability_from(choice: Any, yes: tuple[str, ...], no: tuple[str, ...]) -> float:
    """從第一個 token 的 top_logprobs 算 P(是) / (P(是) + P(否))。"""
    content = getattr(getattr(choice, "logprobs", None), "content", None)
    if content:
        first = content[0]
        candidates = list(getattr(first, "top_logprobs", None) or []) or [first]
        p_yes = p_no = 0.0
        for c in candidates:
            token = (getattr(c, "token", "") or "").strip()
            prob = math.exp(getattr(c, "logprob", -math.inf))
            if token and token.startswith(yes):
                p_yes += prob
            elif token and token.startswith(no):
                p_no += prob
        if p_yes + p_no > 0:
            return p_yes / (p_yes + p_no)
    # 沒有 logprobs，或候選中找不到是／否（例如 token 被切成位元組）：看回答文字
    text = (getattr(getattr(choice, "message", None), "content", None) or "").strip()
    return 1.0 if text.startswith(yes) else 0.0
