from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from bot.utils.history_today import Category
from database.history import Store, Subscription


class SubscriptionTests(unittest.TestCase):
    def test_defaults_and_immutability(self) -> None:
        sub = Subscription(1, 2, 9, 0, "Asia/Taipei")
        self.assertEqual(sub.category, Category.EVENTS)
        self.assertEqual(sub.count, 5)
        with self.assertRaises(FrozenInstanceError):
            sub.hour = 10  # type: ignore[misc]

    def test_validation(self) -> None:
        valid = Subscription(1, 2, 9, 0, "Asia/Taipei")
        invalid = [
            {"guild_id": 0},
            {"guild_id": True},
            {"guild_id": 2**63},
            {"channel_id": -1},
            {"hour": 24},
            {"hour": 1.5},
            {"minute": -1},
            {"minute": 60},
            {"count": 0},
            {"count": 11},
            {"count": True},
            {"random_count": 1},
            {"timezone": "Not/AZone"},
            {"timezone": "../UTC"},
            {"timezone": None},
            {"category": "not-a-category"},
            {"last_sent_date": "2026-02-30"},
            {"last_sent_date": "20261004"},
            {"last_sent_date": "2026-10-04T09:00:00"},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(valid, **kwargs)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "nested" / "history.sqlite3"
        self.store = Store(self.path)
        self.sub = Subscription(1, 2, 9, 0, "Asia/Taipei")

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_persistence_and_parent_creation(self) -> None:
        self.store.upsert(self.sub)
        self.store.mark_sent(1, "2026-10-04")
        self.store.close()
        self.store = Store(self.path)
        saved = self.store.get(1)
        self.assertEqual(saved, replace(self.sub, last_sent_date="2026-10-04"))
        self.assertIs(saved.category, Category.EVENTS)

    def test_upsert_preserves_saved_sent_date(self) -> None:
        self.store.upsert(self.sub)
        self.store.mark_sent(1, "2026-10-04")
        changed = replace(self.sub, channel_id=8, hour=15, category=Category.BIRTHS)
        self.store.upsert(changed)
        self.assertEqual(
            self.store.get(1), replace(changed, last_sent_date="2026-10-04")
        )
        self.assertEqual(len(self.store.list_all()), 1)

    def test_new_subscription_can_have_marker(self) -> None:
        sub = replace(self.sub, last_sent_date="2026-10-04")
        self.store.upsert(sub)
        self.assertEqual(self.store.get(1), sub)

    def test_saved_date_cannot_go_backwards(self) -> None:
        self.store.upsert(self.sub)
        self.store.mark_sent(1, "2026-10-04")
        self.store.mark_sent(1, "2026-10-03")
        self.assertEqual(self.store.get(1).last_sent_date, "2026-10-04")

    def test_get_list_and_delete(self) -> None:
        self.assertIsNone(self.store.get(1))
        self.assertFalse(self.store.delete(1))
        self.store.upsert(replace(self.sub, guild_id=3))
        self.store.upsert(self.sub)
        self.assertEqual([sub.guild_id for sub in self.store.list_all()], [1, 3])
        self.assertTrue(self.store.delete(1))
        self.assertFalse(self.store.delete(1))
        self.assertIsNone(self.store.get(1))

    def test_marker_format_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.store.mark_sent(1, "tomorrow")

    def test_random_count_is_persisted_and_old_schema_is_migrated(self) -> None:
        self.store.upsert(replace(self.sub, random_count=True))
        self.assertTrue(self.store.get(1).random_count)

        import sqlite3

        legacy_path = self.path.parent / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            """CREATE TABLE subscriptions (
                guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
                hour INTEGER NOT NULL, minute INTEGER NOT NULL, timezone TEXT NOT NULL,
                category TEXT NOT NULL, count INTEGER NOT NULL, last_sent_date TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO subscriptions VALUES (1, 2, 9, 0, 'Asia/Taipei', 'events', 5, NULL)"
        )
        connection.commit()
        connection.close()
        migrated = Store(legacy_path)
        try:
            self.assertFalse(migrated.get(1).random_count)
            migrated.upsert(replace(self.sub, random_count=True))
            self.assertTrue(migrated.get(1).random_count)
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
