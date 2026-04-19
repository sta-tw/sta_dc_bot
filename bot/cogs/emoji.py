from __future__ import annotations

import json
import random

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.config_paths import ConfigPaths


class Emoji(commands.Cog):
    RANDOM_CONTEXT_MENU_NAME = "隨機30個emoji"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.command_emoji_map = self._load_mention_config()
        self.random_emoji_pool = self._build_random_emoji_pool()
        self.context_menus: list[app_commands.ContextMenu] = [
            app_commands.ContextMenu(name=name, callback=self.mention_callback)
            for name in self.command_emoji_map
        ]
        self.context_menus.append(
            app_commands.ContextMenu(
                name=self.RANDOM_CONTEXT_MENU_NAME,
                callback=self.random_mention_callback,
            )
        )

    def _load_mention_config(self) -> dict[str, list[str]]:
        command_emoji_map: dict[str, list[str]] = {}

        emoji_file = ConfigPaths.EMOJI_CONFIG
        if emoji_file.exists():
            try:
                data = json.loads(emoji_file.read_text(encoding="utf-8"))
                mention_commands = data.get("mention_commands", {})

                for _, cfg in mention_commands.items():
                    if not isinstance(cfg, dict):
                        continue
                    command_name = str(cfg.get("name", "")).strip()
                    emojis = [str(item).strip() for item in cfg.get("emojis", []) if str(item).strip()]
                    if command_name and emojis:
                        command_emoji_map[command_name] = emojis
            except Exception as exc:
                self.bot.logger.warning("讀取 mention_commands 配置失敗：%s", exc)

        if not command_emoji_map:
            self.bot.logger.warning("mention_commands 未配置有效資料，Emoji 右鍵選單將不會註冊")

        return command_emoji_map

    def _build_random_emoji_pool(self, guild: discord.Guild | None = None) -> list[str]:
        seen: set[str] = set()
        pool: list[str] = []

        def add_emoji(emoji_text: str) -> None:
            emoji_text = emoji_text.strip()
            if not emoji_text or emoji_text in seen:
                return
            seen.add(emoji_text)
            pool.append(emoji_text)

        if guild is not None:
            for emoji in guild.emojis:
                add_emoji(str(emoji))

        for emoji in self.bot.emojis:
            add_emoji(str(emoji))

        emoji_file = ConfigPaths.EMOJI_CONFIG
        if emoji_file.exists():
            try:
                data = json.loads(emoji_file.read_text(encoding="utf-8"))
                for cfg in data.get("emojis", {}).values():
                    if not isinstance(cfg, dict):
                        continue
                    add_emoji(str(cfg.get("format", "")))
            except Exception as exc:
                self.bot.logger.warning("讀取 emojis 配置失敗：%s", exc)

        return pool

    async def cog_load(self) -> None:
        for command in self.context_menus:
            try:
                self.bot.tree.add_command(command)
            except app_commands.CommandAlreadyRegistered:
                self.bot.logger.warning("Context menu %s 已存在，將先移除再重新註冊", command.name)
                self.bot.tree.remove_command(command.name, type=discord.AppCommandType.message)
                self.bot.tree.add_command(command)

    def cog_unload(self) -> None:
        for command in self.context_menus:
            self.bot.tree.remove_command(command.name, type=discord.AppCommandType.message)

    async def mention_callback(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        command_name = interaction.command.name if interaction.command is not None else ""
        emojis = list(self.command_emoji_map.get(command_name, []))
        if not emojis:
            await interaction.edit_original_response(content="找不到對應的 emoji 清單配置。")
            return

        await interaction.edit_original_response(content=emojis[0])

        await message.reply(
            content=f"-# {interaction.user.mention} 加了一大堆 {emojis[0]}",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        for emoji in emojis:
            try:
                await message.add_reaction(discord.PartialEmoji.from_str(emoji))
            except Exception as exc:
                await interaction.edit_original_response(content=str(exc))
                break

    async def random_mention_callback(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        random_emoji_pool = self._build_random_emoji_pool(message.guild)

        if not random_emoji_pool:
            await interaction.edit_original_response(content="找不到可用的 emoji 池，請確認伺服器或 bot 有可用 emoji。")
            return

        target_count = min(30, len(random_emoji_pool))
        selected = random.sample(random_emoji_pool, k=target_count)

        success_count = 0
        for emoji in selected:
            try:
                await message.add_reaction(discord.PartialEmoji.from_str(emoji))
                success_count += 1
            except Exception:
                continue

        await interaction.edit_original_response(content=f"已隨機添加 {success_count} 個 emoji。")

        if success_count < 30 and len(random_emoji_pool) < 30:
            await message.reply(
                content=f"-# {interaction.user.mention} 隨機加了 {success_count} 個 emoji（池內不足 30 個）",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await message.reply(
            content=f"-# {interaction.user.mention} 隨機加了 {success_count} 個 emoji",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Emoji(bot))
