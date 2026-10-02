import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from aiohttp import web

from bot.cogs.resource_library import RESOURCE_DOCUMENTS, ResourceLibraryCog
from bot.utils.resource_library_markdown import markdown_diff
from database.resource_library import ResourceRepository


class FakeRepository:
    def __init__(self, document=None):
        self.document = document
        self.get_calls = []
        self.saved_message_ids = []

    async def get_document(self, slug):
        self.get_calls.append(slug)
        if self.document is None or self.document["slug"] != slug:
            return None
        return dict(self.document)

    async def set_message_id(self, slug, message_id):
        self.saved_message_ids.append((slug, message_id))
        self.document["message_id"] = message_id


class FakeMessage:
    def __init__(self, message_id, author_id, embeds=None, content=""):
        self.id = message_id
        self.author = SimpleNamespace(id=author_id)
        self.embeds = embeds or []
        self.content = content
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]
        if "embed" in kwargs:
            self.embeds = [] if kwargs["embed"] is None else [kwargs["embed"]]
        return self


class FakeChannel:
    def __init__(self, guild, message=None):
        self.guild = guild
        self.message = message
        self.fetch_calls = []
        self.sent = []

    async def fetch_message(self, message_id):
        self.fetch_calls.append(message_id)
        if self.message is None or self.message.id != message_id:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown message")
        return self.message

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        self.message = FakeMessage(9000 + len(self.sent), author_id=999, content=kwargs["content"])
        return self.message


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
        ("x" * 2001, 413),
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
