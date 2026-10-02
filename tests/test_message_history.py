from __future__ import annotations

from collections import Counter
from datetime import timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bot.utils import message_history as history


def message(message_id, user_id=2, *, bot=False, webhook=None, guild=True):
    return SimpleNamespace(id=message_id, author=SimpleNamespace(id=user_id, bot=bot),
                           webhook_id=webhook, guild=SimpleNamespace(id=1) if guild else None)


class Channel:
    def __init__(self, channel_id, messages=(), error=None):
        self.id = channel_id
        self.messages = list(messages)
        self.error = error
        self.calls = []

    async def history(self, **kwargs):
        self.calls.append(kwargs)
        for item in self.messages:
            if item.id < kwargs["before"].id and ("after" not in kwargs or item.id > kwargs["after"].id):
                yield item
        if self.error:
            raise self.error


def cutoff():
    return history.boundary_id(discord.utils.utcnow() - timedelta(days=1))


def snapshot_file(tmp_path, counts=None):
    path = tmp_path / "snapshot.json"
    scope = history.HistoryScope(1, [Channel(11)])
    return path, history.write_snapshot(path, scope, cutoff(), counts or Counter({2: 3}))


@pytest.mark.asyncio
async def test_snapshot_gap_and_new_channel_have_exact_boundaries():
    lower = cutoff()
    upper = lower + (10 << 22)
    old = Channel(11, [message(lower - 1), message(lower), message(lower + 1), message(upper - 1), message(upper)])
    new = Channel(12, [message(lower - 1, 3), message(lower, 3), message(upper, 3)])
    snapshot = history.HistorySnapshot(1, lower, Counter({2: 5}), frozenset({11}), "checksum", ())
    counts = await history.collect_history(history.HistoryScope(1, [old, new]), upper_id=upper,
                                          progress=history.HistoryProgress(), snapshot=snapshot)
    assert counts == {2: 8, 3: 2}
    assert old.calls[0]["after"].id == lower - 1
    assert old.calls[0]["before"].id == upper
    assert "after" not in new.calls[0]
    assert snapshot.counts == {2: 5}


@pytest.mark.asyncio
async def test_collector_filters_bots_webhooks_and_nonguild_messages():
    upper = cutoff()
    channel = Channel(11, [message(upper - 1), message(upper - 2, bot=True),
                           message(upper - 3, webhook=9), message(upper - 4, guild=False)])
    progress = history.HistoryProgress()
    counts = await history.collect_history(history.HistoryScope(1, [channel]), upper_id=upper, progress=progress)
    assert counts == {2: 1}
    assert progress.messages_read == 4
    assert progress.channels_done == 1


@pytest.mark.asyncio
async def test_completed_channel_checkpoint_is_not_recounted():
    upper = cutoff()
    old, new = Channel(11, [message(upper - 1)]), Channel(12, [message(upper - 1, 3)])
    checkpoint = AsyncMock()
    counts = await history.collect_history(history.HistoryScope(1, [old, new]), upper_id=upper,
        progress=history.HistoryProgress(), completed={"11": {"2": 7}}, checkpoint=checkpoint)
    assert counts == {2: 7, 3: 1}
    assert old.calls == []
    checkpoint.assert_awaited_once_with(12, Counter({3: 1}), None)


@pytest.mark.asyncio
async def test_cursor_resumes_without_recounting_or_gap():
    upper = 100 << 22
    channel = Channel(11, [message(upper - i) for i in range(1, 6)])
    progress = history.HistoryProgress()
    counts = await history.collect_history(
        history.HistoryScope(1, [channel]), upper_id=upper, progress=progress,
        cursors={"11": upper - 3},
    )
    assert counts == {2: 2}
    assert channel.calls[0]["before"].id == upper - 3
    assert "after" not in channel.calls[0]


@pytest.mark.asyncio
async def test_failed_pagination_does_not_checkpoint_partial_channel():
    error = discord.HTTPException(SimpleNamespace(status=500, reason="error"), "failed")
    channel = Channel(11, [message(cutoff() - 1)], error)
    checkpoint = AsyncMock()
    with pytest.raises(history.HistoryCollectionError, match="500"):
        await history.collect_history(history.HistoryScope(1, [channel]), upper_id=cutoff(),
                                     progress=history.HistoryProgress(), checkpoint=checkpoint)
    checkpoint.assert_not_awaited()


@pytest.mark.asyncio
async def test_discovery_deduplicates_threads_and_excludes_unreadable_parents(monkeypatch):
    thread = SimpleNamespace(id=21, parent_id=11)

    class TextChannel:
        def __init__(self, channel_id, readable=True):
            self.id = channel_id
            self.readable = readable

        def permissions_for(self, member):
            return SimpleNamespace(view_channel=self.readable, read_message_history=self.readable)

        async def archived_threads(self, **kwargs):
            yield thread
            yield SimpleNamespace(id=22, parent_id=self.id)

    class ForumChannel(TextChannel):
        pass

    monkeypatch.setattr(history.discord, "TextChannel", TextChannel)
    monkeypatch.setattr(history.discord, "ForumChannel", ForumChannel)
    guild = SimpleNamespace(id=1, fetch_channels=AsyncMock(return_value=[TextChannel(11), TextChannel(12, False)]),
                            active_threads=AsyncMock(return_value=[thread]), fetch_member=AsyncMock())
    scope = await history.discover_scope(guild, 8, history.HistoryProgress())
    assert [channel.id for channel in scope.channels] == [11, 21, 22]
    assert scope.excluded == [{"channel_id": "12", "reason": "missing_read_permission"}]


@pytest.mark.asyncio
async def test_failed_thread_discovery_is_not_complete():
    guild = SimpleNamespace(id=1, fetch_channels=AsyncMock(return_value=[]),
                            active_threads=AsyncMock(side_effect=OSError()), fetch_member=AsyncMock())
    with pytest.raises(history.HistoryCollectionError):
        await history.discover_scope(guild, 8, history.HistoryProgress())


def test_snapshot_roundtrip_and_wrong_guild(tmp_path):
    path, snapshot = snapshot_file(tmp_path)
    assert history.load_snapshot(path, 1) == snapshot
    with pytest.raises(ValueError, match="another guild"):
        history.load_snapshot(path, 9)


@pytest.mark.parametrize("change", [
    {"complete": False}, {"counts": {"2": -1}}, {"counts": {"2": True}},
    {"completed_channel_ids": []}, {"channel_ids": ["11", "11"]},
    {"version": True}, {"total_messages": 123},
    {"cutoff_id": str(history.boundary_id(discord.utils.utcnow() + timedelta(days=1)))},
])
def test_invalid_snapshot_is_rejected_even_with_valid_checksum(tmp_path, change):
    path, _ = snapshot_file(tmp_path)
    value = history.read_json(path)
    value.pop("sha256")
    value.update(change)
    value["sha256"] = hashlib.sha256(history._canonical(value)).hexdigest()
    history.atomic_json(path, value)
    with pytest.raises(ValueError):
        history.load_snapshot(path, 1)


def test_checksum_and_duplicate_keys_are_rejected(tmp_path):
    path, _ = snapshot_file(tmp_path)
    value = history.read_json(path)
    value["counts"] = {"2": 4}
    history.atomic_json(path, value)
    with pytest.raises(ValueError, match="checksum"):
        history.load_snapshot(path, 1)
    path.write_text('{"version":1,"version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        history.load_snapshot(path, 1)


@pytest.mark.asyncio
async def test_cli_uses_plain_rest_client_without_gateway_or_database(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("preload_cli_test", Path(__file__).parents[1] / "scripts/preload_leaderboard.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    calls = []

    class Client:
        user = SimpleNamespace(id=8, bot=True)

        def __init__(self, **kwargs):
            calls.append("client")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls.append("close")

        async def login(self, token):
            assert token == "dummy-test-token"
            calls.append("login")

        async def fetch_guild(self, guild_id):
            calls.append("fetch_guild")
            return SimpleNamespace(id=guild_id)

        async def connect(self, *args, **kwargs):
            raise AssertionError("gateway must not connect")

        start = connect
        run = connect

    monkeypatch.setattr(cli.discord, "Client", Client)
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("DISCORD_TOKEN", "dummy-test-token")
    monkeypatch.setattr(cli, "discover_scope", AsyncMock(return_value=history.HistoryScope(1, [Channel(11)])))
    monkeypatch.setattr(cli, "collect_history", AsyncMock(return_value=Counter({2: 3})))
    output, state = tmp_path / "out.json", tmp_path / "state.json"
    await cli.preload(1, output, state)
    assert calls == ["client", "login", "fetch_guild", "close"]
    assert history.load_snapshot(output, 1).counts == {2: 3}
    assert '"event": "complete"' in capsys.readouterr().out
