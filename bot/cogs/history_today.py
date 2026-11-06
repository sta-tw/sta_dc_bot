"""History lookups and daily delivery within the existing Discord bot."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date, datetime
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.utils.config_paths import ConfigPaths
from bot.utils.history_formatting import build_embed
from bot.utils.history_scheduler import DailyScheduler
from bot.utils.history_today import CATEGORY_LABELS, WIKIPEDIA_USER_AGENT, Category, HistoryClient, HistoryError, validate_date
from database.history import Store, Subscription


logger = logging.getLogger(__name__)
CATEGORY_CHOICES = [
    app_commands.Choice(name=CATEGORY_LABELS[item], value=item.value) for item in Category
]
COUNT_CHOICES = [
    app_commands.Choice(name=f"{count} 筆", value=str(count)) for count in range(1, 11)
] + [app_commands.Choice(name="隨機抽 5 筆", value="random5")]


def parse_count_selection(value: str) -> tuple[int, bool]:
    if type(value) is int:
        value = str(value)
    if value == "random5":
        return 5, True
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError("請選擇 1 到 10 筆，或隨機抽 5 筆。") from None
    if not 1 <= count <= 10 or str(count) != value:
        raise ValueError("請選擇 1 到 10 筆，或隨機抽 5 筆。")
    return count, False


class HistoryToday(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        *,
        database_path: str | Path | None = None,
        start_task: bool = True,
    ) -> None:
        self.bot = bot
        settings = getattr(getattr(bot, "settings", None), "history_today", None)
        self.timezone = getattr(settings, "timezone", "Asia/Taipei")
        self.store = Store(database_path if database_path is not None else ConfigPaths.HISTORY_DATABASE)
        self.history: HistoryClient | None = None
        self.session: aiohttp.ClientSession | None = None
        self.scheduler = DailyScheduler(self.store, self.send_daily)
        self._start_task = start_task
        self._closed = False

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            trust_env=True,
            headers={"User-Agent": WIKIPEDIA_USER_AGENT},
        )
        self.history = HistoryClient(self.session)
        if self._start_task:
            self.daily_loop.start()

    async def cog_unload(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self.daily_loop.get_task()
        self.daily_loop.cancel()
        try:
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            try:
                if self.history is not None:
                    await self.history.aclose()
            finally:
                try:
                    if self.session is not None and not self.session.closed:
                        await self.session.close()
                finally:
                    self.history = None
                    self.store.close()

    @tasks.loop(seconds=30)
    async def daily_loop(self) -> None:
        try:
            await self.scheduler.tick()
        except Exception as error:
            logger.error("排程檢查失敗：%s。", type(error).__name__)

    @daily_loop.before_loop
    async def before_daily_loop(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def can_post(channel: discord.TextChannel, member: discord.Member) -> bool:
        permissions = channel.permissions_for(member)
        return permissions.view_channel and permissions.send_messages and permissions.embed_links

    async def send_daily(self, subscription: Subscription, delivery_date: date) -> None:
        guild = self.bot.get_guild(subscription.guild_id)
        if guild is None:
            raise ValueError("機器人已不在這個伺服器。")
        channel = guild.get_channel(subscription.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(subscription.channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise ValueError("推送頻道不存在或不是文字頻道。")
        if guild.me is None or not self.can_post(channel, guild.me):
            raise PermissionError("推送頻道缺少查看、傳送訊息或嵌入連結權限。")
        if self.history is None:
            raise RuntimeError("資料查詢服務尚未就緒。")
        result = await self.history.get(delivery_date.month, delivery_date.day, subscription.category)
        await channel.send(
            embed=build_embed(
                result,
                subscription.count,
                random_count=subscription.random_count,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            message = "這個指令需要「管理伺服器」權限。"
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "請在 Discord 伺服器內使用這個指令。"
        elif isinstance(original, HistoryError):
            message = str(original)
        elif isinstance(original, discord.Forbidden):
            message = "機器人缺少頻道權限，請確認可查看頻道、傳送訊息及嵌入連結。"
        else:
            logger.error("指令執行失敗：%s。", type(original).__name__)
            message = "暫時無法完成這個指令，請稍後再試。"
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=message, embed=None)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.warning("無法回覆指令錯誤訊息。")

    @app_commands.command(name="today", description="查看歷史上的今天，或查詢指定月日")
    @app_commands.describe(
        month="月份（需與日期一起填寫）",
        day="日期（需與月份一起填寫）",
        category="資料分類",
        count="顯示筆數，或隨機抽 5 筆",
    )
    @app_commands.choices(category=CATEGORY_CHOICES, count=COUNT_CHOICES)
    async def today(
        self,
        interaction: discord.Interaction,
        month: app_commands.Range[int, 1, 12] | None = None,
        day: app_commands.Range[int, 1, 31] | None = None,
        category: str = "events",
        count: str = "5",
    ) -> None:
        if (month is None) != (day is None):
            await interaction.response.send_message("查詢指定日期時，請同時填寫 month 與 day。", ephemeral=True)
            return
        if month is None or day is None:
            now = datetime.now(ZoneInfo(self.timezone))
            month, day = now.month, now.day
        try:
            validate_date(month, day)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        try:
            count_value, random_count = parse_count_selection(count)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        if self.history is None:
            raise RuntimeError("資料查詢服務尚未就緒。")
        result = await self.history.get(month, day, Category(category))
        await interaction.edit_original_response(
            embed=build_embed(result, count_value, random_count=random_count)
        )

    @app_commands.command(name="daily", description="設定每天推送歷史上的今天（需管理伺服器權限）")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        channel="推送的文字頻道",
        hour="推送時間：小時，預設 9",
        minute="推送時間：分鐘，預設 0",
        timezone="IANA 時區，例如 Asia/Taipei",
        category="推送的資料分類",
        count="顯示筆數，或隨機抽 5 筆",
    )
    @app_commands.choices(category=CATEGORY_CHOICES, count=COUNT_CHOICES)
    async def daily(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        hour: app_commands.Range[int, 0, 23] = 9,
        minute: app_commands.Range[int, 0, 59] = 0,
        timezone: str | None = None,
        category: str = "events",
        count: str = "5",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            raise app_commands.NoPrivateMessage()
        if channel.guild.id != guild.id or guild.me is None or not self.can_post(channel, guild.me):
            await interaction.response.send_message(
                "請選擇本伺服器的文字頻道，並給機器人查看頻道、傳送訊息、嵌入連結權限。",
                ephemeral=True,
            )
            return
        if isinstance(interaction.user, discord.Member) and not channel.permissions_for(interaction.user).view_channel:
            await interaction.response.send_message("請選擇你可以查看的頻道。", ephemeral=True)
            return
        try:
            count_value, random_count = parse_count_selection(count)
            subscription = Subscription(
                guild_id=guild.id,
                channel_id=channel.id,
                hour=hour,
                minute=minute,
                timezone=(timezone or self.timezone).strip(),
                category=Category(category),
                count=count_value,
                random_count=random_count,
            )
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.scheduler.set_subscription(subscription)
        await interaction.edit_original_response(
            content=f"已設定每天 **{hour:02d}:{minute:02d}**（{subscription.timezone}）在 {channel.mention} "
            f"推送 **{CATEGORY_LABELS[subscription.category]}**，"
            f"{'隨機抽 5 筆' if random_count else f'最多 {count_value} 筆'}。\n"
            "若今天已過設定時間，會在下次排程檢查時推送；今天已成功推送則明天繼續。"
        )

    @app_commands.command(name="daily-status", description="查看此伺服器的每日推送設定")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def daily_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            raise app_commands.NoPrivateMessage()
        subscription = self.store.get(interaction.guild_id)
        if subscription is None:
            await interaction.response.send_message("尚未設定每日推送，請使用 /daily。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"頻道：<#{subscription.channel_id}>\n"
            f"時間：{subscription.hour:02d}:{subscription.minute:02d}（{subscription.timezone}）\n"
            f"分類：{CATEGORY_LABELS[subscription.category]} · "
            f"{'隨機抽 5 筆' if subscription.random_count else f'最多 {subscription.count} 筆'}\n"
            f"最後成功推送日期：{subscription.last_sent_date or '尚未推送'}",
            ephemeral=True,
        )

    @app_commands.command(name="daily-off", description="停用此伺服器的每日推送")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def daily_off(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            raise app_commands.NoPrivateMessage()
        await interaction.response.defer(ephemeral=True)
        removed = await self.scheduler.remove_subscription(interaction.guild_id)
        await interaction.edit_original_response(content="已停用每日推送。" if removed else "目前沒有每日推送設定。")

    @app_commands.command(name="history-help", description="查看歷史機器人的指令與資料來源")
    async def history_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "**歷史上的今天**\n"
            "`/today`：查看今天的大事記。\n"
            "`/today month:10 day:4`：查看指定月日，支援 2 月 29 日。\n"
            "`category`：大事記／出生／逝世／節假日與習俗／隨機分類；"
            "`count`：1～10 筆或隨機抽 5 筆。\n"
            "`/daily`：指定頻道、時間、時區及分類，每天自動推送。\n"
            "`/daily-status`、`/daily-off`：查看設定、停用推送。\n"
            "每日推送設定需要「管理伺服器」權限。\n"
            f"`/today` 預設時區：{self.timezone}。每個伺服器可設定一個推送頻道。\n"
            "資料來自中文維基百科；每則訊息附上來源連結。",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    settings = getattr(getattr(bot, "settings", None), "history_today", None)
    if getattr(settings, "enabled", True):
        guild_id = getattr(getattr(bot, "settings", None), "guild_id", 0)
        guild = discord.Object(id=guild_id) if guild_id else None
        await bot.add_cog(HistoryToday(bot), guild=guild)
