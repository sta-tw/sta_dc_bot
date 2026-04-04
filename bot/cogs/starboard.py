from __future__ import annotations

import asyncio
import io
import json
import re
from urllib import error, request

import discord
from discord.ext import commands


class Starboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._posted_map: dict[int, int] = {}
        self.quote_api_base_url = (self.bot.settings.quote_api_base_url or "").strip()
        self.quote_api_enabled = bool(self.quote_api_base_url)
        self.quote_api_timeout = self.bot.settings.quote_api_timeout
        self.quote_api_key = self.bot.settings.quote_api_key
        self.quote_api_user_agent = self.bot.settings.quote_api_user_agent

    def _is_target_emoji(self, emoji: str) -> bool:
        target = (self.bot.settings.starboard_emoji or "⭐").strip()
        return emoji == target

    async def _count_human_reactors(self, reaction: discord.Reaction) -> int:
        count = 0
        async for user in reaction.users(limit=None):
            if not user.bot:
                count += 1
        return count

    def _normalize_quote_text(self, message: discord.Message) -> str:
        text = (getattr(message, "clean_content", "") or message.content or "").strip()

        def _replace_user_mention(match: re.Match[str]) -> str:
            user_id = int(match.group(1))
            member = message.guild.get_member(user_id) if message.guild else None
            if member is not None:
                return f"@{member.display_name}"
            return "@使用者"

        text = re.sub(r"<@!?(\d+)>", _replace_user_mention, text)
        text = re.sub(r"<@&(\d+)>", "@身分組", text)
        text = re.sub(r"<#(\d+)>", "#頻道", text)
        text = re.sub(r"<a?:([A-Za-z0-9_]+):\d+>", r":\1:", text)
        text = re.sub(r"^\s*[-*#>]+\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _generate_quote_image_via_api(self, payload: dict) -> bytes | None:
        if not self.quote_api_enabled:
            return None
        endpoint = f"{self.quote_api_base_url}/api/generate"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "accept": "image/png,application/json;q=0.9,*/*;q=0.8",
            "origin": self.quote_api_base_url,
            "referer": f"{self.quote_api_base_url}/",
        }
        if self.quote_api_user_agent:
            headers["user-agent"] = self.quote_api_user_agent
        if self.quote_api_key:
            headers["authorization"] = f"Bearer {self.quote_api_key}"
        req = request.Request(
            endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.quote_api_timeout) as resp:
                if resp.status != 200:
                    self.bot.logger.warning("Quote API non-200: %s", resp.status)
                    return None
                return resp.read()
        except error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read()
                detail = raw.decode("utf-8", errors="ignore")[:400]
            except Exception:
                detail = ""
            self.bot.logger.warning("Quote API HTTPError: %s status=%s detail=%s", exc, exc.code, detail)
            return None
        except Exception as exc:
            self.bot.logger.warning("Quote API request failed: %s", exc)
            return None

    async def _build_quote_image_remote(self, message: discord.Message, content_text: str) -> discord.File | None:
        channel_name = getattr(message.channel, "name", "unknown")
        payload = {
            "discordId": str(message.author.id),
            "content": (content_text or "").strip()[:500],
            "displayName": message.author.display_name[:64],
            "attributionSuffix": f"in #{channel_name}"[:80],
            "template": "left-half",
        }
        image_bytes = await asyncio.to_thread(self._generate_quote_image_via_api, payload)
        if not image_bytes:
            return None
        return discord.File(fp=io.BytesIO(image_bytes), filename=f"starboard-{message.id}.png")

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

        content_text = self._normalize_quote_text(message)
        image_file = await self._build_quote_image_remote(message, content_text)
        post_content = f"{self.bot.settings.starboard_emoji} **{star_count}** in {message.channel.mention}"

        posted_message_id = self._posted_map.get(message.id)
        if posted_message_id:
            try:
                posted_message = await target_channel.fetch_message(posted_message_id)
                await posted_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            finally:
                self._posted_map.pop(message.id, None)

        try:
            if image_file is not None:
                sent = await target_channel.send(content=post_content, file=image_file)
            else:
                fallback = discord.Embed(
                    description=content_text or None,
                    color=discord.Color.gold(),
                    timestamp=message.created_at,
                )
                fallback.set_author(
                    name=message.author.display_name,
                    icon_url=message.author.display_avatar.url,
                    url=message.jump_url,
                )
                sent = await target_channel.send(content=post_content, embed=fallback)
            self._posted_map[message.id] = sent.id
        except discord.HTTPException as exc:
            self.bot.logger.warning("發送爆言失敗(message=%s): %s", message.id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Starboard(bot))
