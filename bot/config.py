"""從環境變數（.env）載入設定。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class ModelPrice:
    """每百萬 tokens 的價格（USD）。"""

    cache_hit: float
    cache_miss: float
    output: float


# 預設價格僅供 /usage 估算費用使用，請以 DeepSeek 官方價目表為準，
# 可用 PRICE_TABLE 環境變數覆寫。
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "deepseek-v4-flash": ModelPrice(cache_hit=0.028, cache_miss=0.28, output=0.42),
    "deepseek-v4-pro": ModelPrice(cache_hit=0.145, cache_miss=1.74, output=3.48),
}
FALLBACK_PRICE = ModelPrice(cache_hit=0.028, cache_miss=0.28, output=0.42)


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
    model_default: str = "deepseek-v4-flash"
    model_deep: str = "deepseek-v4-pro"
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
            model_default=_get(env, "MODEL_DEFAULT", "deepseek-v4-flash"),
            model_deep=_get(env, "MODEL_DEEP", "deepseek-v4-pro"),
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

    def secrets(self) -> list[str]:
        return [s for s in (self.discord_token, self.deepseek_api_key) if s]

    def __repr__(self) -> str:  # 避免意外把 token 印進 log
        return (
            f"Config(guilds={sorted(self.allowed_guild_ids)}, admins={len(self.admin_user_ids)}, "
            f"models=({self.model_default}, {self.model_deep}), token=***, api_key=***)"
        )
