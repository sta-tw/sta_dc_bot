"""Local-calendar daily delivery with persisted duplicate prevention."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from time import monotonic
from zoneinfo import ZoneInfo

from database.history import Store, Subscription

logger = logging.getLogger(__name__)


def _require_aware(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("排程時間必須包含時區。")


def is_due(sub: Subscription, now: datetime) -> bool:
    """Send at or after the scheduled local time, at most once per date.

    A start later in the day catches up on today's post. A future sent marker
    suppresses delivery until the local calendar has advanced beyond it.
    """
    _require_aware(now)
    local_now = now.astimezone(ZoneInfo(sub.timezone))
    local_date = local_now.date().isoformat()
    return (
        (local_now.hour, local_now.minute) >= (sub.hour, sub.minute)
        and (sub.last_sent_date is None or sub.last_sent_date < local_date)
    )


@dataclass(frozen=True)
class _Retry:
    local_date: str
    attempts: int
    not_before: float


class DailyScheduler:
    def __init__(
        self, store: Store, send: Callable[[Subscription, date], Awaitable[None]]
    ) -> None:
        self.store = store
        self._send = send
        self._tick_lock = asyncio.Lock()
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._retries: dict[int, _Retry] = {}

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    async def set_subscription(self, sub: Subscription) -> None:
        async with self._guild_lock(sub.guild_id):
            self.store.upsert(sub)
            self._retries.pop(sub.guild_id, None)

    async def remove_subscription(self, guild_id: int) -> bool:
        async with self._guild_lock(guild_id):
            removed = self.store.delete(guild_id)
            self._retries.pop(guild_id, None)
            return removed

    async def tick(self, now: datetime | None = None) -> None:
        if now is not None:
            _require_aware(now)
        async with self._tick_lock:
            semaphore = asyncio.Semaphore(4)
            results = await asyncio.gather(
                *(
                    self._send_if_due(sub.guild_id, now, semaphore)
                    for sub in self.store.list_all()
                ),
                return_exceptions=True,
            )
            # Finish the batch before releasing the tick lock, including when a
            # persistence error occurs after one of the sends has completed.
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    async def _send_if_due(
        self, guild_id: int, now: datetime | None, semaphore: asyncio.Semaphore
    ) -> None:
        async with semaphore, self._guild_lock(guild_id):
            # Settings may have changed while another server was sending.
            sub = self.store.get(guild_id)
            # A queued guild can enter a new local day while earlier sends wait.
            # Keep the source date and the persisted delivery marker identical.
            current_time = now if now is not None else datetime.now(timezone.utc)
            if sub is None or not is_due(sub, current_time):
                return
            local_day = current_time.astimezone(ZoneInfo(sub.timezone)).date()
            local_date = local_day.isoformat()
            retry = self._retries.get(guild_id)
            if retry is not None and retry.local_date != local_date:
                retry = None
            if retry is not None and monotonic() < retry.not_before:
                return
            try:
                await self._send(sub, local_day)
            except Exception as exc:
                attempts = retry.attempts + 1 if retry is not None else 1
                delay = min(60 * 2 ** min(attempts - 1, 4), 900)
                self._retries[guild_id] = _Retry(
                    local_date, attempts, monotonic() + delay
                )
                # Do not expose exception messages, response bodies, or secrets.
                logger.warning(
                    "Daily send failed: guild=%s channel=%s error=%s",
                    sub.guild_id,
                    sub.channel_id,
                    type(exc).__name__,
                )
                return
            self.store.mark_sent(guild_id, local_date)
            self._retries.pop(guild_id, None)
