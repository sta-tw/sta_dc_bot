"""Fetch and parse calendar-day history from Chinese Wikipedia."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from enum import Enum
import math
import random
import re
from time import monotonic
from urllib.parse import quote

import aiohttp
from bs4 import BeautifulSoup


class Category(str, Enum):
    EVENTS = "events"
    BIRTHS = "births"
    DEATHS = "deaths"
    HOLIDAYS = "holidays"
    RANDOM = "random"


DATA_CATEGORIES = (
    Category.EVENTS,
    Category.BIRTHS,
    Category.DEATHS,
    Category.HOLIDAYS,
)


CATEGORY_LABELS: dict[Category, str] = {
    Category.EVENTS: "大事記",
    Category.BIRTHS: "出生",
    Category.DEATHS: "逝世",
    Category.HOLIDAYS: "節日與習俗",
    Category.RANDOM: "隨機分類",
}


@dataclass(frozen=True)
class HistoryItem:
    year: str | None
    text: str


@dataclass(frozen=True)
class HistoryResult:
    month: int
    day: int
    category: Category
    items: tuple[HistoryItem, ...]
    source_url: str


class HistoryError(Exception):
    """An upstream failure with a message suitable for a Discord response."""


WIKIPEDIA_API = "https://zh.wikipedia.org/w/api.php"
WIKIPEDIA_USER_AGENT = (
    "HistoryOnThisDayDiscordBot/1.0 "
    "(https://github.com/steventeng2022/sta_dc_bot-1; calendar history lookup)"
)
CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_MAX_ENTRIES = 366
REQUEST_TIMEOUT_SECONDS = 15
MAX_RETRIES = 2

_HEADING_CATEGORIES = {
    "大事記": Category.EVENTS,
    "大事记": Category.EVENTS,
    "事件": Category.EVENTS,
    "出生": Category.BIRTHS,
    "誕生": Category.BIRTHS,
    "诞生": Category.BIRTHS,
    "逝世": Category.DEATHS,
    "節假日和習俗": Category.HOLIDAYS,
    "节假日和习俗": Category.HOLIDAYS,
    "節假日與習俗": Category.HOLIDAYS,
    "节假日与习俗": Category.HOLIDAYS,
    "節日和習俗": Category.HOLIDAYS,
    "节日和习俗": Category.HOLIDAYS,
    "節日與習俗": Category.HOLIDAYS,
    "节日与习俗": Category.HOLIDAYS,
    "節假日和風俗": Category.HOLIDAYS,
    "节假日和风俗": Category.HOLIDAYS,
}
_YEAR_PREFIX = re.compile(
    r"^(?P<year>(?:(?:公元|西元)?前\s*)?[0-9０-９]{1,4}\s*年)"
    r"\s*(?:[：:―—–−－-]\s*)?"
)
_EXCLUDED_SELECTORS = (
    "script, style, sup.reference, .mw-editsection, .navbox, .vertical-navbox, "
    ".metadata, .references, .reflist, .catlinks, .toc, #toc, .noprint, "
    ".mw-empty-elt"
)


def validate_date(month: int, day: int) -> None:
    """Validate a recurring calendar date, including February 29."""
    if isinstance(month, bool) or isinstance(day, bool):
        raise ValueError("日期無效，請輸入有效的月份與日期（例如 2 月 29 日）。")
    try:
        date(2000, month, day)
    except (TypeError, ValueError):
        raise ValueError(
            "日期無效，請輸入有效的月份與日期（例如 2 月 29 日）。"
        ) from None


def source_url(month: int, day: int) -> str:
    validate_date(month, day)
    title = quote(f"{month}月{day}日", safe="")
    return f"https://zh.wikipedia.org/wiki/{title}?variant=zh-tw"


def parse_history_html(html: str) -> dict[Category, tuple[HistoryItem, ...]]:
    """Read recognized H2 sections in both current and older MediaWiki HTML."""
    if not isinstance(html, str):
        raise ValueError("歷史資料必須是 HTML 文字。")
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup.select(_EXCLUDED_SELECTORS):
        # A selected node may already have been removed with its parent.
        if unwanted.parent is not None:
            unwanted.decompose()
    for line_break in soup.find_all("br"):
        line_break.replace_with(" ")
    # Keep Chinese inline links joined, but separate facts in nested lists and
    # other block elements even when the upstream HTML has no whitespace.
    for block in soup.find_all(["ul", "ol", "li", "p", "div"]):
        block.insert_before(" ")
        block.insert_after(" ")

    items: dict[Category, list[HistoryItem]] = {
        category: [] for category in DATA_CATEGORIES
    }
    seen: dict[Category, set[HistoryItem]] = {
        category: set() for category in DATA_CATEGORIES
    }
    current_category: Category | None = None
    for node in soup.find_all(["h2", "li"]):
        if node.name == "h2":
            heading = re.sub(r"\s+", "", node.get_text())
            current_category = _HEADING_CATEGORIES.get(heading)
            continue
        if current_category is None:
            continue
        # Nested bullet text belongs to its enclosing entry; do not repeat it.
        if node.find_parent("li") is not None:
            continue
        if node.find_parent(["table", "nav", "aside"]) is not None:
            continue
        text = re.sub(r"\s+", " ", node.get_text()).strip()
        if not text:
            continue
        match = _YEAR_PREFIX.match(text)
        year = re.sub(r"\s+", "", match.group("year")) if match else None
        description = text[match.end() :].strip() if match else text
        if not description:
            continue
        item = HistoryItem(year=year, text=description)
        if item not in seen[current_category]:
            items[current_category].append(item)
            seen[current_category].add(item)
    return {category: tuple(values) for category, values in items.items()}


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    items: dict[Category, tuple[HistoryItem, ...]]


class _TransientResponse(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class HistoryClient:
    """Share bounded cached results and in-flight requests across categories."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._closed = False
        self._cache: OrderedDict[tuple[int, int], _CacheEntry] = OrderedDict()
        self._inflight: dict[
            tuple[int, int], asyncio.Task[dict[Category, tuple[HistoryItem, ...]]]
        ] = {}

    async def get(
        self, month: int, day: int, category: Category = Category.EVENTS
    ) -> HistoryResult:
        if self._closed:
            raise HistoryError("歷史查詢服務已停止，請稍後再試。")
        validate_date(month, day)
        try:
            category = Category(category)
        except (TypeError, ValueError):
            raise ValueError("不支援這個歷史分類。") from None
        key = (month, day)
        cached = self._cache.get(key)
        if cached is not None and cached.expires_at > monotonic():
            self._cache.move_to_end(key)
            data = cached.items
        else:
            self._cache.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._fetch_and_cache(month, day))
                self._inflight[key] = task
                task.add_done_callback(lambda done: self._finish_request(key, done))
            # Cancelling a Discord interaction must not cancel other waiters.
            data = await asyncio.shield(task)
        if category is Category.RANDOM:
            available = [item for item in DATA_CATEGORIES if data[item]]
            category = random.choice(available)
        return HistoryResult(month, day, category, data[category], source_url(month, day))

    async def aclose(self) -> None:
        """Cancel owned background requests before the Cog closes its session."""
        self._closed = True
        tasks = list(self._inflight.values())
        self._inflight.clear()
        self._cache.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _finish_request(
        self,
        key: tuple[int, int],
        task: asyncio.Task[dict[Category, tuple[HistoryItem, ...]]],
    ) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        # Retrieve failures even when every original waiter was cancelled.
        if not task.cancelled():
            task.exception()

    async def _fetch_and_cache(
        self, month: int, day: int
    ) -> dict[Category, tuple[HistoryItem, ...]]:
        data = await self._fetch(month, day)
        key = (month, day)
        self._cache[key] = _CacheEntry(monotonic() + CACHE_TTL_SECONDS, data)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)
        return data

    async def _fetch(
        self, month: int, day: int
    ) -> dict[Category, tuple[HistoryItem, ...]]:
        last_error = HistoryError("連線至維基百科失敗，請稍後再試。")
        for attempt in range(MAX_RETRIES + 1):
            delay = 0.5 * (2**attempt)
            try:
                return await self._request(month, day)
            except _TransientResponse as exc:
                last_error = HistoryError(str(exc))
                if exc.retry_after is not None:
                    delay = exc.retry_after
            except asyncio.TimeoutError:
                last_error = HistoryError("查詢維基百科逾時，請稍後再試。")
            except aiohttp.ClientError:
                last_error = HistoryError("連線至維基百科失敗，請稍後再試。")
            if attempt == MAX_RETRIES:
                raise last_error from None
            await asyncio.sleep(delay)
        raise last_error  # Unreachable, but keeps the return contract explicit.

    async def _request(
        self, month: int, day: int
    ) -> dict[Category, tuple[HistoryItem, ...]]:
        params = {
            "action": "parse",
            "page": f"{month}月{day}日",
            "prop": "text",
            "redirects": "1",
            "format": "json",
            "formatversion": "2",
            "variant": "zh-tw",
            "uselang": "zh-tw",
        }
        async with self._session.get(
            WIKIPEDIA_API,
            params=params,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            headers={"User-Agent": WIKIPEDIA_USER_AGENT},
        ) as response:
            if response.status == 429:
                raise _TransientResponse(
                    "維基百科目前請求過於頻繁，請稍後再試。",
                    self._retry_after(response.headers.get("Retry-After")),
                )
            if response.status >= 500:
                raise _TransientResponse("維基百科暫時無法提供資料，請稍後再試。")
            if response.status != 200:
                raise HistoryError("維基百科查詢失敗，請稍後再試。")
            try:
                payload = await response.json()
            except (ValueError, TypeError, aiohttp.ContentTypeError):
                raise HistoryError("維基百科回傳的資料格式異常，請稍後再試。") from None
        if not isinstance(payload, dict):
            raise HistoryError("維基百科回傳的資料格式異常，請稍後再試。")
        if "error" in payload:
            error = payload["error"]
            if isinstance(error, dict) and error.get("code") in {
                "missingtitle",
                "invalidtitle",
                "nosuchpageid",
            }:
                raise HistoryError("維基百科找不到這個日期的資料。")
            raise HistoryError("維基百科查詢失敗，請稍後再試。")
        parsed = payload.get("parse")
        html = parsed.get("text") if isinstance(parsed, dict) else None
        if not isinstance(html, str) or not html.strip():
            raise HistoryError("維基百科回傳的資料格式異常，請稍後再試。")
        data = parse_history_html(html)
        if not any(data.values()):
            raise HistoryError("無法解析維基百科的歷史資料，請稍後再試。")
        return data

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return min(seconds, 30.0)
