from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord.ext import commands

from ..utils.llm_client import GeminiClient


class LLMChat(commands.Cog):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = GeminiClient()
        self._lock = asyncio.Lock()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        if not self._is_mentioning_bot(message):
            return

        async with self._lock:
            await self._handle_llm_interaction(message)

    async def _handle_llm_interaction(self, message: discord.Message) -> None:
        await message.channel.typing()

        context_lines, previous_reply = await self._collect_context(message)

        prompt_parts = [
            "### 對話上下文\n",
            "\n".join(context_lines) or "(無歷史訊息)",
            "\n\n### 上一次機器人回覆\n",
            previous_reply or "(尚未有回覆)",
            "\n\n### 目前訊息\n",
            f"{message.author.display_name}: {message.clean_content}",
        ]

        try:
            response = await self.client.generate(prompt_parts)
        except Exception as exc:
            self.bot.logger.exception("Gemini request failed", exc_info=exc)
            response = "抱歉，我目前無法處理您的請求。"

        await message.reply(response, mention_author=False)

    async def _collect_context(self, message: discord.Message) -> tuple[list[str], Optional[str]]:
        history: list[discord.Message] = []
        async for msg in message.channel.history(limit=10, before=message):
            history.append(msg)

        history.reverse()
        lines = [f"{msg.author.display_name}: {msg.clean_content}" for msg in history]

        previous_reply: Optional[str] = None
        bot_user = self.bot.user
        if bot_user:
            for msg in reversed(history):
                if msg.author.id == bot_user.id:
                    previous_reply = msg.clean_content
                    break

        return lines, previous_reply

    def _is_mentioning_bot(self, message: discord.Message) -> bool:
        bot_user = self.bot.user
        if bot_user is None:
            return False

        if bot_user in message.mentions:
            return True

        content = message.content
        mention_formats = {f"<@{bot_user.id}>", f"<@!{bot_user.id}>"}
        return any(token in content for token in mention_formats)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LLMChat(bot))
