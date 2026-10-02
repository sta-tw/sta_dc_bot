from __future__ import annotations

import asyncio
from uuid import uuid4

from bot.utils.config_paths import ConfigPaths
from bot.utils.message_history import (
    HistoryProgress,
    HistorySnapshot,
    collect_history,
    discover_scope,
    is_countable_message,
    load_snapshot,
    next_boundary_id,
)

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
        page_entries = self.entries[start : start + self.PAGE_SIZE]

        lines: list[str] = []
        for offset, (member, message_count) in enumerate(page_entries):
            rank = start + offset + 1
            marker = f"#{rank}"
            display_name = discord.utils.escape_markdown(member.display_name)
            lines.append(f"{marker} **{display_name}** **{message_count:,}** 則訊息")

        embed = discord.Embed(
            title="最佳幹話王",
            description="\n".join(lines) if lines else "目前還沒有可顯示的排名。",
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
        return True

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
        self._history_cutoffs: dict[int, int] = {}
        self._seed_tokens: dict[int, str] = {}
        self._snapshots: dict[int, HistorySnapshot | None] = {}
        self._seed_tasks: dict[int, asyncio.Task] = {}
        self._seed_progress: dict[int, HistoryProgress] = {}

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
            snapshot_path = ConfigPaths.DATA_DIR / "leaderboard_preloads" / f"{guild.id}.json"
            if not await db.is_message_history_seeded() or (
                snapshot_path.exists() and not await db.is_message_history_verified()
            ):
                await self._prepare_history_seed(guild, db)
            self._dbs[guild.id] = db
            return db

    def _is_countable_message(self, message: discord.Message) -> bool:
        return is_countable_message(message)

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
            if cutoff is not None and message.id < cutoff:
                return
            await db.increment_message_count(message.author.id)

    async def _ensure_history_seeded(
        self,
        guild: discord.Guild,
        db: DatabaseManager,
    ) -> bool:
        async with self._get_lock(self._seed_locks, guild.id):
            if await db.is_message_history_seeded():
                return False
            if guild.id not in self._seed_tokens:
                await self._prepare_history_seed(guild, db)
                if await db.is_message_history_seeded():
                    return False
            cutoff_id = self._history_cutoffs[guild.id]
            delay = (discord.utils.snowflake_time(cutoff_id) - discord.utils.utcnow()).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            progress = self._seed_progress.setdefault(guild.id, HistoryProgress())
            async with asyncio.timeout(6 * 60 * 60):
                scope = await discover_scope(guild, self.bot.user.id, progress)
                counts = await collect_history(
                    scope,
                    upper_id=cutoff_id,
                    progress=progress,
                    snapshot=self._snapshots.get(guild.id),
                )
            snapshot = self._snapshots.get(guild.id)
            async with self._get_lock(self._write_locks, guild.id):
                committed = await db.finish_message_history_seed(
                    self._seed_tokens[guild.id], counts, snapshot.checksum if snapshot else "full-history"
                )
            progress.phase = "統計完成"
            self.bot.logger.info(
                "最佳幹話王歷史統計完成(guild=%s channels=%s messages=%s users=%s)",
                guild.id, progress.channels_done, sum(counts.values()), len(counts),
            )
            return committed

    async def _run_history_seed(self, guild: discord.Guild, db: DatabaseManager) -> None:
        progress = self._seed_progress[guild.id]
        try:
            await self._ensure_history_seeded(guild, db)
        except asyncio.CancelledError:
            progress.error = "統計工作已取消"
            raise
        except Exception as exc:
            progress.error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            self.bot.logger.exception("最佳幹話王初始化失敗(guild=%s)", guild.id)

    def _start_history_seed(self, guild: discord.Guild, db: DatabaseManager) -> HistoryProgress:
        task = self._seed_tasks.get(guild.id)
        if task is None or task.done():
            self._seed_progress[guild.id] = HistoryProgress()
            self._seed_tasks[guild.id] = asyncio.create_task(
                self._run_history_seed(guild, db), name=f"leaderboard-seed-{guild.id}"
            )
        return self._seed_progress[guild.id]

    async def cog_unload(self) -> None:
        tasks = list(self._seed_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _get_current_member_entries(
        self,
        guild: discord.Guild,
        db: DatabaseManager,
    ) -> list[tuple[discord.Member, int]]:
        if not guild.chunked:
            try:
                await asyncio.wait_for(guild.chunk(cache=True), timeout=20)
            except (discord.ClientException, discord.HTTPException, TimeoutError) as exc:
                raise ValueError("成員資料尚未載入完成，請稍後重新查詢。") from exc

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

        try:
            db = await self._get_db(guild)
            seeded = await db.is_message_history_seeded()
            if not seeded:
                previous = self._seed_progress.get(guild.id)
                previous_error = previous.error if previous else None
                progress = self._start_history_seed(guild, db)
                text = progress.text()
                if previous_error and progress.error is None:
                    text = f"上次統計失敗：{previous_error}。已重新嘗試。\n" + text
                await interaction.edit_original_response(content=text)
                return

            entries = await self._get_current_member_entries(guild, db)
        except Exception as exc:
            self.bot.logger.exception(
                "failed(guild=%s)",
                guild.id,
                exc_info=exc,
            )
            if not interaction.is_expired():
                await interaction.edit_original_response(
                    content=str(exc) if isinstance(exc, ValueError) else (
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
