from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from bot.cogs import channel_cleanup


class FakeMessage:
    def __init__(self, created_at, is_bot=False):
        self.created_at = created_at
        self.author = SimpleNamespace(bot=is_bot)


class FakeTextChannel:
    def __init__(self, channel_id, name, created_at, messages):
        self.id = channel_id
        self.name = name
        self.created_at = created_at
        self.messages = messages
        self.deleted = False

    async def send(self, *, embed):
        return None

    async def delete(self, *, reason):
        self.deleted = True

    def history(self, *, limit, oldest_first):
        async def messages():
            for message in self.messages:
                yield message

        return messages()


class FakeGuild:
    def __init__(self, channels):
        self.id = 123
        self.name = "測試伺服器"
        self.channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


class FakeDatabase:
    applications = {
        "pending": [
            {"user_id": 1, "channel_id": 101, "status": "pending"},
        ],
        "closed": [
            {"user_id": 2, "channel_id": 102, "status": "closed"},
        ],
        "approved": [
            {"user_id": 5, "channel_id": 105, "status": "approved"},
        ],
    }
    suggestions = {
        "pending": [],
        "closed": [
            {"user_id": 3, "channel_id": 103, "status": "closed"},
        ],
        "approved": [
            {"user_id": 6, "channel_id": 106, "status": "approved"},
        ],
    }

    def __init__(self, guild_id, guild_name):
        self.expired_applications = []
        self.expired_suggestions = []
        self.removed_channels = []

    async def init_db(self):
        return None

    async def get_applications_by_status(self, status):
        return self.applications.get(status, [])

    async def get_suggestions_by_status(self, status):
        return self.suggestions.get(status, [])

    async def get_all_applications(self):
        return [application for applications in self.applications.values() for application in applications]

    async def get_all_suggestions(self):
        return [suggestion for suggestions in self.suggestions.values() for suggestion in suggestions]

    async def update_application_status(self, user_id, status):
        self.expired_applications.append((user_id, status))

    async def update_suggestion_status(self, user_id, status):
        self.expired_suggestions.append((user_id, status))

    async def remove_bot_created_channel(self, channel_id):
        self.removed_channels.append(channel_id)


def test_cleanup_extension_is_enabled():
    with open("config/bot.json", encoding="utf-8") as config_file:
        config = json.load(config_file)

    assert "bot.cogs.channel_cleanup" in config["extensions"]


@pytest.mark.asyncio
async def test_cleanup_removes_stale_channels_in_all_statuses(monkeypatch):
    now = channel_cleanup.discord.utils.utcnow()
    stale_time = now - timedelta(hours=36, minutes=1)
    fresh_time = now - timedelta(hours=35)
    channels = [
        FakeTextChannel(101, "身分組申請-使用者", now, [FakeMessage(stale_time)]),
        FakeTextChannel(102, "交換備審申請-使用者", now, [FakeMessage(stale_time)]),
        FakeTextChannel(103, "建議-使用者", now, [FakeMessage(stale_time)]),
        FakeTextChannel(104, "身分組申請-仍活躍", now, [FakeMessage(fresh_time)]),
        FakeTextChannel(105, "身分組申請-已核准", now, [FakeMessage(stale_time)]),
        FakeTextChannel(106, "建議-已核准", now, [FakeMessage(stale_time)]),
    ]
    database = FakeDatabase(123, "測試伺服器")
    bot = SimpleNamespace(logger=SimpleNamespace(warning=lambda *args: None))
    cleanup = channel_cleanup.ChannelCleanup.__new__(channel_cleanup.ChannelCleanup)
    cleanup.bot = bot

    monkeypatch.setattr(channel_cleanup, "DatabaseManager", lambda *args: database)
    monkeypatch.setattr(channel_cleanup.discord, "TextChannel", FakeTextChannel)

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(channel_cleanup.asyncio, "sleep", no_sleep)

    await cleanup._cleanup_stale_channels(FakeGuild(channels))

    assert sorted(channel.id for channel in channels if channel.deleted) == [101, 102, 103, 105, 106]
    assert not channels[3].deleted
    assert database.expired_applications == [(1, "expired"), (2, "expired"), (5, "expired")]
    assert database.expired_suggestions == [(3, "expired"), (6, "expired")]
    assert sorted(database.removed_channels) == [101, 102, 103, 105, 106]
