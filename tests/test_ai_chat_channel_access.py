import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.cogs.ai_chat import AiChat
from bot.utils.config import Settings, save_llm_disabled_channel_ids


def _write_bot_config(path, llm_settings):
    path.write_text(
        json.dumps(
            {
                "guild_id": 0,
                "welcome_channel_id": 1,
                "ticket_category_id": 2,
                "ticket_panel_channel_id": 3,
                "transcript_dir": str(path.parent / "transcripts"),
                "llm": llm_settings,
                "unrelated": {"keep": True},
            }
        ),
        encoding="utf-8",
    )


def test_settings_load_disabled_llm_channels(tmp_path):
    config_path = tmp_path / "bot.json"
    _write_bot_config(
        config_path,
        {"disabled_channel_ids": ["123", 123, 0, "invalid", -1]},
    )

    settings = Settings.from_file(config_path)

    assert settings.llm_disabled_channel_ids == [123]


def test_save_llm_disabled_channels_preserves_other_settings(tmp_path):
    config_path = tmp_path / "bot.json"
    _write_bot_config(config_path, {"model": "existing-model", "api_keys": ["keep"]})

    saved_channel_ids = save_llm_disabled_channel_ids(config_path, [456, 123, 456, 0, -1])

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved_channel_ids == [123, 456]
    assert data["llm"] == {
        "model": "existing-model",
        "api_keys": ["keep"],
        "disabled_channel_ids": [123, 456],
    }
    assert data["unrelated"] == {"keep": True}
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "disabled_channel_ids",
        "channel_id",
        "parent_channel_id",
        "mentioned",
        "expected_memory_calls",
    ),
    [
        ([123], 123, None, False, 0),
        ([123], 456, 123, False, 0),
        ([123], 123, None, True, 0),
        ([], 123, None, False, 1),
    ],
)
async def test_on_message_skips_disabled_channels_before_llm_and_memory(
    disabled_channel_ids,
    channel_id,
    parent_channel_id,
    mentioned,
    expected_memory_calls,
):
    chat = AiChat.__new__(AiChat)
    bot_user = SimpleNamespace(id=999)
    chat.bot = SimpleNamespace(
        user=bot_user,
        settings=SimpleNamespace(llm_disabled_channel_ids=disabled_channel_ids),
        logger=SimpleNamespace(exception=Mock()),
    )
    chat.client = object()
    chat._save_message_memory = Mock()
    chat._collect_context_records = AsyncMock()
    chat._is_identity_question = Mock(return_value=False)
    chat._rate_limited_until = 0
    guild = SimpleNamespace(id=1, me=bot_user)
    message = SimpleNamespace(
        guild=guild,
        author=SimpleNamespace(id=2, name="member", bot=False),
        channel=SimpleNamespace(
            id=channel_id,
            parent_id=parent_channel_id,
            name="restricted",
        ),
        content="hello",
        mentions=[bot_user] if mentioned else [],
        role_mentions=[],
        mention_everyone=False,
        reply=AsyncMock(),
    )

    await chat.on_message(message)

    assert chat._save_message_memory.call_count == expected_memory_calls
    assert chat._collect_context_records.await_count == 0


def _make_channel_interaction(*, manage_guild=True, roles=(), channel_guild_id=1):
    guild = SimpleNamespace(id=1)
    channel_guild = SimpleNamespace(id=channel_guild_id)
    channel = SimpleNamespace(id=456, guild=channel_guild, mention="<#456>")
    member = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_guild=manage_guild),
        roles=list(roles),
    )
    interaction = SimpleNamespace(
        guild=guild,
        user=member,
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    return interaction, channel


@pytest.mark.asyncio
async def test_llm_channel_command_persists_and_updates_runtime_settings(tmp_path):
    config_path = tmp_path / "bot.json"
    _write_bot_config(config_path, {"disabled_channel_ids": []})
    chat = AiChat.__new__(AiChat)
    chat.bot = SimpleNamespace(
        settings=SimpleNamespace(support_role_ids=[], llm_disabled_channel_ids=[]),
        settings_path=config_path,
        logger=SimpleNamespace(warning=Mock()),
    )
    interaction, channel = _make_channel_interaction()

    await AiChat.llm_channel.callback(chat, interaction, channel, False)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert chat.bot.settings.llm_disabled_channel_ids == [456]
    assert data["llm"]["disabled_channel_ids"] == [456]
    assert "已停用" in interaction.response.send_message.await_args.args[0]

    enable_interaction, enable_channel = _make_channel_interaction()
    await AiChat.llm_channel.callback(chat, enable_interaction, enable_channel, True)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert chat.bot.settings.llm_disabled_channel_ids == []
    assert data["llm"]["disabled_channel_ids"] == []
    assert "已啟用" in enable_interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_llm_channel_command_requires_manager_or_support_role(tmp_path):
    config_path = tmp_path / "bot.json"
    _write_bot_config(config_path, {"disabled_channel_ids": []})
    chat = AiChat.__new__(AiChat)
    chat.bot = SimpleNamespace(
        settings=SimpleNamespace(support_role_ids=[789], llm_disabled_channel_ids=[]),
        settings_path=config_path,
        logger=SimpleNamespace(warning=Mock()),
    )
    interaction, channel = _make_channel_interaction(manage_guild=False)

    await AiChat.llm_channel.callback(chat, interaction, channel, False)

    assert chat.bot.settings.llm_disabled_channel_ids == []
    assert json.loads(config_path.read_text(encoding="utf-8"))["llm"]["disabled_channel_ids"] == []
    assert "需要伺服器管理權限" in interaction.response.send_message.await_args.args[0]

    support_member = SimpleNamespace(id=789)
    support_interaction, support_channel = _make_channel_interaction(
        manage_guild=False,
        roles=[support_member],
    )
    await AiChat.llm_channel.callback(chat, support_interaction, support_channel, False)

    assert chat.bot.settings.llm_disabled_channel_ids == [456]


@pytest.mark.asyncio
async def test_llm_channel_command_rejects_channels_from_other_guilds(tmp_path):
    config_path = tmp_path / "bot.json"
    _write_bot_config(config_path, {"disabled_channel_ids": []})
    chat = AiChat.__new__(AiChat)
    chat.bot = SimpleNamespace(
        settings=SimpleNamespace(support_role_ids=[], llm_disabled_channel_ids=[]),
        settings_path=config_path,
        logger=SimpleNamespace(warning=Mock()),
    )
    interaction, channel = _make_channel_interaction(channel_guild_id=2)

    await AiChat.llm_channel.callback(chat, interaction, channel, False)

    assert chat.bot.settings.llm_disabled_channel_ids == []
    assert "必須屬於目前的伺服器" in interaction.response.send_message.await_args.args[0]
