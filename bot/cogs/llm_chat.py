from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from ..utils.cloudflare_ai_client import CloudflareAIClient


class LLMChat(commands.Cog):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.client = CloudflareAIClient(bot.settings)
        self._lock = asyncio.Lock()
        self._processing_queue: dict[int, discord.Message] = {}

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

        try:
            thinking_msg = await message.reply("思考中...", mention_author=False)
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.bot.logger.warning(f"無法發送思考中訊息: {exc}")
            return

        async with self._lock:
            queue_size = len(self._processing_queue)
            self.bot.logger.info(
                f"開始處理 LLM 請求 用戶={message.author.display_name} 頻道={message.channel.name} | 當前隊列={queue_size}"
            )
            await self._handle_llm_interaction(message, thinking_msg)

    async def _handle_llm_interaction(self, message: discord.Message, thinking_msg: discord.Message) -> None:
        try:
            self._processing_queue[message.id] = thinking_msg
            self.bot.logger.info(f"已發送思考中訊息 | msg_id={thinking_msg.id}")

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

            self.bot.logger.info("開始呼叫 Cloudflare AI API...")
            try:
                response = await self.client.generate_chat_reply(
                    user_display=message.author.display_name,
                    message=message.clean_content,
                    reference_info=reference_info,
                )
                self.bot.logger.info(f"AI 回應長度: {len(response)} 字元")
            except Exception as exc:
                self.bot.logger.exception("Cloudflare AI request failed", exc_info=exc)
                response = "抱歉，我目前無法處理您的請求。"

            self.bot.logger.info("準備編輯訊息...")
            try:
                await thinking_msg.edit(content=response)
                self.bot.logger.info("訊息編輯成功")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                self.bot.logger.warning(f"無法編輯回覆訊息: {exc}")
        except Exception as exc:
            self.bot.logger.exception("處理 LLM 互動時發生錯誤", exc_info=exc)
            try:
                await thinking_msg.edit(content="抱歉，處理您的請求時發生錯誤。")
            except Exception:
                pass
        finally:
            self._processing_queue.pop(message.id, None)
            queue_size = len(self._processing_queue)
            self.bot.logger.info(
                f"完成處理 LLM 請求 | 用戶={message.author.display_name} | 剩餘隊列={queue_size}"
            )

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
