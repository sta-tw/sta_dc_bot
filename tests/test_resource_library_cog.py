import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from aiohttp import ClientSession, web

from bot.cogs.resource_library import RESOURCE_DOCUMENTS, ResourceLibraryCog
from bot.utils.resource_library_markdown import (
    MAX_MARKDOWN_CHARS,
    markdown_diff,
    split_resource_markdown,
    utf16_length,
)
from database.resource_library import ResourceRepository


class FakeRepository:
    def __init__(self, document=None):
        self.document = document
        self.get_calls = []
        self.saved_message_ids = []
        self.saved_message_lists = []

    def message_ids(self):
        message_id = self.document.get("message_id")
        message_ids = self.document.get("message_ids", [message_id] if message_id else [])
        if len(message_ids) <= 1 and message_ids != ([message_id] if message_id else []):
            message_ids = [message_id] if message_id else []
        return list(message_ids)

    async def get_document(self, slug):
        self.get_calls.append(slug)
        if self.document is None or self.document["slug"] != slug:
            return None
        return {**self.document, "message_ids": self.message_ids()}

    async def set_message_id(self, slug, message_id):
        self.saved_message_ids.append((slug, message_id))
        self.document.update(message_id=message_id, message_ids=[message_id])

    async def set_message_ids(
        self, slug, message_ids, *, expected_channel_id, expected_version, expected_message_ids
    ):
        if (
            self.document["slug"] != slug
            or self.document["channel_id"] != expected_channel_id
            or self.document["version"] != expected_version
            or self.message_ids() != expected_message_ids
        ):
            raise ValueError("文件或正式訊息已變更")
        previous = self.document.get("message_id")
        first = message_ids[0] if message_ids else None
        self.saved_message_lists.append((slug, list(message_ids)))
        if first is not None and first != previous:
            self.saved_message_ids.append((slug, first))
        self.document.update(message_id=first, message_ids=list(message_ids))


class FakeMessage:
    def __init__(self, message_id, author_id, embeds=None, content=""):
        self.id = message_id
        self.author = SimpleNamespace(id=author_id)
        self.embeds = embeds or []
        self.content = content
        self.edits = []
        self.deletes = 0
        self.channel = None

    async def edit(self, **kwargs):
        if "content" in kwargs:
            assert 0 < utf16_length(kwargs["content"]) <= 2000
            assert kwargs["content"].strip()
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]
        if "embed" in kwargs:
            self.embeds = [] if kwargs["embed"] is None else [kwargs["embed"]]
        return self

    async def delete(self):
        self.deletes += 1
        if self.channel is None or self.id not in self.channel.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown message")
        self.channel.messages.pop(self.id)
        if self.channel._message is self:
            self.channel._message = None


class FakeChannel:
    def __init__(self, guild, message=None):
        self.guild = guild
        self.messages = {}
        self._message = None
        self.message = message
        self.fetch_calls = []
        self.sent = []

    @property
    def message(self):
        return self._message

    @message.setter
    def message(self, value):
        if self._message is not None:
            self.messages.pop(self._message.id, None)
        self._message = value
        if value is not None:
            self.messages[value.id] = value
            value.channel = self

    async def fetch_message(self, message_id):
        self.fetch_calls.append(message_id)
        if message_id not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown message")
        return self.messages[message_id]

    async def send(self, **kwargs):
        assert 0 < utf16_length(kwargs["content"]) <= 2000
        assert kwargs["content"].strip()
        self.sent.append(kwargs)
        message = FakeMessage(9000 + len(self.sent), author_id=999, content=kwargs["content"])
        message.channel = self
        self.messages[message.id] = message
        self._message = message
        return message


class FakeGuild:
    def __init__(self, guild_id, channel):
        self.id = guild_id
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == 7001 else None


class FakeBot:
    def __init__(self, guild):
        self.user = SimpleNamespace(id=999)
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


@pytest.mark.parametrize(
    ("administrator", "manage_guild", "expected"),
    [
        (False, False, False),
        (False, True, True),
        (True, False, True),
    ],
)
def test_approval_requires_fresh_discord_management_permission(administrator, manage_guild, expected):
    member = SimpleNamespace(
        guild_permissions=SimpleNamespace(
            administrator=administrator,
            manage_guild=manage_guild,
        )
    )
    assert ResourceLibraryCog._can_review(member) is expected


def test_resource_labels_match_the_five_active_channels():
    assert [title for _, title in RESOURCE_DOCUMENTS] == [
        "資訊社群分享",
        "學習、比賽資源分享",
        "特選心得彙整",
        "公開備審資料彙整",
        "做備審的好工具",
    ]


def test_review_embed_displays_submitter_username_not_numeric_id():
    embed = ResourceLibraryCog._draft_embed(
        {"title": "做備審的好工具"},
        {"created_by": "882952356508094484", "base_version": 1},
        "review_author",
    )

    assert embed.fields[0].name == "提交者"
    assert embed.fields[0].value == "review\\_author"


@pytest.mark.asyncio
async def test_approved_document_edits_the_existing_bot_message(monkeypatch):
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    guild_id = 5001
    message = FakeMessage(8001, author_id=999)
    guild = FakeGuild(guild_id, None)
    channel = FakeChannel(guild, message)
    guild.channel = channel
    bot = FakeBot(guild)
    cog = ResourceLibraryCog(bot)
    repository = FakeRepository()
    cog._repositories[guild_id] = repository
    document = {
        "guild_id": guild_id,
        "slug": "communities",
        "title": "資訊社群分享",
        "channel_id": 7001,
        "message_id": 8001,
        "content_md": "# 社群\n- [社群](https://example.org/)\n",
        "version": 2,
        "updated_by": "42",
    }

    cog._repositories[guild_id].document = document
    result = await cog.sync_document(document)

    assert result is message
    assert channel.fetch_calls == [8001]
    assert channel.sent == []
    assert len(message.edits) == 1
    assert message.edits[0]["content"] == document["content_md"]
    assert message.edits[0]["embed"] is None
    assert repository.saved_message_ids == []


@pytest.mark.asyncio
async def test_sync_replaces_legacy_embed_with_plain_markdown(monkeypatch):
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    guild_id = 5001
    stored_embed = discord.Embed(
        title="公開備審資料彙整",
        description="# 公開備審資料彙整",
        color=discord.Color.blurple(),
    )
    message = FakeMessage(8001, author_id=999, embeds=[stored_embed])
    guild = FakeGuild(guild_id, None)
    channel = FakeChannel(guild, message)
    guild.channel = channel
    cog = ResourceLibraryCog(FakeBot(guild))
    cog._repositories[guild_id] = FakeRepository()
    document = {
        "guild_id": guild_id,
        "slug": "portfolios",
        "title": "公開備審資料彙整",
        "channel_id": 7001,
        "message_id": 8001,
        "content_md": "# 公開備審資料彙整\n\n- [範例](https://example.org/)\n",
        "version": 1,
        "updated_by": "system",
    }

    cog._repositories[guild_id].document = document
    result = await cog.sync_document(document)

    assert result is message
    assert len(message.edits) == 1
    assert message.edits[0]["content"] == document["content_md"]
    assert message.edits[0]["embed"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("preview_type", ["link", "article", "rich"])
async def test_sync_ignores_discord_generated_previews(monkeypatch, preview_type):
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    guild_id = 5001
    previews = [
        discord.Embed.from_dict({"type": preview_type, "url": "https://example.org/"}),
    ]
    message = FakeMessage(8001, author_id=999, embeds=previews, content="# 工具\n- [範例](https://example.org/)\n")
    guild = FakeGuild(guild_id, None)
    channel = FakeChannel(guild, message)
    guild.channel = channel
    cog = ResourceLibraryCog(FakeBot(guild))
    cog._repositories[guild_id] = FakeRepository()
    document = {
        "guild_id": guild_id,
        "slug": "tools",
        "title": "做備審的好工具",
        "channel_id": 7001,
        "message_id": 8001,
        "content_md": message.content,
        "version": 1,
        "updated_by": "system",
    }

    cog._repositories[guild_id].document = document
    await cog.sync_document(document)
    await cog.sync_document(document)

    assert channel.fetch_calls == [8001, 8001]
    assert message.edits == []


@pytest.mark.asyncio
async def test_sync_ignores_custom_emoji_ids_stripped_by_discord(monkeypatch):
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    guild_id = 5001
    content = "<a:attention:1330598329603850260> 工具\n- [範例](https://example.org/)\n"
    message = FakeMessage(
        8001,
        author_id=999,
        content=":attention: 工具\n- [範例](https://example.org/)\n",
        embeds=[discord.Embed.from_dict({"type": "link", "url": "https://example.org/"})],
    )
    guild = FakeGuild(guild_id, None)
    channel = FakeChannel(guild, message)
    guild.channel = channel
    cog = ResourceLibraryCog(FakeBot(guild))
    cog._repositories[guild_id] = FakeRepository()
    document = {
        "guild_id": guild_id,
        "slug": "tools",
        "title": "做備審的好工具",
        "channel_id": 7001,
        "message_id": 8001,
        "content_md": content,
        "version": 1,
        "updated_by": "system",
    }

    cog._repositories[guild_id].document = document
    await cog.sync_document(document)
    await cog.sync_document(document)

    assert channel.fetch_calls == [8001, 8001]
    assert message.edits == []


@pytest.mark.asyncio
async def test_sync_ignores_discord_trimmed_trailing_newlines(monkeypatch):
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    guild_id = 5001
    message = FakeMessage(8001, author_id=999, content="# 資訊社群分享")
    guild = FakeGuild(guild_id, None)
    channel = FakeChannel(guild, message)
    guild.channel = channel
    cog = ResourceLibraryCog(FakeBot(guild))
    cog._repositories[guild_id] = FakeRepository()
    document = {
        "guild_id": guild_id,
        "slug": "communities",
        "title": "資訊社群分享",
        "channel_id": 7001,
        "message_id": 8001,
        "content_md": "# 資訊社群分享\n\n",
        "version": 1,
        "updated_by": "system",
    }

    cog._repositories[guild_id].document = document
    result = await cog.sync_document(document)

    assert result is message
    assert message.edits == []


@pytest.mark.asyncio
async def test_sync_creates_and_records_message_when_not_initialized(monkeypatch):
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    guild_id = 5001
    guild = FakeGuild(guild_id, None)
    channel = FakeChannel(guild)
    guild.channel = channel
    cog = ResourceLibraryCog(FakeBot(guild))
    repository = FakeRepository()
    cog._repositories[guild_id] = repository
    document = {
        "guild_id": guild_id,
        "slug": "communities",
        "title": "資訊社群分享",
        "channel_id": 7001,
        "message_id": None,
        "content_md": "# 資訊社群分享\n\n",
        "version": 1,
        "updated_by": "system",
    }

    cog._repositories[guild_id].document = document
    result = await cog.sync_document(document)

    assert result.id == 9001
    assert len(channel.sent) == 1
    assert channel.sent[0]["content"] == document["content_md"]
    assert "embed" not in channel.sent[0]
    assert repository.saved_message_ids == [("communities", 9001)]


def _sync_cog(monkeypatch, *, content="# 工具\n\n", message_content="# 舊內容\n"):
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    document = {
        "guild_id": 5001,
        "slug": "tools",
        "title": "做備審的好工具",
        "channel_id": 7001,
        "message_id": 8001,
        "content_md": content,
        "version": 1,
        "updated_by": "system",
    }
    message = FakeMessage(8001, author_id=999, content=message_content)
    guild = FakeGuild(5001, None)
    channel = FakeChannel(guild, message)
    guild.channel = channel
    cog = ResourceLibraryCog(FakeBot(guild))
    repository = FakeRepository(dict(document))
    cog._repositories[guild.id] = repository
    cog._document_message_index[(guild.id, message.id)] = document["slug"]
    return cog, repository, channel, document


def _edit_payload(content, *, message_id=8001):
    return SimpleNamespace(guild_id=5001, message_id=message_id, data={"content": content})


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"embeds": []}, {"pinned": True}, {}])
async def test_embed_only_raw_updates_do_not_reconcile(monkeypatch, data):
    cog, repository, channel, _ = _sync_cog(monkeypatch)

    await cog.on_raw_message_edit(SimpleNamespace(guild_id=5001, message_id=8001, data=data))

    assert repository.get_calls == []
    assert channel.fetch_calls == []
    assert channel.message.edits == []


@pytest.mark.asyncio
async def test_own_edit_echo_and_regenerated_rich_preview_do_not_repeat_patch(monkeypatch):
    cog, _, channel, document = _sync_cog(monkeypatch)
    message = await cog.sync_document(document)
    message.embeds = [discord.Embed.from_dict({"type": "rich", "url": "https://example.org/"})]

    for _ in range(3):
        await cog.on_raw_message_edit(_edit_payload(message.content))
        await cog.on_raw_message_edit(SimpleNamespace(
            guild_id=5001, message_id=8001, data={"embeds": [message.embeds[0].to_dict()]}
        ))

    assert len(message.edits) == 1
    assert channel.fetch_calls == [8001]


@pytest.mark.asyncio
async def test_sync_accepts_returned_discord_content_without_a_normalization_loop(monkeypatch):
    cog, _, channel, document = _sync_cog(monkeypatch, content="# 工具\r\n\r\n- 保留縮排  \r\n")
    original = channel.message
    returned = FakeMessage(8001, author_id=999)

    async def normalized_edit(**kwargs):
        original.edits.append(kwargs)
        returned.content = kwargs["content"].replace("\r\n", "\n")
        channel.message = returned
        return returned

    original.edit = normalized_edit

    result = await cog.sync_document(document)
    await cog.on_raw_message_edit(_edit_payload(returned.content))
    await cog.on_raw_message_edit(_edit_payload("# 舊的排隊事件"))
    await cog.sync_document(document)

    assert result is returned
    assert len(original.edits) == 1
    assert returned.edits == []
    assert channel.fetch_calls == [8001, 8001, 8001]
    assert original.edits[0]["content"] == document["content_md"]


@pytest.mark.asyncio
async def test_real_content_tampering_is_restored_after_successful_sync(monkeypatch):
    cog, _, channel, document = _sync_cog(monkeypatch)
    message = await cog.sync_document(document)
    message.content = "# 未審核的修改"

    await cog.on_raw_message_edit(_edit_payload(message.content))
    await cog.on_raw_message_edit(_edit_payload(message.content))

    assert message.content == document["content_md"]
    assert len(message.edits) == 2
    assert channel.fetch_calls == [8001, 8001]


@pytest.mark.asyncio
async def test_new_document_version_invalidates_the_published_acknowledgement(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch)
    message = await cog.sync_document(document)
    repository.document.update(version=2, content_md="# 最新核准版本\n")

    await cog.on_raw_message_edit(_edit_payload(message.content))

    assert message.content == repository.document["content_md"]
    assert len(message.edits) == 2
    assert message.edits[-1]["content"] == "# 最新核准版本\n"


@pytest.mark.asyncio
async def test_concurrent_syncs_serialize_before_fetch_and_patch_once(monkeypatch):
    cog, _, channel, document = _sync_cog(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    queued = asyncio.Event()
    original_edit = channel.message.edit

    async def blocked_edit(**kwargs):
        entered.set()
        await release.wait()
        return await original_edit(**kwargs)

    async def second_sync():
        queued.set()
        return await cog.sync_document(document)

    channel.message.edit = blocked_edit
    first = asyncio.create_task(cog.sync_document(document))
    await entered.wait()
    second = asyncio.create_task(second_sync())
    await queued.wait()
    fetches_before_release = list(channel.fetch_calls)
    release.set()
    await asyncio.gather(first, second)

    assert fetches_before_release == [8001]
    assert len(channel.message.edits) == 1
    assert channel.sent == []


@pytest.mark.asyncio
async def test_concurrent_deletions_create_only_one_replacement(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch)
    channel.message = None
    entered = asyncio.Event()
    release = asyncio.Event()
    queued = asyncio.Event()
    original_send = channel.send
    payload = SimpleNamespace(guild_id=5001, message_id=8001)

    async def blocked_send(**kwargs):
        entered.set()
        await release.wait()
        return await original_send(**kwargs)

    async def second_delete():
        queued.set()
        await cog.on_raw_message_delete(payload)

    channel.send = blocked_send
    first = asyncio.create_task(cog.on_raw_message_delete(payload))
    await entered.wait()
    second = asyncio.create_task(second_delete())
    await queued.wait()
    release.set()
    await asyncio.gather(first, second)

    assert channel.fetch_calls == [8001]
    assert len(channel.sent) == 1
    assert repository.saved_message_ids == [(document["slug"], 9001)]
    assert repository.document["message_id"] == 9001
    assert (5001, 8001) not in cog._document_message_index
    assert cog._document_message_index[(5001, 9001)] == document["slug"]


@pytest.mark.asyncio
async def test_sync_reloads_latest_version_and_message_id_after_waiting_for_lock(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch)
    lock = cog._get_sync_lock((5001, document["slug"]))
    entered = asyncio.Event()

    async def sync_stale_snapshot():
        entered.set()
        return await cog.sync_document(document)

    async with lock:
        task = asyncio.create_task(sync_stale_snapshot())
        await entered.wait()
        repository.document.update(version=2, message_id=8002, content_md="# 最新版本\n")
        channel.message = FakeMessage(8002, author_id=999, content="# 舊訊息")
    result = await task

    assert channel.fetch_calls == [8002]
    assert result.content == repository.document["content_md"]
    assert result.edits[0]["content"] == "# 最新版本\n"


@pytest.mark.asyncio
async def test_terminal_429_blocks_queued_and_new_reconciliation_until_retry_after(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("bot.cogs.resource_library.monotonic", lambda: clock[0])
    entered = asyncio.Event()
    release = asyncio.Event()
    queued = asyncio.Event()
    error = discord.HTTPException(
        SimpleNamespace(status=429, reason="Too Many Requests", headers={"Retry-After": "120.5"}),
        "You are being rate limited.",
    )
    original_edit = channel.message.edit

    async def limited_edit(**kwargs):
        entered.set()
        await release.wait()
        raise error

    async def second_edit():
        queued.set()
        await cog.on_raw_message_edit(_edit_payload("# 舊內容\n"))

    channel.message.edit = AsyncMock(side_effect=limited_edit)
    first = asyncio.create_task(cog.on_raw_message_edit(_edit_payload("# 舊內容\n")))
    await entered.wait()
    second = asyncio.create_task(second_edit())
    await queued.wait()
    repository.document.update(version=2, content_md="# 最新核准版本\n")
    release.set()
    await asyncio.gather(first, second)
    await cog.on_raw_message_edit(_edit_payload("# 另一個事件"))
    await cog.on_raw_message_delete(SimpleNamespace(guild_id=5001, message_id=8001))
    clock[0] = 220.0
    with pytest.raises(ValueError, match="退避"):
        await cog.sync_document(document)

    assert channel.fetch_calls == [8001]
    assert channel.message.edit.await_count == 1

    channel.message.edit = original_edit
    clock[0] = 220.5
    await cog.sync_document(document)

    assert channel.fetch_calls == [8001, 8001]
    assert channel.message.content == "# 最新核准版本\n"
    assert len(channel.message.edits) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", [None, "invalid", "nan", "inf", "-1", "0"])
async def test_terminal_429_without_valid_retry_after_uses_conservative_cooldown(monkeypatch, retry_after):
    cog, _, channel, document = _sync_cog(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr("bot.cogs.resource_library.monotonic", lambda: clock[0])
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    error = discord.HTTPException(
        SimpleNamespace(status=429, reason="Too Many Requests", headers=headers), "rate limited"
    )
    original_edit = channel.message.edit
    channel.message.edit = AsyncMock(side_effect=error)

    with pytest.raises(discord.HTTPException):
        await cog.sync_document(document)
    clock[0] = 159.9
    with pytest.raises(ValueError, match="退避"):
        await cog.sync_document(document)
    assert channel.fetch_calls == [8001]

    channel.message.edit = original_edit
    clock[0] = 160.0
    await cog.sync_document(document)
    assert channel.fetch_calls == [8001, 8001]


@pytest.mark.asyncio
async def test_non_429_failure_does_not_activate_cooldown(monkeypatch):
    cog, _, channel, document = _sync_cog(monkeypatch)
    error = discord.HTTPException(SimpleNamespace(status=500, reason="Server Error", headers={}), "failed")
    original_edit = channel.message.edit
    channel.message.edit = AsyncMock(side_effect=error)

    with pytest.raises(discord.HTTPException):
        await cog.sync_document(document)
    channel.message.edit = original_edit
    await cog.sync_document(document)

    assert channel.fetch_calls == [8001, 8001]
    assert len(channel.message.edits) == 1


class FakeApiRequest:
    def __init__(self, slug, payload, guild_id=5001):
        self.match_info = {"guild_id": str(guild_id), "slug": slug}
        self.payload = payload

    async def json(self):
        return self.payload


async def _api_cog(tmp_path, monkeypatch, *, legacy=False):
    guild_id = 5001
    repository = ResourceRepository(tmp_path / "guild.db")
    await repository.initialize()
    document_labels = list(RESOURCE_DOCUMENTS)
    if legacy:
        document_labels.insert(0, ("events", "活動資訊分享"))
    await repository.configure(
        [
            {"slug": slug, "title": title, "channel_id": 1000 + index}
            for index, (slug, title) in enumerate(document_labels)
        ],
        review_channel_id=8001,
        notification_role_id=8002,
    )
    guild = SimpleNamespace(id=guild_id)
    member = SimpleNamespace(id=77, name="review_author")
    cog = ResourceLibraryCog(SimpleNamespace())
    cog._repositories[guild_id] = repository

    async def authenticate_member(request, requested_guild_id):
        assert requested_guild_id == guild_id
        return guild, member

    monkeypatch.setattr(cog, "_authenticate_member", authenticate_member)
    return cog, repository, guild, member


@pytest.mark.asyncio
async def test_manual_sync_reports_cooldown_without_claiming_success(tmp_path, monkeypatch):
    cog, _, guild, _ = await _api_cog(tmp_path, monkeypatch)
    monkeypatch.setattr("bot.cogs.resource_library.monotonic", lambda: 100.0)
    cog._sync_not_before[(guild.id, RESOURCE_DOCUMENTS[0][0])] = 160.0
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await ResourceLibraryCog.resource_sync.callback(cog, interaction)

    interaction.followup.send.assert_awaited_once()
    assert "同步失敗" in interaction.followup.send.await_args.args[0]
    assert "退避" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cooldown", "http"])
async def test_approval_failure_is_saved_but_never_labeled_published(failure, tmp_path, monkeypatch):
    cog, repository, guild, _ = await _api_cog(tmp_path, monkeypatch)
    draft = await repository.create_draft("tools", 77, 1, "# 最新核准版本\n")
    await repository.set_review_thread(draft["id"], 4001, 4002)
    guild.fetch_member = AsyncMock(return_value=SimpleNamespace(
        id=88, guild_permissions=SimpleNamespace(administrator=False, manage_guild=True)
    ))
    if failure == "cooldown":
        monkeypatch.setattr("bot.cogs.resource_library.monotonic", lambda: 100.0)
        cog._sync_not_before[(guild.id, "tools")] = 160.0
    else:
        cog.sync_document = AsyncMock(side_effect=discord.HTTPException(
            SimpleNamespace(status=429, reason="Too Many Requests", headers={}), "rate limited"
        ))
    message = FakeMessage(4002, author_id=999)
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        channel=SimpleNamespace(),
        channel_id=4001,
        message=message,
        user=SimpleNamespace(id=88),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await cog.resolve_review(interaction, approve=True)

    assert (await repository.get_draft(draft["id"]))["status"] == "approved"
    document = await repository.get_document("tools")
    assert document["version"] == 2
    assert document["content_md"] == "# 最新核准版本\n"
    assert "已同意但尚未發布" in message.edits[0]["embed"].description
    assert "已同意並發布" not in message.edits[0]["embed"].description
    assert "正式訊息同步失敗" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_legacy_six_document_database_lists_only_five_and_preserves_events(
    tmp_path, monkeypatch
):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch, legacy=True)
    await repository.set_message_id("events", 9001)
    original = await repository.get_document("events")
    draft = await repository.create_draft("events", 77, 1, "# 舊活動草稿\n\n")
    await repository.set_review_thread(draft["id"], 4001, 4002)
    original_draft = await repository.get_draft(draft["id"])

    response = await cog._api_list_resources(FakeApiRequest("", {}))

    documents = json.loads(response.text)["documents"]
    assert [document["slug"] for document in documents] == [
        slug for slug, _ in RESOURCE_DOCUMENTS
    ]
    assert [document["channel_id"] for document in documents] == [1001, 1002, 1003, 1004, 1005]
    assert len(await repository.list_documents()) == 6
    assert await repository.get_document("events") == original
    assert await repository.get_draft(draft["id"]) == original_draft


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["preview", "draft"])
async def test_legacy_events_cannot_receive_new_edits(endpoint, tmp_path, monkeypatch):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch, legacy=True)
    original = await repository.get_document("events")
    handler = cog._api_preview if endpoint == "preview" else cog._api_create_draft

    with pytest.raises(web.HTTPNotFound):
        await handler(
            FakeApiRequest("events", {"base_version": 1, "content_md": "# 新活動\n"})
        )

    assert await repository.get_document("events") == original


@pytest.mark.asyncio
async def test_setup_maps_five_channels_without_changing_archived_events(tmp_path, monkeypatch):
    cog, repository, guild, _ = await _api_cog(tmp_path, monkeypatch, legacy=True)
    await repository.set_message_id("events", 9001)
    original = await repository.get_document("events")
    cog._sync_guild = AsyncMock()
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    channels = [SimpleNamespace(id=3000 + index) for index in range(5)]
    review_channel = SimpleNamespace(id=4000, mention="#review")
    role = SimpleNamespace(id=5000, mention="@review", is_default=lambda: False)

    await ResourceLibraryCog.resource_setup.callback(
        cog, interaction, *channels, review_channel, role
    )

    assert interaction.response.defer.await_count == 1
    assert interaction.followup.send.await_count == 1
    cog._sync_guild.assert_awaited_once_with(guild)
    assert [document["channel_id"] for document in await cog._active_documents(repository)] == [
        channel.id for channel in channels
    ]
    assert await repository.get_document("events") == original
    assert len(await repository.list_documents()) == 6


@pytest.mark.asyncio
async def test_startup_and_manual_sync_skip_archived_events(tmp_path, monkeypatch):
    cog, repository, guild, _ = await _api_cog(tmp_path, monkeypatch, legacy=True)
    await repository.set_message_id("events", 9001)
    original = await repository.get_document("events")
    cog.sync_document = AsyncMock()

    await cog._sync_guild(guild)
    assert [call.args[0]["slug"] for call in cog.sync_document.await_args_list] == [
        slug for slug, _ in RESOURCE_DOCUMENTS
    ]
    cog.sync_document.reset_mock()
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    await ResourceLibraryCog.resource_sync.callback(cog, interaction)

    assert [call.args[0]["slug"] for call in cog.sync_document.await_args_list] == [
        slug for slug, _ in RESOURCE_DOCUMENTS
    ]
    assert "五份" in interaction.followup.send.await_args.args[0]
    assert await repository.get_document("events") == original


@pytest.mark.asyncio
async def test_editor_entry_accepts_legacy_six_document_database(tmp_path, monkeypatch):
    cog, _, guild, _ = await _api_cog(tmp_path, monkeypatch, legacy=True)
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    await ResourceLibraryCog.resource_editor.callback(cog, interaction)

    assert interaction.response.send_message.await_args.args[0] == "如果你也有超棒的東西想和大家分享，點我就對了"
    assert isinstance(interaction.response.send_message.await_args.kwargs["view"], discord.ui.View)


@pytest.mark.asyncio
async def test_archived_message_is_never_edited_or_recreated(tmp_path, monkeypatch):
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch, legacy=True)
    await repository.set_message_id("events", 9001)
    original = await repository.get_document("events")
    message = FakeMessage(9001, author_id=999, content=original["content_md"])
    guild = FakeGuild(5001, None)
    channel = FakeChannel(guild, message)
    guild.channel = channel
    cog.bot = FakeBot(guild)

    with pytest.raises(ValueError, match="已停用"):
        await cog.sync_document({**original, "guild_id": guild.id, "channel_id": 7001})
    cog._document_message_index[(guild.id, message.id)] = "events"
    payload = SimpleNamespace(guild_id=guild.id, message_id=message.id)
    await cog.on_raw_message_edit(payload)
    await cog.on_raw_message_delete(payload)

    assert channel.fetch_calls == []
    assert channel.sent == []
    assert message.edits == []
    assert await repository.get_document("events") == original


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_old_pending_review_remains_pending_when_pressed(approve, tmp_path, monkeypatch):
    cog, repository, guild, _ = await _api_cog(tmp_path, monkeypatch, legacy=True)
    await repository.set_message_id("events", 9001)
    original = await repository.get_document("events")
    draft = await repository.create_draft("events", 77, 1, "# 舊活動草稿\n\n")
    await repository.set_review_thread(draft["id"], 4001, 4002)
    original_draft = await repository.get_draft(draft["id"])
    guild.fetch_member = AsyncMock(return_value=SimpleNamespace(
        id=88,
        guild_permissions=SimpleNamespace(administrator=False, manage_guild=True),
    ))
    message = FakeMessage(4002, author_id=999)
    interaction = SimpleNamespace(
        guild=guild,
        guild_id=guild.id,
        channel=SimpleNamespace(),
        channel_id=4001,
        message=message,
        user=SimpleNamespace(id=88),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    cog.sync_document = AsyncMock()
    decide_draft = AsyncMock()
    monkeypatch.setattr(repository, "decide_draft", decide_draft)

    await cog.resolve_review(interaction, approve=approve)

    assert "已停用" in interaction.followup.send.await_args.args[0]
    decide_draft.assert_not_awaited()
    cog.sync_document.assert_not_awaited()
    assert message.edits == []
    assert await repository.get_document("events") == original
    assert await repository.get_draft(draft["id"]) == original_draft


@pytest.mark.asyncio
async def test_api_list_resources_returns_raw_markdown_without_structure(
    tmp_path, monkeypatch
):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    content_md = (
        "# 🧰 工具清單\n\n"
        "- 第一項：備審 📚\n"
        "- [第二項](https://example.org/)\n\n"
        "1. 保留空行\n"
        "2. 原文不重建\n"
    )
    async with repository._connect() as db:
        await db.execute(
            "UPDATE resource_documents SET content_md = ? WHERE slug = 'tools'",
            (content_md,),
        )
        await db.commit()
    expected_documents = await repository.list_documents()

    response = await cog._api_list_resources(FakeApiRequest("", {}))

    documents = json.loads(response.text)["documents"]
    assert documents == expected_documents
    tools_document = next(item for item in documents if item["slug"] == "tools")
    assert tools_document["content_md"] == content_md
    assert all("structure" not in item for item in documents)


@pytest.mark.asyncio
async def test_api_preview_preserves_raw_markdown_and_returns_exact_diff(tmp_path, monkeypatch):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    before = (await repository.get_document("tools"))["content_md"]
    content_md = (
        "# 做備審的好工具  \r\n"
        "\r\n"
        "> **先規劃再整理**\r\n"
        "\r\n"
        "- [官方簡章](https://example.org/admissions)  <!-- 保留註解 -->\r\n"
        "`[程式碼示例](javascript:alert(1))`\r\n"
    )

    response = await cog._api_preview(
        FakeApiRequest("tools", {"base_version": 1, "content_md": content_md})
    )

    result = json.loads(response.text)
    assert result["content_md"] == content_md
    assert result["diff"] == markdown_diff(before, content_md)
    assert (await repository.get_document("tools"))["content_md"] == before


@pytest.mark.asyncio
async def test_api_create_draft_keeps_exact_snapshot_without_publishing(tmp_path, monkeypatch):
    cog, repository, _, member = await _api_cog(tmp_path, monkeypatch)
    before = (await repository.get_document("tools"))["content_md"]
    content_md = "# 工具清單\n\n自由格式 **Markdown**\n- [範例](https://example.org/)\n"

    async def create_review_thread(*args):
        assert args[4] == member.name
        return SimpleNamespace(id=7001), SimpleNamespace(id=7002)

    monkeypatch.setattr(cog, "_create_review_thread", create_review_thread)
    response = await cog._api_create_draft(
        FakeApiRequest("tools", {"base_version": 1, "content_md": content_md})
    )

    result = json.loads(response.text)
    draft = await repository.get_draft(result["draft_id"])
    document = await repository.get_document("tools")
    assert response.status == 201
    assert draft["after_md"] == content_md
    assert draft["created_by"] == str(member.id)
    assert draft["before_md"] == before
    assert draft["diff"] == markdown_diff(before, content_md)
    assert draft["status"] == "pending"
    assert document["content_md"] == before
    assert document["version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["preview", "draft"])
async def test_api_accepts_resource_urls_without_http_scheme(endpoint, tmp_path, monkeypatch):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    content_md = "# 工具清單\n\n- [文件](/docs)\n- [聯絡](mailto:team@example.org)\n"

    async def create_review_thread(*args):
        return SimpleNamespace(id=7001), SimpleNamespace(id=7002)

    monkeypatch.setattr(cog, "_create_review_thread", create_review_thread)
    handler = cog._api_preview if endpoint == "preview" else cog._api_create_draft
    response = await handler(
        FakeApiRequest("tools", {"base_version": 1, "content_md": content_md})
    )

    assert response.status == (200 if endpoint == "preview" else 201)
    if endpoint == "preview":
        assert json.loads(response.text)["content_md"] == content_md
    else:
        draft_id = json.loads(response.text)["draft_id"]
        assert (await repository.get_draft(draft_id))["after_md"] == content_md
    assert (await repository.get_document("tools"))["version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["preview", "draft"])
@pytest.mark.parametrize(
    ("content_md", "expected_status"),
    [
        (" \n\t", 400),
        ("- [Unsafe](javascript:alert(1))\n", 400),
        ("x" * (MAX_MARKDOWN_CHARS + 1), 413),
    ],
)
async def test_api_rejects_blank_unsafe_and_over_limit_markdown(
    endpoint, content_md, expected_status, tmp_path, monkeypatch
):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    handler = cog._api_preview if endpoint == "preview" else cog._api_create_draft
    request = FakeApiRequest("tools", {"base_version": 1, "content_md": content_md})

    with pytest.raises(web.HTTPException) as error:
        await handler(request)

    assert error.value.status == expected_status
    assert (await repository.get_document("tools"))["version"] == 1
    assert (await repository.get_document("tools"))["content_md"] == "# 做備審的好工具\n\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["preview", "draft"])
async def test_api_rejects_stale_base_version(endpoint, tmp_path, monkeypatch):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    handler = cog._api_preview if endpoint == "preview" else cog._api_create_draft

    with pytest.raises(web.HTTPConflict):
        await handler(
            FakeApiRequest(
                "tools",
                {"base_version": 2, "content_md": "# New tools\n"},
            )
        )

    assert (await repository.get_document("tools"))["version"] == 1
    assert (await repository.get_document("tools"))["content_md"] == "# 做備審的好工具\n\n"


@pytest.mark.asyncio
async def test_review_thread_403_keeps_saved_draft_unpublished(tmp_path, monkeypatch):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    content_md = "# 草稿內容\n\n- [範例](https://example.org/)\n"
    response = SimpleNamespace(status=403, reason="Forbidden", headers={})

    async def forbidden_review_thread(*args):
        raise discord.Forbidden(response, "Missing Permissions")

    monkeypatch.setattr(cog, "_create_review_thread", forbidden_review_thread)
    with pytest.raises(web.HTTPForbidden):
        await cog._api_create_draft(
            FakeApiRequest("tools", {"base_version": 1, "content_md": content_md})
        )

    document = await repository.get_document("tools")
    async with repository._connect() as db:
        row = await (await db.execute(
            "SELECT after_md, status FROM resource_drafts WHERE document_slug = 'tools'"
        )).fetchone()
    assert document["content_md"] == "# 做備審的好工具\n\n"
    assert document["version"] == 1
    assert row["after_md"] == content_md
    assert row["status"] == "review_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["preview", "draft"])
@pytest.mark.parametrize(
    "content_md",
    [
        "x" * 2000, "x" * 2001, "x" * 20000, "😀" * 1001, "😀" * 10000,
        "x" * 2000 + "\n", "x" * 1999 + "\r\n", "x" * 2000 + " " * 100,
        "a" * 1999 + "\n" * 2001 + "b", "x\n" + " " * 2200 + "y",
    ],
    ids=[
        "single-limit", "split-boundary", "document-limit", "emoji-split", "emoji-document-limit",
        "trailing-newline", "trailing-crlf", "trailing-spaces", "long-blank-run", "short-line-before-spaces",
    ],
)
async def test_api_accepts_full_documents_beyond_one_discord_message(endpoint, content_md, tmp_path, monkeypatch):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    before = await repository.get_document("tools")
    cog._create_review_thread = AsyncMock(return_value=(SimpleNamespace(id=7001), SimpleNamespace(id=7002)))
    handler = cog._api_preview if endpoint == "preview" else cog._api_create_draft

    response = await handler(FakeApiRequest("tools", {"base_version": 1, "content_md": content_md}))

    payload = json.loads(response.text)
    assert response.status == (200 if endpoint == "preview" else 201)
    if endpoint == "preview":
        assert payload["content_md"] == content_md
        assert payload["diff"] == markdown_diff(before["content_md"], content_md)
        assert payload["message_count"] == len(split_resource_markdown(content_md))
    else:
        draft = await repository.get_draft(payload["draft_id"])
        assert draft["after_md"] == content_md
        assert draft["before_md"] == before["content_md"]
        assert draft["diff"] == markdown_diff(before["content_md"], content_md)
        assert draft["status"] == "pending"
    assert await repository.get_document("tools") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["preview", "draft"])
@pytest.mark.parametrize(
    ("content_md", "status"),
    [("😀" * 10001, 413), ("before\ud800after", 400), ("x" + " " * 4001, 400)],
    ids=["over-document-limit", "invalid-unicode", "unpublishable-whitespace"],
)
async def test_api_rejects_over_document_unicode_limit_and_unpublishable_text(endpoint, content_md, status, tmp_path, monkeypatch):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    before = await repository.get_document("tools")
    handler = cog._api_preview if endpoint == "preview" else cog._api_create_draft

    with pytest.raises(web.HTTPException) as error:
        await handler(FakeApiRequest("tools", {"base_version": 1, "content_md": content_md}))

    assert error.value.status == status
    assert await repository.get_document("tools") == before


@pytest.mark.asyncio
async def test_api_config_reports_shared_document_and_message_limits(monkeypatch):
    cog = ResourceLibraryCog(SimpleNamespace(application_id=1234))
    monkeypatch.setenv("RESOURCE_STANDALONE_ENABLED", "0")
    monkeypatch.setattr(cog, "_client_id", lambda: "1234")

    payload = json.loads((await cog._api_config(SimpleNamespace())).text)

    assert payload["max_markdown_chars"] == MAX_MARKDOWN_CHARS == 20000
    assert payload["discord_message_chars"] == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("diff", ["x" * 3601, "😀" * 1801], ids=["long-ascii", "long-emoji"])
async def test_long_review_diff_uses_exact_utf8_attachment(diff, tmp_path, monkeypatch):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    thread = SimpleNamespace(id=4001, send=AsyncMock(return_value=FakeMessage(4002, 999)))
    starter = SimpleNamespace(id=4000, create_thread=AsyncMock(return_value=thread))
    guild = SimpleNamespace(id=5001, get_role=lambda role_id: None)
    channel = FakeChannel(guild)
    guild.get_channel = lambda channel_id: channel
    channel.send = AsyncMock(return_value=starter)
    document = await repository.get_document("tools")
    draft = {"id": "draft", "created_by": "77", "base_version": 1, "diff": diff}

    await cog._create_review_thread(guild, repository, document, draft, "member")

    kwargs = thread.send.await_args.kwargs
    assert len(kwargs["files"]) == 1
    attachment = kwargs["files"][0]
    try:
        assert attachment.filename == "resource-diff.diff"
        assert attachment.fp.read().decode("utf-8") == diff
        assert "查看附加" in kwargs["embed"].description
        assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    finally:
        attachment.close()


def _parts_content(channel, repository):
    return "".join(channel.messages[message_id].content for message_id in repository.message_ids())


def _http_error(status):
    return discord.HTTPException(SimpleNamespace(status=status, reason="failure", headers={}), "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "A" * 2000 + "B" * 2000 + "C", "# 文件\r\n" + "- 台灣資源 😀\r\n" * 500,
        "x" * 2000 + "\n", "x" * 1999 + "\r\n", "x" * 2000 + " " * 100,
    ],
    ids=["long-line", "multiline-unicode", "trailing-newline", "trailing-crlf", "trailing-spaces"],
)
async def test_multipart_publication_is_lossless_and_repeat_sync_is_idempotent(content, monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content=content)

    first = await cog.sync_document(document)
    ids = repository.message_ids()
    edits = {message_id: len(message.edits) for message_id, message in channel.messages.items()}
    await cog.sync_document(document)

    assert first.id == ids[0] == 8001
    assert len(ids) == len(split_resource_markdown(content)) > 1
    assert len(channel.sent) == len(ids) - 1
    assert _parts_content(channel, repository) == content
    assert {message_id: len(message.edits) for message_id, message in channel.messages.items()} == edits
    for message_id in ids:
        assert cog._document_message_index[(5001, message_id)] == "tools"
    for send in channel.sent:
        assert send["allowed_mentions"].to_dict() == {"parse": []}


@pytest.mark.asyncio
async def test_document_growth_and_shrink_reuse_first_message_and_ignore_retired_events(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="# 起始內容\n")
    first = await cog.sync_document(document)
    repository.document.update(version=2, content_md="A" * 2000 + "B" * 2000 + "C")
    await cog.sync_document(document)
    ids = repository.message_ids()
    surplus = [channel.messages[message_id] for message_id in ids[1:]]
    repository.document.update(version=3, content_md="# 縮短後\n")

    result = await cog.sync_document(document)
    before_fetches = list(channel.fetch_calls)
    for message_id in ids[1:]:
        await cog.on_raw_message_delete(SimpleNamespace(guild_id=5001, message_id=message_id))
        await cog.on_raw_message_edit(_edit_payload("old queued event", message_id=message_id))

    assert result is first
    assert repository.message_ids() == [first.id]
    assert first.content == "# 縮短後\n"
    assert len(channel.sent) == 2
    assert all(message.deletes == 1 for message in surplus)
    assert set(channel.messages) == {first.id}
    assert channel.fetch_calls == before_fetches
    assert all((5001, message_id) not in cog._document_message_index for message_id in ids[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_position", [0, 1, 2])
async def test_deleting_any_chunk_rebuilds_only_suffix_in_chronological_order(missing_position, monkeypatch):
    content = "A" * 2000 + "B" * 2000 + "C"
    cog, repository, channel, document = _sync_cog(monkeypatch, content=content)
    await cog.sync_document(document)
    old_ids = repository.message_ids()
    old_messages = dict(channel.messages)
    channel.messages.pop(old_ids[missing_position])

    await cog.on_raw_message_delete(SimpleNamespace(guild_id=5001, message_id=old_ids[missing_position]))
    new_ids = repository.message_ids()
    before_fetches = list(channel.fetch_calls)
    await cog.on_raw_message_delete(SimpleNamespace(guild_id=5001, message_id=old_ids[missing_position]))

    assert new_ids[:missing_position] == old_ids[:missing_position]
    assert new_ids[missing_position:] != old_ids[missing_position:]
    assert new_ids == sorted(new_ids)
    assert _parts_content(channel, repository) == content
    assert all(message_id not in channel.messages for message_id in old_ids[missing_position:])
    assert all(old_messages[message_id].deletes == 1 for message_id in old_ids[missing_position + 1:])
    assert channel.fetch_calls == before_fetches
    assert all(cog._document_message_index[(5001, message_id)] == "tools" for message_id in new_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [0, 1, 2])
async def test_each_chunk_tampering_is_restored_and_own_echo_is_ignored(position, monkeypatch):
    content = "A" * 2000 + "B" * 2000 + "C"
    cog, repository, channel, document = _sync_cog(monkeypatch, content=content)
    await cog.sync_document(document)
    target = channel.messages[repository.message_ids()[position]]
    before_edits = len(target.edits)
    target.content = "unreviewed content"

    await cog.on_raw_message_edit(_edit_payload(target.content, message_id=target.id))
    before_fetches = list(channel.fetch_calls)
    await cog.on_raw_message_edit(_edit_payload(target.content, message_id=target.id))

    assert _parts_content(channel, repository) == content
    assert len(target.edits) == before_edits + 1
    assert channel.fetch_calls == before_fetches
    assert target.edits[-1]["allowed_mentions"].to_dict() == {"parse": []}


@pytest.mark.asyncio
async def test_foreign_author_in_later_chunk_stops_all_edits_and_deletes(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B")
    await cog.sync_document(document)
    ids = repository.message_ids()
    channel.messages[ids[1]].author.id = 123
    before = {message_id: (len(message.edits), message.deletes) for message_id, message in channel.messages.items()}
    repository.document.update(version=2, content_md="# 縮短\n")

    with pytest.raises(ValueError, match="不是 Bot"):
        await cog.sync_document(document)

    assert repository.message_ids() == ids
    assert {message_id: (len(message.edits), message.deletes) for message_id, message in channel.messages.items()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500])
async def test_second_chunk_send_failure_keeps_progress_for_retry_after_cog_reload(status, monkeypatch):
    content = "A" * 2000 + "B" * 2000 + "C"
    cog, repository, channel, document = _sync_cog(monkeypatch, content=content)
    original_send = channel.send
    attempts = 0

    async def fail_second(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise _http_error(status)
        return await original_send(**kwargs)

    channel.send = fail_second
    with pytest.raises(discord.HTTPException):
        await cog.sync_document(document)
    partial_ids = repository.message_ids()
    assert len(partial_ids) == 2
    assert len(channel.sent) == 1
    reloaded = ResourceLibraryCog(cog.bot)
    reloaded._repositories[5001] = repository
    channel.send = original_send

    await reloaded.sync_document(document)

    assert repository.message_ids()[:2] == partial_ids
    assert len(channel.sent) == 2
    assert _parts_content(channel, repository) == content
    assert not cog._pending_messages


@pytest.mark.asyncio
async def test_later_edit_failure_does_not_repeat_already_successful_edits(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B")
    await cog.sync_document(document)
    first, second = [channel.messages[message_id] for message_id in repository.message_ids()]
    repository.document.update(version=2, content_md="X" * 2000 + "Y")
    original_edit = second.edit
    second.edit = AsyncMock(side_effect=_http_error(500))

    with pytest.raises(discord.HTTPException):
        await cog.sync_document(document)
    first_edits = len(first.edits)
    second.edit = original_edit
    await cog.sync_document(document)

    assert len(first.edits) == first_edits
    assert _parts_content(channel, repository) == repository.document["content_md"]
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_later_delete_failure_checkpoints_tail_removal_and_retry_converges(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B" * 2000 + "C")
    await cog.sync_document(document)
    ids = repository.message_ids()
    second, third = channel.messages[ids[1]], channel.messages[ids[2]]
    original_delete = second.delete
    second.delete = AsyncMock(side_effect=_http_error(500))
    repository.document.update(version=2, content_md="# 短文件\n")

    with pytest.raises(discord.HTTPException):
        await cog.sync_document(document)
    assert repository.message_ids() == ids[:2]
    assert third.deletes == 1
    second.delete = original_delete
    await cog.sync_document(document)

    assert repository.message_ids() == ids[:1]
    assert third.deletes == 1
    assert second.deletes == 1
    assert _parts_content(channel, repository) == "# 短文件\n"


@pytest.mark.asyncio
async def test_failed_send_checkpoint_cleans_known_message_before_retry(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B")
    original_store = repository.set_message_ids
    repository.set_message_ids = AsyncMock(side_effect=OSError("test database unavailable"))

    with pytest.raises(OSError):
        await cog.sync_document(document)

    assert repository.message_ids() == [8001]
    assert set(channel.messages) == {8001}
    assert not cog._pending_messages
    repository.set_message_ids = original_store
    await cog.sync_document(document)
    assert _parts_content(channel, repository) == document["content_md"]
    assert len(channel.sent) == 2
    assert 9001 not in channel.messages


@pytest.mark.asyncio
async def test_failed_checkpoint_and_cleanup_recover_known_receipt_without_resending(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B")
    original_store = repository.set_message_ids
    original_send = channel.send
    repository.set_message_ids = AsyncMock(side_effect=OSError("test database unavailable"))

    async def send_with_failed_cleanup(**kwargs):
        message = await original_send(**kwargs)
        message.delete = AsyncMock(side_effect=_http_error(500))
        return message

    channel.send = send_with_failed_cleanup
    with pytest.raises(OSError):
        await cog.sync_document(document)
    assert repository.message_ids() == [8001]
    assert len(cog._pending_messages) == 1
    repository.set_message_ids = original_store
    channel.send = original_send

    await cog.sync_document(document)

    assert repository.message_ids() == [8001, 9001]
    assert len(channel.sent) == 1
    assert not cog._pending_messages
    assert _parts_content(channel, repository) == document["content_md"]


@pytest.mark.asyncio
async def test_committed_checkpoint_with_lost_ack_is_verified_without_cleanup_or_resend(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B")
    original_store = repository.set_message_ids

    async def committed_then_failed(*args, **kwargs):
        await original_store(*args, **kwargs)
        raise OSError("test commit acknowledgement lost")

    repository.set_message_ids = committed_then_failed

    await cog.sync_document(document)
    await cog.sync_document(document)

    assert repository.message_ids() == [8001, 9001]
    assert channel.messages[9001].deletes == 0
    assert len(channel.sent) == 1
    assert not cog._pending_messages


@pytest.mark.asyncio
async def test_cancellation_after_known_send_finishes_checkpoint_before_unlocking(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B")
    original_store = repository.set_message_ids
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_store(*args, **kwargs):
        entered.set()
        await release.wait()
        await original_store(*args, **kwargs)

    repository.set_message_ids = blocked_store
    task = asyncio.create_task(cog.sync_document(document))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert cog._get_sync_lock((5001, "tools")).locked()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repository.message_ids() == [8001, 9001]
    assert not cog._get_sync_lock((5001, "tools")).locked()
    repository.set_message_ids = original_store

    await cog.sync_document(document)

    assert len(channel.sent) == 1
    assert _parts_content(channel, repository) == document["content_md"]


@pytest.mark.asyncio
async def test_partial_send_429_keeps_ids_and_blocks_all_chunks_until_cooldown(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B" * 2000 + "C")
    clock = [100.0]
    monkeypatch.setattr("bot.cogs.resource_library.monotonic", lambda: clock[0])
    original_send = channel.send
    attempts = 0

    async def rate_limit_second(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise discord.HTTPException(SimpleNamespace(status=429, reason="limited", headers={"Retry-After": "10"}), "limited")
        return await original_send(**kwargs)

    channel.send = rate_limit_second
    with pytest.raises(discord.HTTPException):
        await cog.sync_document(document)
    fetches = list(channel.fetch_calls)
    with pytest.raises(ValueError, match="退避"):
        await cog.sync_document(document)
    await cog.on_raw_message_edit(_edit_payload("tampered", message_id=9001))
    assert channel.fetch_calls == fetches
    assert repository.message_ids() == [8001, 9001]
    clock[0] = 110.0
    channel.send = original_send

    await cog.sync_document(document)

    assert len(channel.sent) == 2
    assert _parts_content(channel, repository) == document["content_md"]


@pytest.mark.asyncio
async def test_bot_tail_delete_event_queued_before_checkpoint_does_not_recreate_it(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B")
    await cog.sync_document(document)
    tail = channel.messages[repository.message_ids()[1]]
    original_delete = tail.delete
    events = []

    async def delete_with_raw_event():
        await original_delete()
        events.append(asyncio.create_task(cog.on_raw_message_delete(SimpleNamespace(guild_id=5001, message_id=tail.id))))
        await asyncio.sleep(0)

    tail.delete = delete_with_raw_event
    repository.document.update(version=2, content_md="# 短文件\n")
    await cog.sync_document(document)
    await asyncio.gather(*events)

    assert repository.message_ids() == [8001]
    assert set(channel.messages) == {8001}
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_expected_approval_version_cannot_report_newer_version_as_its_own_publish(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch)
    repository.document.update(version=2, content_md="# 新版本\n")

    with pytest.raises(ValueError, match="核准版本已被更新"):
        await cog.sync_document(document, expected_version=1)

    assert channel.fetch_calls == []
    assert channel.sent == []


@pytest.mark.asyncio
async def test_http_accepts_maximum_escaped_unicode_document_above_old_request_size(tmp_path, monkeypatch):
    cog, repository, _, _ = await _api_cog(tmp_path, monkeypatch)
    cog.bot.logger = SimpleNamespace(info=lambda *args: None)
    monkeypatch.setenv("RESOURCE_STANDALONE_ENABLED", "0")
    monkeypatch.setenv("RESOURCE_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("RESOURCE_WEB_PORT", "0")
    content_md = "😀" * 10000
    body = json.dumps({"base_version": 1, "content_md": content_md}, ensure_ascii=True)
    assert 64 * 1024 < len(body.encode("utf-8")) < 256 * 1024
    await cog._start_web_server()
    try:
        site = next(iter(cog._web_runner.sites))
        port = site._server.sockets[0].getsockname()[1]
        async with ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/api/guilds/5001/resources/tools/preview",
                data=body,
                headers={"Content-Type": "application/json"},
            ) as response:
                assert response.status == 200
                payload = await response.json()
                assert payload["content_md"] == content_md
                assert payload["message_count"] == 10
    finally:
        await cog._web_runner.cleanup()
    assert (await repository.get_document("tools"))["version"] == 1


async def _real_sync_cog(tmp_path, monkeypatch, content):
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    repository = ResourceRepository(tmp_path / "resources.db")
    await repository.initialize()
    await repository.configure(
        [{"slug": "tools", "title": "做備審的好工具", "channel_id": 7001}], 8100, 8200,
    )
    draft = await repository.create_draft("tools", 77, 1, content)
    await repository.set_review_thread(draft["id"], 4001, 4002)
    await repository.decide_draft(draft["id"], 88, True)
    await repository.set_message_id("tools", 8001)
    guild = FakeGuild(5001, None)
    channel = FakeChannel(guild, FakeMessage(8001, 999, content="# 舊內容\n"))
    guild.channel = channel
    cog = ResourceLibraryCog(FakeBot(guild))
    cog._repositories[5001] = repository
    document = await repository.get_document("tools")
    document["guild_id"] = guild.id
    return cog, repository, guild, channel, document


@pytest.mark.asyncio
async def test_partial_publication_resumes_from_reopened_real_database(tmp_path, monkeypatch):
    content = "A" * 2000 + "B" * 2000 + "C"
    cog, repository, _, channel, document = await _real_sync_cog(tmp_path, monkeypatch, content)
    original_send = channel.send
    attempts = 0

    async def second_send_fails(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise _http_error(500)
        return await original_send(**kwargs)

    channel.send = second_send_fails
    with pytest.raises(discord.HTTPException):
        await cog.sync_document(document)
    partial = await repository.get_document("tools")
    assert partial["message_ids"] == [8001, 9001]
    assert partial["content_md"] == content
    assert partial["version"] == 2
    reopened = ResourceRepository(repository.db_path)
    await reopened.initialize()
    reloaded = ResourceLibraryCog(cog.bot)
    reloaded._repositories[5001] = reopened
    channel.send = original_send

    await reloaded.sync_document(document)

    current = await reopened.get_document("tools")
    assert current["message_ids"][:2] == partial["message_ids"]
    assert len(channel.sent) == 2
    assert "".join(channel.messages[message_id].content for message_id in current["message_ids"]) == content
    assert current["updated_by"] == partial["updated_by"]
    assert current["updated_at"] == partial["updated_at"]


@pytest.mark.asyncio
async def test_approval_waits_for_existing_multipart_publication_lock(tmp_path, monkeypatch):
    old_content = "A" * 2000 + "B"
    new_content = "X" * 2000 + "Y"
    cog, repository, guild, channel, document = await _real_sync_cog(tmp_path, monkeypatch, old_content)
    draft = await repository.create_draft("tools", 77, 2, new_content)
    await repository.set_review_thread(draft["id"], 4101, 4102)
    guild.fetch_member = AsyncMock(return_value=SimpleNamespace(
        id=88, guild_permissions=SimpleNamespace(administrator=False, manage_guild=True),
    ))
    interaction = SimpleNamespace(
        guild=guild, guild_id=guild.id, channel=SimpleNamespace(), channel_id=4101,
        message=FakeMessage(4102, 999), user=SimpleNamespace(id=88),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    waiting = asyncio.Event()
    original_edit = channel.messages[8001].edit
    original_lock = cog._get_sync_lock
    repository.decide_draft = AsyncMock(wraps=repository.decide_draft)
    approval = None

    async def blocked_edit(**kwargs):
        entered.set()
        await release.wait()
        return await original_edit(**kwargs)

    def observed_lock(key):
        if asyncio.current_task() is approval:
            waiting.set()
        return original_lock(key)

    channel.messages[8001].edit = blocked_edit
    cog._get_sync_lock = observed_lock
    first = asyncio.create_task(cog.sync_document(document))
    await entered.wait()
    approval = asyncio.create_task(cog.resolve_review(interaction, approve=True))
    await waiting.wait()
    assert (await repository.get_document("tools"))["version"] == 2
    repository.decide_draft.assert_not_awaited()
    assert not approval.done()
    release.set()
    await asyncio.gather(first, approval)
    repository.decide_draft.assert_awaited_once()

    current = await repository.get_document("tools")
    assert current["version"] == 3
    assert "".join(channel.messages[message_id].content for message_id in current["message_ids"]) == new_content
    assert len(channel.sent) == 1
    assert "已同意並發布" in interaction.message.edits[0]["embed"].description


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["revoked", "unavailable"])
async def test_review_rechecks_permissions_after_waiting_for_publication_lock(failure, tmp_path, monkeypatch):
    cog, repository, guild, _ = await _api_cog(tmp_path, monkeypatch)
    before = await repository.get_document("tools")
    draft = await repository.create_draft("tools", 77, 1, "A" * 2000 + "B")
    await repository.set_review_thread(draft["id"], 4001, 4002)
    permitted = SimpleNamespace(id=88, guild_permissions=SimpleNamespace(administrator=False, manage_guild=True))
    revoked = SimpleNamespace(id=88, guild_permissions=SimpleNamespace(administrator=False, manage_guild=False))
    guild.fetch_member = AsyncMock(side_effect=[permitted, revoked if failure == "revoked" else _http_error(500)])
    interaction = SimpleNamespace(
        guild=guild, guild_id=guild.id, channel=SimpleNamespace(), channel_id=4001,
        message=FakeMessage(4002, 999), user=SimpleNamespace(id=88),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )
    original_lock = cog._get_sync_lock
    lock = original_lock((guild.id, "tools"))
    waiting = asyncio.Event()

    def observed_lock(key):
        waiting.set()
        return original_lock(key)

    cog._get_sync_lock = observed_lock
    cog.sync_document = AsyncMock()
    async with lock:
        review = asyncio.create_task(cog.resolve_review(interaction, approve=True))
        await waiting.wait()
        assert guild.fetch_member.await_count == 1
        assert not review.done()
    await review

    assert guild.fetch_member.await_count == 2
    assert (await repository.get_draft(draft["id"]))["status"] == "pending"
    assert await repository.get_document("tools") == before
    assert interaction.message.edits == []
    cog.sync_document.assert_not_awaited()
    assert "權限" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_pending_known_send_is_deleted_when_latest_document_no_longer_needs_it(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B")
    original_store = repository.set_message_ids
    original_send = channel.send
    receipt = []

    async def send_with_failed_cleanup(**kwargs):
        message = await original_send(**kwargs)
        receipt.append((message, message.delete))
        message.delete = AsyncMock(side_effect=_http_error(500))
        return message

    channel.send = send_with_failed_cleanup
    repository.set_message_ids = AsyncMock(side_effect=OSError("test database unavailable"))
    with pytest.raises(OSError):
        await cog.sync_document(document)
    message, original_delete = receipt[0]
    assert cog._pending_messages
    repository.document.update(version=2, content_md="# 較短的新版本\n")
    repository.set_message_ids = original_store
    message.delete = original_delete

    await cog.sync_document(document)

    assert repository.message_ids() == [8001]
    assert set(channel.messages) == {8001}
    assert channel.messages[8001].content == repository.document["content_md"]
    assert message.deletes == 1
    assert len(channel.sent) == 1
    assert not cog._pending_messages


@pytest.mark.asyncio
async def test_version_change_during_send_rejects_stale_id_checkpoint_and_cleans_receipt(monkeypatch):
    cog, repository, channel, document = _sync_cog(monkeypatch, content="A" * 2000 + "B")
    original_send = channel.send

    async def send_after_external_update(**kwargs):
        message = await original_send(**kwargs)
        repository.document.update(version=2, content_md="# 最新正式版本\n")
        return message

    channel.send = send_after_external_update
    with pytest.raises(ValueError, match="已變更"):
        await cog.sync_document(document)
    assert repository.message_ids() == [8001]
    assert set(channel.messages) == {8001}
    assert not cog._pending_messages

    await cog.sync_document(document)

    assert channel.messages[8001].content == "# 最新正式版本\n"
    assert repository.document["version"] == 2
    assert len(channel.sent) == 1
