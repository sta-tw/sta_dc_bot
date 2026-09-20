from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.utils import instagram_feed as feed
from bot.utils.config_paths import ConfigPaths
from utils.instagram_feed_ui import InstagramFeedRoleView, setup_persistent_views_instagram


DEFAULT_POLL_MINUTES = 5
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True, slots=True)
class InstagramRuntimeConfig:
    enabled: bool
    source_url: str
    source_kind: str
    guild_id: int
    channel_id: int
    role_id: int
    poll_minutes: int
    timeout_seconds: float
    user_agent: str
    retention_days: int
    mention_enabled: bool
    state_path: Path

    @property
    def feed_url(self) -> str:
        """Backward-compatible alias for older local test/config code."""

        return self.source_url

    @classmethod
    def from_bot(cls, bot: commands.Bot) -> "InstagramRuntimeConfig":
        settings = getattr(bot, "settings", None)
        configured = getattr(settings, "instagram_feed", None)

        profile_url = _env_text("INSTAGRAM_PROFILE_URL") or str(
            getattr(configured, "profile_url", "") or ""
        ).strip()
        legacy_feed_url = _env_text("INSTAGRAM_FEED_URL") or str(
            getattr(configured, "feed_url", "") or ""
        ).strip()
        source_url = feed.normalise_instagram_profile_url(profile_url) if profile_url else legacy_feed_url
        source_kind = "profile" if feed.is_valid_instagram_profile_url(source_url) else "feed"
        guild_id = _int_value(getattr(configured, "guild_id", 0), 0)
        channel_id = _int_value(getattr(configured, "channel_id", 0), 0)
        role_id = _int_value(getattr(configured, "role_id", 0), 0)
        poll_minutes = max(
            1,
            _int_value(getattr(configured, "poll_minutes", DEFAULT_POLL_MINUTES), DEFAULT_POLL_MINUTES),
        )
        timeout_seconds = max(5.0, _float_value("INSTAGRAM_FEED_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
        retention_days = max(1, _int_value(os.getenv("INSTAGRAM_SEEN_RETENTION_DAYS"), DEFAULT_RETENTION_DAYS))
        user_agent = _env_text("INSTAGRAM_FEED_USER_AGENT") or feed.DEFAULT_USER_AGENT
        mention_enabled = _bool_value(os.getenv("INSTAGRAM_MENTION_ENABLED"), True)

        scope_id = guild_id or channel_id or _int_value(getattr(settings, "guild_id", 0), 0)
        state_path = ConfigPaths.instagram_feed_state(scope_id)
        enabled = bool(getattr(configured, "enabled", False)) and bool(
            source_url and channel_id and role_id and feed.is_valid_feed_url(source_url)
        )

        return cls(
            enabled=enabled,
            source_url=source_url,
            source_kind=source_kind,
            guild_id=guild_id,
            channel_id=channel_id,
            role_id=role_id,
            poll_minutes=poll_minutes,
            timeout_seconds=timeout_seconds,
            user_agent=user_agent,
            retention_days=retention_days,
            mention_enabled=mention_enabled,
            state_path=state_path,
        )


class InstagramFeed(commands.Cog):
    """Poll a configured public Instagram profile and notify Discord."""

    def __init__(self, bot: commands.Bot, *, start_task: bool = True):
        self.bot = bot
        self.config = InstagramRuntimeConfig.from_bot(bot)
        self.state_path = self.config.state_path
        self.state = feed.load_state(self.state_path)
        self._logger = getattr(bot, "logger", logging.getLogger(__name__))
        self._auto_start_task = start_task
        self._task_started = False

        if start_task:
            self._sync_poll_task()
            if not self.config.enabled:
                self._logger.info(
                    "[InstagramFeed] 功能未啟用；請在伺服器使用 /instagram_setup 設定 Instagram 帳號、通知頻道與通知身分組"
                )

    def cog_unload(self) -> None:
        if self.poll_task.is_running():
            self.poll_task.cancel()

    @tasks.loop(minutes=DEFAULT_POLL_MINUTES)
    async def poll_task(self) -> None:
        await self._poll_once()

    @poll_task.before_loop
    async def before_poll_task(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="instagram_setup",
        description="設定 Instagram 貼文通知的公開帳號、頻道與身分組",
    )
    @app_commands.describe(
        profile_url="Instagram 公開個人頁面網址或帳號名稱",
        channel="發送 Instagram 通知的文字頻道",
        role="通知時要提及、也可讓成員領取的身分組",
    )
    async def instagram_setup(
        self,
        interaction: discord.Interaction,
        profile_url: str,
        channel: discord.TextChannel,
        role: discord.Role,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("請在伺服器內使用此指令。", ephemeral=True)
            return
        if not self._can_run_test(interaction):
            await interaction.response.send_message(
                "需要伺服器管理權限或客服身分組。",
                ephemeral=True,
            )
            return

        normalised_profile_url = feed.normalise_instagram_profile_url(profile_url)
        if not feed.is_valid_instagram_profile_url(normalised_profile_url):
            await interaction.response.send_message(
                "請提供有效的 Instagram 公開個人頁面網址或帳號名稱。",
                ephemeral=True,
            )
            return

        if _env_text("INSTAGRAM_PROFILE_URL"):
            await interaction.response.send_message(
                "目前設定了 INSTAGRAM_PROFILE_URL 環境變數，會覆蓋 Slash Command 設定；請先清除後再使用此指令。",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        if getattr(getattr(channel, "guild", None), "id", None) != guild_id:
            await interaction.response.send_message(
                "通知頻道必須屬於目前的伺服器。",
                ephemeral=True,
            )
            return
        if getattr(getattr(role, "guild", None), "id", None) != guild_id:
            await interaction.response.send_message(
                "通知身分組必須屬於目前的伺服器。",
                ephemeral=True,
            )
            return
        if bool(getattr(role, "managed", False)) or _role_is_default(role):
            await interaction.response.send_message(
                "不能使用 @everyone 或由整合服務管理的身分組。",
                ephemeral=True,
            )
            return

        settings_path = getattr(self.bot, "settings_path", None)
        if settings_path is None:
            settings_path = getattr(getattr(self.bot, "settings", None), "config_path", None)
        if settings_path is None:
            await interaction.response.send_message(
                "找不到 Bot 設定檔路徑，無法儲存 Instagram 設定。",
                ephemeral=True,
            )
            return

        try:
            feed.save_instagram_configuration(
                settings_path,
                enabled=True,
                profile_url=normalised_profile_url,
                guild_id=guild_id,
                channel_id=channel.id,
                role_id=role.id,
            )
        except (OSError, TypeError, ValueError) as exc:
            self._logger.warning("[InstagramFeed] 儲存 Slash Command 設定失敗：%s", exc)
            await interaction.response.send_message(
                f"儲存 Instagram 設定失敗：{exc}",
                ephemeral=True,
            )
            return

        configured = getattr(getattr(self.bot, "settings", None), "instagram_feed", None)
        if configured is None:
            await interaction.response.send_message(
                "設定已寫入檔案，但目前執行中的 Bot 缺少 Instagram 設定物件，請重啟 Bot。",
                ephemeral=True,
            )
            return

        configured.enabled = True
        configured.profile_url = normalised_profile_url
        configured.guild_id = guild_id
        configured.channel_id = channel.id
        configured.role_id = role.id
        self._refresh_runtime_config()
        setup_persistent_views_instagram(self.bot)

        await interaction.response.send_message(
            "Instagram 通知設定完成："
            f"\n帳號：{normalised_profile_url}"
            f"\n頻道：{channel.mention}"
            f"\n身分組：{role.mention}"
            "\n已啟用輪詢（目前間隔預設為 5 分鐘）。"
            "\n接著可使用 /instagram_role_button 建立獨立的領取身分組面板。",
            ephemeral=True,
        )

    @app_commands.command(
        name="instagram_role_button",
        description="在目前執行指令的頻道建立獨立的領取身分組面板",
    )
    async def instagram_role_button(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("請在伺服器內使用此指令。", ephemeral=True)
            return
        if not self._can_run_test(interaction):
            await interaction.response.send_message(
                "需要伺服器管理權限或客服身分組。",
                ephemeral=True,
            )
            return
        if not self.config.source_url or not self.config.channel_id or not self.config.role_id:
            await interaction.response.send_message(
                "Instagram 尚未完成設定，請先使用 /instagram_setup。",
                ephemeral=True,
            )
            return
        if self.config.guild_id and interaction.guild.id != self.config.guild_id:
            await interaction.response.send_message(
                "請在 Instagram 設定的伺服器內使用這個指令。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = getattr(interaction, "channel", None)
        if channel is None or not callable(getattr(channel, "send", None)):
            await interaction.followup.send("目前頻道無法建立身分組面板。", ephemeral=True)
            return

        panel_embed = discord.Embed(
            title="取得走在時代尖端身分組",
            description="點擊下方按鈕取得身分組，之後即可收到 Instagram 新貼文通知。",
            color=discord.Color.purple(),
        )
        try:
            await channel.send(
                embed=panel_embed,
                view=InstagramFeedRoleView(self.bot),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Bot 沒有權限在 Instagram 通知頻道建立身分組面板。",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            self._logger.warning("[InstagramFeed] 建立身分組面板失敗：%s", exc)
            await interaction.followup.send(
                "建立身分組面板失敗，請稍後再試。",
                ephemeral=True,
            )
            return

        setup_persistent_views_instagram(self.bot)
        await interaction.followup.send(
            f"已在 <#{self.config.channel_id}> 建立獨立的身分組領取面板。",
            ephemeral=True,
        )

    def _can_run_test(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        permissions = getattr(member, "guild_permissions", None)
        if getattr(permissions, "administrator", False) or getattr(permissions, "manage_guild", False):
            return True
        support_role_ids = set(
            getattr(getattr(self.bot, "settings", None), "support_role_ids", []) or []
        )
        return any(
            getattr(role, "id", None) in support_role_ids
            for role in getattr(member, "roles", [])
        )

    def _refresh_runtime_config(self) -> None:
        self.config = InstagramRuntimeConfig.from_bot(self.bot)
        self.state_path = self.config.state_path
        self.state = feed.load_state(self.state_path)
        self._sync_poll_task()

    def _sync_poll_task(self) -> None:
        if self.config.enabled:
            self.poll_task.change_interval(minutes=self.config.poll_minutes)
            if self._auto_start_task and not self.poll_task.is_running():
                self.poll_task.start()
                self._task_started = True
            return

        if self.poll_task.is_running():
            self.poll_task.cancel()
        self._task_started = False

    async def _poll_once(self) -> None:
        if not self.config.enabled:
            return

        channel = await self._get_notification_channel()
        if channel is None:
            self._logger.warning(
                "[InstagramFeed] 找不到通知頻道 %s，略過這次檢查",
                self.config.channel_id,
            )
            return

        try:
            posts = await self._fetch_posts()
        except Exception as exc:
            self._logger.warning("[InstagramFeed] 讀取公開 Instagram 頁面失敗：%s", exc)
            return

        if self.state.get("source_url") not in {"", self.config.source_url}:
            self.state = feed.empty_state(self.config.source_url)
        self.state["source_url"] = self.config.source_url

        if not self.state.get("initialized"):
            await self._mark_snapshot_seen(posts)
            return

        seen = self.state.get("seen", {})
        if not isinstance(seen, dict):
            seen = {}
            self.state["seen"] = seen

        new_posts = [post for post in posts if post.source_key not in seen]
        new_posts.sort(key=_post_sort_key)

        for post in new_posts:
            if not await self._notify(channel, post):
                break

            seen[post.source_key] = _now_iso()
            self.state["watermark"] = _now_iso()
            feed.prune_seen(self.state, retention_days=self.config.retention_days)
            await self._save_state()

    async def _fetch_posts(self) -> list[feed.InstagramPost]:
        if self.config.source_kind == "profile":
            payload = await asyncio.to_thread(
                feed.fetch_public_profile,
                self.config.source_url,
                timeout=self.config.timeout_seconds,
                user_agent=self.config.user_agent,
            )
            return feed.parse_public_profile(payload, self.config.source_url)

        payload = await asyncio.to_thread(
            feed.fetch_feed,
            self.config.source_url,
            timeout=self.config.timeout_seconds,
            user_agent=self.config.user_agent,
        )
        return feed.parse_feed(payload)

    async def _mark_snapshot_seen(self, posts: list[feed.InstagramPost]) -> None:
        if self.state.get("source_url") not in {"", self.config.source_url}:
            self.state = feed.empty_state(self.config.source_url)
        self.state["source_url"] = self.config.source_url
        timestamp = _now_iso()
        seen = self.state.setdefault("seen", {})
        if isinstance(seen, dict):
            for post in posts:
                seen[post.source_key] = timestamp
        self.state["initialized"] = True
        self.state["watermark"] = timestamp
        feed.prune_seen(self.state, retention_days=self.config.retention_days)
        await self._save_state()

    async def _get_notification_channel(self):
        channel = self.bot.get_channel(self.config.channel_id)
        if channel is not None:
            return channel

        fetch_channel = getattr(self.bot, "fetch_channel", None)
        if not callable(fetch_channel):
            return None
        try:
            return await fetch_channel(self.config.channel_id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            self._logger.warning("[InstagramFeed] 取得通知頻道失敗：%s", exc)
            return None

    def _build_post_embed(self, post: feed.InstagramPost) -> discord.Embed:
        preview_text = post.preview_text or post.title or "點擊標題查看 Instagram 貼文。"
        embed = discord.Embed(
            title=(post.title or "Instagram 新貼文")[:256],
            url=post.link,
            description=preview_text[:4096],
            color=discord.Color.purple(),
        )
        if post.thumbnail_url:
            embed.set_image(url=post.thumbnail_url)
        return embed

    async def _notify(self, channel, post: feed.InstagramPost) -> bool:
        guild = getattr(channel, "guild", None)
        role = None
        if guild is not None and hasattr(guild, "get_role"):
            role = guild.get_role(self.config.role_id)

        if self.config.mention_enabled and role is None:
            self._logger.warning(
                "[InstagramFeed] 找不到通知身分組 %s，略過貼文 %s",
                self.config.role_id,
                post.link,
            )
            return False

        role_mention = role.mention if role is not None else f"<@&{self.config.role_id}>"
        message_text = "有新的 Instagram，趕快來按讚分享吧！"
        if self.config.mention_enabled:
            content = f"{role_mention} {message_text}\n{post.link}"
            allowed_roles = [role] if role is not None else False
        else:
            content = f"{message_text}\n{post.link}"
            allowed_roles = False

        try:
            await channel.send(
                content=content,
                embed=self._build_post_embed(post),
                allowed_mentions=discord.AllowedMentions(
                    roles=allowed_roles,
                    users=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
        except discord.Forbidden:
            self._logger.warning("[InstagramFeed] 沒有權限在通知頻道發送貼文")
            return False
        except discord.HTTPException as exc:
            self._logger.warning("[InstagramFeed] 發送貼文通知失敗：%s", exc)
            return False
        except Exception:
            self._logger.exception("[InstagramFeed] 發送貼文通知時發生未預期錯誤")
            return False
        return True

    async def _save_state(self) -> None:
        try:
            await asyncio.to_thread(feed.save_state, self.state_path, self.state)
        except OSError:
            self._logger.exception("[InstagramFeed] 儲存貼文去重狀態失敗")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InstagramFeed(bot))


def _post_sort_key(post: feed.InstagramPost):
    if post.published_at is None:
        return (1, datetime.max.replace(tzinfo=timezone.utc), post.source_key)
    return (0, post.published_at, post.source_key)


def _env_text(name: str) -> str:
    return os.getenv(name, "").strip()


def _int_value(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool_value(value, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _role_is_default(role: discord.Role) -> bool:
    checker = getattr(role, "is_default", None)
    return bool(checker()) if callable(checker) else False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
