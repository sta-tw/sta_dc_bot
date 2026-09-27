from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager


class LeaderboardView(discord.ui.View):
    PAGE_SIZE = 10

    def __init__(
        self,
        guild: discord.Guild,
        entries: list[tuple[discord.Member, int]],
        owner_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.guild = guild
        self.entries = entries
        self.owner_id = owner_id
        self.page = 0
        self.total_pages = max(
            1,
            (len(entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE,
        )

        self.first_button = discord.ui.Button(
            label="第一頁",
            emoji="⏮️",
            style=discord.ButtonStyle.secondary,
        )
        self.previous_button = discord.ui.Button(
            label="上一頁",
            emoji="◀️",
            style=discord.ButtonStyle.primary,
        )
        self.next_button = discord.ui.Button(
            label="下一頁",
            emoji="▶️",
            style=discord.ButtonStyle.primary,
        )
        self.last_button = discord.ui.Button(
            label="最後一頁",
            emoji="⏭️",
            style=discord.ButtonStyle.secondary,
        )

        self.first_button.callback = self._first_page
        self.previous_button.callback = self._previous_page
        self.next_button.callback = self._next_page
        self.last_button.callback = self._last_page

        self.add_item(self.first_button)
        self.add_item(self.previous_button)
        self.add_item(self.next_button)
        self.add_item(self.last_button)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        at_first = self.page <= 0
        at_last = self.page >= self.total_pages - 1
        self.first_button.disabled = at_first
        self.previous_button.disabled = at_first
        self.next_button.disabled = at_last
        self.last_button.disabled = at_last

    def build_embed(self) -> discord.Embed:
        start = self.page * self.PAGE_SIZE
        page_entries = self.entries[start:start + self.PAGE_SIZE]

        lines: list[str] = []
        for offset, (member, message_count) in enumerate(page_entries):
            rank = start + offset + 1
            if rank == 1:
                marker = "🥇"
            elif rank == 2:
                marker = "🥈"
            elif rank == 3:
                marker = "🥉"
            else:
                marker = f"#{rank}"

            display_name = discord.utils.escape_markdown(member.display_name)
            lines.append(
                f"{marker} **{display_name}**\n"
                f"> **{message_count:,}** 則訊息"
            )

        embed = discord.Embed(
            title="🏆 最佳幹話王",
            description="\n\n".join(lines) if lines else "目前還沒有可顯示的排名。",
            color=discord.Color.gold(),
        )
        if self.guild.icon:
            embed.set_thumbnail(url=self.guild.icon.url)

        embed.set_footer(
            text=(
                f"第 {self.page + 1} / {self.total_pages} 頁"
                f" · 共 {len(self.entries)} 人"
                " · 每頁 10 名"
            )
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "只有叫出這份排行榜的人可以翻頁。",
            ephemeral=True,
        )
        return False

    async def _show_page(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

    async def _first_page(self, interaction: discord.Interaction) -> None:
        self.page = 0
        await self._show_page(interaction)

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._show_page(interaction)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._show_page(interaction)

    async def _last_page(self, interaction: discord.Interaction) -> None:
        self.page = self.total_pages - 1
        await self._show_page(interaction)


class KingOfNonsense(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._dbs: dict[int, DatabaseManager] = {}
        self._db_init_locks: dict[int, asyncio.Lock] = {}
        self._write_locks: dict[int, asyncio.Lock] = {}
        self._seed_locks: dict[int, asyncio.Lock] = {}
        self._history_cutoffs: dict[int, datetime] = {}

    def _get_lock(
        self,
        locks: dict[int, asyncio.Lock],
        guild_id: int,
    ) -> asyncio.Lock:
        lock = locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[guild_id] = lock
        return lock

    async def _get_db(self, guild: discord.Guild) -> DatabaseManager:
        existing = self._dbs.get(guild.id)
        if existing is not None:
            return existing

        lock = self._get_lock(self._db_init_locks, guild.id)
        async with lock:
            existing = self._dbs.get(guild.id)
            if existing is not None:
                return existing

            db = DatabaseManager(guild.id, guild.name)
            await db.init_db()
            self._dbs[guild.id] = db
            return db

    def _is_countable_message(self, message: discord.Message) -> bool:
        return (
            message.guild is not None
            and not message.author.bot
            and message.webhook_id is None
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._is_countable_message(message):
            return

        guild = message.guild
        if guild is None:
            return

        db = await self._get_db(guild)
        write_lock = self._get_lock(self._write_locks, guild.id)

        async with write_lock:
            cutoff = self._history_cutoffs.get(guild.id)
            if cutoff is not None and message.created_at < cutoff:
                return
            await db.increment_message_count(message.author.id)

    async def _scan_messageable_history(
        self,
        channel,
        *,
        cutoff: datetime,
        counts: Counter[int],
    ) -> int:
        scanned = 0
        try:
            async for message in channel.history(
                limit=None,
                before=cutoff,
                oldest_first=False,
            ):
                if not self._is_countable_message(message):
                    continue
                counts[message.author.id] += 1
                scanned += 1
        except (
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException,
        ) as exc:
            self.bot.logger.warning(
                "最佳幹話王略過無法讀取的頻道/討論串 %s (%s): %s",
                getattr(channel, "id", "?"),
                getattr(channel, "name", "unknown"),
                exc,
            )
        return scanned

    async def _scan_guild_history(
        self,
        guild: discord.Guild,
        *,
        cutoff: datetime,
    ) -> tuple[Counter[int], int, int]:
        counts: Counter[int] = Counter()
        seen_channel_ids: set[int] = set()
        scanned_messages = 0
        scanned_channels = 0

        async def scan(channel) -> None:
            nonlocal scanned_messages, scanned_channels
            channel_id = getattr(channel, "id", None)
            if channel_id is None or channel_id in seen_channel_ids:
                return

            seen_channel_ids.add(channel_id)
            scanned_channels += 1
            scanned_messages += await self._scan_messageable_history(
                channel,
                cutoff=cutoff,
                counts=counts,
            )

        for channel in guild.text_channels:
            await scan(channel)

        for thread in guild.threads:
            await scan(thread)

        for parent in guild.channels:
            archived_threads = getattr(parent, "archived_threads", None)
            if archived_threads is None:
                continue
            try:
                async for thread in archived_threads(limit=None):
                    await scan(thread)
            except (
                discord.Forbidden,
                discord.HTTPException,
                TypeError,
            ) as exc:
                self.bot.logger.warning(
                    "最佳幹話王無法列出 %s 的封存討論串: %s",
                    getattr(parent, "id", "?"),
                    exc,
                )

        return counts, scanned_messages, scanned_channels

    async def _ensure_history_seeded(
        self,
        guild: discord.Guild,
        db: DatabaseManager,
    ) -> bool:
        if await db.is_message_history_seeded():
            return False

        seed_lock = self._get_lock(self._seed_locks, guild.id)
        async with seed_lock:
            if await db.is_message_history_seeded():
                return False

            cutoff = discord.utils.utcnow()
            write_lock = self._get_lock(self._write_locks, guild.id)

            async with write_lock:
                self._history_cutoffs[guild.id] = cutoff
                await db.clear_message_counts()

            counts, scanned_messages, scanned_channels = (
                await self._scan_guild_history(
                    guild,
                    cutoff=cutoff,
                )
            )

            async with write_lock:
                await db.bulk_increment_message_counts(counts)
                await db.set_message_history_seeded(True)

            self.bot.logger.info(
                "最佳幹話王歷史統計完成(guild=%s channels=%s messages=%s users=%s)",
                guild.id,
                scanned_channels,
                scanned_messages,
                len(counts),
            )
            return True

    async def _get_current_member_entries(
        self,
        guild: discord.Guild,
        db: DatabaseManager,
    ) -> list[tuple[discord.Member, int]]:
        if not guild.chunked:
            try:
                await guild.chunk(cache=True)
            except (discord.ClientException, discord.HTTPException):
                pass

        rows = await db.get_message_leaderboard()
        entries: list[tuple[discord.Member, int]] = []

        for row in rows:
            member = guild.get_member(row["user_id"])
            if member is None or member.bot:
                continue
            entries.append((member, row["message_count"]))

        return entries

    @app_commands.command(
        name="最佳幹話王",
        description="查看伺服器內使用者的訊息數排行榜",
    )
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "這個指令只能在伺服器內使用。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        db = await self._get_db(guild)

        try:
            seeded = await db.is_message_history_seeded()
            if not seeded:
                await interaction.edit_original_response(
                    content="第一次使用，正在統計 Bot 有權限讀取的歷史訊息……"
                )
                await self._ensure_history_seeded(guild, db)

            entries = await self._get_current_member_entries(guild, db)
        except Exception as exc:
            self.bot.logger.exception(
                "最佳幹話王建立排行榜失敗(guild=%s)",
                guild.id,
                exc_info=exc,
            )
            await interaction.edit_original_response(
                content=(
                    "排行榜統計失敗，請確認 Bot 擁有"
                    "「查看頻道」與「讀取訊息歷史」權限。"
                )
            )
            return

        if not entries:
            await interaction.edit_original_response(
                content="目前還沒有可統計的使用者訊息。"
            )
            return

        view = LeaderboardView(
            guild=guild,
            entries=entries,
            owner_id=interaction.user.id,
        )
        await interaction.edit_original_response(
            content=None,
            embed=view.build_embed(),
            view=view,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KingOfNonsense(bot))
