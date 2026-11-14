# Cog tests drive Discord event handlers with mocks, which are untyped by
# nature, and set up the cog's internal state directly.
# pyright: reportAny=false, reportPrivateUsage=false
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from bot.cogs.count import Count
from database.count import CountRepository, InvalidGraceError

CHANNEL = 1
GUILD = 10
MONDAY = 1_700_409_600.0


def snowflake(milliseconds: int) -> int:
    return milliseconds << 22


def make_cog(tmp_path: Path) -> tuple[Count, MagicMock]:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.get_context = AsyncMock(return_value=SimpleNamespace(prefix=None))
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHANNEL
    channel.send = AsyncMock()
    bot.get_channel = lambda channel_id: channel if channel_id == CHANNEL else None
    cog = Count(bot)
    cog._repositories[GUILD] = CountRepository(tmp_path / f"{GUILD}.db")
    return cog, channel


async def setup_channel(cog: Count) -> CountRepository:
    repository = cog._repositories[GUILD]
    cog._channels[GUILD] = await repository.initialize()
    await repository.configure(CHANNEL, 0, snowflake(0))
    cog._channels[GUILD].add(CHANNEL)
    return repository


def fake_message(channel: MagicMock, milliseconds: int, content: str, user: int):
    message = MagicMock()
    message.id = snowflake(milliseconds)
    message.content = content
    message.webhook_id = None
    message.guild = SimpleNamespace(id=GUILD)
    message.channel = channel
    message.author = SimpleNamespace(id=user, bot=False, mention=f"<@{user}>")
    message.created_at.timestamp.return_value = MONDAY
    message.add_reaction = AsyncMock()
    message.reply = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_unexpected_errors_are_not_shown_to_members() -> None:
    ctx = MagicMock()
    ctx.send = AsyncMock()
    cog = Count(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    internal = commands.CommandInvokeError(ValueError("secret internal detail"))
    await cog.cog_command_error(ctx, internal)
    assert "secret" not in ctx.send.call_args.args[0]

    grace = commands.CommandInvokeError(InvalidGraceError("免死時間錯誤"))
    await cog.cog_command_error(ctx, grace)
    assert ctx.send.call_args.args[0] == "免死時間錯誤"


@pytest.mark.asyncio
async def test_mistake_replies_are_throttled_per_channel(tmp_path: Path) -> None:
    cog, channel = make_cog(tmp_path)
    _ = await setup_channel(cog)
    first = fake_message(channel, 1_000, "5", 1)
    second = fake_message(channel, 2_000, "7", 2)
    await cog.on_message(first)
    await cog.on_message(second)
    first.reply.assert_awaited_once()
    second.reply.assert_not_awaited()
    # Every mistake still gets its reaction.
    second.add_reaction.assert_awaited_once_with("❌")


@pytest.mark.asyncio
async def test_editing_back_and_forth_announces_once(tmp_path: Path) -> None:
    cog, channel = make_cog(tmp_path)
    _ = await setup_channel(cog)
    await cog.on_message(fake_message(channel, 1_000, "1", 1))

    def edit(content: str) -> discord.RawMessageUpdateEvent:
        payload = SimpleNamespace(
            guild_id=GUILD,
            channel_id=CHANNEL,
            message_id=snowflake(1_000),
            message=SimpleNamespace(content=content),
            cached_message=None,
        )
        # Building a real update event needs a full Message; only these
        # attributes are read by the handler.
        return cast(discord.RawMessageUpdateEvent, cast(object, payload))

    for content in ("哈哈", "1", "嘿嘿", "1", "呵呵"):
        await cog.on_raw_message_edit(edit(content))
    channel.send.assert_awaited_once()
    assert channel.send.call_args.kwargs["allowed_mentions"].users is False


@pytest.mark.asyncio
async def test_deleting_an_older_count_is_silent(tmp_path: Path) -> None:
    cog, channel = make_cog(tmp_path)
    _ = await setup_channel(cog)
    await cog.on_message(fake_message(channel, 1_000, "1", 1))
    await cog.on_message(fake_message(channel, 2_000, "2", 2))
    deleted = discord.RawMessageDeleteEvent(
        {
            "id": str(snowflake(1_000)),
            "channel_id": str(CHANNEL),
            "guild_id": str(GUILD),
        }
    )
    await cog.on_raw_message_delete(deleted)
    channel.send.assert_not_awaited()


def test_expensive_and_admin_commands_are_guarded() -> None:
    group = Count.count
    by_name = {command.name: command for command in group.commands}
    for name in ("stats", "leaderboard"):
        assert by_name[name]._buckets._cooldown is not None
        assert by_name[name]._max_concurrency is not None
    admin = ("setup", "settings", "remove", "give", "give_server", "ban", "unban")
    for name in (*admin, "bans"):
        checks = [check.__qualname__ for check in by_name[name].checks]
        assert any("has_guild_permissions" in check for check in checks), name
