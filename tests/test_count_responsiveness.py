# Responsiveness tests: a heavy expression must not stall the event loop.
# pyright: reportAny=false, reportPrivateUsage=false
import asyncio
import time
from collections.abc import Awaitable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from bot.cogs.count import Count, CountTestModal
from bot.utils import counting
from database.count import CountRepository

CHANNEL = 1
GUILD = 10
# Ten factorizations of a prime near 10**12: about 0.4 s of pure CPU work.
HEAVY = "∑ₖ₌₁¹⁰ totient(999999999989)"


def snowflake(milliseconds: int) -> int:
    return milliseconds << 22


def make_cog(tmp_path: Path) -> tuple[Count, MagicMock]:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.get_context = AsyncMock(return_value=SimpleNamespace(prefix=None))
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHANNEL
    channel.send = AsyncMock()
    cog = Count(bot)
    cog._repositories[GUILD] = CountRepository(tmp_path / f"{GUILD}.db")
    return cog, channel


async def setup_channel(cog: Count) -> None:
    repository = cog._repositories[GUILD]
    cog._channels[GUILD] = await repository.initialize()
    await repository.configure(CHANNEL, 0, snowflake(0))
    cog._channels[GUILD].add(CHANNEL)


def fake_message(channel: MagicMock, content: str) -> MagicMock:
    message = MagicMock()
    message.id = snowflake(1_000)
    message.content = content
    message.webhook_id = None
    message.guild = SimpleNamespace(id=GUILD)
    message.channel = channel
    message.author = SimpleNamespace(id=1, bot=False, mention="<@1>")
    message.created_at.timestamp.return_value = 1_700_409_600.0
    message.add_reaction = AsyncMock()
    message.reply = AsyncMock()
    return message


async def longest_stall_during(work: Awaitable[object]) -> float:
    """Longest gap, in seconds, between 10 ms event loop heartbeats while work ran.

    A synchronous evaluation delays the first heartbeat until it finishes, so the
    gap equals the evaluation time. Offloaded work keeps the gap near 10 ms.
    """
    last = time.monotonic()
    longest = 0.0

    async def heartbeat() -> None:
        nonlocal last, longest
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            longest = max(longest, now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    try:
        _ = await work
    finally:
        _ = beat.cancel()
    return max(longest, time.monotonic() - last)


@pytest.mark.asyncio
async def test_heavy_count_message_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Breaks if counting parses messages on the event loop: heartbeats would stall
    # for the whole evaluation instead of only for the time limit.
    monkeypatch.setattr(counting, "EVALUATION_TIME_LIMIT", 0.2, raising=False)
    cog, channel = make_cog(tmp_path)
    await setup_channel(cog)

    stall = await longest_stall_during(cog.on_message(fake_message(channel, HEAVY)))

    assert stall < 0.1


@pytest.mark.asyncio
async def test_count_test_modal_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Breaks if the /count test modal evaluates on the event loop, the same way the
    # counting channel would. A timed-out expression shows 0, as the rule says.
    monkeypatch.setattr(counting, "EVALUATION_TIME_LIMIT", 0.2, raising=False)
    modal = CountTestModal()
    modal.content._value = HEAVY
    send = AsyncMock()
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=send))
    try:
        stall = await longest_stall_during(modal.on_submit(interaction))
    finally:
        modal.content._value = None

    assert stall < 0.1
    assert send.await_args is not None
    assert send.await_args.args[0] == "結果：**0**"
