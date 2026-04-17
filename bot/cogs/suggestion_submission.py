from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput, View, Button, Select

from database.db_manager import DatabaseManager


async def _resolve_suggestion_user_id(interaction: discord.Interaction) -> int:
    db = DatabaseManager(interaction.guild.id, interaction.guild.name)
    await db.init_db()
    if interaction.channel:
        uid = await db.get_suggestion_user_by_channel(interaction.channel.id)
        if uid:
            return uid
        parent_id = getattr(interaction.channel, "parent_id", None)
        if parent_id:
            uid = await db.get_suggestion_user_by_channel(parent_id)
            if uid:
                return uid
    return 0


async def _resolve_suggestion_type(interaction: discord.Interaction) -> str:
    if interaction.message and interaction.message.embeds:
        embed = interaction.message.embeds[0]
        for field in embed.fields:
            if field.name in {"建議類型", "類型", "提交類型"}:
                return field.value

        if embed.description:
            match = re.search(r"(?:建議類型|類型)[:：]\s*\*\*(.+?)\*\*", embed.description)
            if match:
                return match.group(1)

    if interaction.channel and isinstance(interaction.channel, discord.Thread):
        match = re.match(r"^審核-(.+)-(.+)建議$", interaction.channel.name)
        if match:
            return match.group(2)

    return "未指定"


class SuggestionDetailModal(Modal):
    def __init__(self, cog: "SuggestionSubmission", user_id: int, suggestion_type: str):
        super().__init__(title=f"提交建議：{suggestion_type}")
        self.cog = cog
        self.user_id = user_id
        self.suggestion_type = suggestion_type

        self.title_input = TextInput(
            label="建議標題",
            placeholder="例如：新增 AI 指令文件自動同步工具",
            required=True,
            max_length=100,
        )
        self.content_input = TextInput(
            label="建議內容",
            placeholder="請描述你想新增/調整的功能與預期用途",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.benefit_input = TextInput(
            label="預期效益",
            placeholder="例如：提升管理效率、降低人工操作錯誤",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )

        self.add_item(self.title_input)
        self.add_item(self.content_input)
        self.add_item(self.benefit_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        db = await self.cog.ensure_db_manager(interaction)

        suggestion_channel_data = await db.get_suggestion_channel(self.user_id)
        suggestion_channel = None
        if suggestion_channel_data:
            suggestion_channel = interaction.guild.get_channel(suggestion_channel_data["channel_id"])

        if not isinstance(suggestion_channel, discord.TextChannel):
            await interaction.followup.send("找不到建議頻道，請重新執行 /submit_suggestion。", ephemeral=True)
            return

        status_embed = discord.Embed(
            title="您的建議正在審核中",
            color=discord.Color.yellow(),
        )
        status_embed.add_field(name="建議類型", value=self.suggestion_type, inline=True)
        status_embed.add_field(name="建議標題", value=self.title_input.value, inline=True)
        status_embed.add_field(name="建議內容", value=self.content_input.value, inline=False)
        if self.benefit_input.value:
            status_embed.add_field(name="預期效益", value=self.benefit_input.value, inline=False)
        status_embed.add_field(name="提交時間", value=f"<t:{int(interaction.created_at.timestamp())}:F>", inline=False)

        await suggestion_channel.send(embed=status_embed)

        await db.update_suggestion_status(self.user_id, "submitted")

        thread_name = f"審核-{interaction.user.display_name}-{self.suggestion_type}建議"[:95]
        existing_thread = next((t for t in suggestion_channel.threads if t.name == thread_name), None)

        if existing_thread:
            review_thread = existing_thread
            await review_thread.send("-------------------\n**有新的建議更新！**\n-------------------")
        else:
            review_thread = await suggestion_channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
            )

            support_role_ids = getattr(self.cog.bot.settings, "support_role_ids", []) if self.cog.bot else []
            for role_id in support_role_ids:
                role = interaction.guild.get_role(role_id)
                if role:
                    for member in role.members:
                        try:
                            await review_thread.add_user(member)
                        except discord.HTTPException:
                            continue

            admin_role = discord.utils.get(interaction.guild.roles, name="管理員")
            if admin_role:
                for member in admin_role.members:
                    try:
                        await review_thread.add_user(member)
                    except discord.HTTPException:
                        continue

        support_mentions = []
        seen_role_ids: set[int] = set()
        support_role_ids = getattr(self.cog.bot.settings, "support_role_ids", []) if self.cog.bot else []
        for role_id in support_role_ids:
            if role_id in seen_role_ids:
                continue
            role = interaction.guild.get_role(role_id)
            if role:
                support_mentions.append(role.mention)
                seen_role_ids.add(role_id)

        notify_role_id = self.cog.get_suggestion_review_settings(db)
        if notify_role_id and notify_role_id not in seen_role_ids:
            notify_role = interaction.guild.get_role(notify_role_id)
            if notify_role:
                support_mentions.append(notify_role.mention)
                seen_role_ids.add(notify_role_id)

        mention_text = " ".join(support_mentions) if support_mentions else "@管理員"

        review_embed = discord.Embed(
            title="建議審核面板",
            description=(
                f"**提交者：** {interaction.user.mention}\n"
                f"**建議類型：** {self.suggestion_type}\n"
                f"**提交時間：** <t:{int(interaction.created_at.timestamp())}:F>"
            ),
            color=discord.Color.blue(),
        )
        review_embed.add_field(name="建議標題", value=self.title_input.value, inline=False)
        review_embed.add_field(name="建議內容", value=self.content_input.value, inline=False)
        if self.benefit_input.value:
            review_embed.add_field(name="預期效益", value=self.benefit_input.value, inline=False)

        await review_thread.send(
            content=f"{mention_text} 有新的建議需要審核！",
            embed=review_embed,
            view=SuggestionReviewView(self.cog, self.user_id, self.suggestion_type),
        )

        await interaction.followup.send("建議已送出，等待管理員審核。", ephemeral=True)


class SuggestionTypeSelectView(View):
    def __init__(self, cog: "SuggestionSubmission", user_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.user_id = user_id

        self.type_select = Select(
            placeholder="選擇建議類型",
            custom_id="suggestion_type_select",
            options=[
                discord.SelectOption(label="新增工具", value="新增工具", description="新增指令、功能或機器人工具"),
                discord.SelectOption(label="流程優化", value="流程優化", description="改善現有流程或管理方式"),
                discord.SelectOption(label="活動提案", value="活動提案", description="提出活動企劃或執行建議"),
                discord.SelectOption(label="其他建議", value="其他建議", description="其他尚未分類的提案"),
            ],
            max_values=1,
        )
        self.type_select.callback = self.select_callback

        self.close_button = Button(
            label="關閉建議",
            style=discord.ButtonStyle.danger,
            custom_id="suggestion_type_close",
        )
        self.close_button.callback = self.close_callback

        self.add_item(self.type_select)
        self.add_item(self.close_button)

    async def select_callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("只有提交者可操作。", ephemeral=True)
            return

        suggestion_type = interaction.data["values"][0]
        await interaction.response.send_modal(SuggestionDetailModal(self.cog, self.user_id, suggestion_type))

    async def close_callback(self, interaction: discord.Interaction) -> None:
        db = await self.cog.ensure_db_manager(interaction)

        if interaction.user.id != self.user_id and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("只有提交者或管理員可關閉。", ephemeral=True)
            return

        await self.cog.close_suggestion_channel(interaction, db, self.user_id, "已關閉建議頻道。")


class SuggestionPanelView(View):
    def __init__(self, cog: "SuggestionSubmission"):
        super().__init__(timeout=None)
        self.cog = cog

        open_button = Button(
            label="提交建議",
            style=discord.ButtonStyle.success,
            custom_id="open_suggestion_panel",
        )
        open_button.callback = self.open_callback

        self.add_item(open_button)

    async def open_callback(self, interaction: discord.Interaction) -> None:
        await self.cog.open_suggestion_flow(interaction)


class SuggestionManageTypeView(View):
    def __init__(self, cog: "SuggestionSubmission", user_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.user_id = user_id

        type_select = Select(
            placeholder="選擇建議分類並核准",
            custom_id="suggestion_manage_type_select",
            options=[
                discord.SelectOption(label="新增工具", value="新增工具"),
                discord.SelectOption(label="流程優化", value="流程優化"),
                discord.SelectOption(label="活動提案", value="活動提案"),
                discord.SelectOption(label="其他建議", value="其他建議"),
            ],
            max_values=1,
        )
        type_select.callback = self.approve_with_type

        close_button = Button(
            label="關閉建議",
            style=discord.ButtonStyle.danger,
            custom_id="suggestion_manage_close",
        )
        close_button.callback = self.close_callback

        self.add_item(type_select)
        self.add_item(close_button)

    async def approve_with_type(self, interaction: discord.Interaction) -> None:
        if self.user_id == 0:
            self.user_id = await _resolve_suggestion_user_id(interaction)
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("只有管理員可以核准。", ephemeral=True)
            return

        selected_type = interaction.data["values"][0]
        await self.cog.approve_suggestion(interaction, self.user_id, selected_type)

    async def close_callback(self, interaction: discord.Interaction) -> None:
        if self.user_id == 0:
            self.user_id = await _resolve_suggestion_user_id(interaction)
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("只有管理員可以關閉。", ephemeral=True)
            return

        db = await self.cog.ensure_db_manager(interaction)
        await self.cog.close_suggestion_channel(interaction, db, self.user_id, "建議已關閉。")


class SuggestionReviewView(View):
    def __init__(self, cog: "SuggestionSubmission", user_id: int, suggestion_type: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.user_id = user_id
        self.suggestion_type = suggestion_type

        approve_button = Button(label="核准", style=discord.ButtonStyle.success, custom_id="suggestion_review_approve")
        reject_button = Button(label="拒絕", style=discord.ButtonStyle.danger, custom_id="suggestion_review_reject")

        approve_button.callback = self.approve_callback
        reject_button.callback = self.reject_callback

        self.add_item(approve_button)
        self.add_item(reject_button)

    async def approve_callback(self, interaction: discord.Interaction) -> None:
        if self.user_id == 0:
            self.user_id = await _resolve_suggestion_user_id(interaction)
        if self.suggestion_type == "placeholder":
            self.suggestion_type = await _resolve_suggestion_type(interaction)
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("只有管理員可以核准。", ephemeral=True)
            return

        embed = discord.Embed(
            title="核准建議",
            description="請選擇建議分類後核准，或按下關閉建議。",
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=SuggestionManageTypeView(self.cog, self.user_id),
            ephemeral=True,
        )

    async def reject_callback(self, interaction: discord.Interaction) -> None:
        if self.user_id == 0:
            self.user_id = await _resolve_suggestion_user_id(interaction)
        if self.suggestion_type == "placeholder":
            self.suggestion_type = await _resolve_suggestion_type(interaction)
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("只有管理員可以拒絕。", ephemeral=True)
            return

        await interaction.response.send_modal(SuggestionRejectionModal(self.cog, self.user_id, self))


class SuggestionRejectionModal(Modal):
    def __init__(self, cog: "SuggestionSubmission", user_id: int, review_view: SuggestionReviewView):
        super().__init__(title="拒絕建議原因")
        self.cog = cog
        self.user_id = user_id
        self.review_view = review_view

        self.reason = TextInput(
            label="拒絕原因",
            placeholder="請輸入拒絕原因",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=500,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = await self.cog.ensure_db_manager(interaction)
        await db.update_suggestion_status(self.user_id, "rejected")

        for child in self.review_view.children:
            child.disabled = True

        await interaction.response.edit_message(view=self.review_view)

        thread_embed = discord.Embed(
            title="已拒絕建議",
            description=f"{interaction.user.mention} 已拒絕此建議。",
            color=discord.Color.red(),
        )
        thread_embed.add_field(name="拒絕原因", value=self.reason.value, inline=False)
        await interaction.followup.send(embed=thread_embed)

        main_channel = interaction.channel.parent if interaction.channel else None
        applicant = interaction.guild.get_member(self.user_id)
        if main_channel and applicant:
            user_embed = discord.Embed(
                title="建議未通過",
                description=f"{applicant.mention}，你的建議未通過。",
                color=discord.Color.red(),
            )
            user_embed.add_field(name="拒絕原因", value=self.reason.value, inline=False)
            await main_channel.send(embed=user_embed)


class SuggestionSubmission(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_manager: DatabaseManager | None = None

    async def ensure_db_manager(self, interaction: discord.Interaction) -> DatabaseManager:
        guild_id = interaction.guild.id
        guild_name = interaction.guild.name

        if self.db_manager is None or self.db_manager.guild_id != guild_id:
            self.db_manager = DatabaseManager(guild_id, guild_name)
            await self.db_manager.init_db()

        return self.db_manager

    def get_suggestion_review_settings(self, db: DatabaseManager) -> int | None:
        config = db.load_guild_settings()
        settings = config.get("settings", {})
        notify_role_id = settings.get("suggestion_notify_role")

        try:
            notify_role_id = int(notify_role_id) if notify_role_id else None
        except (TypeError, ValueError):
            notify_role_id = None

        return notify_role_id

    def save_suggestion_review_settings(
        self,
        db: DatabaseManager,
        category_id: int,
        notify_role_id: int | None,
    ) -> None:
        config = db.load_guild_settings()
        if "settings" not in config or not isinstance(config["settings"], dict):
            config["settings"] = {}

        config["settings"]["suggestion_category"] = int(category_id)
        config["settings"]["suggestion_notify_role"] = int(notify_role_id) if notify_role_id else None

        db.save_guild_settings(config)

    async def open_suggestion_flow(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        db = await self.ensure_db_manager(interaction)

        current = await db.get_suggestion_channel(interaction.user.id)
        if current and current.get("status") not in {"closed", "rejected", "approved", "cancelled"}:
            existing_channel = interaction.guild.get_channel(current["channel_id"])
            if existing_channel:
                await interaction.followup.send(
                    f"你已有進行中的建議頻道：{existing_channel.mention}",
                    ephemeral=True,
                )
                return

        config = db.load_guild_settings()
        category_id = config.get("settings", {}).get("suggestion_category")
        category = interaction.guild.get_channel(category_id) if category_id else None

        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send("尚未設定建議分類，請管理員先執行 /suggestion_setup。", ephemeral=True)
            return

        channel_name = f"建議-{interaction.user.display_name}"[:90]
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        admin_role = discord.utils.get(interaction.guild.roles, name="管理員")
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel = await interaction.guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="建立建議提交頻道",
        )

        await db.register_bot_created_channel(channel.id)
        await db.save_suggestion_channel(interaction.user.id, channel.id)

        selection_embed = discord.Embed(
            title="提交建議",
            description="請先選擇建議類型，再填寫詳細內容。",
            color=discord.Color.blue(),
        )

        await channel.send(embed=selection_embed, view=SuggestionTypeSelectView(self, interaction.user.id))
        await interaction.followup.send(f"已建立建議頻道：{channel.mention}", ephemeral=True)

    async def close_suggestion_channel(
        self,
        interaction: discord.Interaction,
        db: DatabaseManager,
        user_id: int,
        response_text: str,
    ) -> None:
        channel_data = await db.get_suggestion_channel(user_id)
        if not channel_data:
            await interaction.response.send_message("找不到建議頻道資訊。", ephemeral=True)
            return

        channel = interaction.guild.get_channel(channel_data["channel_id"])
        applicant = interaction.guild.get_member(user_id)

        if channel and applicant:
            overwrites = channel.overwrites
            if applicant in overwrites:
                del overwrites[applicant]
                await channel.edit(overwrites=overwrites)

        await db.update_suggestion_status(user_id, "closed")

        if interaction.response.is_done():
            await interaction.followup.send(response_text, ephemeral=True)
        else:
            await interaction.response.send_message(response_text, ephemeral=True)

    async def notify_channel_owner(
        self,
        interaction: discord.Interaction,
        user_id: int,
        title: str,
        description: str,
        color: discord.Color,
    ) -> None:
        db = await self.ensure_db_manager(interaction)
        channel_data = await db.get_suggestion_channel(user_id)
        if not channel_data:
            return

        channel = interaction.guild.get_channel(channel_data["channel_id"])
        if not channel:
            return

        embed = discord.Embed(title=title, description=description, color=color)
        await channel.send(embed=embed)

    async def approve_suggestion(self, interaction: discord.Interaction, user_id: int, suggestion_type: str) -> None:
        db = await self.ensure_db_manager(interaction)
        await db.update_suggestion_status(user_id, "approved")

        await self.notify_channel_owner(
            interaction,
            user_id,
            title="建議已核准",
            description=f"{interaction.user.mention} 已核准此建議，分類：**{suggestion_type}**。",
            color=discord.Color.green(),
        )

        if interaction.response.is_done():
            await interaction.followup.send("已核准此建議。", ephemeral=True)
        else:
            await interaction.response.send_message("已核准此建議。", ephemeral=True)

    @app_commands.command(name="suggestion_setup", description="設定建議提交系統（管理員）")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        category="建議頻道要建立在哪個分類",
        notify_role="審核通知要提及的身分組（可留空）",
    )
    async def suggestion_setup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        notify_role: discord.Role | None = None,
    ) -> None:
        db = await self.ensure_db_manager(interaction)
        self.save_suggestion_review_settings(
            db,
            category_id=category.id,
            notify_role_id=notify_role.id if notify_role else None,
        )

        embed = discord.Embed(
            title="建議系統設定完成",
            description=(
                f"建議分類：{category.mention}\n"
                "審核方式：建議頻道內私密審核串\n"
                f"通知身分組：{notify_role.mention if notify_role else '未設定'}"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="submit_suggestion", description="建立建議提交頻道")
    async def submit_suggestion(self, interaction: discord.Interaction) -> None:
        await self.open_suggestion_flow(interaction)

    @app_commands.command(name="manage_suggestion", description="管理建議頻道（管理員）")
    @app_commands.choices(action=[
        app_commands.Choice(name="核准建議", value="approve"),
        app_commands.Choice(name="拒絕建議", value="reject"),
        app_commands.Choice(name="關閉建議", value="close"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def manage_suggestion(self, interaction: discord.Interaction, action: app_commands.Choice[str]) -> None:
        db = await self.ensure_db_manager(interaction)

        channel_id = interaction.channel_id
        user_id = await db.get_suggestion_user_by_channel(channel_id)

        if not user_id and interaction.channel and getattr(interaction.channel, "parent_id", None):
            user_id = await db.get_suggestion_user_by_channel(interaction.channel.parent_id)

        if not user_id:
            await interaction.response.send_message("此頻道不是有效的建議頻道。", ephemeral=True)
            return

        if action.value == "close":
            await self.close_suggestion_channel(interaction, db, user_id, "建議已關閉。")
            return

        if action.value == "reject":
            await db.update_suggestion_status(user_id, "rejected")
            await self.notify_channel_owner(
                interaction,
                user_id,
                title="建議未通過",
                description=f"{interaction.user.mention} 已拒絕此建議。",
                color=discord.Color.red(),
            )
            await interaction.response.send_message("已拒絕此建議。", ephemeral=True)
            return

        embed = discord.Embed(
            title="核准建議",
            description="請選擇建議分類後核准，或直接按下關閉建議。",
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, view=SuggestionManageTypeView(self, user_id), ephemeral=True)

    @app_commands.command(name="suggestion_panel", description="建立建議提交面板（管理員）")
    @app_commands.checks.has_permissions(administrator=True)
    async def suggestion_panel(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="建議提交",
            description="點擊下方按鈕開始提交建議。",
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, view=SuggestionPanelView(self))


async def setup(bot: commands.Bot) -> None:
    cog = SuggestionSubmission(bot)
    await bot.add_cog(cog)
    bot.add_view(SuggestionPanelView(cog))
    bot.add_view(SuggestionTypeSelectView(cog, 0))
    bot.add_view(SuggestionManageTypeView(cog, 0))
    bot.add_view(SuggestionReviewView(cog, 0, "placeholder"))
