from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

class AdminTools(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            return False
        return member.guild_permissions.manage_guild

    @app_commands.command(name="sync", description="強制重新同步所有 Slash 命令（管理員）")
    async def sync_commands(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self._is_admin(interaction):
            await interaction.response.send_message("需要管理員權限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self.bot.settings.guild_id:
                guild_obj = discord.Object(id=self.bot.settings.guild_id)
                copied = self.bot.tree.copy_global_to_guild(guild_obj)
                await self.bot.tree.sync(guild=guild_obj)
                await interaction.followup.send(f"已重新同步 {len(copied)} 個指令（Guild 模式）。", ephemeral=True)
            else:
                synced = await self.bot.tree.sync()
                await interaction.followup.send(f"已重新同步 {len(synced)} 個指令（Global 模式）。", ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"同步失敗：{exc}", ephemeral=True)

    @app_commands.command(name="sync_global", description="將所有指令改為全域同步（管理員）")
    async def sync_global(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self._is_admin(interaction):
            await interaction.response.send_message("需要管理員權限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            synced = await self.bot.tree.sync()
            await interaction.followup.send(f"已全域同步 {len(synced)} 個指令。可能需要幾分鐘才在其他伺服器顯示。", ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"全域同步失敗：{exc}", ephemeral=True)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminTools(bot))
