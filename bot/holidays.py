"""中國法定節假日與調休資料，用於判斷 DeepSeek 的高峰／空閒時段。

資料來源：NateScarlet/holiday-cn（自動抓取國務院公告），格式：
    {"year": 2026, "days": [{"name": "元旦", "date": "2026-01-01", "isOffDay": true}, ...]}
isOffDay=true 為放假日，false 為調休補班日。

抓取失敗時沿用本機快取（data/holidays/{year}.json），再不行就只用 CN_HOLIDAYS 手動設定。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable, Iterable

log = logging.getLogger(__name__)

DEFAULT_SOURCES = (
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json",
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
)

def pricing_years(now: datetime | None = None) -> list[int]:
    """需要的年份：北京時間的今年與明年（明年的安排通常在 11～12 月公告）。"""
    from .config import PRICING_TZ

    year = (now or datetime.now(PRICING_TZ)).astimezone(PRICING_TZ).year
    return [year, year + 1]


# 回傳 JSON 文字；失敗時丟例外
Fetcher = Callable[[str], Awaitable[str]]


@dataclass
class YearData:
    off_days: set[date] = field(default_factory=set)
    work_days: set[date] = field(default_factory=set)


def parse_year(text: str) -> YearData:
    data = json.loads(text)
    out = YearData()
    for d in data.get("days", []):
        day = date.fromisoformat(d["date"])
        (out.off_days if d["isOffDay"] else out.work_days).add(day)
    return out


async def aiohttp_fetch(url: str, timeout: float = 15.0) -> str:
    import aiohttp  # discord.py 已依賴 aiohttp

    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()


class HolidayCalendar:
    def __init__(
        self,
        cache_dir: str | Path | None = None,
        sources: Iterable[str] = DEFAULT_SOURCES,
        manual_off_days: Iterable[date] = (),
        fetcher: Fetcher = aiohttp_fetch,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.sources = tuple(sources)
        self.manual_off_days = frozenset(manual_off_days)
        self._fetch = fetcher
        self._years: dict[int, YearData] = {}

    # --- 查詢 ---
    def is_off_day(self, day: date) -> bool:
        """國定假日（含連假中的週間放假日）。"""
        if day in self.manual_off_days:
            return True
        y = self._years.get(day.year)
        return y is not None and day in y.off_days

    def is_makeup_workday(self, day: date) -> bool:
        """調休補班日（通常是週末）。"""
        y = self._years.get(day.year)
        return y is not None and day in y.work_days

    def loaded_years(self) -> list[int]:
        return sorted(self._years)

    # --- 載入 ---
    def _cache_path(self, year: int) -> Path | None:
        return self.cache_dir / f"{year}.json" if self.cache_dir else None

    def load_cache(self, years: Iterable[int]) -> None:
        for year in years:
            path = self._cache_path(year)
            if path is None or not path.exists():
                continue
            try:
                self._years[year] = parse_year(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError) as exc:
                log.warning("節假日快取 %s 讀取失敗：%s", path, exc)

    async def refresh(self, years: Iterable[int]) -> dict[int, bool]:
        """從來源下載指定年份，成功則更新記憶體與快取。回傳 {年份: 是否成功}。"""
        results = {}
        for year in years:
            results[year] = await self._refresh_year(year)
        return results

    async def _refresh_year(self, year: int) -> bool:
        for template in self.sources:
            url = template.format(year=year)
            try:
                text = await self._fetch(url)
                data = parse_year(text)
            except Exception as exc:  # noqa: BLE001 - 換下一個來源
                log.debug("節假日來源 %s 失敗：%s", url, exc)
                continue
            if not data.off_days and not data.work_days:
                # 國務院尚未公告（例如明年的資料），不要覆蓋已有資料
                log.info("%d 年節假日尚未公告", year)
                return False
            self._years[year] = data
            path = self._cache_path(year)
            if path is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                except OSError as exc:
                    log.warning("節假日快取寫入失敗：%s", exc)
            log.info("已更新 %d 年節假日：放假 %d 天、補班 %d 天", year, len(data.off_days), len(data.work_days))
            return True
        log.warning("%d 年節假日下載失敗，沿用快取／手動設定", year)
        return False

    async def run_periodic_refresh(self, years_fn: Callable[[], Iterable[int]], interval: float = 86400,
                                   *, run_immediately: bool = False) -> None:
        """背景工作：每隔 interval 秒更新一次（國務院通常在 11～12 月公告隔年安排）。"""
        first = True
        while True:
            if not (first and run_immediately):
                await asyncio.sleep(interval)
            first = False
            try:
                await self.refresh(years_fn())
            except Exception:  # noqa: BLE001
                log.exception("更新節假日失敗")
