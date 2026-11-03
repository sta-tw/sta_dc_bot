from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from bot import (
    _GLOBAL_FALLBACK_COMMANDS,
    _HISTORY_COMMANDS,
    _sync_global_commands,
    _sync_global_resource_setup_command,
    _sync_missing_global_commands,
)
from bot.cogs.ai_chat import AiChat
from bot.cogs.king_of_nonsense import KingOfNonsense
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
    bot.tree.add_command(KingOfNonsense.leaderboard)
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
@pytest.mark.parametrize("leaderboard_present", [False, True])
async def test_global_50240_fallback_syncs_leaderboard_without_bulk_replacement(leaderboard_present):
    bot = _make_bot()
    llm_channel = bot.tree.get_command("llm_channel")
    leaderboard = bot.tree.get_command("最佳幹話王")
    assert llm_channel is not None
    assert leaderboard is not None
    remote_llm_channel = llm_channel.to_dict(bot.tree)
    remote_llm_channel.update({"id": 3003, "application_id": APPLICATION_ID})
    bot.http.get_global_commands.return_value = [
        _remote_command(bot, 3001),
        _remote_command(bot, 3002, name="another_command"),
        remote_llm_channel,
        _entry_point_command(),
    ]
    leaderboard_payload = leaderboard.to_dict(bot.tree)
    if leaderboard_present:
        remote_leaderboard = deepcopy(leaderboard_payload)
        remote_leaderboard.update({
            "id": 3004,
            "application_id": APPLICATION_ID,
            "description": "Old leaderboard description",
        })
        bot.http.get_global_commands.return_value.append(remote_leaderboard)

    statuses = await _sync_global_commands(bot, _GLOBAL_FALLBACK_COMMANDS)

    assert statuses == {
        "resource_setup": "already matches",
        "llm_channel": "already matches",
        "最佳幹話王": "updated" if leaderboard_present else "created",
        **{name: "local command unavailable" for name in _HISTORY_COMMANDS},
    }
    if leaderboard_present:
        bot.http.edit_global_command.assert_awaited_once_with(
            APPLICATION_ID,
            3004,
            {
                "description": leaderboard_payload["description"],
                "options": leaderboard_payload.get("options", []),
            },
        )
        bot.http.upsert_global_command.assert_not_awaited()
    else:
        bot.http.upsert_global_command.assert_awaited_once_with(
            APPLICATION_ID,
            leaderboard_payload,
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


def _make_history_bot() -> commands.Bot:
    from bot.cogs.history_today import HistoryToday

    bot = _make_bot()
    for command in (
        HistoryToday.today,
        HistoryToday.daily,
        HistoryToday.daily_status,
        HistoryToday.daily_off,
        HistoryToday.history_help,
    ):
        bot.tree.add_command(command)
    return bot


def _remote_payload_for_name(
    bot: commands.Bot, name: str, command_id: int,
) -> dict[str, object]:
    command = bot.tree.get_command(name)
    assert command is not None
    payload = deepcopy(command.to_dict(bot.tree))
    payload.update({"id": command_id, "application_id": APPLICATION_ID})
    return payload


@pytest.mark.asyncio
async def test_global_50240_fallback_publishes_history_without_touching_activity():
    bot = _make_history_bot()
    old_names = ("resource_setup", "llm_channel", "最佳幹話王")
    remote_commands = [
        _remote_payload_for_name(bot, name, index + 5000)
        for index, name in enumerate(old_names)
    ]
    remote_commands.extend([
        _remote_command(bot, 6000, name="another_command"),
        _entry_point_command(),
    ])
    original_remote = deepcopy(remote_commands)
    bot.http.get_global_commands.return_value = remote_commands

    statuses = await _sync_global_commands(bot, _GLOBAL_FALLBACK_COMMANDS)

    assert statuses == {
        **{name: "already matches" for name in old_names},
        **{name: "created" for name in _HISTORY_COMMANDS},
    }
    assert bot.http.upsert_global_command.await_count == len(_HISTORY_COMMANDS)
    published = {
        call.args[1]["name"]: call.args[1]
        for call in bot.http.upsert_global_command.await_args_list
    }
    assert set(published) == set(_HISTORY_COMMANDS)
    for name, payload in published.items():
        assert payload == bot.tree.get_command(name).to_dict(bot.tree)
    assert remote_commands == original_remote
    bot.http.edit_global_command.assert_not_awaited()
    bot.http.bulk_upsert_global_commands.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("string_permissions", [False, True])
async def test_global_50240_fallback_leaves_matching_history_commands_unchanged(
    string_permissions,
):
    bot = _make_history_bot()
    bot.http.get_global_commands.return_value = [
        _remote_payload_for_name(bot, name, index + 7000)
        for index, name in enumerate(_HISTORY_COMMANDS)
    ] + [_entry_point_command()]
    if string_permissions:
        for payload in bot.http.get_global_commands.return_value:
            if payload.get("default_member_permissions") is not None:
                payload["default_member_permissions"] = str(payload["default_member_permissions"])
    # Discord may omit false/default option fields in its responses.
    for payload in bot.http.get_global_commands.return_value:
        for option in payload.get("options", []):
            if option.get("required") is False:
                option.pop("required")

    statuses = await _sync_global_commands(bot, _HISTORY_COMMANDS)

    assert statuses == {name: "already matches" for name in _HISTORY_COMMANDS}
    bot.http.edit_global_command.assert_not_awaited()
    bot.http.upsert_global_command.assert_not_awaited()
    bot.http.bulk_upsert_global_commands.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command_name", ["daily", "daily-status", "daily-off"])
@pytest.mark.parametrize(
    ("field", "legacy_value"),
    [
        ("default_member_permissions", None),
        ("dm_permission", True),
        ("contexts", [0, 1, 2]),
    ],
)
async def test_history_fallback_updates_admin_metadata_with_matching_options(
    command_name, field, legacy_value,
):
    bot = _make_history_bot()
    command = bot.tree.get_command(command_name)
    assert command is not None
    local_payload = command.to_dict(bot.tree)
    assert local_payload["default_member_permissions"] == discord.Permissions(manage_guild=True).value
    assert local_payload["dm_permission"] is False
    assert local_payload["contexts"] == [0]
    remote = _remote_payload_for_name(bot, command_name, 8000)
    remote[field] = legacy_value
    unrelated = _remote_command(bot, 8001, name="another_command")
    entry_point = _entry_point_command()
    bot.http.get_global_commands.return_value = [remote, unrelated, entry_point]

    statuses = await _sync_global_commands(bot, (command_name,))

    assert statuses == {command_name: "updated"}
    bot.http.edit_global_command.assert_awaited_once_with(
        APPLICATION_ID,
        8000,
        {
            "description": local_payload["description"],
            "options": local_payload.get("options", []),
            "default_member_permissions": discord.Permissions(manage_guild=True).value,
            "dm_permission": False,
            "contexts": [0],
        },
    )
    bot.http.upsert_global_command.assert_not_awaited()
    bot.http.bulk_upsert_global_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_missing_global_commands_creates_only_missing_context_menu():
    from bot.cogs.google_meme import COMMAND_NAME, GoogleMeme

    bot = _make_bot()
    bot.tree.add_command(GoogleMeme(bot).context_menu)
    bot.http.get_global_commands.return_value = [
        _remote_command(bot, 1001),
        _entry_point_command(),
    ]
    llm_channel = bot.tree.get_command("llm_channel").to_dict(bot.tree)
    llm_channel.update({"id": 1002, "application_id": APPLICATION_ID})
    leaderboard = KingOfNonsense.leaderboard.to_dict(bot.tree)
    leaderboard.update({"id": 1003, "application_id": APPLICATION_ID})
    bot.http.get_global_commands.return_value += [llm_channel, leaderboard]

    statuses = await _sync_missing_global_commands(bot)

    assert statuses == {COMMAND_NAME: "created"}
    bot.http.upsert_global_command.assert_awaited_once()
    assert bot.http.upsert_global_command.await_args.args[1]["type"] == 3
    bot.http.bulk_upsert_global_commands.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type", [discord.AppCommandType.message, discord.AppCommandType.user])
@pytest.mark.parametrize("remote_present", [False, True])
async def test_missing_context_menu_can_share_a_name_with_a_slash_command(
    command_type, remote_present,
):
    async def message_callback(interaction: discord.Interaction, message: discord.Message):
        pass

    async def user_callback(interaction: discord.Interaction, user: discord.User):
        pass

    bot = _make_history_bot()
    menu = app_commands.ContextMenu(
        name="daily",
        callback=message_callback if command_type == discord.AppCommandType.message else user_callback,
    )
    bot.tree.add_command(menu)
    remote_commands = [
        _remote_payload_for_name(bot, command.name, index + 9000)
        for index, command in enumerate(bot.tree.get_commands(type=discord.AppCommandType.chat_input))
    ] + [_entry_point_command()]
    if remote_present:
        remote_commands.append({
            **menu.to_dict(bot.tree), "id": 10000, "application_id": APPLICATION_ID,
            "description": "",
        })
    bot.http.get_global_commands.return_value = remote_commands
    original_remote = deepcopy(remote_commands)

    statuses = await _sync_missing_global_commands(bot)

    if remote_present:
        assert statuses == {}
        bot.http.upsert_global_command.assert_not_awaited()
    else:
        assert statuses == {"daily": "created"}
        bot.http.upsert_global_command.assert_awaited_once_with(
            APPLICATION_ID, menu.to_dict(bot.tree),
        )
    assert remote_commands == original_remote
    bot.http.edit_global_command.assert_not_awaited()
    bot.http.bulk_upsert_global_commands.assert_not_awaited()
