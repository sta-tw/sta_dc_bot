from __future__ import annotations

import asyncio
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord
import pytest

from bot.cogs import king_of_nonsense as king
from bot.utils.message_history import HistoryProgress, HistoryScope, boundary_id
from database.db_manager import DatabaseManager


async def database(tmp_path):
    db = DatabaseManager.__new__(DatabaseManager)
    db.db_name = str(tmp_path / "guild.db")
    await db.init_db()
    async with aiosqlite.connect(db.db_name) as connection:
        await connection.execute("CREATE TABLE resource_sentinel (value TEXT)")
        await connection.execute("INSERT INTO resource_sentinel VALUES ('keep')")
        await connection.commit()
    return db


def cog():
    return king.KingOfNonsense(SimpleNamespace(user=SimpleNamespace(id=8), logger=Mock()))


@pytest.mark.asyncio
async def test_atomic_seed_preserves_live_counts_and_unrelated_tables(tmp_path):
    db = await database(tmp_path)
    await db.increment_message_count(2, 99)
    assert await db.begin_message_history_seed("attempt", 100)
    await db.increment_message_count(2, 2)
    assert await db.finish_message_history_seed("attempt", {2: 3, 3: 4}, "snapshot")
    assert await db.is_message_history_verified()
    assert await db.get_message_leaderboard() == [{"user_id": 2, "message_count": 5}, {"user_id": 3, "message_count": 4}]
    assert not await db.finish_message_history_seed("attempt", {2: 3, 3: 4}, "snapshot")
    assert not await db.begin_message_history_seed("again", 200, allow_legacy_reseed=True)
    async with aiosqlite.connect(db.db_name) as connection:
        assert await (await connection.execute("SELECT value FROM resource_sentinel")).fetchone() == ("keep",)


@pytest.mark.asyncio
async def test_stale_attempt_cannot_finalize(tmp_path):
    db = await database(tmp_path)
    await db.begin_message_history_seed("current", 100)
    await db.increment_message_count(2)
    with pytest.raises(ValueError, match="取代"):
        await db.finish_message_history_seed("stale", {2: 9}, "snapshot")
    assert not await db.is_message_history_seeded()
    assert await db.get_message_leaderboard() == [{"user_id": 2, "message_count": 1}]


@pytest.mark.asyncio
async def test_metadata_failure_rolls_back_historical_merge(tmp_path):
    db = await database(tmp_path)
    await db.begin_message_history_seed("attempt", 100)
    await db.increment_message_count(2)
    async with aiosqlite.connect(db.db_name) as connection:
        await connection.execute("""
            CREATE TRIGGER reject_completion BEFORE INSERT ON message_stats_meta
            WHEN NEW.key = 'history_seeded' AND NEW.value = '1'
            BEGIN SELECT RAISE(ABORT, 'test rollback'); END
        """)
        await connection.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await db.finish_message_history_seed("attempt", {2: 8}, "snapshot")
    assert not await db.is_message_history_seeded()
    assert await db.get_message_leaderboard() == [{"user_id": 2, "message_count": 1}]


@pytest.mark.asyncio
async def test_legacy_seed_can_only_be_replaced_explicitly(tmp_path):
    db = await database(tmp_path)
    await db.set_message_history_seeded(True)
    await db.increment_message_count(2, 5)
    assert not await db.is_message_history_verified()
    assert not await db.begin_message_history_seed("denied", 100)
    assert await db.begin_message_history_seed("upgrade", 100, allow_legacy_reseed=True)
    assert not await db.is_message_history_seeded()
    assert await db.get_message_leaderboard() == []


@pytest.mark.asyncio
async def test_listener_uses_snowflake_boundary(tmp_path):
    db = await database(tmp_path)
    instance = cog()
    guild = SimpleNamespace(id=1)
    instance._get_db = AsyncMock(return_value=db)
    instance._history_cutoffs[1] = 100
    for message_id in [99, 100, 101]:
        await instance.on_message(SimpleNamespace(id=message_id, guild=guild,
            author=SimpleNamespace(id=2, bot=False), webhook_id=None))
    assert await db.get_message_leaderboard() == [{"user_id": 2, "message_count": 2}]


@pytest.mark.asyncio
async def test_live_messages_during_scan_are_added_once(tmp_path, monkeypatch):
    db = await database(tmp_path)
    await db.begin_message_history_seed("attempt", 100)
    instance = cog()
    guild = SimpleNamespace(id=1)
    instance._get_db = AsyncMock(return_value=db)
    instance._history_cutoffs[1] = 100
    instance._seed_tokens[1] = "attempt"
    entered, release = asyncio.Event(), asyncio.Event()

    async def collect(*args, **kwargs):
        entered.set()
        await release.wait()
        return Counter({2: 3})

    monkeypatch.setattr(king, "discover_scope", AsyncMock(return_value=HistoryScope(1)))
    monkeypatch.setattr(king, "collect_history", collect)
    task = asyncio.create_task(instance._ensure_history_seeded(guild, db))
    await entered.wait()
    for message_id in [99, 100, 101]:
        await instance.on_message(SimpleNamespace(id=message_id, guild=guild,
            author=SimpleNamespace(id=2, bot=False), webhook_id=None))
    release.set()
    assert await task
    assert await db.get_message_leaderboard() == [{"user_id": 2, "message_count": 5}]


@pytest.mark.asyncio
async def test_retry_retains_boundary_and_live_counts(tmp_path, monkeypatch):
    db = await database(tmp_path)
    await db.begin_message_history_seed("attempt", 100)
    instance = cog()
    instance._history_cutoffs[1] = 100
    instance._seed_tokens[1] = "attempt"
    monkeypatch.setattr(king, "discover_scope", AsyncMock(return_value=HistoryScope(1)))
    monkeypatch.setattr(king, "collect_history", AsyncMock(side_effect=[ValueError("read failed"), Counter({2: 3})]))
    guild = SimpleNamespace(id=1)
    with pytest.raises(ValueError):
        await instance._ensure_history_seeded(guild, db)
    assert not await db.is_message_history_seeded()
    await db.increment_message_count(2, 2)
    assert await instance._ensure_history_seeded(guild, db)
    assert instance._history_cutoffs[1] == 100
    assert await db.get_message_leaderboard() == [{"user_id": 2, "message_count": 5}]


@pytest.mark.asyncio
async def test_unseeded_commands_return_progress_and_share_one_background_task():
    instance = cog()
    guild = SimpleNamespace(id=1)
    db = SimpleNamespace(is_message_history_seeded=AsyncMock(return_value=False))
    instance._get_db = AsyncMock(return_value=db)
    instance._ensure_history_seeded = AsyncMock(side_effect=lambda *args: None)
    blocked = asyncio.Event()

    async def run(*args):
        await blocked.wait()

    instance._run_history_seed = run
    interaction = SimpleNamespace(guild=guild, response=SimpleNamespace(defer=AsyncMock()),
                                  edit_original_response=AsyncMock())
    await king.KingOfNonsense.leaderboard.callback(instance, interaction)
    first = instance._seed_tasks[1]
    await king.KingOfNonsense.leaderboard.callback(instance, interaction)
    assert instance._seed_tasks[1] is first
    assert interaction.edit_original_response.await_count == 2
    assert "背景" in interaction.edit_original_response.call_args.kwargs["content"]
    instance._ensure_history_seeded.assert_not_awaited()
    await instance.cog_unload()
    assert first.cancelled()


@pytest.mark.asyncio
async def test_cancelled_seed_is_not_completed(tmp_path, monkeypatch):
    db = await database(tmp_path)
    await db.begin_message_history_seed("attempt", 100)
    instance = cog()
    instance._history_cutoffs[1] = 100
    instance._seed_tokens[1] = "attempt"
    instance._seed_progress[1] = HistoryProgress()
    entered = asyncio.Event()
    never = asyncio.Event()

    async def collect(*args, **kwargs):
        entered.set()
        await never.wait()

    monkeypatch.setattr(king, "discover_scope", AsyncMock(return_value=HistoryScope(1)))
    monkeypatch.setattr(king, "collect_history", collect)
    task = asyncio.create_task(instance._run_history_seed(SimpleNamespace(id=1), db))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await db.is_message_history_seeded()
    assert instance._seed_progress[1].error == "統計工作已取消"


@pytest.mark.asyncio
async def test_member_chunk_has_a_timeout(monkeypatch):
    instance = cog()
    guild = SimpleNamespace(chunked=False, chunk=AsyncMock())

    async def timeout(awaitable, *, timeout):
        assert timeout == 20
        awaitable.close()
        raise TimeoutError()

    monkeypatch.setattr(king.asyncio, "wait_for", timeout)
    with pytest.raises(ValueError, match="成員資料"):
        await instance._get_current_member_entries(guild, SimpleNamespace())


@pytest.mark.asyncio
async def test_boundary_is_installed_before_database_is_exposed(tmp_path, monkeypatch):
    db = await database(tmp_path)
    instance = cog()
    guild = SimpleNamespace(id=1, name="guild")
    upper = boundary_id(discord.utils.utcnow() + timedelta(seconds=1))
    monkeypatch.setattr(king, "DatabaseManager", lambda *args: db)
    monkeypatch.setattr(king.ConfigPaths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(king, "next_boundary_id", lambda: upper)
    await instance.on_message(SimpleNamespace(id=upper - 1, guild=guild,
        author=SimpleNamespace(id=2, bot=False), webhook_id=None))
    assert instance._history_cutoffs[1] == upper
    assert instance._seed_tokens[1]
    assert await db.get_message_leaderboard() == []
    await instance.on_message(SimpleNamespace(id=upper, guild=guild,
        author=SimpleNamespace(id=2, bot=False), webhook_id=None))
    assert await db.get_message_leaderboard() == [{"user_id": 2, "message_count": 1}]
