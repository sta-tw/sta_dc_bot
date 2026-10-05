import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.utils.ai_context import estimate_tokens
from bot.utils.ai_conversation import ConversationManager, ConversationScope


class FakeReference:
    def __init__(self, message_id, channel_id, guild_id=1, resolved=None):
        self.message_id = message_id
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.resolved = resolved


class FakeMessage:
    def __init__(
        self,
        message_id,
        channel,
        author,
        content,
        *,
        reply_to=None,
        reference=None,
        created_at=None,
        guild=None,
        webhook_id=None,
    ):
        self.id = message_id
        self.channel = channel
        self.channel_id = channel.id
        self.guild = guild or channel.guild
        self.guild_id = self.guild.id if self.guild else None
        self.author = author
        self.content = content
        self.created_at = created_at or datetime.fromtimestamp(message_id, timezone.utc)
        self.reference = reference
        if self.reference is None and reply_to is not None:
            self.reference = FakeReference(reply_to, channel.id, self.guild.id)
        self.webhook_id = webhook_id


class FakeChannel:
    def __init__(self, channel_id=10, guild_id=1, parent_id=None):
        self.id = channel_id
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id)
        self.parent_id = parent_id
        self.parent = SimpleNamespace(id=parent_id) if parent_id is not None else None
        self.messages = {}
        self.fetch_calls = []
        self.history_calls = []
        self.fetch_error = None
        self.fetch_gate = None

    def add(self, message):
        self.messages[message.id] = message
        return message

    def fetch_message(self, message_id):
        async def fetch():
            self.fetch_calls.append(message_id)
            if self.fetch_gate is not None:
                await self.fetch_gate.wait()
            if self.fetch_error is not None:
                raise self.fetch_error
            if message_id not in self.messages:
                raise LookupError("deleted")
            return self.messages[message_id]

        return fetch()

    def history(self, *, limit, before, oldest_first=False):
        before_id = getattr(before, "id", None)
        self.history_calls.append((limit, before_id, oldest_first))

        async def iterator():
            for message_id in sorted(self.messages, reverse=True):
                if before_id is not None and message_id >= before_id:
                    continue
                yield self.messages[message_id]

        return iterator()


class ThreadChannel(FakeChannel):
    pass


def author(author_id, name, *, bot=False):
    return SimpleNamespace(id=author_id, display_name=name, name=name, bot=bot)


def make_message(channel, message_id, name="甲", author_id=11, content=None, **kwargs):
    return channel.add(
        FakeMessage(
            message_id,
            channel,
            author(author_id, name, bot=kwargs.pop("bot", False)),
            content or f"訊息 {message_id}",
            **kwargs,
        )
    )


def run(coro):
    return asyncio.run(coro)


def collect(manager, message, *, bot_user_id=99, token_budget=10000, is_allowed=None):
    return run(
        manager.collect(
            message,
            bot_user_id=bot_user_id,
            token_budget=token_budget,
            is_allowed=is_allowed,
        )
    )


def test_reply_ancestors_are_collected_before_nearby_pagination():
    channel = FakeChannel()
    make_message(channel, 20, content="祖先根訊息")
    make_message(channel, 40, content="直接回覆", reply_to=20)
    for message_id in range(90, 100):
        make_message(channel, message_id, content=f"附近 {message_id}")
    trigger = make_message(channel, 100, content="目前問題", reply_to=40)
    manager = ConversationManager(history_page_size=2, max_api_calls=4)

    context = collect(manager, trigger)

    ids = [record.message_id for record in context.records]
    assert ids[:2] == [20, 40]
    assert "附近 99" in [record.content for record in context.records]
    assert channel.fetch_calls == [40, 20]
    assert len(channel.history_calls) <= 2
    assert context.reply_branch_ids == (40, 20)
    assert context.as_data()["reply_branch_ids"] == [40, 20]
    assert manager.is_current(context)


def test_related_reply_branches_precede_nearby_messages_in_chronological_output():
    channel = FakeChannel()
    make_message(channel, 10, content="話題根", author_id=11)
    make_message(channel, 11, content="相關分支甲", author_id=22, reply_to=10)
    make_message(channel, 12, content="分支中的追問", author_id=33, reply_to=11)
    make_message(channel, 13, content="另一個相關分支", author_id=44, reply_to=10)
    make_message(channel, 14, content="一般附近訊息", author_id=55)
    trigger = make_message(channel, 20, content="延續話題", reply_to=10)
    manager = ConversationManager(history_page_size=50, max_api_calls=2)

    context = collect(manager, trigger)

    assert [record.message_id for record in context.records] == [10, 11, 12, 13, 14]
    assert len({record.author_id for record in context.records}) == 5


def test_older_reply_ancestor_survives_history_far_beyond_nearby_window():
    channel = FakeChannel()
    root = make_message(channel, 1, content="很舊的必要根訊息")
    parent = make_message(channel, 2, content="舊的直接父訊息", reply_to=1)
    for message_id in range(3, 30):
        make_message(channel, message_id, content=f"其他內容 {message_id}")
    trigger = make_message(channel, 30, content="問題", reply_to=2)
    trigger.reference = FakeReference(2, channel.id, channel.guild.id)
    manager = ConversationManager(history_page_size=3, max_api_calls=4)

    context = collect(manager, trigger)

    assert [record.message_id for record in context.records[:2]] == [root.id, parent.id]
    assert channel.fetch_calls == [2, 1]
    assert len(channel.history_calls) <= 2


def test_edit_invalidation_refetches_fresh_content_and_invalidates_old_snapshot():
    channel = FakeChannel()
    old_message = make_message(channel, 5, content="舊內容")
    trigger = make_message(
        channel,
        6,
        content="新問題",
        reference=FakeReference(5, channel.id, channel.guild.id, resolved=old_message),
    )
    manager = ConversationManager(max_api_calls=1)
    old_context = collect(manager, make_message(channel, 7, content="先前提問"))

    manager.invalidate_message(1, channel.id, old_message.id)
    edited = FakeMessage(5, channel, author(11, "甲"), "更新後內容")
    channel.messages[edited.id] = edited
    context = collect(manager, trigger)

    assert "舊內容" not in [record.content for record in context.records]
    assert "更新後內容" in [record.content for record in context.records]
    assert not manager.is_current(old_context)
    assert channel.fetch_calls == [old_message.id]


def test_deleted_tombstone_blocks_reference_fetch_history_and_remember():
    channel = FakeChannel()
    deleted = make_message(channel, 5, content="已刪除內容")
    trigger = make_message(
        channel,
        6,
        content="新問題",
        reference=FakeReference(5, channel.id, channel.guild.id, resolved=deleted),
    )
    manager = ConversationManager(max_api_calls=2)

    manager.invalidate_message(1, channel.id, deleted.id, deleted=True)
    context = collect(manager, trigger)

    assert context.records == ()
    assert context.missing_ids == (deleted.id,)
    assert channel.fetch_calls == []
    assert manager.remember(deleted, bot_user_id=99) is None
    assert "已刪除內容" not in [record.content for record in collect(manager, trigger).records]


def test_forbidden_and_deleted_references_fail_softly():
    channel = FakeChannel()
    trigger = make_message(channel, 20, content="問題", reply_to=19)
    channel.fetch_error = PermissionError("Forbidden")
    manager = ConversationManager(max_api_calls=1)

    forbidden = collect(manager, trigger)

    assert forbidden.records == ()
    assert forbidden.missing_ids == (19,)

    channel.fetch_error = None
    channel.messages.pop(19, None)
    deleted = collect(manager, trigger)
    assert deleted.missing_ids == (19,)
    assert manager.is_current(deleted)


def test_reference_fetch_timeout_returns_a_missing_id_without_hanging():
    async def scenario():
        channel = FakeChannel()
        trigger = make_message(channel, 20, content="問題", reply_to=19)
        channel.fetch_gate = asyncio.Event()
        manager = ConversationManager(max_api_calls=1, fetch_timeout=0.01)
        return await manager.collect(
            trigger,
            bot_user_id=99,
            token_budget=1000,
            is_allowed=lambda scope: True,
        )

    context = run(scenario())

    assert context.records == ()
    assert context.missing_ids == (19,)


def test_reply_cycles_terminate_without_duplicate_records():
    channel = FakeChannel()
    make_message(channel, 3, content="回覆乙", reply_to=4)
    make_message(channel, 4, content="回覆甲", reply_to=3)
    trigger = make_message(channel, 5, content="觸發", reply_to=4)
    manager = ConversationManager(max_api_calls=4)

    context = collect(manager, trigger)

    assert [record.message_id for record in context.records] == [3, 4]
    assert channel.fetch_calls == [4, 3]
    assert len({record.message_id for record in context.records}) == len(context.records)


def test_cross_scope_references_and_private_thread_parent_references_are_rejected():
    channel = FakeChannel(channel_id=10, guild_id=1)
    other_channel = FakeChannel(channel_id=11, guild_id=1)
    foreign = make_message(other_channel, 4, content="秘密頻道內容")
    cross_scope = make_message(
        channel,
        5,
        content="問題",
        reference=FakeReference(4, other_channel.id, 1, resolved=foreign),
    )
    manager = ConversationManager(max_api_calls=3)

    result = collect(manager, cross_scope)
    assert result.records == ()
    assert result.missing_ids == (4,)
    assert channel.fetch_calls == []

    thread = ThreadChannel(channel_id=30, guild_id=1, parent_id=10)
    parent_message = make_message(channel, 8, content="不得跨入 thread")
    thread_trigger = make_message(
        thread,
        31,
        content="private thread question",
        reference=FakeReference(parent_message.id, channel.id, 1, resolved=parent_message),
    )
    thread_result = collect(manager, thread_trigger)
    assert thread_result.scope == ConversationScope(1, 30, 10)
    assert thread_result.records == ()
    assert thread_result.missing_ids == (8,)
    assert thread.fetch_calls == []


def test_reference_with_wrong_guild_is_rejected_even_if_channel_id_matches():
    channel = FakeChannel(channel_id=10, guild_id=1)
    trigger = make_message(
        channel,
        5,
        content="問題",
        reference=FakeReference(4, channel.id, 2),
    )
    manager = ConversationManager(max_api_calls=2)

    context = collect(manager, trigger)

    assert context.records == ()
    assert context.missing_ids == (4,)
    assert channel.fetch_calls == []


def test_fetched_reference_is_rejected_when_returned_message_is_cross_scope():
    channel = FakeChannel(channel_id=10, guild_id=1)
    foreign_channel = FakeChannel(channel_id=11, guild_id=2)
    foreign = make_message(foreign_channel, 4, content="跨伺服器內容")
    channel.messages[4] = foreign
    trigger = make_message(channel, 5, content="問題", reply_to=4)
    manager = ConversationManager(max_api_calls=1)

    context = collect(manager, trigger)

    assert context.records == ()
    assert context.missing_ids == (4,)
    assert channel.fetch_calls == [4]
    assert "跨伺服器內容" not in [record.content for record in context.records]


def test_pagination_collects_more_than_eight_records_and_respects_api_budget():
    channel = FakeChannel()
    for message_id in range(1, 31):
        make_message(channel, message_id, content=f"歷史 {message_id}")
    trigger = make_message(channel, 31, content="當前問題")
    manager = ConversationManager(history_page_size=3, max_api_calls=6, max_records=300)

    context = collect(manager, trigger)

    ids = [record.message_id for record in context.records]
    assert len(ids) == 18
    assert ids == list(range(13, 31))
    assert 13 in ids
    assert len(channel.history_calls) == 6


def test_selected_records_fit_token_budget_and_session_record_cap():
    channel = FakeChannel()
    for message_id in range(1, 21):
        make_message(channel, message_id, content="非常長的歷史訊息" * 20)
    trigger = make_message(channel, 21, content="當前問題")
    manager = ConversationManager(max_records=3, history_page_size=50, max_api_calls=1)

    context = collect(manager, trigger, token_budget=30)

    estimated_tokens = sum(
        estimate_tokens(f"{record.author_name} ({record.author_id}): {record.content}") + 6
        for record in context.records
    )
    assert len(context.records) <= 3
    assert estimated_tokens <= 30
    assert context.records[-1].message_id == 20
    assert len(context.records) < 20


def test_history_pagination_stops_after_a_useful_page_fills_token_budget():
    channel = FakeChannel()
    for message_id in range(1, 101):
        make_message(channel, message_id, content="短訊")
    trigger = make_message(channel, 101, content="問題")
    manager = ConversationManager(history_page_size=10, max_api_calls=6, max_records=300)

    context = collect(manager, trigger, token_budget=20)

    assert len(channel.history_calls) == 1
    assert context.records
    assert context.records[-1].message_id == 100
    assert len(context.records) < 10


def test_excerpt_preserves_head_tail_names_ids_and_marks_omission():
    channel = FakeChannel()
    long_name = "名" * 150
    make_message(
        channel,
        1,
        name=long_name,
        content="BEGIN" + "中" * 200 + "END",
    )
    trigger = make_message(channel, 2, content="問題")
    manager = ConversationManager(max_api_calls=1)

    context = collect(manager, trigger, token_budget=250)

    record = context.records[0]
    assert len(record.author_name) == 100
    assert record.author_id == 11
    assert record.message_id == 1
    assert record.content.startswith("BEGIN")
    assert record.content.endswith("END")
    assert "…" in record.content
    assert record.source == "history_excerpt"


def test_multi_speaker_records_keep_only_own_bot_and_label_bot_data():
    channel = FakeChannel()
    make_message(channel, 1, name="甲", author_id=11, content="甲的訊息")
    make_message(channel, 2, name="乙", author_id=22, content="乙的訊息")
    make_message(channel, 3, name="管理機器人", author_id=99, bot=True, content="機器人回答")
    make_message(channel, 4, name="外部機器人", author_id=77, bot=True, content="排除外部 bot")
    trigger = make_message(channel, 5, content="追問")
    manager = ConversationManager(max_api_calls=1)

    context = collect(manager, trigger, bot_user_id=99)

    assert [record.author_id for record in context.records] == [11, 22, 99]
    bot_record = context.records[-1]
    assert bot_record.is_bot is True
    assert bot_record.source == "own_bot_reply"
    assert "排除外部 bot" not in [record.content for record in context.records]
    assert json.dumps(context.as_data(), ensure_ascii=False)


def test_remember_uses_actual_sent_id_and_serializes_scope_data():
    channel = FakeChannel(channel_id=12)
    sent = make_message(channel, 456, name="管理機器人", author_id=99, bot=True, content="已送出的回答")
    next_message = make_message(channel, 457, content="後續問題")
    manager = ConversationManager(max_api_calls=0)

    remembered = manager.remember(sent, bot_user_id=99)
    context = collect(manager, next_message, bot_user_id=99)
    remembered_latest = manager.remember(next_message, bot_user_id=99)

    assert remembered.message_id == 456
    assert remembered_latest.message_id == 457
    assert manager.is_current(context)
    assert context.latest_id == 457
    assert [record.message_id for record in context.records] == [456]
    assert context.records[0].source == "own_bot_reply"
    assert context.as_data()["scope"] == {
        "guild_id": 1,
        "channel_id": 12,
        "parent_channel_id": None,
    }
    assert json.loads(json.dumps(context.as_data(), ensure_ascii=False))["records"][0]["message_id"] == 456
    assert manager.validate_source_ids(context, [456])
    assert not manager.validate_source_ids(context, [999])


def test_capture_guard_is_metadata_only_and_does_not_expose_cached_records():
    channel = FakeChannel(channel_id=12)
    sent = make_message(channel, 456, name="管理機器人", author_id=99, bot=True, content="已送出的回答")
    scope = ConversationScope(1, channel.id)
    manager = ConversationManager(max_api_calls=0)

    fresh = manager.capture_guard(scope)

    assert fresh.scope == scope
    assert fresh.records == ()
    assert fresh.missing_ids == ()
    assert fresh.latest_id is None
    assert manager.is_current(fresh)
    assert channel.fetch_calls == []
    assert channel.history_calls == []

    manager.remember(sent, bot_user_id=99)
    cached = manager.capture_guard(scope)

    assert cached.records == ()
    assert manager.is_current(cached)
    assert channel.fetch_calls == []
    assert channel.history_calls == []


def test_capture_guard_stales_after_message_edit_and_delete_invalidation():
    manager = ConversationManager(max_api_calls=0)
    edited = manager.capture_guard(ConversationScope(1, 10))
    deleted = manager.capture_guard(ConversationScope(1, 20))

    manager.invalidate_message(1, 10, 101)
    manager.invalidate_message(1, 20, 201, deleted=True)

    assert not manager.is_current(edited)
    assert not manager.is_current(deleted)


def test_capture_guard_stales_after_channel_and_parent_thread_invalidation():
    manager = ConversationManager(max_api_calls=0)
    channel_guard = manager.capture_guard(ConversationScope(1, 10))
    thread_guard = manager.capture_guard(ConversationScope(1, 20, 10))
    unrelated_thread_guard = manager.capture_guard(ConversationScope(1, 30, 11))

    manager.invalidate_channel(1, 10, include_children=True)

    assert not manager.is_current(channel_guard)
    assert not manager.is_current(thread_guard)
    assert manager.is_current(unrelated_thread_guard)


def test_capture_guard_ttl_recreation_uses_a_new_generation(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("bot.utils.ai_conversation.time.monotonic", lambda: clock["now"])
    manager = ConversationManager(ttl_seconds=5, max_sessions=2)
    scope = ConversationScope(1, 10)
    original = manager.capture_guard(scope)

    clock["now"] = 105.0
    assert not manager.is_current(original)
    assert len(manager._sessions) == 0

    recreated = manager.capture_guard(scope)

    assert recreated._generation != original._generation
    assert not manager.is_current(original)
    assert manager.is_current(recreated)


def test_capture_guard_eviction_and_recreation_use_a_new_generation_and_stay_bounded():
    manager = ConversationManager(ttl_seconds=100, max_sessions=2)
    first_scope = ConversationScope(1, 10)
    original = manager.capture_guard(first_scope)

    for channel_id in range(20, 30):
        manager.capture_guard(ConversationScope(1, channel_id))
        assert len(manager._sessions) <= manager.max_sessions

    assert not manager.is_current(original)
    recreated = manager.capture_guard(first_scope)

    assert recreated._generation != original._generation
    assert not manager.is_current(original)
    assert manager.is_current(recreated)
    assert len(manager._sessions) <= manager.max_sessions


def test_invalidation_during_fetch_fails_closed_and_never_publishes_stale_data():
    async def scenario():
        channel = FakeChannel()
        parent = make_message(channel, 4, content="deleted while fetching")
        trigger = make_message(channel, 5, content="question", reply_to=4)
        gate = asyncio.Event()
        channel.fetch_gate = gate
        manager = ConversationManager(max_api_calls=1)
        task = asyncio.create_task(
            manager.collect(
                trigger,
                bot_user_id=99,
                token_budget=1000,
                is_allowed=lambda scope: True,
            )
        )
        while not channel.fetch_calls:
            await asyncio.sleep(0)
        manager.invalidate_message(1, channel.id, parent.id)
        gate.set()
        return await task

    context = run(scenario())

    assert context.records == ()
    assert not context._valid_at_collect
    channel = FakeChannel()
    manager = ConversationManager()
    assert context.revision != 0
    assert context.latest_id == 5


def test_allowed_callback_is_rechecked_after_network_fetch_and_by_is_current():
    async def scenario():
        channel = FakeChannel()
        make_message(channel, 4, content="private")
        trigger = make_message(channel, 5, content="question", reply_to=4)
        gate = asyncio.Event()
        channel.fetch_gate = gate
        state = {"allowed": True}
        manager = ConversationManager(max_api_calls=1)
        task = asyncio.create_task(
            manager.collect(
                trigger,
                bot_user_id=99,
                token_budget=1000,
                is_allowed=lambda scope: state["allowed"],
            )
        )
        while not channel.fetch_calls:
            await asyncio.sleep(0)
        state["allowed"] = False
        gate.set()
        return manager, await task

    manager, context = run(scenario())
    assert context.records == ()
    assert not manager.is_current(context)


def test_ttl_and_session_capacity_bound_context_validity(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("bot.utils.ai_conversation.time.monotonic", lambda: clock["now"])
    channel = FakeChannel(channel_id=10)
    first = make_message(channel, 1, content="first")
    manager = ConversationManager(ttl_seconds=5, max_sessions=1, max_api_calls=0)
    first_context = collect(manager, first)

    clock["now"] = 104.0
    assert manager.is_current(first_context)
    other_channel = FakeChannel(channel_id=20)
    second = make_message(other_channel, 2, content="second")
    second_context = collect(manager, second)
    assert manager.is_current(second_context)
    assert not manager.is_current(first_context)

    clock["now"] = 110.0
    assert not manager.is_current(second_context)


def test_invalidate_channel_can_include_child_threads_but_not_unrelated_threads():
    parent = FakeChannel(channel_id=10)
    child = ThreadChannel(channel_id=20, parent_id=10)
    unrelated = ThreadChannel(channel_id=30, parent_id=11)
    manager = ConversationManager(max_api_calls=0)
    parent_context = collect(manager, make_message(parent, 1))
    child_context = collect(manager, make_message(child, 2))
    unrelated_context = collect(manager, make_message(unrelated, 3))

    manager.invalidate_channel(1, 10, include_children=True)

    assert not manager.is_current(parent_context)
    assert not manager.is_current(child_context)
    assert manager.is_current(unrelated_context)
