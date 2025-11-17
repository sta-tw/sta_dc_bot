from __future__ import annotations

import discord
from discord.ext import commands


class Welcome(commands.Cog):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        channel_id = self.bot.settings.welcome_channel_id
        channel = member.guild.get_channel(channel_id)
        if channel is None:
            self.bot.logger.warning("Welcome channel %s not found", channel_id)
            return

        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
            await channel.send(f"歡迎 {member.mention} 加入伺服器！🎉")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
