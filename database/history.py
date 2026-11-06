"""SQLite persistence for one daily history subscription per Discord server."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bot.utils.history_today import Category


def _validate_date(value: str) -> None:
    try:
        valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("傳送日期必須使用 YYYY-MM-DD 格式。")


@dataclass(frozen=True)
class Subscription:
    guild_id: int
    channel_id: int
    hour: int
    minute: int
    timezone: str
    category: Category = Category.EVENTS
    count: int = 5
    random_count: bool = False
    last_sent_date: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("伺服器", self.guild_id), ("頻道", self.channel_id)):
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError(f"{name} ID 必須是有效的正整數。")
        if type(self.hour) is not int or not 0 <= self.hour <= 23:
            raise ValueError("小時必須介於 0 到 23。")
        if type(self.minute) is not int or not 0 <= self.minute <= 59:
            raise ValueError("分鐘必須介於 0 到 59。")
        if type(self.count) is not int or not 1 <= self.count <= 10:
            raise ValueError("筆數必須介於 1 到 10。")
        if type(self.random_count) is not bool:
            raise ValueError("隨機筆數設定必須是布林值。")
        try:
            if not isinstance(self.timezone, str):
                raise ValueError
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError("請提供有效的 IANA 時區，例如 Asia/Taipei。") from None
        try:
            object.__setattr__(self, "category", Category(self.category))
        except (ValueError, TypeError):
            raise ValueError("請選擇有效的歷史類別。") from None
        if self.last_sent_date is not None:
            _validate_date(self.last_sent_date)


class Store:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            path = Path(path).expanduser()
        self._connection = sqlite3.connect(str(path), timeout=10)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    hour INTEGER NOT NULL,
                    minute INTEGER NOT NULL,
                    timezone TEXT NOT NULL,
                    category TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    random_count INTEGER NOT NULL DEFAULT 0,
                    last_sent_date TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(subscriptions)")
            }
            if "random_count" not in columns:
                self._connection.execute(
                    "ALTER TABLE subscriptions ADD COLUMN random_count INTEGER NOT NULL DEFAULT 0"
                )

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _subscription(row: sqlite3.Row) -> Subscription:
        values = dict(row)
        values["random_count"] = bool(values["random_count"])
        return Subscription(**values)

    def get(self, guild_id: int) -> Subscription | None:
        row = self._connection.execute(
            "SELECT * FROM subscriptions WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return self._subscription(row) if row is not None else None

    def list_all(self) -> list[Subscription]:
        rows = self._connection.execute(
            "SELECT * FROM subscriptions ORDER BY guild_id"
        ).fetchall()
        return [self._subscription(row) for row in rows]

    def upsert(self, sub: Subscription) -> None:
        # Validate even if a caller has bypassed the frozen dataclass constructor.
        sub.__post_init__()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO subscriptions (
                    guild_id, channel_id, hour, minute, timezone, category,
                    count, random_count, last_sent_date
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    hour = excluded.hour,
                    minute = excluded.minute,
                    timezone = excluded.timezone,
                    category = excluded.category,
                    count = excluded.count,
                    random_count = excluded.random_count
                """,
                (
                    sub.guild_id,
                    sub.channel_id,
                    sub.hour,
                    sub.minute,
                    sub.timezone,
                    sub.category.value,
                    sub.count,
                    int(sub.random_count),
                    sub.last_sent_date,
                ),
            )

    def delete(self, guild_id: int) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM subscriptions WHERE guild_id = ?", (guild_id,)
            )
        return cursor.rowcount > 0

    def mark_sent(self, guild_id: int, local_date: str) -> None:
        _validate_date(local_date)
        with self._connection:
            self._connection.execute(
                """
                UPDATE subscriptions SET last_sent_date = ?
                WHERE guild_id = ?
                  AND (last_sent_date IS NULL OR last_sent_date < ?)
                """,
                (local_date, guild_id, local_date),
            )
