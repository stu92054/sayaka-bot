"""從環境變數（.env）載入設定。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .holidays import HolidayCalendar

from dotenv import load_dotenv


@dataclass(frozen=True)
class ModelPrice:
    """每百萬 tokens 的「高峰時段」價格（幣別見 PRICE_CURRENCY）。空閒時段乘上 offpeak_ratio。"""

    cache_hit: float
    cache_miss: float
    output: float


def _price_table_for(flash: ModelPrice, pro: ModelPrice) -> dict[str, ModelPrice]:
    return {
        "deepseek-flash": flash,
        # 舊名稱仍可呼叫，由新版 Flash 服務並以 Flash 價格計費
        "deepseek-v4-flash": flash,
        "deepseek-v4-flash-vision-exp": flash,
        "deepseek-v4-pro": pro,
    }


# DeepSeek 官方價目表（高峰時段，每百萬 tokens）。依帳戶的計價幣別選用，可用 PRICE_TABLE 覆寫。
PRICE_TABLES: dict[str, dict[str, ModelPrice]] = {
    # 英文版價目表（美元帳戶）
    "USD": _price_table_for(
        flash=ModelPrice(cache_hit=0.006, cache_miss=0.3, output=1.2),
        pro=ModelPrice(cache_hit=0.044, cache_miss=1.32, output=3.96),
    ),
    # 中文版價目表（人民幣帳戶）
    "CNY": _price_table_for(
        flash=ModelPrice(cache_hit=0.04, cache_miss=2.0, output=8.0),
        pro=ModelPrice(cache_hit=0.30, cache_miss=9.0, output=27.0),
    ),
}
CURRENCY_SYMBOLS = {"USD": "$", "CNY": "¥"}
# Search／Fetch API 每次成功請求的價格（不分高峰時段）：USD $0.75、CNY ¥5 / 每 1,000 次
WEB_REQUEST_PRICES: dict[str, float] = {"USD": 0.75 / 1000, "CNY": 5.0 / 1000}
FALLBACK_MODEL = "deepseek-flash"  # 不認得的模型以 flash 價格估算

# 高峰時段：北京時間週一至週五 9:00-12:00、14:00-18:00（不含中國法定節假日）
PRICING_TZ = ZoneInfo("Asia/Shanghai")
PEAK_HOURS = ((9, 12), (14, 18))
THINKING_CHOICES = ("enabled", "disabled", "omit")
# API 規格的 reasoning_effort 列舉值為 none/low/high/max（none = 關閉思考，請改用 THINKING_*=disabled）
# omit = 不送參數（模型預設 high）
EFFORT_CHOICES = ("low", "high", "max", "omit")


def _strip_comment(value: str) -> str:
    # 容許 "30     # 註解" 這種寫法；整個值只有註解（"# 註解"）時視為空值
    if value.lstrip().startswith("#"):
        return ""
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


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _holiday_sources(value: str) -> tuple[str, ...]:
    from .holidays import DEFAULT_SOURCES

    if not value:
        return DEFAULT_SOURCES
    if value.strip().lower() in ("none", "off", "disabled"):
        return ()
    return _str_list(value)


def _lore_dir(value: str) -> Path | None:
    if value.strip().lower() in ("none", "off", "disabled"):
        return None
    return Path(value)


def _effort(value: str, key: str) -> str:
    value = value.lower()
    if value not in EFFORT_CHOICES:
        raise ValueError(f"{key} 必須是 {'/'.join(EFFORT_CHOICES)} 之一")
    return value


VISION_DETAIL_CHOICES = ("low", "high", "original", "auto")


def _vision_detail(value: str) -> str:
    value = value.strip().lower()
    if value not in VISION_DETAIL_CHOICES:
        raise ValueError(f"VISION_DETAIL 必須是 {'/'.join(VISION_DETAIL_CHOICES)} 之一")
    return value


def _currency(value: str) -> str:
    value = value.strip().upper()
    if value not in PRICE_TABLES:
        raise ValueError(f"PRICE_CURRENCY 必須是 {'/'.join(PRICE_TABLES)} 之一")
    return value


def _price_table(value: str) -> dict[str, ModelPrice]:
    """覆寫用的價格，格式：model:hit:miss:output;model2:hit:miss:output（幣別同 PRICE_CURRENCY）"""
    prices: dict[str, ModelPrice] = {}
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
    # 名字觸發；空 = 不使用名字觸發（只靠 @mention、回覆 bot、私訊）
    bot_names: tuple[str, ...] = ()
    # 歷史紀錄與摘要中 bot 的名字
    bot_display_name: str = "沙耶香"
    model_default: str = "deepseek-flash"
    model_deep: str = "deepseek-flash"
    # 兩個模型預設都開啟思考模式；一般聊天關閉以節省 token、降低延遲，/deep 開啟
    thinking_default: str = "disabled"
    thinking_deep: str = "enabled"
    # 思考強度，只在思考模式開啟時送出
    reasoning_effort_default: str = "low"
    reasoning_effort_deep: str = "high"
    deep_max_tokens: int = 8000
    # 單次 API 呼叫的逾時（秒）。DeepSeek 排隊時會一直送換行保持連線，SDK 的讀取逾時不會觸發，
    # 所以由 LLMClient 自己計時
    api_timeout_seconds: float = 120.0
    deep_api_timeout_seconds: float = 360.0
    # /deep 含重試的總時限：Discord 互動的 token 15 分鐘後失效，要留時間送出錯誤回覆
    deep_total_timeout_seconds: float = 780.0
    # 看圖：只處理觸發回覆的那則訊息裡的圖片（模型要支援，見 vision.VISION_MODELS）
    vision_enabled: bool = True
    # low = 縮到 512×512（便宜、快）；high / original = 保留原圖；auto 目前等同 original
    vision_detail: str = "low"
    vision_max_images: int = 4
    vision_max_bytes: int = 8 * 1024 * 1024
    history_max_messages: int = 30
    # 在最後一則訊息附上現在時間（BOT_TIMEZONE）
    show_current_time: bool = True
    # 對話紀錄的每行使用者訊息加上發言時間
    history_timestamps: bool = True
    # 每日費用上限（幣別同 PRICE_CURRENCY，依每次呼叫記錄的費用加總）；0 = 不限制
    daily_cost_budget: float = 1.0
    # 每日 token 上限（輸入＋輸出，包含命中快取的部分）；0 = 不限制
    daily_token_budget: int = 0
    user_cooldown_seconds: float = 5.0
    max_reply_tokens: int = 600
    random_interject_rate: float = 0.0
    # 插話控制：訊息出現這些話題時改用 interject_keyword_rate（取兩者較高者）
    interject_keywords: tuple[str, ...] = ()
    interject_keyword_rate: float = 0.3
    # 判斷模型認為「現在插話自然」的機率要超過這個門檻才插話
    interject_threshold: float = 0.7
    # 同一頻道在她上次發言後幾分鐘內不插話；每頻道每小時最多插話幾次（0 = 不限制）
    interject_channel_cooldown_minutes: float = 10.0
    interject_max_per_hour: int = 3
    # 查資料工具（DeepSeek Search／Fetch API），預設關閉
    web_tools_enabled: bool = False
    web_max_tool_calls_per_reply: int = 3
    web_daily_limit_per_user: int = 20  # 0 = 不限制
    web_search_results: int = 5
    web_fetch_max_chars: int = 4000
    web_timeout_seconds: float = 20.0
    deepseek_base_url: str = "https://api.deepseek.com"
    persona_path: Path = Path("persona.md")
    replies_path: Path = Path("replies.toml")
    # 原作資料資料夾；None = 不使用
    lore_dir: Path | None = Path("lore")
    db_path: Path = Path("data/bot.sqlite3")
    timezone: str = "Asia/Taipei"
    log_level: str = "INFO"
    # 計價幣別：USD（英文版價目表）或 CNY（中文版價目表），依 DeepSeek 帳戶的計價幣別設定
    price_currency: str = "USD"
    # PRICE_TABLE 覆寫的價格
    price_overrides: dict[str, ModelPrice] = field(default_factory=dict)
    offpeak_ratio: float = 0.5
    cn_holidays: frozenset[date] = frozenset()
    # 自動下載節假日資料的來源（空 = 停用），{year} 會被替換成年份
    holiday_sources: tuple[str, ...] = ()
    # 調休補班的週末是否算高峰（DeepSeek 原文：週末全天為空閒時段，預設照字面不算）
    makeup_workday_peak: bool = False

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
            bot_names=_str_list(_get(env, "BOT_NAMES", "")),
            bot_display_name=_get(env, "BOT_DISPLAY_NAME", "沙耶香"),
            model_default=_get(env, "MODEL_DEFAULT", "deepseek-flash"),
            model_deep=_get(env, "MODEL_DEEP", "deepseek-flash"),
            thinking_default=_thinking(_get(env, "THINKING_DEFAULT", "disabled"), "THINKING_DEFAULT"),
            thinking_deep=_thinking(_get(env, "THINKING_DEEP", "enabled"), "THINKING_DEEP"),
            reasoning_effort_default=_effort(_get(env, "REASONING_EFFORT_DEFAULT", "low"), "REASONING_EFFORT_DEFAULT"),
            reasoning_effort_deep=_effort(_get(env, "REASONING_EFFORT_DEEP", "high"), "REASONING_EFFORT_DEEP"),
            deep_max_tokens=int(_get(env, "DEEP_MAX_TOKENS", "8000")),
            api_timeout_seconds=float(_get(env, "API_TIMEOUT_SECONDS", "120")),
            deep_api_timeout_seconds=float(_get(env, "DEEP_API_TIMEOUT_SECONDS", "360")),
            deep_total_timeout_seconds=float(_get(env, "DEEP_TOTAL_TIMEOUT_SECONDS", "780")),
            vision_enabled=_bool(_get(env, "VISION_ENABLED", "true")),
            vision_detail=_vision_detail(_get(env, "VISION_DETAIL", "low")),
            vision_max_images=int(_get(env, "VISION_MAX_IMAGES", "4")),
            vision_max_bytes=int(float(_get(env, "VISION_MAX_MB", "8")) * 1024 * 1024),
            history_max_messages=int(_get(env, "HISTORY_MAX_MESSAGES", "30")),
            show_current_time=_bool(_get(env, "SHOW_CURRENT_TIME", "true")),
            history_timestamps=_bool(_get(env, "HISTORY_TIMESTAMPS", "true")),
            daily_cost_budget=float(_get(env, "DAILY_COST_BUDGET", "1")),
            daily_token_budget=int(_get(env, "DAILY_TOKEN_BUDGET", "0")),
            user_cooldown_seconds=float(_get(env, "USER_COOLDOWN_SECONDS", "5")),
            max_reply_tokens=int(_get(env, "MAX_REPLY_TOKENS", "600")),
            random_interject_rate=float(_get(env, "RANDOM_INTERJECT_RATE", "0")),
            interject_keywords=_str_list(_get(env, "INTERJECT_KEYWORDS", "")),
            interject_keyword_rate=float(_get(env, "INTERJECT_KEYWORD_RATE", "0.3")),
            interject_threshold=float(_get(env, "INTERJECT_THRESHOLD", "0.7")),
            interject_channel_cooldown_minutes=float(_get(env, "INTERJECT_CHANNEL_COOLDOWN_MINUTES", "10")),
            interject_max_per_hour=int(_get(env, "INTERJECT_MAX_PER_HOUR", "3")),
            web_tools_enabled=_bool(_get(env, "WEB_TOOLS_ENABLED", "false")),
            web_max_tool_calls_per_reply=int(_get(env, "WEB_MAX_TOOL_CALLS_PER_REPLY", "3")),
            web_daily_limit_per_user=int(_get(env, "WEB_DAILY_LIMIT_PER_USER", "20")),
            web_search_results=int(_get(env, "WEB_SEARCH_RESULTS", "5")),
            web_fetch_max_chars=int(_get(env, "WEB_FETCH_MAX_CHARS", "4000")),
            web_timeout_seconds=float(_get(env, "WEB_TIMEOUT_SECONDS", "20")),
            deepseek_base_url=_get(env, "DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            persona_path=Path(_get(env, "PERSONA_PATH", "persona.md")),
            replies_path=Path(_get(env, "REPLIES_PATH", "replies.toml")),
            lore_dir=_lore_dir(_get(env, "LORE_DIR", "lore")),
            db_path=Path(_get(env, "DB_PATH", "data/bot.sqlite3")),
            timezone=_get(env, "BOT_TIMEZONE", "Asia/Taipei"),
            log_level=_get(env, "LOG_LEVEL", "INFO").upper(),
            price_currency=_currency(_get(env, "PRICE_CURRENCY", "USD")),
            price_overrides=_price_table(_get(env, "PRICE_TABLE")),
            offpeak_ratio=float(_get(env, "PRICE_OFFPEAK_RATIO", "0.5")),
            cn_holidays=_date_list(_get(env, "CN_HOLIDAYS")),
            holiday_sources=_holiday_sources(_get(env, "HOLIDAY_SOURCE_URLS")),
            makeup_workday_peak=_bool(_get(env, "HOLIDAY_MAKEUP_WORKDAY_PEAK", "false")),
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
        if not 0 <= self.interject_keyword_rate <= 1:
            problems.append("INTERJECT_KEYWORD_RATE 必須介於 0 和 1")
        if not 0 <= self.interject_threshold <= 1:
            problems.append("INTERJECT_THRESHOLD 必須介於 0 和 1")
        if min(self.api_timeout_seconds, self.deep_api_timeout_seconds, self.deep_total_timeout_seconds) <= 0:
            problems.append("API_TIMEOUT_SECONDS、DEEP_API_TIMEOUT_SECONDS、DEEP_TOTAL_TIMEOUT_SECONDS 必須大於 0")
        if not 1 <= self.web_search_results <= 50:
            problems.append("WEB_SEARCH_RESULTS 必須介於 1 和 50")
        if self.web_max_tool_calls_per_reply < 1:
            problems.append("WEB_MAX_TOOL_CALLS_PER_REPLY 至少要 1")
        if self.deep_total_timeout_seconds >= 900:
            problems.append("DEEP_TOTAL_TIMEOUT_SECONDS 要小於 900（Discord 互動 15 分鐘後失效，會送不出回覆）")
        return problems

    def price_for(self, model: str) -> ModelPrice:
        if model in self.price_overrides:
            return self.price_overrides[model]
        table = PRICE_TABLES[self.price_currency]
        return table.get(model, table[FALLBACK_MODEL])

    @property
    def currency_symbol(self) -> str:
        return CURRENCY_SYMBOLS[self.price_currency]

    @property
    def web_request_price(self) -> float:
        return WEB_REQUEST_PRICES[self.price_currency]

    def money(self, amount: float, digits: int = 4) -> str:
        return f"{self.currency_symbol}{amount:.{digits}f}"

    def is_peak(self, when: datetime, calendar: "HolidayCalendar | None" = None) -> bool:
        """when 必須帶時區。calendar 提供自動下載的節假日與調休資料。"""
        local = when.astimezone(PRICING_TZ)
        day = local.date()
        if day in self.cn_holidays or (calendar is not None and calendar.is_off_day(day)):
            return False
        if local.weekday() >= 5:
            makeup = calendar is not None and calendar.is_makeup_workday(day)
            if not (self.makeup_workday_peak and makeup):
                return False
        return any(start <= local.hour < end for start, end in PEAK_HOURS)

    def call_cost(self, model: str, cache_hit: int, cache_miss: int, output: int, when: datetime,
                  calendar: "HolidayCalendar | None" = None) -> float:
        """單次呼叫費用（幣別同 PRICE_CURRENCY）。"""
        p = self.price_for(model)
        ratio = 1.0 if self.is_peak(when, calendar) else self.offpeak_ratio
        return (cache_hit * p.cache_hit + cache_miss * p.cache_miss + output * p.output) * ratio / 1_000_000

    def secrets(self) -> list[str]:
        return [s for s in (self.discord_token, self.deepseek_api_key) if s]

    def __repr__(self) -> str:  # 避免意外把 token 印進 log
        return (
            f"Config(guilds={sorted(self.allowed_guild_ids)}, admins={len(self.admin_user_ids)}, "
            f"models=({self.model_default}, {self.model_deep}), token=***, api_key=***)"
        )
