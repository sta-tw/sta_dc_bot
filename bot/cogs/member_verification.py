import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import logging
from utils.member_verification_ui import BatchAssignConfirmView
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('MemberVerification')

class MemberVerification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data_file = "config/assign_roles.json"
        self.role_members = self.load_data()
    def load_data(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info(f"成功載入 {len(data)} 筆身分組資料")
                    return data
            except json.JSONDecodeError:
                logger.error(f"加載 {self.data_file} 失敗：JSON 格式錯誤")
        else:
            logger.warning(f"找不到資料檔案: {self.data_file}")
        return {}
    @app_commands.command(name="assign_roles", description="根據 JSON 檔案批次分配身分組（管理員）")
    @app_commands.checks.has_permissions(administrator=True)
    async def assign_roles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.role_members = self.load_data()

        if not self.role_members:
            await interaction.followup.send("沒有可分配的資料，請檢查 JSON 檔案。", ephemeral=True)
            return
        guild = interaction.guild
        results = {
            "success": [],
            "not_found": [],
            "role_not_found": [],
            "already_has": [],
            "errors": []
        }

        for role_name, username in self.role_members.items():
            try:
                role = discord.utils.get(guild.roles, name=role_name)
                if not role:
                    results["role_not_found"].append(f"身分組 `{role_name}` 不存在")
                    logger.warning(f"找不到身分組: {role_name}")
                    continue

                member = None
                if "#" in username:
                    name, discrim = username.split("#", 1)
                    member = discord.utils.get(guild.members, name=name, discriminator=discrim)
                else:
                    members = [m for m in guild.members if m.name == username]
                    if members:
                        member = members[0]

                if not member:
                    results["not_found"].append(f"找不到用戶 `{username}` (可能不在伺服器中)")
                    logger.warning(f"找不到用戶: {username}")
                    continue

                if role in member.roles:
                    results["already_has"].append(f"`{member.name}`#{member.discriminator} 已經有 `{role_name}` 身分組")
                    continue

                await member.add_roles(role)
                results["success"].append(f"`{member.name}`#{member.discriminator} → `{role_name}`")
                logger.info(f"成功分配: {member.name}#{member.discriminator} -> {role_name}")

            except discord.Forbidden:
                results["errors"].append(f"權限不足，無法分配 `{role_name}` 給 `{username}`")
                logger.error(f"權限不足: 無法分配 {role_name} 給 {username}")

            except Exception as e:
                results["errors"].append(f"分配 `{role_name}` 給 `{username}` 時發生錯誤: {str(e)}")
                logger.error(f"分配身分組時出現錯誤: {e}")

        report = "##身分組分配結果\n\n"

        if results["success"]:
            report += f"### 成功分配 ({len(results['success'])})\n"
            report += "\n".join(results["success"][:10])
            if len(results["success"]) > 10:
                report += f"\n... 還有 {len(results['success']) - 10} 筆成功記錄"
            report += "\n\n"

        if results["already_has"]:
            report += f"### 已有身分組 ({len(results['already_has'])})\n"
            report += "\n".join(results["already_has"][:5])
            if len(results["already_has"]) > 5:
                report += f"\n... 還有 {len(results['already_has']) - 5} 筆"
            report += "\n\n"

        if results["not_found"]:
            report += f"### 找不到用戶 ({len(results['not_found'])})\n"
            report += "\n".join(results["not_found"][:5])
            if len(results["not_found"]) > 5:
                report += f"\n... 還有 {len(results['not_found']) - 5} 筆"
            report += "\n\n"

        if results["role_not_found"]:
            report += f"### 找不到身分組 ({len(results['role_not_found'])})\n"
            report += "\n".join(results["role_not_found"])
            report += "\n\n"

        if results["errors"]:
            report += f"### 錯誤 ({len(results['errors'])})\n"
            report += "\n".join(results["errors"])
        if len(report) > 1900:
            chunks = [report[i:i+1900] for i in range(0, len(report), 1900)]
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await interaction.followup.send(chunk, ephemeral=True)
                else:
                    await interaction.followup.send(chunk, ephemeral=True)
        else:
            await interaction.followup.send(report, ephemeral=True)

    @assign_roles.error
    async def assign_roles_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.errors.MissingPermissions):
            await interaction.response.send_message("你沒有權限執行此命令，此命令只能由管理員使用。", ephemeral=True)

    @app_commands.command(name="batch_assign_role", description="批量給予身分組（管理員）")
    @app_commands.describe(
        source_role="持有此身分組的成員",
        target_role="要給予的身分組（預設：特選老人）"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def batch_assign_role(
        self, 
        interaction: discord.Interaction,
        source_role: discord.Role,
        target_role: discord.Role = None
    ):
        await interaction.response.defer(thinking=True)

        guild = interaction.guild

        if not target_role:
            target_role = discord.utils.get(guild.roles, name="特選老人")
            if not target_role:
                await interaction.followup.send("找不到預設的目標身分組「特選老人」，請指定一個有效的目標身分組。", ephemeral=True)
                return

        members = source_role.members
        if not members:
            await interaction.followup.send(f"沒有任何成員擁有 {source_role.mention} 身分組", ephemeral=True)
            return
        
        view = BatchAssignConfirmView(source_role, target_role, members)
        embed = view.create_embed()
        
        await interaction.followup.send(embed=embed, view=view)

    @batch_assign_role.error
    async def batch_assign_role_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.errors.MissingPermissions):
            await interaction.response.send_message("你沒有權限執行此命令，此命令只能由管理員使用。", ephemeral=True)

async def setup(bot):
    await bot.add_cog(MemberVerification(bot))
