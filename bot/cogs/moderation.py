from __future__ import annotations

import discord
from discord.ext import commands


class ModerationCog(commands.Cog):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))