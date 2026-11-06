from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.utils.history_scheduler import DailyScheduler, is_due
from database.history import Store, Subscription


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class DueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sub = Subscription(1, 2, 9, 0, "Asia/Taipei")

    def test_local_schedule_and_late_start(self) -> None:
        self.assertFalse(is_due(self.sub, utc("2026-10-04T00:59:59")))
        self.assertTrue(is_due(self.sub, utc("2026-10-04T01:00:00")))
        self.assertTrue(is_due(self.sub, utc("2026-10-04T15:59:00")))

    def test_local_midnight_and_future_marker(self) -> None:
        sub = replace(self.sub, hour=0, last_sent_date="2026-10-04")
        self.assertFalse(is_due(sub, utc("2026-10-03T15:59:00")))
        self.assertFalse(is_due(sub, utc("2026-10-03T16:00:00")))
        self.assertTrue(is_due(sub, utc("2026-10-04T16:00:00")))

    def test_dst_skipped_time_catches_up(self) -> None:
        sub = replace(self.sub, hour=2, minute=30, timezone="America/New_York")
        self.assertFalse(is_due(sub, utc("2026-03-08T06:59:00")))
        self.assertTrue(is_due(sub, utc("2026-03-08T07:00:00")))

    def test_dst_repeated_hour_respects_saved_date(self) -> None:
        sub = replace(self.sub, hour=1, minute=30, timezone="America/New_York")
        self.assertTrue(is_due(sub, utc("2026-11-01T05:30:00")))
        sent = replace(sub, last_sent_date="2026-11-01")
        self.assertFalse(is_due(sent, utc("2026-11-01T06:30:00")))

    def test_naive_datetime_rejected(self) -> None:
        with self.assertRaises(ValueError):
            is_due(self.sub, datetime(2026, 10, 4, 9))


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = Store(":memory:")
        self.sub = Subscription(1, 2, 9, 0, "Asia/Taipei")
        self.store.upsert(self.sub)
        self.send = AsyncMock()
        self.scheduler = DailyScheduler(self.store, self.send)
        self.now = utc("2026-10-04T01:00:00")

    async def asyncTearDown(self) -> None:
        self.store.close()

    async def test_once_per_day_and_no_overlap(self) -> None:
        await asyncio.gather(self.scheduler.tick(self.now), self.scheduler.tick(self.now))
        self.send.assert_awaited_once_with(self.sub, date(2026, 10, 4))
        self.assertEqual(self.store.get(1).last_sent_date, "2026-10-04")
        await self.scheduler.tick(utc("2026-10-05T00:59:00"))
        self.assertEqual(self.send.await_count, 1)
        await self.scheduler.tick(utc("2026-10-05T01:00:00"))
        self.assertEqual(self.send.await_count, 2)

    async def test_success_marker_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.sqlite3"
            first = Store(path)
            first.upsert(self.sub)
            await DailyScheduler(first, self.send).tick(self.now)
            first.close()
            second = Store(path)
            try:
                await DailyScheduler(second, self.send).tick(self.now)
                self.assertEqual(self.send.await_count, 1)
            finally:
                second.close()

    async def test_failure_retries_without_marking_or_logging_message(self) -> None:
        self.send.side_effect = [RuntimeError("secret-value"), RuntimeError("secret-value"), None]
        with patch("bot.utils.history_scheduler.monotonic", return_value=100):
            with self.assertLogs("bot.utils.history_scheduler", level="WARNING") as logs:
                await self.scheduler.tick(self.now)
        self.assertNotIn("secret-value", " ".join(logs.output))
        self.assertIsNone(self.store.get(1).last_sent_date)
        with patch("bot.utils.history_scheduler.monotonic", return_value=159):
            await self.scheduler.tick(self.now)
        self.assertEqual(self.send.await_count, 1)
        with patch("bot.utils.history_scheduler.monotonic", return_value=160):
            with self.assertLogs("bot.utils.history_scheduler", level="WARNING"):
                await self.scheduler.tick(self.now)
        with patch("bot.utils.history_scheduler.monotonic", return_value=279):
            await self.scheduler.tick(self.now)
        self.assertEqual(self.send.await_count, 2)
        with patch("bot.utils.history_scheduler.monotonic", return_value=280):
            await self.scheduler.tick(self.now)
        self.assertEqual(self.send.await_count, 3)
        self.assertEqual(self.store.get(1).last_sent_date, "2026-10-04")

    async def test_new_local_date_resets_failure_backoff(self) -> None:
        self.send.side_effect = [RuntimeError("failed"), None]
        with patch("bot.utils.history_scheduler.monotonic", return_value=100):
            with self.assertLogs("bot.utils.history_scheduler", level="WARNING"):
                await self.scheduler.tick(self.now)
        with patch("bot.utils.history_scheduler.monotonic", return_value=101):
            await self.scheduler.tick(utc("2026-10-05T01:00:00"))
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.store.get(1).last_sent_date, "2026-10-05")

    async def test_setting_change_clears_failure_backoff(self) -> None:
        self.send.side_effect = [RuntimeError("failed"), None]
        with patch("bot.utils.history_scheduler.monotonic", return_value=100):
            with self.assertLogs("bot.utils.history_scheduler", level="WARNING"):
                await self.scheduler.tick(self.now)
        changed = replace(self.sub, channel_id=9)
        await self.scheduler.set_subscription(changed)
        with patch("bot.utils.history_scheduler.monotonic", return_value=101):
            await self.scheduler.tick(self.now)
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.send.await_args.args[0].channel_id, 9)

    async def test_persistence_error_waits_for_other_sends(self) -> None:
        self.store.upsert(replace(self.sub, guild_id=2))
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(sub: Subscription, local_day: date) -> None:
            if sub.guild_id == 2:
                started.set()
                await release.wait()

        self.scheduler = DailyScheduler(self.store, send)
        mark_sent = self.store.mark_sent

        def failing_marker(guild_id: int, local_date: str) -> None:
            if guild_id == 1:
                raise RuntimeError("persistence failure")
            mark_sent(guild_id, local_date)

        with patch.object(self.store, "mark_sent", side_effect=failing_marker):
            tick = asyncio.create_task(self.scheduler.tick(self.now))
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(0)
            self.assertFalse(tick.done())
            release.set()
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(tick, 1)
        self.assertEqual(self.store.get(2).last_sent_date, "2026-10-04")

    async def test_setting_change_waits_for_inflight_delivery(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(sub: Subscription, local_day: date) -> None:
            started.set()
            await release.wait()

        self.scheduler = DailyScheduler(self.store, send)
        tick = asyncio.create_task(self.scheduler.tick(self.now))
        await asyncio.wait_for(started.wait(), 1)
        changed = replace(self.sub, channel_id=9)
        mutation = asyncio.create_task(self.scheduler.set_subscription(changed))
        await asyncio.sleep(0)
        self.assertFalse(mutation.done())
        release.set()
        await asyncio.wait_for(asyncio.gather(tick, mutation), 1)
        self.assertEqual(
            self.store.get(1), replace(changed, last_sent_date="2026-10-04")
        )

    async def test_remove_waits_for_inflight_delivery(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(sub: Subscription, local_day: date) -> None:
            started.set()
            await release.wait()

        self.scheduler = DailyScheduler(self.store, send)
        tick = asyncio.create_task(self.scheduler.tick(self.now))
        await asyncio.wait_for(started.wait(), 1)
        removal = asyncio.create_task(self.scheduler.remove_subscription(1))
        await asyncio.sleep(0)
        self.assertFalse(removal.done())
        release.set()
        await asyncio.wait_for(tick, 1)
        self.assertTrue(await asyncio.wait_for(removal, 1))
        self.assertIsNone(self.store.get(1))

    async def test_queued_sends_reread_settings_and_limit_concurrency(self) -> None:
        for guild_id in range(2, 6):
            self.store.upsert(replace(self.sub, guild_id=guild_id))
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[Subscription] = []
        active = 0
        peak = 0

        async def send(sub: Subscription, local_day: date) -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            calls.append(sub)
            if len(calls) == 4:
                started.set()
            await release.wait()
            active -= 1

        self.scheduler = DailyScheduler(self.store, send)
        tick = asyncio.create_task(self.scheduler.tick(self.now))
        await asyncio.wait_for(started.wait(), 1)
        await self.scheduler.set_subscription(replace(self.sub, guild_id=5, channel_id=9))
        release.set()
        await asyncio.wait_for(tick, 1)
        self.assertEqual(peak, 4)
        self.assertEqual(next(sub for sub in calls if sub.guild_id == 5).channel_id, 9)

    async def test_queued_send_uses_current_local_day_after_midnight(self) -> None:
        for guild_id in range(1, 6):
            self.store.upsert(replace(self.sub, guild_id=guild_id, hour=0))
        clock = [utc("2026-10-04T15:59:00")]

        class ControlledDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(clock[0].timestamp(), tz)

        started = asyncio.Event()
        release = asyncio.Event()
        delivery_days: dict[int, date] = {}

        async def send(sub: Subscription, local_day: date) -> None:
            delivery_days[sub.guild_id] = local_day
            if len(delivery_days) == 4:
                started.set()
            await release.wait()

        self.scheduler = DailyScheduler(self.store, send)
        with patch("bot.utils.history_scheduler.datetime", ControlledDateTime):
            tick = asyncio.create_task(self.scheduler.tick())
            await asyncio.wait_for(started.wait(), 1)
            clock[0] = utc("2026-10-04T16:00:00")
            release.set()
            await asyncio.wait_for(tick, 1)
        self.assertEqual(delivery_days[1], date(2026, 10, 4))
        self.assertEqual(delivery_days[5], date(2026, 10, 5))
        for guild_id, local_day in delivery_days.items():
            self.assertEqual(
                self.store.get(guild_id).last_sent_date, local_day.isoformat()
            )

    async def test_naive_tick_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.scheduler.tick(datetime(2026, 10, 4, 9))
        self.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
