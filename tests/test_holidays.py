import json
from datetime import date, datetime

import pytest

from bot.holidays import HolidayCalendar, parse_year, pricing_years

from .conftest import TAIPEI, make_config

# 取自 holiday-cn 2026.json 的片段
SAMPLE_2026 = json.dumps({
    "year": 2026,
    "papers": ["https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm"],
    "days": [
        {"name": "国庆节", "date": "2026-09-20", "isOffDay": False},  # 週日補班
        {"name": "国庆节", "date": "2026-10-01", "isOffDay": True},
        {"name": "国庆节", "date": "2026-10-02", "isOffDay": True},
        {"name": "国庆节", "date": "2026-10-10", "isOffDay": False},  # 週六補班
    ],
})
EMPTY_2027 = json.dumps({"year": 2027, "papers": [], "days": []})


def bj(*a):
    return datetime(*a, tzinfo=TAIPEI)  # 與北京同為 UTC+8


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses  # url -> str | Exception
        self.urls = []

    async def __call__(self, url):
        self.urls.append(url)
        r = self.responses.get(url, RuntimeError("404"))
        if isinstance(r, Exception):
            raise r
        return r


SOURCES = ("https://a/{year}.json", "https://b/{year}.json")


def test_parse_year():
    y = parse_year(SAMPLE_2026)
    assert date(2026, 10, 1) in y.off_days
    assert y.work_days == {date(2026, 9, 20), date(2026, 10, 10)}


async def test_refresh_uses_fallback_source_and_writes_cache(tmp_path):
    fetch = FakeFetcher({"https://b/2026.json": SAMPLE_2026})
    cal = HolidayCalendar(tmp_path, sources=SOURCES, fetcher=fetch)
    assert await cal.refresh([2026]) == {2026: True}
    assert fetch.urls == ["https://a/2026.json", "https://b/2026.json"]
    assert cal.is_off_day(date(2026, 10, 1))
    assert (tmp_path / "2026.json").exists()

    # 重新啟動、網路全斷：從快取載入
    cal2 = HolidayCalendar(tmp_path, sources=SOURCES, fetcher=FakeFetcher({}))
    cal2.load_cache([2026])
    assert await cal2.refresh([2026]) == {2026: False}
    assert cal2.is_off_day(date(2026, 10, 2))


async def test_unannounced_year_does_not_override(tmp_path):
    fetch = FakeFetcher({"https://a/2027.json": EMPTY_2027})
    cal = HolidayCalendar(tmp_path, sources=SOURCES, fetcher=fetch)
    assert await cal.refresh([2027]) == {2027: False}
    assert cal.loaded_years() == []
    assert not (tmp_path / "2027.json").exists()


async def test_invalid_json_tries_next_source(tmp_path):
    fetch = FakeFetcher({"https://a/2026.json": "<html>", "https://b/2026.json": SAMPLE_2026})
    cal = HolidayCalendar(tmp_path, sources=SOURCES, fetcher=fetch)
    assert (await cal.refresh([2026]))[2026]


async def _calendar():
    cal = HolidayCalendar(None, sources=SOURCES, fetcher=FakeFetcher({"https://a/2026.json": SAMPLE_2026}))
    await cal.refresh([2026])
    return cal


async def test_peak_with_calendar():
    cfg = make_config()
    cal = await _calendar()
    assert cfg.is_peak(bj(2026, 9, 30, 10, 0), cal)       # 一般週三
    assert not cfg.is_peak(bj(2026, 10, 1, 10, 0), cal)   # 國慶（週四）
    assert not cfg.is_peak(bj(2026, 10, 2, 15, 0), cal)   # 國慶（週五）
    # 調休補班的週六：預設照 DeepSeek 原文算週末空閒
    assert not cfg.is_peak(bj(2026, 10, 10, 10, 0), cal)
    # 沒有日曆時，週間的國定假日會被誤判為高峰
    assert cfg.is_peak(bj(2026, 10, 1, 10, 0))


async def test_makeup_workday_peak_option():
    cfg = make_config(makeup_workday_peak=True)
    cal = await _calendar()
    assert cfg.is_peak(bj(2026, 10, 10, 10, 0), cal)      # 補班週六
    assert not cfg.is_peak(bj(2026, 10, 10, 13, 0), cal)  # 補班日的午休仍是空閒
    assert not cfg.is_peak(bj(2026, 10, 11, 10, 0), cal)  # 一般週日


def test_manual_holidays_still_work():
    cal = HolidayCalendar(None, sources=(), manual_off_days=[date(2026, 11, 4)])
    assert not make_config().is_peak(bj(2026, 11, 4, 10, 0), cal)


async def test_llm_cost_uses_calendar(make_service, fake_openai):
    fake_openai.completions.usage = dict(prompt_tokens=1_000_000, completion_tokens=0,
                                         prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1_000_000)
    svc = make_service(now=bj(2026, 10, 1, 10, 0), price_currency="CNY")  # 國慶上午，原本會被算成高峰
    svc.llm.calendar = await _calendar()
    await svc.llm.chat([{"role": "user", "content": "hi"}])
    assert svc.llm.estimate_cost(svc.llm.usage_today()) == pytest.approx(1.0)  # 2 元 × 0.5


def test_pricing_years():
    assert pricing_years(bj(2026, 12, 31, 23, 0)) == [2026, 2027]


def test_holiday_source_config():
    from bot.config import Config
    from bot.holidays import DEFAULT_SOURCES
    assert Config.from_env({}).holiday_sources == DEFAULT_SOURCES
    assert Config.from_env({"HOLIDAY_SOURCE_URLS": "none"}).holiday_sources == ()
    assert Config.from_env({"HOLIDAY_SOURCE_URLS": "https://x/{year}.json"}).holiday_sources == ("https://x/{year}.json",)
    assert Config.from_env({"HOLIDAY_MAKEUP_WORKDAY_PEAK": "true"}).makeup_workday_peak


async def test_periodic_refresh_runs_immediately():
    import asyncio
    fetch = FakeFetcher({"https://a/2026.json": SAMPLE_2026})
    cal = HolidayCalendar(None, sources=SOURCES, fetcher=fetch)
    task = asyncio.create_task(cal.run_periodic_refresh(lambda: [2026], interval=3600, run_immediately=True))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    assert cal.loaded_years() == [2026]
