from __future__ import annotations

import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from bot.utils.history_today import (
    CACHE_MAX_ENTRIES,
    CACHE_TTL_SECONDS,
    CATEGORY_LABELS,
    DATA_CATEGORIES,
    Category,
    HistoryClient,
    HistoryError,
    HistoryItem,
    parse_history_html,
    source_url,
    validate_date,
)


FIXTURE = (Path(__file__).parent / "fixtures" / "october4.html").read_text(
    encoding="utf-8"
)
VALID_PAYLOAD = {"parse": {"text": FIXTURE}}


class FakeResponse:
    def __init__(
        self,
        payload=VALID_PAYLOAD,
        *,
        status=200,
        headers=None,
        enter_error=None,
        json_error=None,
        gate=None,
        entered=None,
    ):
        self.payload = payload
        self.status = status
        self.headers = headers or {}
        self.enter_error = enter_error
        self.json_error = json_error
        self.gate = gate
        self.entered = entered

    async def __aenter__(self):
        if self.entered:
            self.entered.set()
        if self.enter_error:
            raise self.enter_error
        if self.gate:
            await self.gate.wait()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected upstream request")
        return self.responses.pop(0)


class DateAndParserTests(unittest.TestCase):
    def test_leap_day_and_invalid_dates(self):
        validate_date(2, 29)
        validate_date(12, 31)
        for month, day in [(0, 1), (13, 1), (4, 31), (2, 30), (1, 0), (True, 1), (1, False), ("1", 1), (1.0, 1)]:
            with self.subTest(month=month, day=day):
                with self.assertRaisesRegex(ValueError, "日期無效"):
                    validate_date(month, day)

    def test_source_url_links_to_traditional_variant(self):
        self.assertEqual(
            source_url(10, 4),
            "https://zh.wikipedia.org/wiki/10%E6%9C%884%E6%97%A5?variant=zh-tw",
        )
        with self.assertRaises(ValueError):
            source_url(2, 30)

    def test_all_categories_current_and_old_headings(self):
        parsed = parse_history_html(FIXTURE)
        self.assertEqual(set(parsed), set(DATA_CATEGORIES))
        self.assertEqual(len(parsed[Category.EVENTS]), 2)
        self.assertEqual(
            parsed[Category.EVENTS][0],
            HistoryItem("1957年", "蘇聯發射人類第一顆人造衛星史普尼克1號。"),
        )
        self.assertEqual(parsed[Category.BIRTHS][0].year, "1895年")
        self.assertEqual(parsed[Category.DEATHS][0].year, "公元前42年")
        self.assertEqual(parsed[Category.DEATHS][1].year, "1947年")
        self.assertEqual(parsed[Category.HOLIDAYS][0], HistoryItem(None, "世界動物日"))
        self.assertEqual(parsed[Category.HOLIDAYS][1].text, "賴索托：獨立日")
        all_text = " ".join(item.text for group in parsed.values() for item in group)
        for excluded in ["[1]", "編輯", "導覽列", "引用內容", "外部網站"]:
            self.assertNotIn(excluded, all_text)

    def test_nested_bullets_are_not_repeated(self):
        parsed = parse_history_html(FIXTURE)
        self.assertEqual(
            sum("安薩里X大獎" in item.text for item in parsed[Category.EVENTS]), 1
        )

    def test_compact_nested_blocks_and_line_breaks_keep_separators(self):
        parsed = parse_history_html(
            "<h2>大事記</h2><ul><li>2000年：第一件事"
            "<ul><li>第二件事</li><li>第三件事<br>補充說明</li></ul></li></ul>"
        )
        self.assertEqual(
            parsed[Category.EVENTS],
            (HistoryItem("2000年", "第一件事 第二件事 第三件事 補充說明"),),
        )

    def test_simplified_heading_whitespace_and_bce_prefix(self):
        parsed = parse_history_html(
            '<h2><span>大事记</span></h2><ul>'
            '<li>前 42 年：<a>古羅馬</a>事件</li>'
            '<li>未註明年份的事件</li></ul>'
            '<h2>節假日與習俗</h2><ul><li>節日</li></ul>'
        )
        self.assertEqual(parsed[Category.EVENTS][0], HistoryItem("前42年", "古羅馬事件"))
        self.assertEqual(parsed[Category.EVENTS][1].year, None)
        self.assertEqual(parsed[Category.HOLIDAYS], (HistoryItem(None, "節日"),))

    def test_deduplication_and_unknown_section_boundary(self):
        parsed = parse_history_html(
            "<h2>出生</h2><ul><li>1900年：人物</li><li>1900年：人物</li></ul>"
            "<h2>參考資料</h2><ul><li>應忽略的內容</li></ul>"
        )
        self.assertEqual(parsed[Category.BIRTHS], (HistoryItem("1900年", "人物"),))
        self.assertEqual(parsed[Category.EVENTS], ())

    def test_english_words_keep_spaces_and_references_are_removed(self):
        parsed = parse_history_html(
            "<h2>大事記</h2><ul><li>2000年：<a>New</a> York City"
            '<sup class="reference">[99]</sup></li></ul>'
        )
        self.assertEqual(parsed[Category.EVENTS][0].text, "New York City")

    def test_constants_and_labels(self):
        self.assertEqual(CACHE_TTL_SECONDS, 21600)
        self.assertEqual(CACHE_MAX_ENTRIES, 366)
        self.assertEqual(set(CATEGORY_LABELS), set(Category))
        with self.assertRaises(ValueError):
            parse_history_html(None)


class HistoryClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_cancels_shared_fetch_and_prevents_new_requests(self):
        gate, entered = asyncio.Event(), asyncio.Event()
        session = FakeSession(FakeResponse(gate=gate, entered=entered))
        client = HistoryClient(session)
        request = asyncio.create_task(client.get(10, 4))
        await entered.wait()
        await client.aclose()
        with self.assertRaises(asyncio.CancelledError):
            await request
        with self.assertRaisesRegex(HistoryError, "已停止"):
            await client.get(10, 5)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(client._inflight, {})
        await client.aclose()

    async def test_get_parameters_and_category_cache(self):
        session = FakeSession(FakeResponse())
        client = HistoryClient(session)
        events = await client.get(10, 4)
        births = await client.get(10, 4, Category.BIRTHS)
        self.assertEqual(events.category, Category.EVENTS)
        self.assertEqual(len(events.items), 2)
        self.assertEqual(births.items[0].year, "1895年")
        self.assertEqual(events.source_url, source_url(10, 4))
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://zh.wikipedia.org/w/api.php")
        self.assertEqual(kwargs["params"]["page"], "10月4日")
        self.assertEqual(kwargs["params"]["formatversion"], "2")
        self.assertEqual(kwargs["params"]["variant"], "zh-tw")
        self.assertEqual(kwargs["params"]["uselang"], "zh-tw")
        self.assertEqual(kwargs["timeout"].total, 15)
        self.assertIn("HistoryOnThisDayDiscordBot", kwargs["headers"]["User-Agent"])
        self.assertIn(
            "https://github.com/steventeng2022/sta_dc_bot-1",
            kwargs["headers"]["User-Agent"],
        )

    async def test_invalid_input_never_requests_upstream(self):
        session = FakeSession()
        client = HistoryClient(session)
        with self.assertRaises(ValueError):
            await client.get(4, 31)
        with self.assertRaisesRegex(ValueError, "分類"):
            await client.get(4, 30, "unknown")
        self.assertEqual(session.calls, [])

    async def test_random_category_selects_only_categories_with_data(self):
        session = FakeSession(
            FakeResponse(
                {"parse": {"text": "<h2>出生</h2><ul><li>1900年：人物</li></ul>"}}
            )
        )
        client = HistoryClient(session)
        with patch("bot.utils.history_today.random.choice", side_effect=lambda options: options[0]) as choose:
            result = await client.get(10, 4, Category.RANDOM)
        self.assertEqual(result.category, Category.BIRTHS)
        self.assertEqual(result.items, (HistoryItem("1900年", "人物"),))
        choose.assert_called_once_with([Category.BIRTHS])

    async def test_single_flight_shares_all_categories(self):
        gate, entered = asyncio.Event(), asyncio.Event()
        session = FakeSession(FakeResponse(gate=gate, entered=entered))
        client = HistoryClient(session)
        tasks = [asyncio.create_task(client.get(10, 4, category)) for category in Category]
        await entered.wait()
        self.assertEqual(len(session.calls), 1)
        gate.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual({result.category for result in results}, set(DATA_CATEGORIES))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(client._inflight, {})

    async def test_cancelled_waiter_does_not_cancel_shared_request(self):
        gate, entered = asyncio.Event(), asyncio.Event()
        session = FakeSession(FakeResponse(gate=gate, entered=entered))
        client = HistoryClient(session)
        first = asyncio.create_task(client.get(10, 4))
        second = asyncio.create_task(client.get(10, 4, Category.DEATHS))
        await entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        gate.set()
        result = await second
        self.assertEqual(result.category, Category.DEATHS)
        self.assertEqual(len(session.calls), 1)
        await client.get(10, 4)
        self.assertEqual(len(session.calls), 1)

    async def test_cache_expires_after_six_hours(self):
        session = FakeSession(FakeResponse(), FakeResponse())
        client = HistoryClient(session)
        with patch("bot.utils.history_today.monotonic", return_value=100):
            await client.get(10, 4)
        with patch("bot.utils.history_today.monotonic", return_value=100 + CACHE_TTL_SECONDS - 1):
            await client.get(10, 4)
        self.assertEqual(len(session.calls), 1)
        with patch("bot.utils.history_today.monotonic", return_value=100 + CACHE_TTL_SECONDS):
            await client.get(10, 4)
        self.assertEqual(len(session.calls), 2)

    async def test_lru_cache_evicts_least_recently_used_date(self):
        session = FakeSession(*(FakeResponse() for _ in range(4)))
        client = HistoryClient(session)
        with patch("bot.utils.history_today.CACHE_MAX_ENTRIES", 2):
            await client.get(10, 4)
            await client.get(10, 5)
            await client.get(10, 4)  # Keep October 4 as the most recent entry.
            await client.get(10, 6)
            self.assertEqual(len(client._cache), 2)
            await client.get(10, 4)
            self.assertEqual(len(session.calls), 3)
            await client.get(10, 5)
        self.assertEqual(len(session.calls), 4)

    async def test_transient_status_and_rate_limit_retry(self):
        session = FakeSession(
            FakeResponse(status=503),
            FakeResponse(status=429, headers={"Retry-After": "0"}),
            FakeResponse(),
        )
        with patch("bot.utils.history_today.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await HistoryClient(session).get(10, 4)
        self.assertEqual(result.items[0].year, "1957年")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(sleep.await_args_list[0].args, (0.5,))
        self.assertEqual(sleep.await_args_list[1].args, (0.0,))

    async def test_network_errors_retry_twice_and_then_fail(self):
        for error, expected in [
            (aiohttp.ClientConnectionError("private upstream details"), "連線"),
            (asyncio.TimeoutError(), "逾時"),
        ]:
            with self.subTest(error=type(error).__name__):
                session = FakeSession(*(FakeResponse(enter_error=error) for _ in range(3)))
                with patch("bot.utils.history_today.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaisesRegex(HistoryError, expected) as caught:
                        await HistoryClient(session).get(10, 4)
                self.assertNotIn("private upstream details", str(caught.exception))
                self.assertEqual(len(session.calls), 3)
                self.assertEqual(sleep.await_count, 2)

    async def test_exhausted_rate_limit_returns_friendly_error(self):
        session = FakeSession(*(FakeResponse(status=429) for _ in range(3)))
        with patch("bot.utils.history_today.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(HistoryError, "請求過於頻繁"):
                await HistoryClient(session).get(10, 4)
        self.assertEqual(len(session.calls), 3)

    async def test_permanent_http_error_is_not_retried(self):
        session = FakeSession(FakeResponse(status=403))
        with self.assertRaisesRegex(HistoryError, "查詢失敗"):
            await HistoryClient(session).get(10, 4)
        self.assertEqual(len(session.calls), 1)

    async def test_bad_json_or_schema_is_not_retried(self):
        responses = [
            FakeResponse(json_error=ValueError("not JSON")),
            FakeResponse(payload=[]),
            FakeResponse(payload={}),
            FakeResponse(payload={"parse": {"text": {"*": FIXTURE}}}),
            FakeResponse(payload={"parse": {"text": ""}}),
        ]
        for response in responses:
            with self.subTest(payload=response.payload):
                session = FakeSession(response)
                with self.assertRaisesRegex(HistoryError, "資料格式異常"):
                    await HistoryClient(session).get(10, 4)
                self.assertEqual(len(session.calls), 1)

    async def test_mediawiki_errors_and_unrecognized_html(self):
        for payload, expected in [
            ({"error": {"code": "missingtitle"}}, "找不到"),
            ({"error": {"code": "permissiondenied"}}, "查詢失敗"),
            ({"parse": {"text": "<h2>說明</h2><p>內容</p>"}}, "無法解析"),
        ]:
            with self.subTest(payload=payload):
                session = FakeSession(FakeResponse(payload=payload))
                with self.assertRaisesRegex(HistoryError, expected):
                    await HistoryClient(session).get(10, 4)
                self.assertEqual(len(session.calls), 1)

    async def test_failed_request_is_not_cached(self):
        session = FakeSession(FakeResponse(status=404), FakeResponse())
        client = HistoryClient(session)
        with self.assertRaises(HistoryError):
            await client.get(10, 4)
        result = await client.get(10, 4)
        self.assertTrue(result.items)
        self.assertEqual(len(session.calls), 2)

    async def test_absent_category_returns_empty_tuple(self):
        payload = {"parse": {"text": "<h2>大事記</h2><ul><li>2000年：事件</li></ul>"}}
        session = FakeSession(FakeResponse(payload=payload))
        result = await HistoryClient(session).get(10, 4, Category.HOLIDAYS)
        self.assertEqual(result.items, ())

    async def test_retry_after_is_bounded_and_validated(self):
        self.assertEqual(HistoryClient._retry_after("200"), 30.0)
        self.assertEqual(HistoryClient._retry_after("0.25"), 0.25)
        for value in [None, "invalid", "nan", "inf", "-1"]:
            self.assertIsNone(HistoryClient._retry_after(value))


if __name__ == "__main__":
    unittest.main()
