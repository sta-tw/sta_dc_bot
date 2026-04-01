from __future__ import annotations
import asyncio
from dataclasses import dataclass
from time import monotonic

import discord
from discord.ext import commands


@dataclass
class RepeatState:
    content: str | None = None
    streak: int = 0
    last_echo_at: float = 0.0


class Repeater(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._states: dict[int, RepeatState] = {}
        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._warned_empty_content = False
        self._echo_cooldown_seconds = 3.0

    def _get_channel_lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock
        return lock

    def _get_parent_category_id(self, message: discord.Message) -> int | None:
        channel = message.channel
        if isinstance(channel, discord.TextChannel):
            return channel.category_id
        if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.TextChannel):
            return channel.parent.category_id
        return None

    async def _extract_repeat_text(self, message: discord.Message) -> str:
        content = (message.content or "").strip()
        if content:
            return content
        if isinstance(message.channel, discord.TextChannel):
            try:
                fetched = await message.channel.fetch_message(message.id)
                fetched_content = (fetched.content or "").strip()
                if fetched_content:
                    return fetched_content
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

        if not self._warned_empty_content:
            self._warned_empty_content = True
            self.bot.logger.warning("Repeater 讀不到 message.content，請確認 Message Content Intent 已在 Discord Developer Portal 啟用")

        return ""

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if message.author.bot or message.webhook_id is not None:
            return

        content = await self._extract_repeat_text(message)
        if not content:
            return

        channel_id = message.channel.id
        lock = self._get_channel_lock(channel_id)

        async with lock:
            state = self._states.setdefault(channel_id, RepeatState())

            if state.content == content:
                state.streak += 1
            else:
                state.content = content
                state.streak = 1

            if state.streak < 3:
                return

            now = monotonic()
            if now - state.last_echo_at < self._echo_cooldown_seconds:
                return

            category_id = self._get_parent_category_id(message)
            filtered_category_ids = set(self.bot.settings.repeater_filtered_category_ids)
            if category_id in filtered_category_ids:
                send_text = self.bot.settings.repeater_filtered_response
            else:
                send_text = content

            try:
                await message.channel.send(send_text)
                state.last_echo_at = now
                state.streak = 0
            except discord.HTTPException as exc:
                self.bot.logger.warning("復讀機發送失敗(channel=%s): %s", channel_id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Repeater(bot))
