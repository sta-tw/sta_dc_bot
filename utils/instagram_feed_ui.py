from __future__ import annotations

import logging

import discord


INSTAGRAM_ROLE_BUTTON_CUSTOM_ID = "instagram_feed:claim_role"


def _instagram_config(bot):
    settings = getattr(bot, "settings", None)
    return getattr(settings, "instagram_feed", None)


def _configured_role_id(bot) -> int:
    config = _instagram_config(bot)
    try:
        return int(getattr(config, "role_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


class InstagramFeedRoleView(discord.ui.View):
    """Persistent button view for the configured Instagram notification role."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        button = discord.ui.Button(
            label="取得走在時代尖端身分組",
            style=discord.ButtonStyle.success,
            custom_id=INSTAGRAM_ROLE_BUTTON_CUSTOM_ID,
        )
        button.callback = self._claim_role
        self.add_item(button)

    async def _claim_role(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "這個按鈕只能在伺服器內使用。",
                ephemeral=True,
            )
            return

        config = _instagram_config(self.bot)
        target_guild_id = int(getattr(config, "guild_id", 0) or 0)
        if target_guild_id and interaction.guild.id != target_guild_id:
            await interaction.response.send_message(
                "這個身分組按鈕不適用於目前的伺服器。",
                ephemeral=True,
            )
            return

        role_id = _configured_role_id(self.bot)
        role = interaction.guild.get_role(role_id) if role_id else None
        if role is None:
            await interaction.response.send_message(
                "通知身分組尚未設定或已被刪除，請聯絡管理員。",
                ephemeral=True,
            )
            return

        member = interaction.user
        if role in getattr(member, "roles", ()):
            await interaction.response.send_message(
                f"你已經擁有 {role.mention}。",
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(role, reason="領取 Instagram 貼文通知身分組")
        except discord.Forbidden:
            await interaction.response.send_message(
                "機器人沒有權限賦予這個身分組，請聯絡管理員。",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger = getattr(self.bot, "logger", logging.getLogger(__name__))
            logger.exception("[InstagramFeed] 賦予通知身分組時發生 Discord API 錯誤")
            await interaction.response.send_message(
                "賦予身分組時發生 Discord 錯誤，請稍後再試。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"已取得 {role.mention}，之後會收到 Instagram 新貼文通知。",
            ephemeral=True,
        )


def setup_persistent_views_instagram(bot) -> bool:
    """Register the stable custom ID again whenever the bot starts."""

    if not _configured_role_id(bot):
        return False
    if getattr(bot, "_instagram_feed_view_registered", False):
        return True

    try:
        bot.add_view(InstagramFeedRoleView(bot))
        bot._instagram_feed_view_registered = True
        return True
    except Exception:
        logger = getattr(bot, "logger", logging.getLogger(__name__))
        logger.exception("[InstagramFeed] 註冊持久化身分組按鈕失敗")
        return False
