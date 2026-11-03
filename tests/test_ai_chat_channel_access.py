import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.cogs.ai_chat import AiChat
from bot.utils.config import Settings, save_llm_disabled_channel_ids
from bot.utils.ai_conversation import ConversationManager, ConversationScope
from bot.utils.ai_pipeline import NoticeKind, PipelineResult, ServiceNotice


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


def test_save_llm_disabled_channels_persists_while_editor_holds_file(tmp_path):
    config_path = tmp_path / "bot.json"
    _write_bot_config(config_path, {"model": "existing-model"})

    with open(config_path, encoding="utf-8"):
        saved_channel_ids = save_llm_disabled_channel_ids(config_path, [789])

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved_channel_ids == [789]
    assert data["llm"] == {"model": "existing-model", "disabled_channel_ids": [789]}
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disabled_channel_ids", "channel_id", "parent_channel_id", "mentioned", "expected_turns"),
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
    expected_turns,
):
    chat = AiChat.__new__(AiChat)
    bot_user = SimpleNamespace(id=999)
    chat.bot = SimpleNamespace(
        user=bot_user,
        settings=SimpleNamespace(llm_disabled_channel_ids=disabled_channel_ids),
        logger=SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock()),
    )
    chat.client = object()
    chat._save_memory = Mock()
    chat.mention_only = False
    chat.empty_user_text = "請補充問題"
    chat.conversations = ConversationManager(max_sessions=100)
    chat.scheduler = SimpleNamespace(submit=AsyncMock(return_value=PipelineResult("completed")))
    chat._known_scopes = {}
    guild = SimpleNamespace(id=1, me=bot_user)
    message = SimpleNamespace(
        id=100,
        attachments=[],
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

    chat._save_memory.assert_not_called()
    assert chat.scheduler.submit.await_count == expected_turns


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


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [
    "on_raw_thread_update", "on_raw_thread_delete", "on_raw_thread_member_remove",
])
async def test_uncached_thread_events_invalidate_only_the_affected_scope(event):
    chat = AiChat.__new__(AiChat)
    chat.conversations = SimpleNamespace(invalidate_channel=Mock())
    chat.scheduler = SimpleNamespace(invalidate=Mock())
    chat._known_scopes = {(1, 7): None, (1, 8): 7, (1, 9): None, (2, 7): None}
    payload = SimpleNamespace(guild_id=1, thread_id=7)

    await getattr(chat, event)(payload)

    chat.conversations.invalidate_channel.assert_called_once_with(1, 7, include_children=True)
    assert [call.args[0] for call in chat.scheduler.invalidate.call_args_list] == [(1, 7), (1, 8)]
    assert chat._known_scopes == {(1, 9): None, (2, 7): None}


def test_scope_invalidation_cancels_jobs_even_without_scope_metadata():
    chat = AiChat.__new__(AiChat)
    chat.conversations = SimpleNamespace(invalidate_channel=Mock())
    chat.scheduler = SimpleNamespace(invalidate=Mock())
    chat._known_scopes = {}

    chat._invalidate_channel(1, 7)

    chat.conversations.invalidate_channel.assert_called_once_with(1, 7, include_children=True)
    chat.scheduler.invalidate.assert_called_once_with((1, 7))


def _scheduler_notice_chat():
    chat = AiChat.__new__(AiChat)
    bot_user = SimpleNamespace(id=999)
    guild = SimpleNamespace(id=1, me=bot_user)
    channel = SimpleNamespace(id=10, parent_id=None, guild=guild, view=True,
                              history=AsyncMock(), fetch_message=AsyncMock())
    channel.permissions_for = lambda member: SimpleNamespace(
        view_channel=channel.view, read_message_history=True,
        send_messages=True, send_messages_in_threads=True)
    message = SimpleNamespace(id=100, guild=guild, channel=channel, author=SimpleNamespace(id=2, bot=False),
                              content="<@999> QUESTION_GUARD_CANARY", attachments=[], mentions=[bot_user],
                              role_mentions=[], mention_everyone=False, webhook_id=None, reply=AsyncMock())
    chat.bot = SimpleNamespace(user=bot_user, settings=SimpleNamespace(llm_disabled_channel_ids=[]),
                               logger=SimpleNamespace(info=Mock(), warning=Mock(), exception=Mock()))
    chat.client = object()
    chat.mention_only = False
    chat.empty_user_text = "請補充問題"
    chat._known_scopes = {}
    chat.conversations = ConversationManager(max_sessions=2)
    chat._save_memory = Mock()
    return chat, message


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["busy", "scheduler_timeout"])
@pytest.mark.parametrize("mentioned", [False, True])
@pytest.mark.parametrize("change", ["none", "edit", "delete", "disabled", "permissions", "ttl", "eviction"])
async def test_scheduler_notices_recheck_source_guard_and_permissions_without_fetch(outcome, mentioned, change):
    chat, message = _scheduler_notice_chat()
    if not mentioned:
        message.mentions = []
    captured = []
    original_capture = chat.conversations.capture_guard

    def capture(scope):
        snapshot = original_capture(scope)
        captured.append(snapshot)
        return snapshot

    chat.conversations.capture_guard = Mock(side_effect=capture)
    kind = NoticeKind.BUSY if outcome == "busy" else NoticeKind.UNAVAILABLE

    async def submit(key, message_id, handler):
        assert len(captured) == int(mentioned)
        if captured:
            assert captured[0].records == () and captured[0].missing_ids == ()
            assert captured[0].latest_id is None
        if change in {"edit", "delete"}:
            chat.conversations.invalidate_message(1, 10, message_id, deleted=change == "delete")
        elif change == "disabled":
            chat.bot.settings.llm_disabled_channel_ids.append(10)
            chat.conversations.invalidate_channel(1, 10)
        elif change == "permissions":
            message.channel.view = False
        elif change == "ttl":
            for session in chat.conversations._sessions.values():
                session.last_access -= chat.conversations.ttl_seconds + 1
        elif change == "eviction":
            chat.conversations.max_sessions = 1
            original_capture(ConversationScope(1, 20))
            original_capture(ConversationScope(1, 10))
        return PipelineResult(outcome, notice=ServiceNotice(kind))

    chat.scheduler = SimpleNamespace(submit=AsyncMock(side_effect=submit))
    await chat.on_message(message)
    assert message.reply.await_count == int(mentioned and change == "none")
    if message.reply.await_count:
        assert message.reply.await_args.args[0] == ServiceNotice(kind).render()
        assert message.reply.await_args.kwargs["mention_author"] is False
        mentions = message.reply.await_args.kwargs["allowed_mentions"]
        assert not mentions.everyone and not mentions.roles and not mentions.users and not mentions.replied_user
    message.channel.history.assert_not_called()
    message.channel.fetch_message.assert_not_called()
    chat._save_memory.assert_not_called()
    assert all(not session.records for session in chat.conversations._sessions.values())
    chat.bot.logger.exception.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unavailable", "rate_limited", "too_long", "completed", "rejected", "duplicate", "stale", "cancelled"])
async def test_listener_does_not_forward_pipeline_or_nonpublication_notices(outcome):
    chat, message = _scheduler_notice_chat()
    chat.scheduler = SimpleNamespace(submit=AsyncMock(return_value=PipelineResult(
        outcome, notice=ServiceNotice(NoticeKind.UNAVAILABLE))))
    await chat.on_message(message)
    message.reply.assert_not_awaited()
    chat._save_memory.assert_not_called()
