from __future__ import annotations

from pathlib import Path
from typing import Iterable
import logging

import discord
from discord.ext import commands

from .utils.config import Settings
from .utils.logging_config import setup_logging


def build_bot(settings_path: Path | str) -> commands.Bot:
    setup_logging()

    settings_path = Path(settings_path)
    settings = Settings.from_file(settings_path)

    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True

    bot = commands.Bot(
        command_prefix=commands.when_mentioned_or("!"),
        case_insensitive=True,
        intents=intents,
    )

    bot.settings = settings
    bot.settings_path = settings_path.resolve()
    bot.emoji = {
        "welcome": "🎉",
        "箭頭": "➤"
    }
    logger = logging.getLogger("bot")
    bot.logger = logger

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        raise error

    @bot.event
    async def setup_hook() -> None:
        await _load_extensions(bot, settings.extensions)

        if settings.guild_id:
            guild = discord.Object(id=settings.guild_id)
            try:
                await bot.tree.sync(guild=guild)
            except discord.HTTPException:
                bot.logger.exception("Failed to sync commands for guild %s", settings.guild_id)
        else:
            await bot.tree.sync()

    return bot


async def _load_extensions(bot: commands.Bot, extensions: Iterable[str]) -> None:
    for ext in extensions:
        try:
            await bot.load_extension(ext)
        except Exception as exc:
            bot.logger.exception("Failed to load extension %s", ext, exc_info=exc)
