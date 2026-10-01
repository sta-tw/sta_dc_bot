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
        from utils.instagram_feed_ui import setup_persistent_views_instagram

        setup_persistent_views_role(bot)
        setup_persistent_views_exchange(bot)
        setup_persistent_views_role_button(bot)
        setup_persistent_views_instagram(bot)
        
        await _load_extensions(bot, settings.extensions)

        if settings.guild_id:
            guild = discord.Object(id=settings.guild_id)
            try:
                await bot.tree.sync(guild=guild)
            except discord.HTTPException:
                bot.logger.exception("Failed to sync commands for guild %s", settings.guild_id)
        else:
            try:
                await bot.tree.sync()
            except discord.HTTPException as exc:
                if exc.code != 50240:
                    raise
                try:
                    sync_statuses = await _sync_global_commands(
                        bot,
                        ("resource_setup", "llm_channel"),
                    )
                except Exception:
                    bot.logger.exception(
                        "Targeted command sync failed after global sync was rejected; "
                        "no bulk replacement was attempted"
                    )
                    raise
                bot.logger.warning(
                    "Discord rejected global bulk command sync with code 50240; "
                    "targeted /resource_setup status=%s and /llm_channel status=%s. "
                    "No other remote commands, including the Activity Entry Point, "
                    "were modified: %s",
                    sync_statuses.get("resource_setup", "not attempted"),
                    sync_statuses.get("llm_channel", "not attempted"),
                    exc,
                )

    return bot


_OPTION_DEFAULTS = {
    "choices": [],
    "channel_types": [],
    "autocomplete": False,
    "min_value": None,
    "max_value": None,
    "min_length": None,
    "max_length": None,
    "options": [],
    "name_localizations": {},
    "description_localizations": {},
}


def _normalize_app_command_option(option: object) -> dict[str, object]:
    if isinstance(option, dict):
        option_data = option
    else:
        option_data = option.to_dict()

    normalized: dict[str, object] = {}
    for key, value in option_data.items():
        if key in ("choices", "options"):
            value = [_normalize_app_command_option(item) for item in value]
        if key in _OPTION_DEFAULTS and value == _OPTION_DEFAULTS[key]:
            continue
        normalized[key] = value
    return normalized


async def _sync_global_chat_input_command(
    bot: commands.Bot,
    command_name: str,
) -> str:
    local_command = bot.tree.get_command(command_name)
    if local_command is None:
        return "local command unavailable"

    translator = bot.tree.translator
    if translator is None:
        local_payload = local_command.to_dict(bot.tree)
    else:
        local_payload = await local_command.get_translated_payload(bot.tree, translator)

    remote_commands = await bot.tree.fetch_commands()
    remote_command = next(
        (
            command
            for command in remote_commands
            if command.name == command_name
            and command.type == discord.AppCommandType.chat_input
        ),
        None,
    )

    desired_description = local_payload["description"]
    desired_options = local_payload.get("options", [])
    if remote_command is not None:
        remote_options = [option.to_dict() for option in remote_command.options]
        if (
            remote_command.description == desired_description
            and [_normalize_app_command_option(option) for option in remote_options]
            == [_normalize_app_command_option(option) for option in desired_options]
        ):
            return "already matches"

        await bot.http.edit_global_command(
            bot.application_id,
            remote_command.id,
            {
                "description": desired_description,
                "options": desired_options,
            },
        )
        return "updated"

    await bot.http.upsert_global_command(bot.application_id, local_payload)
    return "created"


async def _sync_global_commands(
    bot: commands.Bot,
    command_names: Iterable[str],
) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for command_name in command_names:
        statuses[command_name] = await _sync_global_chat_input_command(
            bot,
            command_name,
        )
    return statuses


async def _sync_global_resource_setup_command(bot: commands.Bot) -> str:
    return await _sync_global_chat_input_command(bot, "resource_setup")


async def _load_extensions(bot: commands.Bot, extensions: Iterable[str]) -> None:
    for ext in extensions:
        try:
            await bot.load_extension(ext)
        except Exception as exc:
            bot.logger.exception("Failed to load extension %s", ext, exc_info=exc)
