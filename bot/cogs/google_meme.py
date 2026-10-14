from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.config_paths import ConfigPaths

logger = logging.getLogger(__name__)

COMMAND_NAME = "阿你是不會google嗎.png"
IMAGE_FILENAME = "阿你是不會google嗎.png"


class GoogleMeme(commands.Cog):
    COMMAND_NAME = COMMAND_NAME

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.context_menu = app_commands.ContextMenu(
            name=self.COMMAND_NAME,
            callback=self.reply_google_meme,
        )

    def get_image_path(self) -> Path:
        candidates = [
            ConfigPaths.ROOT_DIR / IMAGE_FILENAME,
            ConfigPaths.DATA_DIR / IMAGE_FILENAME,
            ConfigPaths.ROOT_DIR / "assets" / IMAGE_FILENAME,
        ]
        for path in candidates:
            if path.exists():
                return path
        return candidates[0]

    async def cog_load(self) -> None:
        try:
            self.bot.tree.add_command(self.context_menu)
        except app_commands.CommandAlreadyRegistered:
            self.bot.logger.warning(
                "Context menu %s 已存在，將先移除再重新註冊",
                self.context_menu.name,
            )
            self.bot.tree.remove_command(
                self.context_menu.name,
                type=discord.AppCommandType.message,
            )
            self.bot.tree.add_command(self.context_menu)

    def cog_unload(self) -> None:
        self.bot.tree.remove_command(
            self.context_menu.name,
            type=discord.AppCommandType.message,
        )

    async def reply_google_meme(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        image_path = self.get_image_path()
        if not image_path.exists():
            await interaction.edit_original_response(
                content=f"找不到圖片檔案：{IMAGE_FILENAME}",
            )
            return

        try:
            await message.reply(
                file=discord.File(str(image_path), filename=IMAGE_FILENAME),
                mention_author=False,
            )
            await interaction.edit_original_response(content="已成功回覆圖片。")
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="機器人沒有足夠的權限回覆此訊息或上傳檔案。",
            )
        except discord.NotFound:
            await interaction.edit_original_response(
                content="找不到目標訊息，可能已被刪除。",
            )
        except Exception as exc:
            self.bot.logger.error("回覆圖片失敗：%s", exc, exc_info=True)
            await interaction.edit_original_response(content=f"回覆失敗：{exc}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GoogleMeme(bot))
