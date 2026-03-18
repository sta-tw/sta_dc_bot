import discord
from discord import app_commands
from discord.ext import commands
from database.db_manager import DatabaseManager
import asyncio

def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

class Delete_Channel(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_manager = None

    async def ensure_db_manager(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        guild_name = interaction.guild.name

        if (self.db_manager is None or
            self.db_manager.guild_id != guild_id):
            self.db_manager = DatabaseManager(guild_id, guild_name)
            await self.db_manager.init_db()

            self.application_category_id = await self.db_manager.get_application_category()

        return self.db_manager

    @app_commands.command(name="delete_channel", description="刪除當前頻道（僅限機器人創建的頻道）")
    @is_admin()
    async def delete_channel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        await self.ensure_db_manager(interaction)

        is_bot_channel = await self.db_manager.is_bot_created_channel(interaction.channel.id)

        if not is_bot_channel:
            embed = discord.Embed(
                title="無法刪除",
                description="此頻道不是由機器人創建的，無法使用此命令刪除。",
                color=discord.Color.red()
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        channel_data = None
        application_owner_id = None
        for member in interaction.guild.members:
            user_channel = await self.db_manager.get_application_channel(member.id)
            if user_channel and user_channel["channel_id"] == interaction.channel.id:
                channel_data = user_channel
                application_owner_id = member.id
                break

        if not channel_data and (interaction.channel.name.startswith("身分組申請-") or interaction.channel.name.startswith("交換備審申請-")):
            prefix = "身分組申請-" if interaction.channel.name.startswith("身分組申請-") else "交換備審申請-"
            user_display_name = interaction.channel.name[len(prefix):]

            for member in interaction.guild.members:
                if member.display_name.lower() == user_display_name.lower():
                    application_owner_id = member.id
                    break
            else:
                application_owner_id = None
        elif not channel_data:
            application_owner_id = None

        is_admin = interaction.user.guild_permissions.administrator
        is_owner = application_owner_id and interaction.user.id == application_owner_id

        if not (is_admin or is_owner):
            embed = discord.Embed(
                title="權限不足",
                description="只有頻道申請人或管理員可以刪除此頻道。",
                color=discord.Color.red()
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        embed = discord.Embed(
            title="頻道將被刪除",
            description="頻道將在3秒後刪除...",
            color=discord.Color.orange()
        )

        if application_owner_id:
            await self.db_manager.update_application_status(application_owner_id, "closed")

        await self.db_manager.remove_bot_created_channel(interaction.channel.id)

        await interaction.followup.send(embed=embed)

        await asyncio.sleep(3)

        await interaction.channel.delete()

async def setup(bot):
    await bot.add_cog(Delete_Channel(bot))
