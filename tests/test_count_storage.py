# Storage tests drive the real cog and real repositories against a temporary data
# directory, so they assert which files exist and which database statements can run.
# pyright: reportAny=false, reportPrivateUsage=false
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import discord
import pytest
from discord.ext import commands

from bot.cogs.count import Count, StatsQuery
from bot.utils.config_paths import ConfigPaths
from database.resource_library import ResourceRepository

CHANNEL = 1
GUILD = 10
OTHER_GUILD = 11
MONDAY = 1_700_409_600.0


def snowflake(milliseconds: int) -> int:
    return milliseconds << 22


def make_cog() -> Count:
    return Count(commands.Bot(command_prefix="!", intents=discord.Intents.none()))


def make_guild(guild_id: int) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.get_channel = MagicMock(return_value=None)
    return guild


@pytest.mark.asyncio
async def test_resource_database_initializes_while_counting_holds_a_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Breaks if counting and the resource library share one database file again:
    # the resource library switches that file to WAL, which fails while another
    # connection holds a write lock.
    monkeypatch.setattr(ConfigPaths, "DATABASE_DIR", tmp_path)
    cog = make_cog()
    async with cog._lock(GUILD):
        counting = await cog._repository(GUILD)
    holder = sqlite3.connect(counting.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        await ResourceRepository(ConfigPaths.guild_database(GUILD)).initialize()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


@pytest.mark.asyncio
async def test_startup_creates_no_counting_database_for_a_guild_without_counting_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Breaks if startup creates a database for every available guild.
    monkeypatch.setattr(ConfigPaths, "DATABASE_DIR", tmp_path)
    await make_cog().on_guild_available(make_guild(GUILD))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_startup_forgets_channels_deleted_while_the_bot_was_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Breaks if the startup check skips guilds whose counting data already exists.
    monkeypatch.setattr(ConfigPaths, "DATABASE_DIR", tmp_path)
    before = make_cog()
    async with before._lock(GUILD):
        repository = await before._repository(GUILD)
        await repository.configure(CHANNEL, 0, snowflake(0))

    restarted = make_cog()
    await restarted.on_guild_available(make_guild(GUILD))

    assert restarted._channels[GUILD] == set()
    assert await restarted._repositories[GUILD].state(CHANNEL) is None


@pytest.mark.asyncio
async def test_global_statistics_count_every_guilds_counting_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Breaks if the global scan stops finding counting databases after they move.
    monkeypatch.setattr(ConfigPaths, "DATABASE_DIR", tmp_path)
    cog = make_cog()
    for guild_id, user_id in ((GUILD, 7), (OTHER_GUILD, 8)):
        async with cog._lock(guild_id):
            repository = await cog._repository(guild_id)
            await repository.configure(CHANNEL, 0, snowflake(0))
            _ = await repository.accept(CHANNEL, 1, snowflake(1_000), user_id, MONDAY)

    rows = await cog._statistics(GUILD, StatsQuery(is_global=True))

    assert sorted((row["user_id"], row["successes"]) for row in rows) == [(7, 1), (8, 1)]
