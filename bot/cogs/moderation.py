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
        return
        
        if message.guild is None or message.author.bot:
            return

        settings: Settings = self.bot.settings

        if isinstance(message.author, discord.Member):
            if any(role.id in settings.support_role_ids for role in message.author.roles):
                return

        blocked = settings.find_blocked_keyword(message.content)
        if blocked:
            await self._handle_violation(message, f"此訊息包含禁止詞彙（{blocked}），已被移除。", "blocked keyword", blocked=blocked)
            return

        if settings.moderation_delete_invite_links and INVITE_PATTERN.search(message.content):
            await self._handle_violation(message, "不允許張貼邀請連結，訊息已移除。", "invite link")
            return

        if settings.moderation_gibberish_enabled and self._is_gibberish(message.content, settings):
            await self._handle_violation(message, "偵測到無意義或亂碼訊息，已移除。", "gibberish")
            return

        now = time.time()
        
        if await self._check_duplicate_message(message, now, settings):
            return

        if settings.moderation_enable_rate_limit and await self._check_rate_limit(message, now, settings):
            return

    async def _handle_violation(self, message: discord.Message, warning_msg: str, violation_type: str, **extra_log_data) -> None:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        
        try:
            snippet = re.sub(r"\s+", " ", message.content)[:120]
            log_data = {
                "guild": getattr(message.guild, "id", None),
                "channel": getattr(message.channel, "id", None),
                "author": getattr(message.author, "id", None),
                "content": snippet,
                **extra_log_data
            }
            self.bot.logger.info(
                "Delete message for %s | guild=%s channel=%s author=%s",
                violation_type, log_data["guild"], log_data["channel"], log_data["author"],
                extra=log_data
            )
        except Exception:
            pass
        
        try:
            await message.channel.send(warning_msg, delete_after=6)
        except discord.HTTPException:
            pass

    async def _check_duplicate_message(self, message: discord.Message, now: float, settings: Settings) -> bool:
        normalized = self._normalize(message.content)
        if len(normalized) < settings.moderation_duplicate_min_length:
            return False

        async with self._lock:
            recent = self._user_recent_texts[message.author.id]
            window = settings.moderation_duplicate_window_seconds
            
            while recent and now - recent[0][0] > window:
                recent.popleft()
            
            duplicates = sum(1 for ts, txt in recent if now - ts <= window and txt == normalized)
            if duplicates + 1 >= settings.moderation_duplicate_max_repeats:
                warning_msg = f"偵測到重複訊息洗版，已移除（{settings.moderation_duplicate_max_repeats} 次/ {settings.moderation_duplicate_window_seconds} 秒）"
                await self._handle_violation(
                    message, warning_msg, "duplicate spam",
                    repeats=settings.moderation_duplicate_max_repeats,
                    window=settings.moderation_duplicate_window_seconds
                )
                return True
            
            recent.append((now, normalized))
            return False

    async def _check_rate_limit(self, message: discord.Message, now: float, settings: Settings) -> bool:
        async with self._lock:
            history = self._user_messages[message.author.id]
            history.append(now)
            window = settings.moderation_spam_interval_seconds
            
            while history and now - history[0] > window:
                history.popleft()
            
            if len(history) > settings.moderation_spam_max_messages:
                warning_msg = f"發言過於頻繁，請稍候再發送（{settings.moderation_spam_max_messages}/{window}秒）"
                await self._handle_violation(
                    message, warning_msg, "rate limit",
                    count=len(history),
                    window=settings.moderation_spam_interval_seconds
                )
                return True
            return False

    def refresh_settings(self) -> None:
        return

    def _normalize(self, text: str) -> str:
        lowered = text.lower()
        lowered = re.sub(r"\s+", " ", lowered).strip()
        return lowered

    def _is_gibberish(self, text: str, settings: Settings) -> bool:
        if not text:
            return False
        
        t = re.sub(r"<@!?\d+>", "", text)
        t = re.sub(r"<@&\d+>", "", t)
        
        t = re.sub(r"https?://[^\s]+", "", t)
        
        t = unicodedata.normalize("NFKC", t)
        
        if len(t) < settings.moderation_gibberish_min_length:
            return False

        no_space = re.sub(r"\s+", "", t)
        
        if len(no_space) >= settings.moderation_gibberish_long_run_no_space:
            return True
        
        if re.search(r"(.{6,20})\1{2,}", no_space):
            return True
        
        ascii_run = re.sub(r"[^\x00-\x7F]", "", no_space)
        if len(ascii_run) >= settings.moderation_gibberish_ascii_min_length:
            return self._analyze_ascii_pattern(ascii_run)
        
        return False

    def _analyze_ascii_pattern(self, ascii_text: str) -> bool:
        if not ascii_text:
            return False
        
        vowels = sum(1 for c in ascii_text if c.lower() in "aeiou")
        ratio_vowels = vowels / len(ascii_text)
        
        uppers = sum(1 for c in ascii_text if c.isupper())
        frac_upper = uppers / len(ascii_text)
        
        digits = sum(1 for c in ascii_text if c.isdigit())
        frac_digit = digits / len(ascii_text)
        
        consonant_run = self._check_consonant_runs(ascii_text)
        
        return (ratio_vowels < 0.2 and (frac_upper > 0.4 or frac_digit > 0.25)) or consonant_run

    def _check_consonant_runs(self, text: str) -> bool:
        consonants = "bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ"
        max_run = 0
        current_run = 0
        
        for char in text:
            if char in consonants:
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                current_run = 0
        
        return max_run >= 8


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModerationCog(bot))