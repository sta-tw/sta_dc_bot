from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import discord
import pytest
import pytest_asyncio
from discord import app_commands
from discord.ext import commands

from bot.cogs.history_today import HistoryToday
from bot.utils.config_paths import ConfigPaths
from bot.utils.history_today import Category, HistoryError, HistoryItem, HistoryResult, source_url
from database.history import Subscription


HISTORY_COMMANDS = {"today", "daily", "daily-status", "daily-off", "history-help"}


def make_bot(*, enabled=True, guild_id=0, timezone_name="Asia/Taipei"):
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.settings = SimpleNamespace(
        guild_id=guild_id,
        history_today=SimpleNamespace(enabled=enabled, timezone=timezone_name),
    )
    return bot


def interaction(*, guild=None, manage_guild=False):
    permissions = discord.Permissions.none()
    permissions.manage_guild = manage_guild
    return SimpleNamespace(
        guild=guild,
        guild_id=guild.id if guild is not None else 123,
        user=Mock(spec=discord.Member),
        permissions=permissions,
        response=SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), is_done=Mock(return_value=False)
        ),
        edit_original_response=AsyncMock(),
    )


def posting_channel(*, allowed=True, guild_id=123, channel_id=456):
    guild = SimpleNamespace(id=guild_id, me=object(), get_channel=Mock())
    permissions = discord.Permissions.none()
    permissions.view_channel = allowed
    permissions.send_messages = allowed
    permissions.embed_links = allowed
    channel = Mock(spec=discord.TextChannel)
    channel.guild = guild
    channel.id = channel_id
    channel.mention = f"<#{channel_id}>"
    channel.send = AsyncMock()
    channel.permissions_for.return_value = permissions
    guild.get_channel.return_value = channel
    return guild, channel


async def invoke(bot, cog, name, request, **kwargs):
    command = bot.tree.get_command(name)
    assert command is not None
    await command.callback(cog, request, **kwargs)


@pytest_asyncio.fixture
async def loaded():
    bot = make_bot(timezone_name="Pacific/Kiritimati")
    # Extension reloads create a new class object; use the active module.
    cog_type = import_module("bot.cogs.history_today").HistoryToday
    cog = cog_type(bot, database_path=":memory:", start_task=False)
    await bot.add_cog(cog)
    try:
        yield bot, cog
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_real_cog_registration_keeps_existing_commands_and_tree_handler():
    bot = make_bot()
    async def existing_help(request: discord.Interaction):
        pass

    async def global_error(request, error):
        pass

    bot.tree.add_command(app_commands.Command(name="help", description="Existing help", callback=existing_help))
    bot.tree.on_error = global_error
    cog = HistoryToday(bot, database_path=":memory:", start_task=False)
    try:
        await bot.add_cog(cog)
        assert {command.name for command in bot.tree.get_commands()} == HISTORY_COMMANDS | {"help"}
        assert bot.get_cog("HistoryToday") is cog
        assert bot.tree.on_error is global_error
        params = {param.name: param for param in bot.tree.get_command("today").parameters}
        assert params["count"].min_value is None
        assert params["count"].max_value is None
        assert {choice.value for choice in params["count"].choices} == {
            *(str(count) for count in range(1, 11)), "random5"
        }
        assert (params["month"].min_value, params["month"].max_value) == (1, 12)
        assert {choice.value for choice in params["category"].choices} == {category.value for category in Category}
    finally:
        await bot.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("guild_id", [0, 987])
async def test_actual_extension_load_and_unload_follow_existing_guild_scope(tmp_path, monkeypatch, guild_id):
    monkeypatch.setattr(ConfigPaths, "HISTORY_DATABASE", tmp_path / "history.db")
    bot = make_bot(guild_id=guild_id)
    await bot._async_setup_hook()
    try:
        await bot.load_extension("bot.cogs.history_today")
        cog = bot.get_cog("HistoryToday")
        assert isinstance(cog, commands.Cog)
        assert cog.qualified_name == "HistoryToday"
        guild = discord.Object(id=guild_id) if guild_id else None
        assert {command.name for command in bot.tree.get_commands(guild=guild)} == HISTORY_COMMANDS
        if guild_id:
            assert bot.tree.get_commands() == []
        session = cog.session
        task = cog.daily_loop.get_task()
        assert session is not None and not session.closed
        assert session.trust_env
        assert task is not None and not task.done()
        await bot.unload_extension("bot.cogs.history_today")
        assert bot.get_cog("HistoryToday") is None
        assert bot.tree.get_commands(guild=guild) == []
        assert session.closed
        assert task.done()
        await cog.cog_unload()
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_disabled_extension_has_no_commands_or_open_database(tmp_path, monkeypatch):
    path = tmp_path / "history.db"
    monkeypatch.setattr(ConfigPaths, "HISTORY_DATABASE", path)
    bot = make_bot(enabled=False)
    try:
        await bot.load_extension("bot.cogs.history_today")
        assert bot.get_cog("HistoryToday") is None
        assert bot.tree.get_commands() == []
        assert not path.exists()
    finally:
        await bot.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["daily", "daily-status", "daily-off"])
async def test_admin_commands_enforce_manage_guild_at_runtime(loaded, name):
    bot, _ = loaded
    command = bot.tree.get_command(name)
    assert command.default_permissions.manage_guild
    assert command.guild_only
    request = interaction()
    with pytest.raises(app_commands.MissingPermissions):
        await command._check_can_run(request)
    request.permissions.manage_guild = True
    assert await command._check_can_run(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("month, day", [(10, None), (None, 4), (4, 31), (2, 30)])
async def test_invalid_date_replies_before_lookup(loaded, month, day):
    bot, cog = loaded
    cog.history.get = AsyncMock()
    request = interaction()
    await invoke(bot, cog, "today", request, month=month, day=day)
    request.response.send_message.assert_awaited_once()
    assert request.response.send_message.await_args.kwargs["ephemeral"]
    request.response.defer.assert_not_awaited()
    cog.history.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_today_uses_configured_timezone_and_limits_embed_entries(loaded):
    bot, cog = loaded
    assert cog.timezone == bot.settings.history_today.timezone == "Pacific/Kiritimati"
    local_date = datetime(2026, 10, 4, 15, 1, tzinfo=timezone.utc).astimezone(ZoneInfo(cog.timezone))
    result = HistoryResult(
        10, 5, Category.EVENTS,
        tuple(HistoryItem("1957年", f"資料{i}") for i in range(8)), source_url(10, 5),
    )
    cog.history.get = AsyncMock(return_value=result)
    request = interaction()
    with patch("bot.cogs.history_today.datetime") as clock:
        clock.now.return_value = local_date
        await invoke(bot, cog, "today", request, count=3)
        assert str(clock.now.call_args.args[0]) == cog.timezone
    cog.history.get.assert_awaited_once_with(10, 5, Category.EVENTS)
    request.response.defer.assert_awaited_once_with(thinking=True)
    embed = request.edit_original_response.await_args.kwargs["embed"]
    assert embed.url == result.source_url
    assert embed.description.count("•") == 3
    assert "CC BY-SA" in embed.footer.text


@pytest.mark.asyncio
async def test_lookup_uses_actual_history_client_with_mocked_http(loaded):
    bot, cog = loaded
    response = SimpleNamespace(
        status=200,
        json=AsyncMock(return_value={"parse": {"text": "<h2>出生</h2><ul><li>2000年：人物</li></ul>"}}),
    )
    context = AsyncMock()
    context.__aenter__.return_value = response
    request = interaction()
    with patch.object(cog.session, "get", return_value=context) as get:
        await invoke(bot, cog, "today", request, month=2, day=29, category="births")
    assert get.call_args.kwargs["params"]["page"] == "2月29日"
    embed = request.edit_original_response.await_args.kwargs["embed"]
    assert embed.author.name == "出生"
    assert "人物" in embed.description
    assert embed.url == source_url(2, 29)


@pytest.mark.asyncio
async def test_today_random_category_and_random_five(loaded):
    bot, cog = loaded
    items = tuple(HistoryItem(str(index), f"item {index}") for index in range(9))
    result = HistoryResult(10, 4, Category.DEATHS, items, source_url(10, 4))
    cog.history.get = AsyncMock(return_value=result)
    request = interaction()
    with patch("bot.utils.history_formatting.random.sample", return_value=list(items[1:6])) as sample:
        await invoke(bot, cog, "today", request, month=10, day=4, category="random", count="random5")
    cog.history.get.assert_awaited_once_with(10, 4, Category.RANDOM)
    sample.assert_called_once_with(items, 5)
    embed = request.edit_original_response.await_args.kwargs["embed"]
    assert embed.author.name == "逝世"
    assert embed.description.count("•") == 5


@pytest.mark.asyncio
async def test_invalid_timezone_is_rejected_before_persistence(loaded):
    bot, cog = loaded
    guild, channel = posting_channel()
    request = interaction(guild=guild)
    await invoke(bot, cog, "daily", request, channel=channel, timezone="Invalid/Timezone")
    request.response.send_message.assert_awaited_once()
    assert "IANA" in request.response.send_message.await_args.args[0]
    request.response.defer.assert_not_awaited()
    assert cog.store.get(guild.id) is None


@pytest.mark.asyncio
async def test_configure_status_and_disable_daily_subscription(loaded):
    bot, cog = loaded
    guild, channel = posting_channel()
    cog.timezone = "Europe/Paris"
    request = interaction(guild=guild)
    await invoke(bot, cog, "daily", request, channel=channel, hour=8, minute=30, category="births", count=3)
    request.response.defer.assert_awaited_once_with(ephemeral=True)
    sub = cog.store.get(guild.id)
    assert (sub.timezone, sub.hour, sub.minute, sub.category, sub.count) == (
        "Europe/Paris", 8, 30, Category.BIRTHS, 3,
    )
    status = interaction(guild=guild)
    await invoke(bot, cog, "daily-status", status)
    assert "08:30（Europe/Paris）" in status.response.send_message.await_args.args[0]
    off = interaction(guild=guild)
    await invoke(bot, cog, "daily-off", off)
    assert cog.store.get(guild.id) is None
    assert off.edit_original_response.await_args.kwargs["content"] == "已停用每日推送。"


@pytest.mark.asyncio
async def test_daily_persists_random_category_and_five_mode(loaded):
    bot, cog = loaded
    guild, channel = posting_channel()
    request = interaction(guild=guild)
    await invoke(
        bot, cog, "daily", request, channel=channel,
        category="random", count="random5",
    )
    saved = cog.store.get(guild.id)
    assert saved.category is Category.RANDOM
    assert saved.count == 5
    assert saved.random_count is True
    assert "隨機分類" in request.edit_original_response.await_args.kwargs["content"]
    assert "隨機抽 5 筆" in request.edit_original_response.await_args.kwargs["content"]

    status = interaction(guild=guild)
    await invoke(bot, cog, "daily-status", status)
    assert "隨機分類" in status.response.send_message.await_args.args[0]
    assert "隨機抽 5 筆" in status.response.send_message.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["view_channel", "send_messages", "embed_links"])
async def test_daily_configuration_rejects_missing_bot_permission(loaded, missing):
    bot, cog = loaded
    guild, channel = posting_channel()
    setattr(channel.permissions_for.return_value, missing, False)
    request = interaction(guild=guild)
    await invoke(bot, cog, "daily", request, channel=channel)
    assert "權限" in request.response.send_message.await_args.args[0]
    request.response.defer.assert_not_awaited()
    assert cog.store.get(guild.id) is None


@pytest.mark.asyncio
async def test_daily_configuration_rejects_channel_in_other_guild(loaded):
    bot, cog = loaded
    _, channel = posting_channel(guild_id=999)
    request = interaction(guild=SimpleNamespace(id=123, me=object()))
    await invoke(bot, cog, "daily", request, channel=channel)
    request.response.send_message.assert_awaited_once()
    assert cog.store.get(123) is None


@pytest.mark.asyncio
async def test_daily_delivery_uses_scheduler_date_and_disables_mentions(loaded):
    bot, cog = loaded
    guild, channel = posting_channel()
    sub = Subscription(guild.id, channel.id, 9, 0, "Asia/Taipei", Category.HOLIDAYS, 2)
    result = HistoryResult(2, 29, Category.HOLIDAYS, (HistoryItem(None, "節日"),), source_url(2, 29))
    cog.history.get = AsyncMock(return_value=result)
    with patch.object(bot, "get_guild", return_value=guild):
        await cog.send_daily(sub, date(2028, 2, 29))
    cog.history.get.assert_awaited_once_with(2, 29, Category.HOLIDAYS)
    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    assert kwargs["embed"].url == result.source_url
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["permission", "guild", "type"])
async def test_scheduled_delivery_rejects_inaccessible_or_invalid_channel(loaded, failure):
    bot, cog = loaded
    guild, channel = posting_channel(allowed=failure != "permission")
    sub = Subscription(guild.id, channel.id, 9, 0, "Asia/Taipei")
    cog.history.get = AsyncMock()
    if failure == "guild":
        channel.guild = SimpleNamespace(id=999)
    if failure == "type":
        guild.get_channel.return_value = SimpleNamespace(guild=guild)
    error_type = PermissionError if failure == "permission" else ValueError
    with patch.object(bot, "get_guild", return_value=guild):
        with pytest.raises(error_type):
            await cog.send_daily(sub, date(2026, 10, 4))
    cog.history.get.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_error_finishes_deferred_response_locally(loaded):
    bot, cog = loaded
    request = interaction()
    request.response.is_done.return_value = True
    error = app_commands.CommandInvokeError(
        bot.tree.get_command("today"), HistoryError("查詢維基百科逾時，請稍後再試。")
    )
    await cog.cog_app_command_error(request, error)
    request.edit_original_response.assert_awaited_once_with(
        content="查詢維基百科逾時，請稍後再試。", embed=None,
    )


@pytest.mark.asyncio
async def test_unload_cancels_running_scheduler_before_closing_session_and_store():
    bot = make_bot()
    await bot._async_setup_hook()
    bot._ready.set()
    cog = HistoryToday(bot, database_path=":memory:")
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_tick():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not cog.session.closed
            assert cog.store.list_all() == []
            cancelled.set()

    cog.scheduler.tick = blocked_tick
    await bot.add_cog(cog)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        session = cog.session
        task = cog.daily_loop.get_task()
        await bot.remove_cog("HistoryToday")
        assert cancelled.is_set()
        assert task.done()
        assert session.closed
        assert cog.history is None
        await cog.cog_unload()
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_unload_cancels_shared_upstream_fetch_before_closing_session(loaded):
    bot, cog = loaded
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    session = cog.session

    async def blocked_request(month, day):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not session.closed
            cancelled.set()

    cog.history._request = blocked_request
    lookup = asyncio.create_task(cog.history.get(10, 4))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await bot.remove_cog("HistoryToday")
        with pytest.raises(asyncio.CancelledError):
            await lookup
        assert cancelled.is_set()
        assert session.closed
    finally:
        if not lookup.done():
            lookup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await lookup
