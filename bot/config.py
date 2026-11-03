"""從環境變數（.env）載入設定。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class ModelPrice:
    """每百萬 tokens 的「高峰時段」價格（人民幣元）。空閒時段乘上 offpeak_ratio。"""

    cache_hit: float
    cache_miss: float
    output: float


# DeepSeek 官方價目表（高峰時段，元 / 百萬 tokens）。可用 PRICE_TABLE 覆寫。
_FLASH = ModelPrice(cache_hit=0.04, cache_miss=2.0, output=8.0)
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "deepseek-flash": _FLASH,
    # 舊名稱仍可呼叫，由新版 Flash 服務並以 Flash 價格計費
    "deepseek-v4-flash": _FLASH,
    "deepseek-v4-flash-vision-exp": _FLASH,
    "deepseek-v4-pro": ModelPrice(cache_hit=0.30, cache_miss=9.0, output=27.0),
}
FALLBACK_PRICE = _FLASH

# 高峰時段：北京時間週一至週五 9:00-12:00、14:00-18:00（不含中國法定節假日）
PRICING_TZ = ZoneInfo("Asia/Shanghai")
PEAK_HOURS = ((9, 12), (14, 18))
THINKING_CHOICES = ("enabled", "disabled", "omit")


def _strip_comment(value: str) -> str:
    # 容許 "30     # 註解" 這種寫法
    if " #" in value:
        value = value.split(" #", 1)[0]
    return value.strip()


def _get(env: dict[str, str], key: str, default: str = "") -> str:
    raw = env.get(key)
    if raw is None:
        return default
    value = _strip_comment(raw)
    return value if value != "" else default


def _int_list(value: str) -> frozenset[int]:
    out = set()
    for part in value.split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return frozenset(out)


def _str_list(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in value.split(",") if p.strip())


def _date_list(value: str) -> frozenset[date]:
    return frozenset(date.fromisoformat(p) for p in _str_list(value))


def _thinking(value: str, key: str) -> str:
    value = value.lower()
    if value not in THINKING_CHOICES:
        raise ValueError(f"{key} 必須是 {'/'.join(THINKING_CHOICES)} 之一")
    return value


def _price_table(value: str) -> dict[str, ModelPrice]:
    """格式：model:hit:miss:output;model2:hit:miss:output"""
    prices = dict(DEFAULT_PRICES)
    for entry in value.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        name, hit, miss, out = entry.rsplit(":", 3)
        prices[name.strip()] = ModelPrice(float(hit), float(miss), float(out))
    return prices


@dataclass(frozen=True)
class Config:
    discord_token: str
    deepseek_api_key: str
    allowed_guild_ids: frozenset[int] = frozenset()
    admin_user_ids: frozenset[int] = frozenset()
    bot_names: tuple[str, ...] = ("大小姐",)
    model_default: str = "deepseek-flash"
    model_deep: str = "deepseek-v4-pro"
    # 兩個模型預設都開啟思考模式；一般聊天關閉以節省 token、降低延遲，/deep 開啟
    thinking_default: str = "disabled"
    thinking_deep: str = "enabled"
    deep_max_tokens: int = 8000
    history_max_messages: int = 30
    daily_token_budget: int = 2_000_000
    user_cooldown_seconds: float = 5.0
    max_reply_tokens: int = 600
    random_interject_rate: float = 0.0
    deepseek_base_url: str = "https://api.deepseek.com"
    persona_path: Path = Path("persona.md")
    db_path: Path = Path("data/bot.sqlite3")
    timezone: str = "Asia/Taipei"
    log_level: str = "INFO"
    prices: dict[str, ModelPrice] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    offpeak_ratio: float = 0.5
    cn_holidays: frozenset[date] = frozenset()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *, load_dotenv_file: bool = True) -> "Config":
        if env is None:
            if load_dotenv_file:
                load_dotenv()
            env = dict(os.environ)
        return cls(
            discord_token=_get(env, "DISCORD_TOKEN"),
            deepseek_api_key=_get(env, "DEEPSEEK_API_KEY"),
            allowed_guild_ids=_int_list(_get(env, "ALLOWED_GUILD_IDS")),
            admin_user_ids=_int_list(_get(env, "ADMIN_USER_IDS")),
            bot_names=_str_list(_get(env, "BOT_NAMES", "大小姐")),
            model_default=_get(env, "MODEL_DEFAULT", "deepseek-flash"),
            model_deep=_get(env, "MODEL_DEEP", "deepseek-v4-pro"),
            thinking_default=_thinking(_get(env, "THINKING_DEFAULT", "disabled"), "THINKING_DEFAULT"),
            thinking_deep=_thinking(_get(env, "THINKING_DEEP", "enabled"), "THINKING_DEEP"),
            deep_max_tokens=int(_get(env, "DEEP_MAX_TOKENS", "8000")),
            history_max_messages=int(_get(env, "HISTORY_MAX_MESSAGES", "30")),
            daily_token_budget=int(_get(env, "DAILY_TOKEN_BUDGET", "2000000")),
            user_cooldown_seconds=float(_get(env, "USER_COOLDOWN_SECONDS", "5")),
            max_reply_tokens=int(_get(env, "MAX_REPLY_TOKENS", "600")),
            random_interject_rate=float(_get(env, "RANDOM_INTERJECT_RATE", "0")),
            deepseek_base_url=_get(env, "DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            persona_path=Path(_get(env, "PERSONA_PATH", "persona.md")),
            db_path=Path(_get(env, "DB_PATH", "data/bot.sqlite3")),
            timezone=_get(env, "BOT_TIMEZONE", "Asia/Taipei"),
            log_level=_get(env, "LOG_LEVEL", "INFO").upper(),
            prices=_price_table(_get(env, "PRICE_TABLE")),
            offpeak_ratio=float(_get(env, "PRICE_OFFPEAK_RATIO", "0.5")),
            cn_holidays=_date_list(_get(env, "CN_HOLIDAYS")),
        )

    def validate(self) -> list[str]:
        """回傳缺少或不合理的設定項目。"""
        problems = []
        if not self.discord_token:
            problems.append("DISCORD_TOKEN 未設定")
        if not self.deepseek_api_key:
            problems.append("DEEPSEEK_API_KEY 未設定")
        if not self.allowed_guild_ids:
            problems.append("ALLOWED_GUILD_IDS 未設定，bot 不會在任何伺服器回應")
        if self.history_max_messages < 2:
            problems.append("HISTORY_MAX_MESSAGES 至少要 2")
        if not 0 <= self.random_interject_rate <= 1:
            problems.append("RANDOM_INTERJECT_RATE 必須介於 0 和 1")
        return problems

    def price_for(self, model: str) -> ModelPrice:
        return self.prices.get(model, FALLBACK_PRICE)

    def is_peak(self, when: datetime) -> bool:
        """when 必須帶時區。"""
        local = when.astimezone(PRICING_TZ)
        if local.weekday() >= 5 or local.date() in self.cn_holidays:
            return False
        return any(start <= local.hour < end for start, end in PEAK_HOURS)

    def call_cost(self, model: str, cache_hit: int, cache_miss: int, output: int, when: datetime) -> float:
        """單次呼叫費用（元）。"""
        p = self.price_for(model)
        ratio = 1.0 if self.is_peak(when) else self.offpeak_ratio
        return (cache_hit * p.cache_hit + cache_miss * p.cache_miss + output * p.output) * ratio / 1_000_000

    def secrets(self) -> list[str]:
        return [s for s in (self.discord_token, self.deepseek_api_key) if s]

    def __repr__(self) -> str:  # 避免意外把 token 印進 log
        return (
            f"Config(guilds={sorted(self.allowed_guild_ids)}, admins={len(self.admin_user_ids)}, "
            f"models=({self.model_default}, {self.model_deep}), token=***, api_key=***)"
        )
