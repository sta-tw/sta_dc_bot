import asyncio
import discord
from discord.ext import commands, tasks
from database.db_manager import DatabaseManager

STALE_HOURS = 72


class ChannelCleanup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cleanup_task.start()

    def cog_unload(self):
        self.cleanup_task.cancel()

    @tasks.loop(hours=1)
    async def cleanup_task(self):
        for guild in self.bot.guilds:
            try:
                await self._cleanup_stale_channels(guild)
            except Exception as e:
                self.bot.logger.error(f"[ChannelCleanup] guild {guild.id} 清理失敗: {e}")

    @cleanup_task.before_loop
    async def before_cleanup_task(self):
        await self.bot.wait_until_ready()

    async def _cleanup_stale_channels(self, guild: discord.Guild):
        db = DatabaseManager(guild.id, guild.name)
        await db.init_db()

        pending = await db.get_applications_by_status("pending")
        now = discord.utils.utcnow()

        for app in pending:
            channel = guild.get_channel(app["channel_id"])

            if channel is None:
                await db.update_application_status(app["user_id"], "expired")
                continue

            if not channel.name.startswith("身分組申請-"):
                continue

            latest_human_message_time = await self._get_latest_human_message_time(channel)
            if latest_human_message_time is None:
                latest_human_message_time = channel.created_at

            inactive_hours = (now - latest_human_message_time).total_seconds() / 3600
            if inactive_hours < STALE_HOURS:
                continue

            try:
                embed = discord.Embed(
                    title="申請頻道閒置，頻道即將關閉",
                    description=(
                        f"此申請頻道已超過 **{STALE_HOURS} 小時**沒有任何成員發言，"
                        "將在 10 秒後自動關閉。\n"
                        "如需重新申請，請再次點擊申請按鈕。"
                    ),
                    color=discord.Color.orange()
                )
                await channel.send(embed=embed)
                await asyncio.sleep(10)
                await channel.delete(reason=f"申請頻道閒置（{STALE_HOURS} 小時無人發言）")
            except (discord.Forbidden, discord.NotFound):
                pass
            except Exception as e:
                self.bot.logger.warning(f"[ChannelCleanup] 刪除頻道 {app['channel_id']} 失敗: {e}")
            finally:
                await db.update_application_status(app["user_id"], "expired")
                await db.remove_bot_created_channel(app["channel_id"])

    async def _get_latest_human_message_time(self, channel: discord.abc.GuildChannel):
        if not isinstance(channel, discord.TextChannel):
            return None

        try:
            async for message in channel.history(limit=100, oldest_first=False):
                if message.author and not message.author.bot:
                    return message.created_at
        except (discord.Forbidden, discord.NotFound):
            return None
        except Exception as e:
            self.bot.logger.warning(f"[ChannelCleanup] 讀取頻道 {channel.id} 訊息失敗: {e}")
            return None

        return None


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelCleanup(bot))
