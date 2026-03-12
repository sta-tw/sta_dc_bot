import discord
from discord.ui import View, Button
import logging

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
            color=discord.Color.red(),
        )
        
        await interaction.response.edit_message(embed=embed, view=self)
