import discord
from discord import app_commands
from discord.ext import commands
from database.db_manager import DatabaseManager
from discord.ui import Modal, TextInput
from bot.utils.role_helper import get_or_create_role, update_role_id_in_config, get_role_color

def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

class RejectionReasonModal(Modal):
    def __init__(self, user_id: int, cog: 'Manage_Application'):
        super().__init__(title="拒絕申請原因")
        self.user_id = user_id
        self.cog = cog

        self.reason = TextInput(
            label="拒絕原因",
            placeholder="請輸入拒絕此申請的原因",
            required=True,
            style=discord.TextStyle.paragraph
        )

        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.guild.get_member(self.user_id)

        if not user:
            embed = discord.Embed(
                title="錯誤",
                description="找不到申請者，可能已經離開伺服器。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        await self.cog.db_manager.update_application_status(self.user_id, "rejected")

        channel_data = await self.cog.db_manager.get_application_channel(self.user_id)
        channel = interaction.guild.get_channel(channel_data["channel_id"]) if channel_data else None

        if channel:
            embed_channel = discord.Embed(
                title="申請已拒絕",
                description=f"{interaction.user.mention} 已拒絕此申請。",
                color=discord.Color.red()
            )

            embed_channel.add_field(
                name="拒絕原因",
                value=self.reason.value,
                inline=False
            )

            await channel.send(embed=embed_channel)

        embed = discord.Embed(
            title="狀態已更新",
            description=f"{user.mention} 的申請已拒絕。",
            color=discord.Color.red()
        )

        embed.add_field(
            name="拒絕原因",
            value=self.reason.value,
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

class Manage_Application(commands.Cog):
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

        return self.db_manager

    async def is_application_channel(self, channel_id: int, interaction: discord.Interaction) -> bool:
        db_manager = await self.ensure_db_manager(interaction)

        is_bot_channel = await db_manager.is_bot_created_channel(channel_id)

        if is_bot_channel:
            channel = interaction.guild.get_channel(channel_id)
            if channel and channel.name.startswith("身分組申請-"):
                return True

            for member in interaction.guild.members:
                user_channel = await db_manager.get_application_channel(member.id)
                if user_channel and user_channel["channel_id"] == channel_id:
                    return True

        return False

    async def get_channel_owner(self, channel_id: int, interaction: discord.Interaction) -> discord.Member:
        db_manager = await self.ensure_db_manager(interaction)

        for member in interaction.guild.members:
            user_channel = await db_manager.get_application_channel(member.id)
            if user_channel and user_channel["channel_id"] == channel_id:
                return member

        channel = interaction.guild.get_channel(channel_id)
        if channel and channel.name.startswith("身分組申請-"):
            user_display_name = channel.name[6:]

            for member in interaction.guild.members:
                if member.display_name.lower() == user_display_name.lower():
                    return member

        return None

    async def show_role_selection(self, interaction: discord.Interaction, user_id: int):
        db_manager = await self.ensure_db_manager(interaction)

        available_roles = await db_manager.get_available_roles()

        if not available_roles:
            embed = discord.Embed(
                title="錯誤",
                description=(

                    f"`config/guilds/{interaction.guild_id}/verification.json`\n\n"

                ),
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        select = discord.ui.Select(
            placeholder="選擇要賦予的身分組",
            custom_id="manage_application_role_select",
            options=[
                discord.SelectOption(
                    label=role["name"],
                    value=role["name"],
                    description=f"賦予 {role['name']} 身分組" + (" (將自動創建)" if role["id"] is None else "")
                )
                for role in available_roles
            ],
            max_values=len(available_roles)
        )

        async def role_select_callback(select_interaction):
            await select_interaction.response.defer(ephemeral=True)

            selected_role_names = select_interaction.data["values"]

            applicant = interaction.guild.get_member(user_id)
            if not applicant:
                await select_interaction.followup.send("找不到申請者，可能已經離開伺服器。", ephemeral=True)
                return

            try:
                await db_manager.update_application_status(user_id, "approved")

                added_roles = []
                failed_roles = []

                for role_name in selected_role_names:
                    role_info = None
                    for available_role in available_roles:
                        if available_role["name"] == role_name:
                            role_info = available_role
                            break

                    if not role_info:
                        failed_roles.append(f"{role_name} (配置錯誤)")
                        continue

                    role_id = role_info["id"]

                    role = await get_or_create_role(
                        interaction.guild,
                        role_id,
                        role_name,
                        get_role_color(role_name),
                        f"自動創建 {role_name} 身分組"
                    )

                    if not role:
                        failed_roles.append(f"{role_name} (創建失敗)")
                        continue

                    if role_id is None or (role_id and role.id != role_id):
                        await update_role_id_in_config(db_manager, role_name, role.id)

                    try:
                        await db_manager.save_verification_role(str(user_id), role.id, role.name)
                        added_roles.append(role.mention)
                    except Exception as e:
                        failed_roles.append(f"{role.name} ({str(e)})")

                embed = discord.Embed(
                    title="申請已批准",
                    description=f"{applicant.mention} 的申請已批准！",
                    color=discord.Color.green()
                )

                if added_roles:
                    embed.add_field(
                        name="已設置的身分組",
                        value="\n".join(added_roles),
                        inline=False
                    )

                if failed_roles:
                    embed.add_field(
                        name="設置失敗的身分組",
                        value="\n".join(failed_roles),
                        inline=False
                    )

                await select_interaction.followup.send(embed=embed, ephemeral=True)

                channel_data = await db_manager.get_application_channel(user_id)
                if channel_data:
                    channel = interaction.guild.get_channel(channel_data["channel_id"])
                    if channel:
                        channel_embed = discord.Embed(
                            title="申請已批准",
                            description=f"{select_interaction.user.mention} 已批准此申請。",
                            color=discord.Color.green()
                        )

                        if added_roles:
                            channel_embed.add_field(
                                name="已設置的身分組",
                                value="\n".join(added_roles),
                                inline=False
                            )

                        instruction_embed = discord.Embed(
                            title="下一步",
                            description="請回到機器人的驗證按鈕處點擊「驗證身份」按鈕來獲取您的身分組。",
                            color=discord.Color.blue()
                        )

                        await channel.send(content=applicant.mention, embed=channel_embed)
                        await channel.send(embed=instruction_embed)

            except Exception as e:
                await select_interaction.followup.send(f"設置身分組時發生錯誤: {str(e)}", ephemeral=True)

        select.callback = role_select_callback

        view = discord.ui.View(timeout=None)
        view.add_item(select)

        embed = discord.Embed(
            title="批准申請",
            description=f"請選擇要賦予給申請者的身分組（可複選）：",
            color=discord.Color.blue()
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="manage_application", description="管理當前申請頻道的申請")
    @app_commands.choices(action=[
        app_commands.Choice(name="關閉申請", value="close"),
        app_commands.Choice(name="批准申請", value="approve"),
        app_commands.Choice(name="拒絕申請", value="reject"),
    ])
    @is_admin()
    async def manage_application(self, interaction: discord.Interaction, action: app_commands.Choice[str]):
        action_value = action.value

        if action_value != "reject":
            await interaction.response.defer(ephemeral=True)

        await self.ensure_db_manager(interaction)

        if action_value == "reject":
            if not await self.is_application_channel(interaction.channel_id, interaction):
                embed = discord.Embed(
                    title="指令限制",
                    description="此指令只能在申請頻道中使用。",
                    color=discord.Color.red()
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            user = await self.get_channel_owner(interaction.channel_id, interaction)

            if not user:
                embed = discord.Embed(
                    title="找不到申請人",
                    description="無法確定此頻道的申請人。請確認頻道是否為有效的申請頻道。",
                    color=discord.Color.red()
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            modal = RejectionReasonModal(user.id, self)
            return await interaction.response.send_modal(modal)

        if not await self.is_application_channel(interaction.channel_id, interaction):
            embed = discord.Embed(
                title="指令限制",
                description="此指令只能在申請頻道中使用。",
                color=discord.Color.red()
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        user = await self.get_channel_owner(interaction.channel_id, interaction)

        if not user:
            embed = discord.Embed(
                title="找不到申請人",
                description="無法確定此頻道的申請人。請確認頻道是否為有效的申請頻道。",
                color=discord.Color.red()
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        channel_data = await self.db_manager.get_application_channel(user.id)

        if not channel_data:
            embed = discord.Embed(
                title="錯誤",
                description="在資料庫中找不到此申請頻道的資訊。",
                color=discord.Color.red()
            )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        if action_value == "close":
            channel = interaction.guild.get_channel(channel_data["channel_id"])

            if channel:
                overwrites = channel.overwrites
                if user in overwrites:
                    del overwrites[user]
                    await channel.edit(overwrites=overwrites)

                embed_channel = discord.Embed(
                    title="申請已關閉",
                    description=f"{interaction.user.mention} 已關閉此申請。申請者已無法存取此頻道。",
                    color=discord.Color.blue()
                )
                await channel.send(embed=embed_channel)

            await self.db_manager.update_application_status(user.id, "closed")

            embed = discord.Embed(
                title="申請已關閉",
                description=f"{user.mention} 的申請已關閉，申請者已無法存取頻道。",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        elif action_value == "approve":
            return await self.show_role_selection(interaction, user.id)

        else:
            embed = discord.Embed(
                title="無效的操作",
                description="有效的操作有：close, approve, reject",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Manage_Application(bot))
