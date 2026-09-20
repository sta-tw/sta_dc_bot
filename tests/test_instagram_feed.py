from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.cogs import instagram_feed as instagram_cog
from bot.utils import instagram_feed as feed
from utils.instagram_feed_ui import (
    INSTAGRAM_ROLE_BUTTON_CUSTOM_ID,
    InstagramFeedRoleView,
    setup_persistent_views_instagram,
)


RSS_TEMPLATE = """\
<rss version="2.0">
  <channel>
    <title>Instagram test feed</title>
    {items}
  </channel>
</rss>
"""


def rss_item(
    link: str,
    title: str,
    published: str = "Mon, 21 Sep 2026 10:00:00 GMT",
    description: str = "",
) -> str:
    return f"""\
<item>
  <guid>{link}</guid>
  <link>{link}</link>
  <title>{title}</title>
  <description>{description}</description>
  <pubDate>{published}</pubDate>
  <media:content xmlns:media="http://search.yahoo.com/mrss/" url="https://cdn.example.test/image.jpg" />
</item>
"""


class FakeRole:
    def __init__(self, role_id: int, guild=None):
        self.id = role_id
        self.guild = guild
        self.mention = f"<@&{role_id}>"
        self.managed = False


class FakeTextChannel:
    def __init__(self, channel_id: int, guild):
        self.id = channel_id
        self.guild = guild
        self.mention = f"<#{channel_id}>"


class FakeMember:
    def __init__(self, *, manage_guild=False, administrator=False, roles=None):
        self.roles = list(roles or [])
        self.added = []
        self.guild_permissions = SimpleNamespace(
            manage_guild=manage_guild,
            administrator=administrator,
        )

    async def add_roles(self, role, *, reason=None):
        self.roles.append(role)
        self.added.append((role, reason))


class FakeGuild:
    def __init__(self, role: FakeRole):
        self.id = 123
        self.role = role
        role.guild = self

    def get_role(self, role_id):
        return self.role if role_id == self.role.id else None


class FakeChannel:
    def __init__(self, guild: FakeGuild):
        self.guild = guild
        self.messages = []

    async def send(self, **kwargs):
        self.messages.append(kwargs)


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))

    async def defer(self, *args, **kwargs):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class FakeInteraction:
    def __init__(self, guild, user, *, channel_id=None, channel=None):
        self.guild = guild
        self.user = user
        self.channel_id = channel_id
        self.channel = channel
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def make_bot(
    channel=None,
    *,
    enabled=True,
    profile_url="",
    feed_url="https://feed.example.test/ig.xml",
    settings_path=None,
):
    config = SimpleNamespace(
        enabled=enabled,
        profile_url=profile_url,
        feed_url=feed_url,
        guild_id=123,
        channel_id=456,
        role_id=789,
        poll_minutes=5,
    )
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            instagram_feed=config,
            guild_id=123,
            support_role_ids=[],
        ),
        logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
        ),
        get_channel=lambda channel_id: channel,
        add_view=lambda view: None,
    )
    if settings_path is not None:
        bot.settings_path = settings_path
    return bot


def make_notifier(tmp_path: Path, channel: FakeChannel):
    notifier = instagram_cog.InstagramFeed(make_bot(channel), start_task=False)
    notifier.state_path = tmp_path / "state.json"
    notifier.state = feed.load_state(notifier.state_path)
    return notifier


def test_config_wires_instagram_extension_and_defaults():
    config = json.loads(Path("config/bot.json").read_text(encoding="utf-8"))

    assert "bot.cogs.instagram_feed" in config["extensions"]
    assert "profile_url" in config["instagram_feed"]
    assert config["instagram_feed"]["poll_minutes"] == 5
    assert isinstance(config["instagram_feed"]["enabled"], bool)


def test_instagram_commands_are_registered():
    commands = {
        command.name: command
        for command in instagram_cog.InstagramFeed.__cog_app_commands__
    }
    assert {"instagram_setup", "instagram_role_button"} <= set(commands)
    assert [parameter.name for parameter in commands["instagram_setup"].parameters] == [
        "profile_url",
        "channel",
        "role",
    ]
    assert commands["instagram_setup"].default_permissions is None


def test_parse_rss_and_atom_feed():
    rss = RSS_TEMPLATE.format(
        items=rss_item("https://www.instagram.com/p/ABC/", "第一篇貼文")
    ).encode()
    posts = feed.parse_feed(rss)

    assert len(posts) == 1
    assert posts[0].link == "https://www.instagram.com/p/ABC/"
    assert posts[0].title == "第一篇貼文"
    assert posts[0].published_at == datetime(2026, 9, 21, 10, tzinfo=timezone.utc)
    assert posts[0].thumbnail_url == "https://cdn.example.test/image.jpg"

    atom = """\
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:instagram.example,2026:xyz</id>
    <link rel="alternate" href="https://www.instagram.com/p/XYZ/" />
    <title>Atom 貼文</title>
    <published>2026-09-21T12:00:00+00:00</published>
  </entry>
</feed>
""".encode()
    atom_posts = feed.parse_feed(atom)
    assert atom_posts[0].link == "https://www.instagram.com/p/XYZ/"
    assert atom_posts[0].published_at == datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


def test_parse_public_profile_html_and_profile_url_normalization():
    profile_html = """\
<html>
  <body>
    <a href="/p/ABC123/">貼文</a>
    <script>
      {"shortcode":"ABC123","taken_at_timestamp":1789984800,
       "accessibility_caption":"公開貼文預覽文字",
       "display_url":"https:\\/\\/cdn.example.test\\/image.jpg"}
      <a href="https://www.instagram.com/reel/REEL456/">影片</a>
    </script>
  </body>
</html>
"""

    posts = feed.parse_public_profile(
        profile_html,
        "https://www.instagram.com/example/",
    )

    assert [post.link for post in posts] == [
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/reel/REEL456/",
    ]
    assert posts[0].thumbnail_url == "https://cdn.example.test/image.jpg"
    assert posts[0].preview_text == "公開貼文預覽文字"
    assert feed.normalise_instagram_profile_url("example") == "https://www.instagram.com/example/"
    assert feed.is_valid_instagram_profile_url("https://www.instagram.com/example/")
    assert not feed.is_valid_instagram_profile_url("https://www.instagram.com/p/ABC123/")


def test_instagram_post_url_normalization():
    assert feed.normalise_instagram_post_url("/p/ABC123/") == "https://www.instagram.com/p/ABC123/"
    assert (
        feed.normalise_instagram_post_url("https://instagram.com/reel/REEL456?utm_source=test")
        == "https://www.instagram.com/reel/REEL456/"
    )
    assert feed.normalise_instagram_post_url("https://example.test/p/ABC123/") == ""
    assert not feed.is_valid_instagram_profile_url("https://www.instagram.com/not valid/")


def test_save_instagram_configuration_preserves_other_settings(tmp_path):
    path = tmp_path / "bot.json"
    path.write_text(
        json.dumps(
            {
                "guild_id": 123,
                "unrelated": {"keep": True},
                "instagram_feed": {
                    "poll_minutes": 7,
                    "legacy": "keep",
                },
            }
        ),
        encoding="utf-8",
    )

    feed.save_instagram_configuration(
        path,
        enabled=True,
        profile_url="https://www.instagram.com/example/",
        guild_id=123,
        channel_id=456,
        role_id=789,
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["guild_id"] == 123
    assert data["unrelated"] == {"keep": True}
    assert data["instagram_feed"] == {
        "poll_minutes": 7,
        "legacy": "keep",
        "enabled": True,
        "profile_url": "https://www.instagram.com/example/",
        "guild_id": 123,
        "channel_id": 456,
        "role_id": 789,
    }
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.asyncio
async def test_instagram_setup_command_persists_and_refreshes_runtime(monkeypatch, tmp_path):
    config_path = tmp_path / "bot.json"
    config_path.write_text(
        json.dumps(
            {
                "guild_id": 0,
                "instagram_feed": {"poll_minutes": 7, "legacy": "keep"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(instagram_cog.ConfigPaths, "INSTAGRAM_FEED_DIR", tmp_path / "states")

    bot = make_bot(settings_path=config_path)
    notifier = instagram_cog.InstagramFeed(bot, start_task=False)
    role = FakeRole(789)
    guild = FakeGuild(role)
    channel = FakeTextChannel(456, guild)
    interaction = FakeInteraction(guild, FakeMember(manage_guild=True))

    await instagram_cog.InstagramFeed.instagram_setup.callback(
        notifier,
        interaction,
        "spec_talent.tw",
        channel,
        role,
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["instagram_feed"]["enabled"] is True
    assert data["instagram_feed"]["profile_url"] == "https://www.instagram.com/spec_talent.tw/"
    assert data["instagram_feed"]["guild_id"] == 123
    assert data["instagram_feed"]["channel_id"] == 456
    assert data["instagram_feed"]["role_id"] == 789
    assert data["instagram_feed"]["poll_minutes"] == 7
    assert data["instagram_feed"]["legacy"] == "keep"
    assert notifier.config.enabled is True
    assert notifier.config.source_url == "https://www.instagram.com/spec_talent.tw/"
    assert notifier.state_path == tmp_path / "states" / "123" / "state.json"
    assert getattr(bot, "_instagram_feed_view_registered", False) is True
    assert "設定完成" in interaction.response.messages[-1][0][0]


@pytest.mark.asyncio
async def test_instagram_role_button_sends_panel_in_invocation_channel(tmp_path):
    role = FakeRole(789)
    guild = FakeGuild(role)
    channel = FakeChannel(guild)
    bot = make_bot(channel, profile_url="example", settings_path=tmp_path / "bot.json")
    notifier = instagram_cog.InstagramFeed(bot, start_task=False)
    interaction = FakeInteraction(
        guild,
        FakeMember(manage_guild=True),
        channel_id=999,
        channel=channel,
    )

    await instagram_cog.InstagramFeed.instagram_role_button.callback(notifier, interaction)

    assert interaction.response.deferred is True
    assert len(channel.messages) == 1
    message = channel.messages[0]
    assert "view" in message
    assert isinstance(message["view"], InstagramFeedRoleView)
    assert message["embed"].title == "取得走在時代尖端身分組"
    assert "建立獨立" in interaction.followup.messages[-1][0][0]


def test_feed_parser_rejects_invalid_items_and_urls():
    payload = """<rss><channel><item><title>沒有連結</title></item></channel></rss>""".encode()
    assert feed.parse_feed(payload) == []
    assert feed.is_valid_feed_url("https://example.test/feed.xml")
    assert feed.is_valid_feed_url("http://127.0.0.1:8080/feed")
    assert not feed.is_valid_feed_url("file:///tmp/feed.xml")
    assert not feed.is_valid_feed_url("ftp://example.test/feed")
    assert not feed.is_valid_feed_url("")

    with pytest.raises(Exception):
        feed.parse_feed(b"<rss>")


def test_state_round_trip_and_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    state = feed.empty_state("https://feed.example.test/ig.xml")
    state["initialized"] = True
    state["seen"] = {"https://www.instagram.com/p/ABC": "2026-09-21T10:00:00+00:00"}
    feed.save_state(path, state)

    assert feed.load_state(path) == state
    path.write_text("not json", encoding="utf-8")
    assert feed.load_state(path) == feed.empty_state()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.asyncio
async def test_first_run_seeds_and_new_posts_are_notified_once(monkeypatch, tmp_path):
    role = FakeRole(789)
    channel = FakeChannel(FakeGuild(role))
    notifier = make_notifier(tmp_path, channel)
    old_payload = RSS_TEMPLATE.format(
        items=rss_item("https://www.instagram.com/p/OLD/", "舊貼文")
    ).encode()
    new_payload = RSS_TEMPLATE.format(
        items=(
            rss_item("https://www.instagram.com/p/NEW/", "新貼文", "Mon, 21 Sep 2026 11:00:00 GMT")
            + rss_item("https://www.instagram.com/p/OLD/", "舊貼文")
        )
    ).encode()
    payloads = [old_payload, old_payload, new_payload]

    def fake_fetch(*args, **kwargs):
        return payloads.pop(0)

    monkeypatch.setattr(instagram_cog.feed, "fetch_feed", fake_fetch)

    await notifier._poll_once()
    await notifier._poll_once()
    assert channel.messages == []

    await notifier._poll_once()
    assert len(channel.messages) == 1
    assert "<@&789>" in channel.messages[0]["content"]
    assert "有新的 Instagram，趕快來按讚分享吧！" in channel.messages[0]["content"]
    assert "https://www.instagram.com/p/NEW/" in channel.messages[0]["content"]
    assert set(notifier.state["seen"]) == {
        "https://www.instagram.com/p/OLD",
        "https://www.instagram.com/p/NEW",
    }


@pytest.mark.asyncio
async def test_posts_are_sent_oldest_first(monkeypatch, tmp_path):
    role = FakeRole(789)
    channel = FakeChannel(FakeGuild(role))
    notifier = make_notifier(tmp_path, channel)
    notifier.state = feed.empty_state(notifier.config.feed_url)
    notifier.state["initialized"] = True

    payload = RSS_TEMPLATE.format(
        items=(
            rss_item(
                "https://www.instagram.com/p/LATE/",
                "晚",
                "Mon, 21 Sep 2026 12:00:00 GMT",
                description="晚貼文預覽",
            )
            + rss_item(
                "https://www.instagram.com/p/EARLY/",
                "早",
                "Mon, 21 Sep 2026 10:00:00 GMT",
                description="早貼文預覽",
            )
        )
    ).encode()
    monkeypatch.setattr(instagram_cog.feed, "fetch_feed", lambda *args, **kwargs: payload)

    await notifier._poll_once()

    assert [message["embed"].url for message in channel.messages] == [
        "https://www.instagram.com/p/EARLY/",
        "https://www.instagram.com/p/LATE/",
    ]
    assert all("view" not in message for message in channel.messages)
    assert channel.messages[0]["embed"].description == "早貼文預覽"
    assert channel.messages[0]["embed"].image.url == "https://cdn.example.test/image.jpg"


@pytest.mark.asyncio
async def test_profile_source_uses_public_page_scraper(monkeypatch, tmp_path):
    role = FakeRole(789)
    channel = FakeChannel(FakeGuild(role))
    notifier = instagram_cog.InstagramFeed(
        make_bot(channel, profile_url="example"),
        start_task=False,
    )
    notifier.state_path = tmp_path / "profile-state.json"
    notifier.state = feed.load_state(notifier.state_path)

    payloads = [
        b'<a href="/p/OLD123/">old</a>',
        b'<a href="/p/NEW123/">new</a><a href="/p/OLD123/">old</a>',
    ]
    monkeypatch.setattr(
        instagram_cog.feed,
        "fetch_public_profile",
        lambda *args, **kwargs: payloads.pop(0),
    )

    assert notifier.config.source_kind == "profile"
    assert notifier.config.source_url == "https://www.instagram.com/example/"
    await notifier._poll_once()
    await notifier._poll_once()

    assert len(channel.messages) == 1
    assert "NEW123" in channel.messages[0]["content"]


@pytest.mark.asyncio
async def test_fetch_failure_does_not_advance_state(monkeypatch, tmp_path):
    role = FakeRole(789)
    channel = FakeChannel(FakeGuild(role))
    notifier = make_notifier(tmp_path, channel)
    before = dict(notifier.state)

    def fail_fetch(*args, **kwargs):
        raise OSError("feed offline")

    monkeypatch.setattr(instagram_cog.feed, "fetch_feed", fail_fetch)
    await notifier._poll_once()

    assert channel.messages == []
    assert notifier.state == before


@pytest.mark.asyncio
async def test_persistent_view_claims_configured_role():
    role = FakeRole(789)
    guild = FakeGuild(role)
    member = FakeMember()
    bot = make_bot()
    view = InstagramFeedRoleView(bot)

    assert view.timeout is None
    assert view.children[0].custom_id == INSTAGRAM_ROLE_BUTTON_CUSTOM_ID

    interaction = FakeInteraction(guild, member)
    await view.children[0].callback(interaction)
    assert member.roles == [role]
    assert interaction.response.messages[-1][1]["ephemeral"] is True

    already_owned = FakeInteraction(guild, member)
    await view.children[0].callback(already_owned)
    assert len(member.roles) == 1
    assert "已經擁有" in already_owned.response.messages[-1][0][0]


@pytest.mark.asyncio
async def test_persistent_view_is_registered_once():
    bot = make_bot()
    registered = []
    bot.add_view = registered.append

    assert setup_persistent_views_instagram(bot) is True
    assert len(registered) == 1
    assert isinstance(registered[0], InstagramFeedRoleView)
