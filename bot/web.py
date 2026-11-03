"""DeepSeek Search／Fetch API 客戶端。

- Search：POST {base}/v1/tools/search，依 query 搜尋網頁
- Fetch：POST {base}/v1/tools/fetch，抓取單一網頁並轉成 Markdown

與聊天 API 共用同一把 key（Bearer）。依官方說明：408、429、5xx 等待後重試；
402（餘額不足）與其他 4xx 不重試。只有 HTTP 200 且有結果的請求會計費。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

# (url, json body, headers, timeout 秒) → (HTTP 狀態碼, 解析後的 JSON 或 None)
PostFn = Callable[[str, dict, dict, float], Awaitable[tuple[int, Any]]]

RETRYABLE_STATUS = {408, 429}
SEARCH_QUERY_MAX = 128
FETCH_URL_MAX = 8192


class WebError(Exception):
    """查詢失敗。訊息是給模型看的簡短說明（不含 key 與請求內容）。"""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    summary: str
    site_name: str | None
    date: str | None
    rerank_score: float


@dataclass(frozen=True)
class Page:
    title: str
    content: str


async def aiohttp_post(url: str, body: dict, headers: dict, timeout: float) -> tuple[int, Any]:
    import aiohttp  # discord.py 已依賴 aiohttp

    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post(url, json=body, headers=headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001 - 錯誤頁不一定是 JSON
                data = None
            return resp.status, data


def _error_message(data: Any) -> str:
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return str(data["error"].get("message") or "")[:200]
    return ""


class DeepSeekWeb:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        *,
        timeout: float = 20.0,
        max_attempts: int = 2,
        post: PostFn = aiohttp_post,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_billable: Callable[[str], None] | None = None,
    ):
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._post = post
        self._sleep = sleep
        self._on_billable = on_billable or (lambda kind: None)

    async def search(self, query: str, *, count: int = 5, ori_question: str | None = None) -> list[SearchResult]:
        query = query.strip()[:SEARCH_QUERY_MAX]
        if not query:
            raise WebError("搜尋關鍵字是空的")
        body: dict[str, Any] = {"query": query, "count": count, "need_content": False}
        if ori_question and ori_question.strip():
            body["ori_question"] = ori_question.strip()[:SEARCH_QUERY_MAX]
        data = await self._request("search", body)
        results = []
        for r in (data or {}).get("results") or []:
            results.append(SearchResult(
                title=str(r.get("title") or ""),
                url=str(r.get("url") or ""),
                summary=str(r.get("summary") or r.get("snippet") or ""),
                site_name=r.get("site_name"),
                date=r.get("date"),
                rerank_score=float(r.get("rerank_score") or 0),
            ))
        if results:
            self._on_billable("search")  # 只有結果非空才計費
        return results

    async def fetch(self, url: str) -> Page:
        data = await self._request("fetch", {"url": url})
        page = Page(title=str((data or {}).get("title") or ""), content=str((data or {}).get("content") or ""))
        if page.content.strip():
            self._on_billable("fetch")  # 只有取得正文才計費
        return page

    async def _request(self, kind: str, body: dict) -> Any:
        url = f"{self.base_url}/v1/tools/{kind}"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        detail = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                status, data = await asyncio.wait_for(self._post(url, body, headers, self.timeout), self.timeout)
            except (asyncio.TimeoutError, OSError) as exc:
                detail = "逾時" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__
                status, data = None, None
            except Exception as exc:  # noqa: BLE001 - aiohttp 的連線錯誤等
                detail = type(exc).__name__
                status, data = None, None
            if status == 200:
                return data
            if status is not None:
                detail = f"HTTP {status}" + (f"：{_error_message(data)}" if _error_message(data) else "")
            retryable = status is None or status in RETRYABLE_STATUS or status >= 500
            log.warning("DeepSeek %s 失敗（第 %d/%d 次）：%s", kind, attempt, self.max_attempts, detail)
            if not retryable:
                raise WebError(self._explain(kind, status))
            if attempt < self.max_attempts:
                await self._sleep(1.0 * attempt)
        raise WebError("查詢服務暫時沒有回應")

    @staticmethod
    def _explain(kind: str, status: int | None) -> str:
        if status == 402:
            return "查詢服務的額度不足"
        if status == 401:
            return "查詢服務無法使用"
        if kind == "fetch" and status in (403, 404):
            return "這個網頁無法讀取（可能不存在、被網站阻擋，或沒有可讀的內容）"
        if status == 400:
            return "查詢的格式不正確"
        return "查詢失敗"
