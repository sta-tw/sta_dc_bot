from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
import re
import unicodedata

import discord
from discord.ext import commands

from ..utils.config import Settings

INVITE_PATTERN = re.compile(r"discord\.gg/|discord\.com/invite/", re.IGNORECASE)


class ModerationCog(commands.Cog):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._user_messages: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=50))
        self._lock = asyncio.Lock()
        self._user_recent_texts: dict[int, deque[tuple[float, str]]] = defaultdict(lambda: deque(maxlen=50))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        settings: Settings = self.bot.settings

        if isinstance(message.author, discord.Member):
            if any(role.id in settings.support_role_ids for role in message.author.roles):
                return

        blocked = settings.find_blocked_keyword(message.content)
        if blocked:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                snippet = re.sub(r"\s+", " ", message.content)[:120]
                self.bot.logger.info(
                    "Delete message for blocked keyword | guild=%s channel=%s author=%s keyword=%s content=%s",
                    getattr(message.guild, "id", None), getattr(message.channel, "id", None), getattr(message.author, "id", None), blocked, snippet,
                )
            except Exception:
                pass
            try:
                await message.channel.send(
                    f"此訊息包含禁止詞彙（{blocked}），已被移除。",
                    delete_after=6,
                )
            except discord.HTTPException:
                pass
            return

        if settings.moderation_delete_invite_links and INVITE_PATTERN.search(message.content):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                snippet = re.sub(r"\s+", " ", message.content)[:120]
                self.bot.logger.info(
                    "Delete message for invite link | guild=%s channel=%s author=%s content=%s",
                    getattr(message.guild, "id", None), getattr(message.channel, "id", None), getattr(message.author, "id", None), snippet,
                )
            except Exception:
                pass
            try:
                await message.channel.send("不允許張貼邀請連結，訊息已移除。", delete_after=6)
            except discord.HTTPException:
                pass
            return

        if settings.moderation_gibberish_enabled and self._is_gibberish(message.content, settings):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            try:
                snippet = re.sub(r"\s+", " ", message.content)[:120]
                self.bot.logger.info(
                    "Delete message for gibberish | guild=%s channel=%s author=%s content=%s",
                    getattr(message.guild, "id", None), getattr(message.channel, "id", None), getattr(message.author, "id", None), snippet,
                )
            except Exception:
                pass
            try:
                await message.channel.send("偵測到無意義或亂碼訊息，已移除。", delete_after=6)
            except discord.HTTPException:
                pass
            return

        now = time.time()
        normalized = self._normalize(message.content)
        if len(normalized) >= settings.moderation_duplicate_min_length:
            async with self._lock:
                recent = self._user_recent_texts[message.author.id]
                window = settings.moderation_duplicate_window_seconds
                while recent and now - recent[0][0] > window:
                    recent.popleft()
                duplicates = sum(1 for ts, txt in recent if now - ts <= window and txt == normalized)
                if duplicates + 1 >= settings.moderation_duplicate_max_repeats:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                    try:
                        snippet = re.sub(r"\s+", " ", message.content)[:120]
                        self.bot.logger.info(
                            "Delete message for duplicate spam | guild=%s channel=%s author=%s repeats=%s window=%ss content=%s",
                            getattr(message.guild, "id", None), getattr(message.channel, "id", None), getattr(message.author, "id", None), settings.moderation_duplicate_max_repeats, settings.moderation_duplicate_window_seconds, snippet,
                        )
                    except Exception:
                        pass
                    try:
                        await message.channel.send(
                            f"偵測到重複訊息洗版，已移除（{settings.moderation_duplicate_max_repeats} 次/ {settings.moderation_duplicate_window_seconds} 秒）",
                            delete_after=6,
                        )
                    except discord.HTTPException:
                        pass
                    return
                recent.append((now, normalized))

        if settings.moderation_enable_rate_limit:
            async with self._lock:
                history = self._user_messages[message.author.id]
                history.append(now)
                window = settings.moderation_spam_interval_seconds
                while history and now - history[0] > window:
                    history.popleft()
                if len(history) > settings.moderation_spam_max_messages:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
                    try:
                        self.bot.logger.info(
                            "Delete message for rate limit | guild=%s channel=%s author=%s count=%s window=%ss",
                            getattr(message.guild, "id", None), getattr(message.channel, "id", None), getattr(message.author, "id", None), len(history), settings.moderation_spam_interval_seconds,
                        )
                    except Exception:
                        pass
                    try:
                        await message.channel.send(
                            f"發言過於頻繁，請稍候再發送（{settings.moderation_spam_max_messages}/{window}秒）",
                            delete_after=6,
                        )
                    except discord.HTTPException:
                        pass
                    return

    def refresh_settings(self) -> None:
        return

    def _normalize(self, text: str) -> str:
        lowered = text.lower()
        lowered = re.sub(r"\s+", " ", lowered).strip()
        return lowered

    def _is_gibberish(self, text: str, settings: Settings) -> bool:
        if not text:
            return False
        t = unicodedata.normalize("NFKC", text)
        if len(t) < settings.moderation_gibberish_min_length:
            return False
        no_space = re.sub(r"\s+", "", t)
        if len(no_space) >= settings.moderation_gibberish_long_run_no_space:
            return True
        if re.search(r"(.{6,20})\1{2,}", no_space):
            return True
        ascii_run = re.sub(r"[^A-Za-z0-9]", "", no_space)
        if len(ascii_run) >= settings.moderation_gibberish_ascii_min_length:
            vowels = sum(c in "aeiouAEIOU" for c in ascii_run)
            ratio_vowels = vowels / len(ascii_run) if ascii_run else 0.0
            uppers = sum(c.isupper() for c in ascii_run)
            digits = sum(c.isdigit() for c in ascii_run)
            frac_upper = uppers / len(ascii_run)
            frac_digit = digits / len(ascii_run)
            if ratio_vowels < 0.2 and (frac_upper > 0.4 or frac_digit > 0.25):
                return True
        return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))
