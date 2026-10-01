from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from bot import _sync_global_commands, _sync_global_resource_setup_command
from bot.cogs.ai_chat import AiChat
from bot.cogs.resource_library import ResourceLibraryCog


APPLICATION_ID = 123456789


async def _resource_setup_callback(
    interaction: discord.Interaction,
    archive: discord.TextChannel,
    updates: discord.TextChannel,
    support: discord.TextChannel,
    announcements: discord.TextChannel,
    questions: discord.TextChannel,
) -> None:
    pass


def _make_bot() -> commands.Bot:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.application_id = APPLICATION_ID

    bot.http.get_global_commands = AsyncMock(return_value=[])
    bot.http.edit_global_command = AsyncMock()
    bot.http.upsert_global_command = AsyncMock()
    bot.http.bulk_upsert_global_commands = AsyncMock()

    bot.tree.add_command(
        app_commands.Command(
            name="resource_setup",
            description="Configure resource channels",
            callback=_resource_setup_callback,
        )
    )
    bot.tree.add_command(AiChat.llm_channel)
    return bot


def _local_payload(bot: commands.Bot) -> dict[str, object]:
    command = bot.tree.get_command("resource_setup")
    assert command is not None
    return command.to_dict(bot.tree)


def _remote_command(
    bot: commands.Bot,
    command_id: int,
    *,
    name: str = "resource_setup",
    command_type: int = 1,
    description: str | None = None,
    options: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload = deepcopy(_local_payload(bot))
    payload.update(
        {
            "id": command_id,
            "application_id": APPLICATION_ID,
            "name": name,
            "type": command_type,
        }
    )
    if description is not None:
        payload["description"] = description
    if options is not None:
        payload["options"] = options
    return payload


def _entry_point_command() -> dict[str, object]:
    return {
        "id": 9001,
        "application_id": APPLICATION_ID,
        "name": "Launch Entry Point",
        "description": "Open the Activity",
        "type": 4,
        "options": [],
    }


@pytest.mark.asyncio
async def test_global_50240_fallback_does_not_edit_identical_command():
    bot = _make_bot()
    bot.http.get_global_commands.return_value = [
        _remote_command(bot, 1001),
        _remote_command(bot, 1002, name="another_command"),
        _entry_point_command(),
    ]

    status = await _sync_global_resource_setup_command(bot)

    assert status == "already matches"
    bot.http.get_global_commands.assert_awaited_once_with(APPLICATION_ID)
    bot.http.edit_global_command.assert_not_awaited()
    bot.http.upsert_global_command.assert_not_awaited()
    bot.http.bulk_upsert_global_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_50240_fallback_updates_only_changed_resource_setup():
    bot = _make_bot()
    old_options = deepcopy(_local_payload(bot)["options"])
    old_options.append(
        {
            "type": discord.AppCommandOptionType.channel.value,
            "name": "legacy_channel",
            "description": "Legacy channel",
            "required": True,
        }
    )
    bot.http.get_global_commands.return_value = [
        _remote_command(
            bot,
            2001,
            description="Configure six resource channels",
            options=old_options,
        ),
        _remote_command(bot, 2002, name="another_command"),
        _entry_point_command(),
    ]
    local_payload = _local_payload(bot)

    status = await _sync_global_resource_setup_command(bot)

    assert status == "updated"
    bot.http.edit_global_command.assert_awaited_once_with(
        APPLICATION_ID,
        2001,
        {
            "description": local_payload["description"],
            "options": local_payload["options"],
        },
    )
    bot.http.upsert_global_command.assert_not_awaited()
    bot.http.bulk_upsert_global_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_targeted_edit_publishes_the_real_five_channel_command():
    bot = _make_bot()
    bot.tree.remove_command("resource_setup")
    bot.tree.add_command(ResourceLibraryCog.resource_setup)
    old_options = deepcopy(_local_payload(bot)["options"])
    old_options.insert(0, {
        "type": discord.AppCommandOptionType.channel.value,
        "name": "activity_info",
        "description": "活動資訊分享",
        "required": True,
    })
    bot.http.get_global_commands.return_value = [
        _remote_command(bot, 2001, options=old_options),
        _entry_point_command(),
    ]

    status = await _sync_global_resource_setup_command(bot)

    assert status == "updated"
    bot.http.edit_global_command.assert_awaited_once()
    _, command_id, payload = bot.http.edit_global_command.await_args.args
    assert command_id == 2001
    assert [option["name"] for option in payload["options"]] == [
        "information_communities", "learning_competitions", "selected_experiences",
        "admission_portfolios", "admission_tools", "review_channel", "notification_role",
    ]
    bot.http.bulk_upsert_global_commands.assert_not_awaited()
    bot.http.upsert_global_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_50240_fallback_upserts_only_when_resource_setup_is_missing():
    bot = _make_bot()
    bot.http.get_global_commands.return_value = [
        _remote_command(bot, 3001, name="another_command"),
        _entry_point_command(),
    ]
    local_payload = _local_payload(bot)

    status = await _sync_global_resource_setup_command(bot)

    assert status == "created"
    bot.http.upsert_global_command.assert_awaited_once_with(
        APPLICATION_ID,
        local_payload,
    )
    bot.http.edit_global_command.assert_not_awaited()
    bot.http.bulk_upsert_global_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_50240_fallback_syncs_llm_channel_without_bulk_replacement():
    bot = _make_bot()
    bot.http.get_global_commands.return_value = [
        _remote_command(bot, 3001),
        _remote_command(bot, 3002, name="another_command"),
        _entry_point_command(),
    ]
    llm_channel = bot.tree.get_command("llm_channel")
    assert llm_channel is not None
    llm_channel_payload = llm_channel.to_dict(bot.tree)

    statuses = await _sync_global_commands(
        bot,
        ("resource_setup", "llm_channel"),
    )

    assert statuses == {
        "resource_setup": "already matches",
        "llm_channel": "created",
    }
    bot.http.upsert_global_command.assert_awaited_once_with(
        APPLICATION_ID,
        llm_channel_payload,
    )
    bot.http.edit_global_command.assert_not_awaited()
    bot.http.bulk_upsert_global_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_50240_fallback_propagates_targeted_http_errors():
    bot = _make_bot()
    bot.http.get_global_commands.return_value = [
        _remote_command(bot, 4001, description="Old description")
    ]
    bot.http.edit_global_command.side_effect = RuntimeError("targeted edit failed")

    with pytest.raises(RuntimeError, match="targeted edit failed"):
        await _sync_global_resource_setup_command(bot)

    bot.http.edit_global_command.assert_awaited_once()
    bot.http.bulk_upsert_global_commands.assert_not_awaited()
