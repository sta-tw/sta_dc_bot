from __future__ import annotations

import discord
from discord.ext import commands


class Starboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._posted_map: dict[int, int] = {}

    def _is_target_emoji(self, emoji: str) -> bool:
        target = (self.bot.settings.starboard_emoji or "⭐").strip()
        return emoji == target

    async def _count_human_reactors(self, reaction: discord.Reaction) -> int:
        count = 0
        async for user in reaction.users(limit=None):
            if not user.bot:
                count += 1
        return count

    async def _build_reply_preview(self, message: discord.Message) -> str:
        if message.reference is None or message.reference.message_id is None:
            return ""

        referenced: discord.Message | None = message.reference.resolved if isinstance(message.reference.resolved, discord.Message) else None
        if referenced is None and isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            try:
                referenced = await message.channel.fetch_message(message.reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return ""

        if referenced is None:
            return ""

        preview_text = (referenced.content or "").strip()
        if len(preview_text) > 90:
            preview_text = f"{preview_text[:90].rstrip()}…"
        return f"> Replying to {referenced.author.display_name}:\n> {preview_text}\n"

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return

        starboard_channel_id = self.bot.settings.starboard_channel_id
        if not starboard_channel_id:
            return

        if payload.channel_id == starboard_channel_id:
            return

        if not self._is_target_emoji(str(payload.emoji)):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = guild.get_channel(payload.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(payload.channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        target_reaction: discord.Reaction | None = None
        for reaction in message.reactions:
            if self._is_target_emoji(str(reaction.emoji)):
                target_reaction = reaction
                break

        if target_reaction is None:
            return

        star_count = await self._count_human_reactors(target_reaction)
        if star_count < self.bot.settings.starboard_min_reactions:
            return

        target_channel = guild.get_channel(starboard_channel_id)
        if target_channel is None:
            try:
                target_channel = await self.bot.fetch_channel(starboard_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                self.bot.logger.warning("找不到爆言頻道 %s", starboard_channel_id)
                return

        if not isinstance(target_channel, discord.TextChannel):
            self.bot.logger.warning("爆言頻道 %s 不是文字頻道", starboard_channel_id)
            return

        content_text = message.content.strip() if message.content else ""
        reply_preview = await self._build_reply_preview(message)
        description_parts: list[str] = []
        if reply_preview:
            description_parts.append(reply_preview)
        if content_text:
            description_parts.append(content_text)

        embed_description = "\n\n".join(description_parts) if description_parts else None

        embed = discord.Embed(
            description=embed_description,
            color=discord.Color.gold(),
            timestamp=message.created_at,
        )
        embed.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
            url=message.jump_url,
        )

        image_attachment = next(
            (att for att in message.attachments if (att.content_type or "").startswith("image/")),
            None,
        )
        if image_attachment is not None:
            embed.set_image(url=image_attachment.url)

        post_content = f"{self.bot.settings.starboard_emoji} **{star_count}** in {message.channel.mention}"

        posted_message_id = self._posted_map.get(message.id)
        if posted_message_id:
            try:
                posted_message = await target_channel.fetch_message(posted_message_id)
                await posted_message.edit(content=post_content, embed=embed)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                self._posted_map.pop(message.id, None)

        try:
            sent = await target_channel.send(content=post_content, embed=embed)
            self._posted_map[message.id] = sent.id
        except discord.HTTPException as exc:
            self.bot.logger.warning("發送爆言失敗(message=%s): %s", message.id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Starboard(bot))
