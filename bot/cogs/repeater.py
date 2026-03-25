from __future__ import annotations
from dataclasses import dataclass
import discord
from discord.ext import commands
@dataclass
class RepeatState:
    content: str | None = None
    streak: int = 0
    echoed_content: str | None = None


class Repeater(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._states: dict[int, RepeatState] = {}
        self._warned_empty_content = False

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
        state = self._states.setdefault(channel_id, RepeatState())

        if state.content == content:
            state.streak += 1
        else:
            state.content = content
            state.streak = 1

        if state.streak >= 3 and state.echoed_content != content:
            try:
                await message.channel.send(content)
                state.echoed_content = content
            except discord.HTTPException as exc:
                self.bot.logger.warning("復讀機發送失敗(channel=%s): %s", channel_id, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Repeater(bot))
