from __future__ import annotations

from pathlib import Path
from typing import Iterable
import logging
import os
import json

import discord
from discord.ext import commands

from .utils.config import Settings
from .utils.config_paths import ConfigPaths
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
    
    logger = logging.getLogger("bot")
    bot.logger = logger
    
    bot.emoji = {}
    
    ConfigPaths.ensure_directories()
    
    emoji_file = ConfigPaths.EMOJI_CONFIG
    if emoji_file.exists():
        try:
            with open(emoji_file, 'r', encoding='utf-8') as f:
                emoji_data = json.load(f)
                if 'emojis' in emoji_data:
                    for emoji_name, emoji_info in emoji_data['emojis'].items():
                        if 'format' in emoji_info:
                            bot.emoji[emoji_name] = emoji_info['format']
            bot.logger.info(f"已載入 {len(bot.emoji)} 個表情符號")
        except Exception as e:
            bot.logger.error(f"載入 {emoji_file} 時發生錯誤: {str(e)}")
    else:
        with open(emoji_file, 'w', encoding='utf-8') as f:
            json.dump({"emojis": {}}, f, ensure_ascii=False, indent=4)
    
    def get_emoji(name):
        return bot.emoji.get(name, f":{name}:")
    
    bot.get_emoji = get_emoji

    @bot.event
    async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        raise error

    @bot.event
    async def setup_hook() -> None:
        from utils.role_ui import setup_persistent_views_role
        from utils.exchange_ui import setup_persistent_views_exchange
        from utils.role_button_ui import setup_persistent_views_role_button
        
        setup_persistent_views_role(bot)
        setup_persistent_views_exchange(bot)
        setup_persistent_views_role_button(bot)
        
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
