import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import logging
from discord.ui import View, Button
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('MemberVerification')

class BatchAssignConfirmView(View):
    def __init__(self, source_role: discord.Role, target_role: discord.Role, members: list):
        super().__init__(timeout=300)
        self.source_role = source_role
        self.target_role = target_role
        self.members = members
        self.current_page = 0
        self.per_page = 10
        self.total_pages = (len(members) - 1) // self.per_page + 1
        
        self.prev_button = Button(label="上一頁", style=discord.ButtonStyle.secondary, disabled=True)
        self.prev_button.callback = self.prev_page
        
        self.next_button = Button(label="下一頁", style=discord.ButtonStyle.secondary, disabled=(self.total_pages <= 1))
        self.next_button.callback = self.next_page
        
        self.confirm_button = Button(label="確定執行", style=discord.ButtonStyle.success, emoji="✅")
        self.confirm_button.callback = self.confirm_assign
        
        self.cancel_button = Button(label="取消", style=discord.ButtonStyle.danger, emoji="❌")
        self.cancel_button.callback = self.cancel_assign
        
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)
    
    def create_embed(self):
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_members = self.members[start:end]
        
        embed = discord.Embed(
            title="批量給予身分組確認",
            description=f"**來源身分組:** {self.source_role.mention}\n**目標身分組:** {self.target_role.mention}\n**找到成員數:** {len(self.members)} 位",
            color=discord.Color.blue()
        )
        
        member_list = []
        for i, member in enumerate(page_members, start=start+1):
            member_list.append(f"{i}. {member.mention}")
        
        embed.add_field(
            name=f"成員名單（第 {self.current_page + 1}/{self.total_pages} 頁）",
            value="\n".join(member_list) if member_list else "無成員",
            inline=False
        )
        
        return embed
    
    async def prev_page(self, interaction: discord.Interaction):
        self.current_page -= 1
        self.prev_button.disabled = (self.current_page == 0)
        self.next_button.disabled = False
        
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def next_page(self, interaction: discord.Interaction):
        self.current_page += 1
        self.next_button.disabled = (self.current_page >= self.total_pages - 1)
        self.prev_button.disabled = False
        
        embed = self.create_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def confirm_assign(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        for child in self.children:
            child.disabled = True
        
        await interaction.edit_original_response(view=self)
        
        success_count = 0
        already_has_count = 0
        error_count = 0
        error_messages = []
        
        for member in self.members:
            try:
                if self.target_role in member.roles:
                    already_has_count += 1
                    continue
                
                await member.add_roles(self.target_role)
                success_count += 1
                
            except discord.Forbidden:
                error_count += 1
                error_messages.append(f"{member.name}: 權限不足")
                logger.error(f"權限不足: 無法給予 {member.name} {self.target_role.name} 身分組")
            except Exception as e:
                error_count += 1
                error_messages.append(f"{member.name}: {str(e)}")
                logger.error(f"給予身分組時出錯: {e}")
        
        result_embed = discord.Embed(
            title="批量給予身分組完成",
            description=(
                f"**來源身分組:** {self.source_role.mention}\n"
                f"**目標身分組:** {self.target_role.mention}\n\n"
                f"**處理總數:** {len(self.members)}\n"
                f"**成功給予:** {success_count}\n"
                f"**已有身分組:** {already_has_count}\n"
                f"**失敗:** {error_count}"
            ),
            color=discord.Color.green()
        )
        
        if error_messages:
            error_text = "\n".join(error_messages[:10])
            if len(error_messages) > 10:
                error_text += f"\n... 還有 {len(error_messages) - 10} 筆錯誤"
            result_embed.add_field(name="錯誤詳情", value=error_text, inline=False)
        
        await interaction.followup.send(embed=result_embed)
    
    async def cancel_assign(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        
        embed = discord.Embed(
            title="已取消操作",
            description="批量給予身分組已取消",
            color=discord.Color.red()
        )
        
        await interaction.response.edit_message(embed=embed, view=self)

class MemberVerification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data_file = "data/member_roles.json"
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

    @app_commands.command(name="batch_assign_role", description="批量給予身分組")
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
                try:
                    target_role = await guild.create_role(
                        name="特選老人",
                        color=discord.Color.purple(),
                        reason="自動建立特選老人身分組"
                    )
                    logger.info(f"成功建立「特選老人」身分組")
                except Exception as e:
                    await interaction.followup.send(f"建立「特選老人」身分組失敗: {str(e)}", ephemeral=True)
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
