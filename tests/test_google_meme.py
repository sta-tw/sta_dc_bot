from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from bot.cogs.google_meme import GoogleMeme, COMMAND_NAME, IMAGE_FILENAME
from bot.utils.config_paths import ConfigPaths


@pytest.fixture
def bot() -> commands.Bot:
    b = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    b.logger = MagicMock()
    return b


@pytest.mark.asyncio
async def test_google_meme_cog_load_and_unload(bot: commands.Bot) -> None:
    cog = GoogleMeme(bot)
    await bot.add_cog(cog)

    registered = bot.tree.get_command(COMMAND_NAME, type=discord.AppCommandType.message)
    assert registered is not None
    assert registered.name == COMMAND_NAME
    assert registered.type == discord.AppCommandType.message

    await bot.remove_cog("GoogleMeme")
    assert bot.tree.get_command(COMMAND_NAME, type=discord.AppCommandType.message) is None


@pytest.mark.asyncio
async def test_google_meme_cog_load_handles_already_registered(bot: commands.Bot) -> None:
    cog = GoogleMeme(bot)
    bot.tree.add_command(cog.context_menu)

    await cog.cog_load()
    registered = bot.tree.get_command(COMMAND_NAME, type=discord.AppCommandType.message)
    assert registered is not None


@pytest.mark.asyncio
async def test_reply_google_meme_success(bot: commands.Bot, tmp_path: Path) -> None:
    cog = GoogleMeme(bot)
    fake_img = tmp_path / IMAGE_FILENAME
    fake_img.write_bytes(b"fake image content")

    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.reply = AsyncMock()

    with patch.object(cog, "get_image_path", return_value=fake_img):
        await cog.reply_google_meme(interaction, message)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    message.reply.assert_awaited_once()
    _, kwargs = message.reply.await_args
    assert "file" in kwargs
    assert isinstance(kwargs["file"], discord.File)
    assert kwargs["file"].filename == IMAGE_FILENAME
    assert kwargs.get("mention_author") is False
    kwargs["file"].close()

    interaction.edit_original_response.assert_awaited_once_with(content="已成功回覆圖片。")


@pytest.mark.asyncio
async def test_reply_google_meme_image_not_found(bot: commands.Bot, tmp_path: Path) -> None:
    cog = GoogleMeme(bot)
    missing_img = tmp_path / "non_existent.png"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.reply = AsyncMock()

    with patch.object(cog, "get_image_path", return_value=missing_img):
        await cog.reply_google_meme(interaction, message)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    message.reply.assert_not_awaited()
    interaction.edit_original_response.assert_awaited_once_with(
        content=f"找不到圖片檔案：{IMAGE_FILENAME}"
    )


@pytest.mark.asyncio
async def test_reply_google_meme_forbidden(bot: commands.Bot, tmp_path: Path) -> None:
    cog = GoogleMeme(bot)
    fake_img = tmp_path / IMAGE_FILENAME
    fake_img.write_bytes(b"fake")

    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.status = 403
    mock_resp.reason = "Forbidden"

    message = MagicMock(spec=discord.Message)
    message.reply = AsyncMock(side_effect=discord.Forbidden(mock_resp, "Missing Permissions"))

    with patch.object(cog, "get_image_path", return_value=fake_img):
        await cog.reply_google_meme(interaction, message)

    interaction.edit_original_response.assert_awaited_once_with(
        content="機器人沒有足夠的權限回覆此訊息或上傳檔案。"
    )


@pytest.mark.asyncio
async def test_reply_google_meme_not_found(bot: commands.Bot, tmp_path: Path) -> None:
    cog = GoogleMeme(bot)
    fake_img = tmp_path / IMAGE_FILENAME
    fake_img.write_bytes(b"fake")

    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.reason = "Not Found"

    message = MagicMock(spec=discord.Message)
    message.reply = AsyncMock(side_effect=discord.NotFound(mock_resp, "Unknown Message"))

    with patch.object(cog, "get_image_path", return_value=fake_img):
        await cog.reply_google_meme(interaction, message)

    interaction.edit_original_response.assert_awaited_once_with(
        content="找不到目標訊息，可能已被刪除。"
    )


@pytest.mark.asyncio
async def test_reply_google_meme_generic_error(bot: commands.Bot, tmp_path: Path) -> None:
    cog = GoogleMeme(bot)
    fake_img = tmp_path / IMAGE_FILENAME
    fake_img.write_bytes(b"fake")

    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.reply = AsyncMock(side_effect=RuntimeError("Network timeout"))

    with patch.object(cog, "get_image_path", return_value=fake_img):
        await cog.reply_google_meme(interaction, message)

    interaction.edit_original_response.assert_awaited_once_with(
        content="回覆失敗：Network timeout"
    )


def test_extension_configured_in_bot_json() -> None:
    with open(ConfigPaths.BOT_CONFIG, 'r', encoding='utf-8') as f:
        data = json.load(f)
    assert "bot.cogs.google_meme" in data["extensions"]


def test_real_image_file_exists() -> None:
    image_path = ConfigPaths.ROOT_DIR / IMAGE_FILENAME
    assert image_path.exists()
    assert image_path.is_file()
    assert image_path.stat().st_size > 0
