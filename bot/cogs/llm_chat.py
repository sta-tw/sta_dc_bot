from __future__ import annotations

import asyncio
from typing import Optional

import discord
from discord.ext import commands

from ..utils.cloudflare_ai_client import CloudflareAIClient


class LLMChat(commands.Cog):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = CloudflareAIClient(bot.settings)
        self._lock = asyncio.Lock()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        if not self._is_mentioning_bot(message):
            return

        blocked = self.bot.settings.find_blocked_keyword(message.content)
        if blocked:
            return

        async with self._lock:
            try:
                await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.Forbidden):
                return
            await self._handle_llm_interaction(message)

    async def _handle_llm_interaction(self, message: discord.Message) -> None:
        await message.channel.typing()

        context_lines, previous_reply = await self._collect_context(message)

        info_lines = []
        
        if self.bot.settings.ticket_categories:
            info_lines.append("【客服分類資訊】")
            for cat in self.bot.settings.ticket_categories:
                info_lines.append(f"- {cat.label}: {cat.description}")
            info_lines.append("")

        if self.bot.settings.faq_content:
            info_lines.append("【常見問題 (FAQ)】")
            info_lines.append(self.bot.settings.faq_content)
        
        reference_info = "\n".join(info_lines) if info_lines else None

        try:
            response = await self.client.generate_chat_reply(
                context=context_lines,
                previous_reply=previous_reply,
                user_display=message.author.display_name,
                message=message.clean_content,
                reference_info=reference_info,
            )
        except Exception as exc:
            self.bot.logger.exception("Cloudflare AI request failed", exc_info=exc)
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

    def refresh_settings(self) -> None:
        self.client = CloudflareAIClient(self.bot.settings)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LLMChat(bot))
