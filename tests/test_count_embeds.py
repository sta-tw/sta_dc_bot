# Discord command callbacks are exercised with mocks instead of a live client.
# pyright: reportAny=false, reportPrivateUsage=false
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp import ClientResponse
from discord.ext import commands

import bot.cogs.count as count_cog
from bot.cogs.count import Count, StatsQuery
from database.count import COOLDOWN, UserStatistics

NOW = 1_700_409_600.0


async def invoke(command: object, cog: Count, ctx: MagicMock, **options: object) -> None:
    # Command.callback runs with the cog argument even though the decorator's
    # public type describes invocation through the bound command object.
    callback = cast(Callable[..., Awaitable[None]], getattr(command, "callback"))
    await callback(cog, ctx, **options)


@pytest.fixture
def command_context(monkeypatch: pytest.MonkeyPatch):
    bot = MagicMock(spec=commands.Bot)
    bot.get_user.return_value = None
    bot.fetch_user = AsyncMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 10
    guild.name = "測試伺服器"
    guild.get_member.side_effect = lambda user_id: SimpleNamespace(
        id=user_id, name=f"user_{user_id}", display_name=f"暱稱 {user_id}"
    )
    ctx = MagicMock()
    ctx.guild = guild
    ctx.channel = SimpleNamespace(id=123, name="數數", mention="<#123>")
    ctx.author = SimpleNamespace(id=42, name="author.user", mention="<@42>")
    ctx.send = AsyncMock()
    ctx.defer = AsyncMock()
    repository = MagicMock()
    cog = Count(bot)
    monkeypatch.setattr(cog, "_repository", AsyncMock(return_value=repository))
    monkeypatch.setattr(count_cog.time, "time", lambda: NOW)
    return cog, ctx, repository


def sent_embed(ctx: MagicMock) -> discord.Embed:
    ctx.send.assert_awaited_once()
    sent = ctx.send.await_args
    assert sent is not None
    embed = sent.kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    assert sent.kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    return embed


def field_text(embed: discord.Embed) -> str:
    return "\n".join(str(field.value) for field in embed.fields)


def statistics(
    user_id: int, successes: int = 0, errors: int = 0, protected: int = 0
) -> UserStatistics:
    attempts = successes + errors
    return {
        "user_id": user_id,
        "successes": successes,
        "errors": errors,
        "protected": protected,
        "revivals": 2,
        "sacrifices": 3,
        "last_active": NOW,
        "error_rate": errors / attempts if attempts else 0,
    }


@pytest.mark.asyncio
async def test_setup_requires_embed_permission_before_touching_database(command_context) -> None:
    cog, ctx, _ = command_context
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 123
    channel.mention = "<#123>"
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        add_reactions=True,
        embed_links=False,
    )

    await invoke(Count.count_setup, cog, ctx, channel=channel)

    cog._repository.assert_not_awaited()
    sent = ctx.send.await_args
    assert sent is not None
    assert "嵌入連結" in sent.args[0]
    assert sent.kwargs.get("embed") is None
    assert sent.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_status_embed_formats_progress_and_records(command_context) -> None:
    cog, ctx, repository = command_context
    repository.state = AsyncMock(
        return_value={
            "current_number": "1234",
            "best_number": "6132",
            "grace_seconds": 0.5,
        }
    )

    await invoke(Count.count_status, cog, ctx)

    embed = sent_embed(ctx)
    values = field_text(embed)
    assert len(embed.fields) >= 4
    assert "1,234" in values
    assert "1,235" in values
    assert "6,132" in values
    assert "0.5" in values
    assert "<#123>" in (embed.description or "")


@pytest.mark.asyncio
async def test_wallet_embed_preserves_fractions_and_relative_timestamp(command_context) -> None:
    cog, ctx, repository = command_context
    repository.wallet = AsyncMock(
        return_value=(
            {
                "units": 175,
                "last_claim": NOW - 3600,
                "claim_date": "2023-11-20",
                "claims_today": 1,
            },
            225,
        )
    )

    await invoke(Count.count_wallet, cog, ctx)

    embed = sent_embed(ctx)
    values = field_text(embed)
    assert "1.75" in values
    assert "2.25" in values
    assert f"<t:{int(NOW - 3600 + COOLDOWN)}:R>" in values
    assert "```" not in values
    repository.wallet.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_stats_embed_formats_totals_and_keeps_query_scope(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    query_rows = AsyncMock(
        return_value=[statistics(1, 6132, 100, 1200), statistics(2, 3473, 200, 145)]
    )
    monkeypatch.setattr(cog, "_statistics", query_rows)

    await invoke(Count.count_stats, cog, ctx, scope="global", period="7d")

    embed = sent_embed(ctx)
    values = field_text(embed)
    assert "9,605" in values
    assert "300" in values
    assert "1,345" in values
    assert "3.0%" in values
    assert f"<t:{int(NOW)}:R>" in values
    assert "全域" in (embed.description or "")
    assert "近 7 天" in (embed.description or "")
    assert query_rows.await_args is not None
    sent_query = query_rows.await_args.args[1]
    assert isinstance(sent_query, StatsQuery)
    assert (sent_query.is_global, sent_query.days, sent_query.user_id) == (True, 7, None)
    ctx.defer.assert_awaited_once()


@pytest.mark.asyncio
async def test_stats_embed_handles_zero_attempts(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    monkeypatch.setattr(cog, "_statistics", AsyncMock(return_value=[statistics(42, protected=1)]))

    await invoke(Count.count_stats, cog, ctx)

    assert "0.0%" in field_text(sent_embed(ctx))


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", ["count_stats", "count_leaderboard"])
async def test_empty_statistics_are_still_embeds(command_context, monkeypatch, command_name) -> None:
    cog, ctx, _ = command_context
    monkeypatch.setattr(cog, "_statistics", AsyncMock(return_value=[]))

    await invoke(getattr(Count, command_name), cog, ctx)

    embed = sent_embed(ctx)
    assert "紀錄" in (embed.description or "")
    cog.bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_leaderboard_uses_usernames_and_requested_line_format(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    names = {1: "baihu._.0321", 2: "littleorangemeow", 3: "s0323010"}
    ctx.guild.get_member.side_effect = lambda user_id: SimpleNamespace(
        name=names[user_id], display_name="這個暱稱不應取代帳號名稱"
    )
    monkeypatch.setattr(
        cog,
        "_statistics",
        AsyncMock(return_value=[statistics(3, 3048), statistics(1, 6132), statistics(2, 3473)]),
    )

    await invoke(Count.count_leaderboard, cog, ctx)

    embed = sent_embed(ctx)
    description = embed.description or ""
    assert "#1 baihu._.0321, 6,132\n#2 littleorangemeow, 3,473\n#3 s0323010, 3,048" in description
    assert description.count("```") == 2
    assert "這個暱稱" not in description
    assert "<@" not in description
    assert "成功次數" in (embed.title or "")
    cog.bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_leaderboard_pagination_preserves_global_rank(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    source = AsyncMock(return_value=[statistics(user_id, 10000 - user_id) for user_id in range(1, 16)])
    monkeypatch.setattr(cog, "_statistics", source)

    await invoke(Count.count_leaderboard, cog, ctx, scope="global", period="30d", page=2)

    embed = sent_embed(ctx)
    description = embed.description or ""
    assert "#11 user_11, 9,989" in description
    assert "#15 user_15, 9,985" in description
    assert "#10 " not in description
    assert len([line for line in description.splitlines() if line.startswith("#")]) == 5
    assert "全域" in description
    assert "近 30 天" in description
    assert "15" in (embed.footer.text or "")
    assert source.await_args is not None
    assert source.await_args.args[1].page == 2
    assert ctx.guild.get_member.call_count == 5
    assert {call.args[0] for call in ctx.guild.get_member.call_args_list} == set(range(11, 16))


@pytest.mark.asyncio
async def test_error_leaderboard_displays_selected_metric(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    monkeypatch.setattr(
        cog,
        "_statistics",
        AsyncMock(return_value=[statistics(1, 6132, 100), statistics(2, 1, 3473)]),
    )

    await invoke(Count.count_leaderboard, cog, ctx, sort="errors")

    embed = sent_embed(ctx)
    assert "#1 user_2, 3,473\n#2 user_1, 100" in (embed.description or "")
    assert "錯誤次數" in (embed.title or "")


@pytest.mark.asyncio
async def test_error_rate_leaderboard_sorts_lowest_first_and_zero_attempts_last(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    monkeypatch.setattr(
        cog,
        "_statistics",
        AsyncMock(return_value=[statistics(1, 9, 1), statistics(3), statistics(2, 98, 2)]),
    )

    await invoke(Count.count_leaderboard, cog, ctx, sort="error_rate")

    embed = sent_embed(ctx)
    assert "#1 user_2, 2.0%\n#2 user_1, 10.0%\n#3 user_3, 0.0%" in (embed.description or "")
    assert "錯誤率" in (embed.title or "")


@pytest.mark.asyncio
async def test_leaderboard_resolves_cached_users_without_http(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    ctx.guild.get_member.side_effect = None
    ctx.guild.get_member.return_value = None
    cog.bot.get_user.return_value = SimpleNamespace(name="cached.user")
    monkeypatch.setattr(cog, "_statistics", AsyncMock(return_value=[statistics(1, 6132)]))

    await invoke(Count.count_leaderboard, cog, ctx)

    assert "#1 cached.user, 6,132" in (sent_embed(ctx).description or "")
    cog.bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_leaderboard_fetches_missing_users(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    ctx.guild.get_member.side_effect = None
    ctx.guild.get_member.return_value = None
    cog.bot.fetch_user.return_value = SimpleNamespace(name="remote.user")
    monkeypatch.setattr(cog, "_statistics", AsyncMock(return_value=[statistics(1, 6132)]))

    await invoke(Count.count_leaderboard, cog, ctx)

    assert "#1 remote.user, 6,132" in (sent_embed(ctx).description or "")
    cog.bot.fetch_user.assert_awaited_once_with(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("timed_out", [False, True])
async def test_leaderboard_missing_user_failure_falls_back_to_id(command_context, monkeypatch, timed_out) -> None:
    cog, ctx, _ = command_context
    ctx.guild.get_member.side_effect = None
    ctx.guild.get_member.return_value = None
    if timed_out:
        cog.bot.fetch_user.side_effect = TimeoutError
    else:
        response = cast(ClientResponse, SimpleNamespace(status=404, reason="Not Found"))
        cog.bot.fetch_user.side_effect = discord.NotFound(response, "Unknown User")
    monkeypatch.setattr(cog, "_statistics", AsyncMock(return_value=[statistics(1, 6132)]))

    await invoke(Count.count_leaderboard, cog, ctx)

    assert "#1 未知使用者 (1), 6,132" in (sent_embed(ctx).description or "")


@pytest.mark.asyncio
async def test_leaderboard_username_cannot_break_code_block(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    ctx.guild.get_member.side_effect = lambda user_id: SimpleNamespace(name="eve\n```@everyone")
    monkeypatch.setattr(cog, "_statistics", AsyncMock(return_value=[statistics(1, 6132)]))

    await invoke(Count.count_leaderboard, cog, ctx)

    embed = sent_embed(ctx)
    description = embed.description or ""
    assert description.count("```") == 2
    assert "\n```@everyone" not in description
    assert len([line for line in description.splitlines() if line.startswith("#")]) == 1


@pytest.mark.asyncio
async def test_out_of_range_leaderboard_page_remains_ephemeral(command_context, monkeypatch) -> None:
    cog, ctx, _ = command_context
    monkeypatch.setattr(cog, "_statistics", AsyncMock(return_value=[statistics(1, 6132)]))

    await invoke(Count.count_leaderboard, cog, ctx, page=2)

    sent = ctx.send.await_args
    assert sent is not None
    assert "1" in sent.args[0]
    assert "頁" in sent.args[0]
    assert sent.kwargs["ephemeral"] is True
    cog.bot.fetch_user.assert_not_awaited()
