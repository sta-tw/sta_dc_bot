import discord
from discord.ui import Button, View, Modal, TextInput, Select
from database.db_manager import DatabaseManager
from bot.utils.role_helper import get_or_create_role, update_role_id_in_config, get_role_color
import json
import asyncio

class Verfication_View(View):
    def __init__(self, bot=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.emoji = self.bot.emoji

        verify_button = Button(
            label="驗證身份",
            style=discord.ButtonStyle.primary,
            emoji=self.emoji.get('verify_check'),
            custom_id="verify_identity"
        )
        verify_button.callback = self.verify_callback

        apply_button = Button(
            label="申請身份組",
            style=discord.ButtonStyle.success,
            emoji=self.emoji.get('F'),
            custom_id="apply_role"
        )
        apply_button.callback = self.apply_callback

        self.add_item(verify_button)
        self.add_item(apply_button)

    async def verify_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        db_manager = DatabaseManager(interaction.guild.id, interaction.guild.name)

        user_id = str(interaction.user.id)

        all_roles = await db_manager.get_all_user_roles(user_id)
        if all_roles:
            added_roles = []
            failed_roles = []

            for role_info in all_roles:
                role_id = role_info.get("role_id")
                role_name = role_info.get("role_name")

                role = await get_or_create_role(
                    interaction.guild,
                    role_id,
                    role_name,
                    get_role_color(role_name),
                    f"自動創建 {role_name} 身份組"
                )

                if not role:
                    failed_roles.append(f"{role_name} (創建失敗)")
                    continue

                if role.id != role_id:
                    await update_role_id_in_config(db_manager, role_name.lower().replace(" ", "_"), role.id)

                try:
                    await interaction.user.add_roles(role)
                    added_roles.append(role.name)
                except discord.Forbidden:
                    failed_roles.append(role.name)

            if added_roles:
                embed = discord.Embed(
                    title="驗證成功",
                    description=f"已給予您以下身份組：\n- {', '.join(added_roles)}",
                    color=discord.Color.green()
                )

                if failed_roles:
                    embed.add_field(
                        name="以下身份組給予失敗",
                        value=f"- {', '.join(failed_roles)}\n請聯繫管理員手動添加。",
                        inline=False
                    )
            else:
                embed = discord.Embed(
                    title="驗證失敗",
                    description="您尚未驗證身份，請先申請身份組。",
                    color=discord.Color.red()
                )

                if failed_roles:
                    embed.add_field(
                        name="失敗原因",
                        value=f"找不到或無權限添加以下身份組：\n- {', '.join(failed_roles)}",
                        inline=False
                    )
        else:
            role_id = await db_manager.get_verification_role(user_id)
            if role_id:
                role = interaction.guild.get_role(role_id)

                if not role:
                    role_name = "Verified"
                    role = await get_or_create_role(
                        interaction.guild,
                        role_id,
                        role_name,
                        discord.Color.green(),
                        f"自動創建 {role_name} 身份組"
                    )

                if role:
                    try:
                        await interaction.user.add_roles(role)
                        embed = discord.Embed(
                            title="驗證成功",
                            description=f"已給予您「{role.name}」身份組！",
                            color=discord.Color.green()
                        )
                    except discord.Forbidden:
                        embed = discord.Embed(
                            title="權限錯誤",
                            description="機器人無法給予該身份組。請聯繫管理員。",
                            color=discord.Color.red()
                        )
                else:
                    embed = discord.Embed(
                        title="角色不存在",
                        description="找不到對應的身份組。請聯繫管理員。",
                        color=discord.Color.red()
                    )
            else:
                embed = discord.Embed(
                    title="驗證失敗",
                    description="您尚未驗證身份，請先申請身份組。",
                    color=discord.Color.red()
                )

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def apply_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        db_manager = DatabaseManager(interaction.guild.id, interaction.guild.name)
        await db_manager.init_db()

        channel_data = await db_manager.get_application_channel(interaction.user.id)

        if (channel_data):
            channel = interaction.guild.get_channel(channel_data["channel_id"])

            if channel:
                embed = discord.Embed(
                    title="已有存在的申請頻道",
                    description=f"你有一個申請正在進行中！請前往 {self.emoji.get('arrow')} {channel.mention} 查看",
                    color=discord.Color.yellow()
                )
                return await interaction.followup.send(embed=embed, ephemeral=True)

        category = None
        category_id = await db_manager.get_application_category()
        category = interaction.guild.get_channel(category_id)

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        mod_role = discord.utils.get(interaction.guild.roles, name="管理員")
        if mod_role:
            overwrites[mod_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel_name = f"身份組申請-{interaction.user.display_name}"
        channel = await interaction.guild.create_text_channel(
            name=channel_name,
            overwrites=overwrites,
            category=category
        )

        await db_manager.register_bot_created_channel(channel.id)
        await db_manager.save_application_channel(interaction.user.id, channel.id)

        embed = discord.Embed(
            title="身份組申請",
            description=f"歡迎 {interaction.user.mention}！\n請選擇你的身份 {self.emoji.get('loading1')}",
            color=discord.Color.blue()
        )

        await channel.send(embed=embed, view=RoleSelectionView(interaction.user.id, self.bot))

        embed = discord.Embed(
            title="申請頻道已建立",
            description=f"你的身份組申請頻道已建立 {self.emoji.get('arrow')} {channel.mention}",
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

class RoleSelectionView(View):
    def __init__(self, user_id: int, bot=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.bot = bot
        self.emoji = bot.emoji

        select = Select(
            placeholder="請選擇您的身份",
            options=[
                discord.SelectOption(
                    label="應屆特選生",
                    description="115 特選生選這個",
                    emoji=self.emoji.get('golden_dimond'),
                    value="special_student"
                ),
                discord.SelectOption(
                    label="特選老人",
                    description="歷屆特選生選這個",
                    emoji=self.emoji.get('purple_dimond'),
                    value="special_elder"
                )
            ],
            custom_id=f"role_select_{user_id}" if user_id != 0 else "role_select_placeholder"
        )

        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id", "")
        if self.user_id == 0 and custom_id.startswith("role_select_"):
            try:
                self.user_id = int(custom_id.split("_")[-1])
            except (ValueError, IndexError):
                print(f"無法從 custom_id {custom_id} 中提取用戶 ID")
                self.user_id = interaction.user.id

        selected_role = interaction.data["values"][0]

        if selected_role == "special_student":
            role_name = "115特選生"
            form = StudentApplicationForm(self.user_id, self, self.bot)
        else:
            role_name = "歷屆特選生"
            form = ElderApplicationForm(self.user_id, self, self.bot)

        embed = discord.Embed(
            title=f"您的身份是{role_name}",
            description="請填寫下方申請表格",
            color=discord.Color.blue()
        )

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"{self.emoji.get('cat1')} 請先點擊下方按鈕填寫表單 {self.emoji.get('cat1')}", view=form)

class StudentApplicationForm(View):
    def __init__(self, user_id: int, form_view=None, bot=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.form_view = form_view
        self.bot = bot
        self.emoji = bot.emoji

        submit_button = Button(
            label="點擊此按鈕填寫表單",
            style=discord.ButtonStyle.primary,
            emoji=self.emoji.get('red_light'),
            custom_id=f"submit_student_form_{user_id}" if user_id != 0 else "submit_student_form_placeholder"
        )

        submit_button.callback = self.show_form
        self.add_item(submit_button)

    async def show_form(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id", "")
        if self.user_id == 0 and custom_id.startswith("submit_student_form_"):
            try:
                self.user_id = int(custom_id.split("_")[-1])
            except (ValueError, IndexError):
                self.user_id = interaction.user.id

        if interaction.user.id != self.user_id:
            embed = discord.Embed(
                title="權限不足",
                description="只有申請人可以填寫表單！",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        await interaction.response.send_modal(StudentApplicationModal(self.user_id, self, self.bot))

class StudentApplicationModal(Modal):
    def __init__(self, user_id: int, form_view=None, bot=None):
        super().__init__(title="115 特選生申請表")
        self.user_id = user_id
        self.form_view = form_view
        self.bot = bot
        self.emoji = bot.emoji

        self.role = TextInput(
            label="想要申請哪些身份組",
            placeholder="資工系、不分系、資工+不分、等等",
            required=True
        )

        self.verify = TextInput(
            label="請先輸入提交的證明類型並在提交後上傳證明",
            placeholder="學生證、在學證明、等等",
            required=True,
        )

        self.add_item(self.role)
        self.add_item(self.verify)

    async def on_submit(self, interaction: discord.Interaction):
        application_data = {
            "type": "115 特選生",
            "fields": [
                {"name": "申請者身份", "value": "115 特選生", "inline": False},
                {"name": "要申請的身份組", "value": self.role.value, "inline": True},
                {"name": "證明", "value": self.verify.value, "inline": True}
            ],
            "admin_fields": [
                {
                    "name": "要申請的身份組",
                    "value": self.role.value,
                    "inline": False
                },
                {
                    "name": "證明",
                    "value": self.verify.value,
                    "inline": False
                }
            ]
        }

        status_embed = discord.Embed(
            title=f"{self.emoji.get('announce1')} 表單已填寫完畢",
            description=(

                f"{self.emoji.get('num1')} 請先在此頻道上傳相關證明文件\n"
                f"{self.emoji.get('num2')} 確認資料無誤且已上傳證明後，點擊下方「送出申請」按鈕\n\n"
            ),
            color=discord.Color.blue()
        )

        for field in application_data["fields"]:
            status_embed.add_field(**field)

        await interaction.response.send_message(
            embed=status_embed,
            view=SubmitApplicationView(self.user_id, application_data, self.bot)
        )

        if self.form_view:
            for child in self.form_view.children:
                child.disabled = True
            try:
                original_message = interaction.message
                if original_message:
                    await original_message.edit(view=self.form_view)
            except:
                try:
                    await interaction.followup.edit_message(
                        message_id=interaction.message.id,
                        view=self.form_view
                    )
                except:
                    pass

class ElderApplicationForm(View):
    def __init__(self, user_id: int, form_view=None, bot=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.form_view = form_view
        self.bot = bot
        self.emoji = bot.emoji

        submit_button = Button(
            label="點擊此按鈕填寫表單",
            style=discord.ButtonStyle.primary,
            emoji=self.emoji.get('red_light'),
            custom_id=f"submit_elder_form_{user_id}" if user_id != 0 else "submit_elder_form_placeholder"
        )

        submit_button.callback = self.show_form
        self.add_item(submit_button)

    async def show_form(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id", "")
        if self.user_id == 0 and custom_id.startswith("submit_elder_form_"):
            try:
                self.user_id = int(custom_id.split("_")[-1])
            except (ValueError, IndexError):
                self.user_id = interaction.user.id

        if interaction.user.id != self.user_id:
            embed = discord.Embed(
                title="權限不足",
                description="只有申請人可以提交表單。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        await interaction.response.send_modal(ElderApplicationModal(self.user_id, self, self.bot))

class ElderApplicationModal(Modal):
    def __init__(self, user_id: int, form_view=None, bot=None):
        super().__init__(title="特選老人申請表")
        self.user_id = user_id
        self.form_view = form_view
        self.bot = bot
        self.emoji = bot.emoji

        self.verify = TextInput(
            label="請先輸入提交的證明類型並在提交後上傳證明",
            placeholder="錄取通知書、新聞、等等",
            required=True,
        )

        self.add_item(self.verify)

    async def on_submit(self, interaction: discord.Interaction):
        application_data = {
            "type": "特選老人",
            "fields": [
                {"name": "申請者身份", "value": "特選老人", "inline": False},
                {"name": "證明", "value": self.verify.value, "inline": False}
            ],
            "admin_fields": [
                {
                    "name": "證明",
                    "value": self.verify.value,
                    "inline": False
                }
            ]
        }

        status_embed = discord.Embed(
            title=f"{self.emoji.get('announce1')} 表單已填寫完畢",
            description=(

                f"{self.emoji.get('num1')} 請先在此頻道上傳相關證明文件\n"
                f"{self.emoji.get('num2')} 確認資料無誤且已上傳證明後，點擊下方「送出申請」按鈕\n\n"
            ),
            color=discord.Color.blue()
        )

        for field in application_data["fields"]:
            status_embed.add_field(**field)

        await interaction.response.send_message(
            embed=status_embed,
            view=SubmitApplicationView(self.user_id, application_data, self.bot)
        )

        if self.form_view:
            for child in self.form_view.children:
                child.disabled = True
            try:
                original_message = interaction.message
                if original_message:
                    await original_message.edit(view=self.form_view)
            except:
                try:
                    await interaction.followup.edit_message(
                        message_id=interaction.message.id,
                        view=self.form_view
                    )
                except:
                    pass

class SubmitApplicationView(View):
    def __init__(self, user_id: int, application_data: dict, bot=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.application_data = application_data
        self.bot = bot
        self.emoji = bot.emoji

        submit_button = Button(
            label="送出申請",
            style=discord.ButtonStyle.success,
            emoji=self.emoji.get('send'),
            custom_id=f"submit_application_{user_id}"
        )
        submit_button.callback = self.submit_callback

        self.add_item(submit_button)

    async def submit_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            embed = discord.Embed(
                title="權限不足",
                description="只有申請人可以送出申請。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        has_file_or_link = False
        async for msg in interaction.channel.history(limit=50):
            if msg.author.id == interaction.user.id:
                if msg.attachments or "http://" in msg.content.lower() or "https://" in msg.content.lower():
                    has_file_or_link = True
                    break
        
        if not has_file_or_link:
            embed = discord.Embed(
                title="缺少證明文件or連結",
                description="請先上傳證明文件或提供連結後再送出申請",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        status_embed = discord.Embed(
            title=f"您的申請正在審核中 {self.emoji.get('loading2')}",
            description=(

            ),
            color=discord.Color.yellow()
        )

        for field in self.application_data["fields"]:
            status_embed.add_field(**field)

        status_embed.add_field(
            name="提交時間",
            value=f"<t:{int(interaction.created_at.timestamp())}:F>",
            inline=False
        )

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        await interaction.followup.send(embed=status_embed)

        thread_name = f"審核-{interaction.user.display_name}-{self.application_data['type']}申請"

        existing_thread = None
        for thread in interaction.channel.threads:
            if thread.name == thread_name:
                existing_thread = thread
                break

        if existing_thread:
            thread = existing_thread
            await thread.send("-------------------\n**有新的申請！**\n-------------------")
        else:
            thread = await interaction.channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread
            )

            mod_role = discord.utils.get(interaction.guild.roles, name="管理員")
            if mod_role:
                for member in mod_role.members:
                    try:
                        await thread.add_user(member)
                    except:
                        continue

        config = DatabaseManager(interaction.guild.id, interaction.guild.name)
        with open(config.config_json, "r", encoding="utf-8") as file:
            admin_id = json.load(file)["roles"]["admin"]

        if admin_id:
            admin_role = interaction.guild.get_role(admin_id)
            mention_text = admin_role.mention if admin_role else "@管理員"
        else:
            mention_text = "@管理員"

        admin_embed = discord.Embed(
            title=f"{self.emoji.get('frog1')} 申請審核面板",
            description=(
                f"**申請者：** {interaction.user.mention}\n"
                f"**申請者身份：** {self.application_data['type']}\n"
                f"**提交時間：** <t:{int(interaction.created_at.timestamp())}:F>\n\n"
            ),
            color=discord.Color.blue()
        )

        for field in self.application_data["admin_fields"]:
            admin_embed.add_field(**field)

        approval_view = ApplicationApprovalView(self.user_id, self.application_data["type"], self.bot)

        await thread.send(
            content=f"{mention_text} 有新的申請需要審核！",
            embed=admin_embed,
            view=approval_view
        )

class ApplicationApprovalView(View):
    def __init__(self, user_id: int, role_name: str = "placeholder", bot=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.role_name = role_name
        self.bot = bot
        self.emoji = bot.emoji

        approve_button = Button(
            label="批准",
            style=discord.ButtonStyle.success,
            emoji=self.emoji.get('green_motion'),
            custom_id=f"approve_{user_id}" if user_id != 0 else "approve_placeholder"
        )
        approve_button.callback = self.approve_callback

        reject_button = Button(
            label="拒絕",
            style=discord.ButtonStyle.danger,
            emoji=self.emoji.get('angry_motion'),
            custom_id=f"reject_{user_id}" if user_id != 0 else "reject_placeholder"
        )
        reject_button.callback = self.reject_callback

        self.add_item(approve_button)
        self.add_item(reject_button)

    async def extract_data_from_interaction(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id", "")
        if self.user_id == 0 and custom_id.startswith(("approve_", "reject_")):
            try:
                self.user_id = int(custom_id.split("_")[-1])
            except (ValueError, IndexError):
                print(f"無法從 custom_id {custom_id} 中提取用戶 ID")
                return False

        return True

    async def show_role_selection(self, interaction: discord.Interaction):
        db_manager = DatabaseManager(interaction.guild.id, interaction.guild.name)

        available_roles = await db_manager.get_available_roles()

        if not available_roles:
            embed = discord.Embed(
                title="錯誤",
                description="找不到任何可用的身份組。請先在 JSON 中設置可用的身份組。",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        unique_roles = {role["id"]: role for role in available_roles}.values()

        select = Select(
            placeholder="選擇要給予的身份組",
            custom_id=f"role_select_{self.user_id}",
            options=[
                discord.SelectOption(
                    label=role["name"],
                    value=str(role["id"]),
                    description=f"賦予 {role['name']} 身份組"
                )
                for role in unique_roles
            ],
            max_values=len(unique_roles)
        )
        select.callback = self.role_selected_callback

        view = View(timeout=None)
        view.add_item(select)

        embed = discord.Embed(
            title="選擇身份組",
            description="請選擇要給予的身份組（可複選）：",
            color=discord.Color.blue()
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def role_selected_callback(self, interaction: discord.Interaction):
        role_ids = [int(value) for value in interaction.data["values"]]

        user = interaction.guild.get_member(self.user_id)
        if not user:
            embed = discord.Embed(
                title="錯誤",
                description="找不到申請者。",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            db_manager = DatabaseManager(interaction.guild.id, interaction.guild.name)

            special_chat_id = await db_manager.get_channel_id(channel_name="special_chat")
            special_chat = interaction.guild.get_channel(special_chat_id)

            await db_manager.update_application_status(self.user_id, "approved")

            added_roles = []
            failed_roles = []

            for role_id in role_ids:
                role = interaction.guild.get_role(role_id)
                role_name = role.name if role else f"Role_{role_id}"

                if not role:
                    role = await get_or_create_role(
                        interaction.guild,
                        role_id,
                        role_name,
                        get_role_color(role_name),
                        f"自動創建 {role_name} 身份組"
                    )

                if not role:
                    failed_roles.append(f"ID: {role_id} (創建失敗)")
                    continue
                try:
                    await db_manager.save_verification_role(str(self.user_id), role.id, role.name)
                    added_roles.append(role.mention)
                except Exception as e:
                    failed_roles.append(f"{role.name} ({str(e)})")

            thread_embed = discord.Embed(
                title="已批准申請",
                description=f"{interaction.user.mention} 已批准此申請。",
                color=discord.Color.green()
            )

            if added_roles:
                thread_embed.add_field(
                    name="已給予的身份組",
                    value="\n".join(added_roles),
                    inline=False
                )

            if failed_roles:
                thread_embed.add_field(
                    name="給予失敗的身份組",
                    value="\n".join(failed_roles),
                    inline=False
                )

            for child in self.children:
                child.disabled = True

            try:
                await interaction.message.edit(view=self)
            except discord.NotFound:
                pass

            await interaction.response.send_message(embed=thread_embed)

            main_channel = interaction.channel.parent
            if main_channel:
                main_channel_embed = discord.Embed(
                    title=f"申請已通過 {self.emoji.get('green_motion')}",
                    description=f"{self.emoji.get('frog2')}{self.emoji.get('frog2')}{self.emoji.get('frog2')}{self.emoji.get('frog2')}{self.emoji.get('frog2')}",
                    color=discord.Color.green()
                )
                await main_channel.send(content=user.mention, embed=main_channel_embed)

                instruction_embed = discord.Embed(
                    title="下一步",
                    description=f"可以前往特選生聊天區閒聊囉! {self.emoji.get('arrow')} {special_chat.mention}",
                    color=discord.Color.yellow()
                )
                await main_channel.send(embed=instruction_embed)

                close_embed = discord.Embed(
                    title="關閉申請頻道",
                    description="沒事就可以關起來囉！",
                    color=discord.Color.blue()
                )
                await main_channel.send(embed=close_embed, view=ReapplyView(self.user_id, self.bot))

        except Exception as e:
            error_embed = discord.Embed(
                title="錯誤",
                description=f"設定身份組時發生錯誤: {str(e)}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=error_embed, ephemeral=True)

    async def approve_callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="權限不足",
                description="只有管理員才能批准申請。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if not await self.extract_data_from_interaction(interaction):
            embed = discord.Embed(
                title="數據錯誤",
                description="無法獲取用戶信息。請嘗試使用 `/manage_application` 命令進行管理。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        await self.show_role_selection(interaction)

    async def reject_callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="權限不足",
                description="只有管理員才能拒絕申請。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if not await self.extract_data_from_interaction(interaction):
            embed = discord.Embed(
                title="數據錯誤",
                description="無法獲取用戶信息。請嘗試使用 `/manage_application` 命令進行管理。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        await interaction.response.send_modal(RejectionReasonModal(self.user_id, self, self.bot))

class RejectionReasonModal(Modal):
    def __init__(self, user_id: int, approval_view: 'ApplicationApprovalView', bot=None):
        super().__init__(title="拒絕申請視窗")
        self.user_id = user_id
        self.approval_view = approval_view
        self.bot = bot
        self.emoji = bot.emoji

        self.reason = TextInput(
            label="拒絕原因",
            placeholder="請輸入拒絕原因",
            required=True,
            style=discord.TextStyle.paragraph
        )

        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.guild.get_member(self.user_id)

        db_manager = DatabaseManager(interaction.guild.id, interaction.guild.name)
        await db_manager.update_application_status(self.user_id, "rejected")

        thread_embed = discord.Embed(
            title="已拒絕申請",
            description=f"{interaction.user.mention} 已拒絕此申請",
            color=discord.Color.red()
        )

        thread_embed.add_field(
            name="拒絕原因",
            value=self.reason.value,
            inline=False
        )

        for child in self.approval_view.children:
            child.disabled = True

        await interaction.response.edit_message(view=self.approval_view)
        await interaction.followup.send(embed=thread_embed)

        main_channel = interaction.channel.parent
        if main_channel:
            main_channel_embed = discord.Embed(
                title="申請已拒絕",
                description=f"{user.mention}，您的申請未通過。",
                color=discord.Color.red()
            )

            main_channel_embed.add_field(
                name="拒絕原因",
                value=self.reason.value,
                inline=False
            )

            await main_channel.send(embed=main_channel_embed)

            options_embed = discord.Embed(
                title="接下來你可以...",
                description=f"**重新申請** - 重新填寫申請表單\n"
                            f"**取消申請** - 按下此按鈕後將會關閉此申請頻道",
                color=discord.Color.blue()
            )

            view = View(timeout=None)

            reapply_button = Button(
                label="重新申請",
                style=discord.ButtonStyle.primary,
                custom_id=f"reapply_{self.user_id}"
            )
            reapply_button.callback = self.reapply_callback

            close_button = Button(
                label="取消申請",
                style=discord.ButtonStyle.danger,
                custom_id=f"close_{self.user_id}"
            )
            close_button.callback = self.close_callback

            view.add_item(reapply_button)
            view.add_item(close_button)

            await main_channel.send(embed=options_embed, view=view)

    async def reapply_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            embed = discord.Embed(
                title="權限不足",
                description="只有申請人可以重新申請。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        db_manager = DatabaseManager(interaction.guild.id, interaction.guild.name)
        await db_manager.update_application_status(self.user_id, "pending")

        for child in interaction.message.components[0].children:
            child.disabled = True
        await interaction.message.edit(view=None)

        selection_embed = discord.Embed(
            title="身份組申請",
            description=f"請選擇你的身份 {self.emoji.get('loading1')}",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=selection_embed, view=RoleSelectionView(self.user_id, self.bot))

    async def close_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            embed = discord.Embed(
                title="權限不足",
                description="只有申請人可以取消申請。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        db_manager = DatabaseManager(interaction.guild.id, interaction.guild.name)
        await db_manager.update_application_status(self.user_id, "cancelled")

        for child in interaction.message.components[0].children:
            child.disabled = True
        await interaction.message.edit(view=None)

        embed = discord.Embed(
            title="申請已取消",
            description="此頻道將在 3 秒後關閉",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed)

        await asyncio.sleep(3)

        overwrites = interaction.channel.overwrites
        if interaction.user in overwrites:
            del overwrites[interaction.user]
            await interaction.channel.edit(overwrites=overwrites)

            admin_embed = discord.Embed(
                title="管理員選項",
                description="請選擇後續操作：",
                color=discord.Color.yellow()
            )
            await interaction.followup.send(embed=admin_embed, view=ReopenView(self.user_id, self.bot))

class ReopenView(View):
    def __init__(self, user_id: int, bot=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.bot = bot
        self.emoji = bot.emoji
        self.db_manager = None

        reopen_button = Button(
            label="重新審核",
            style=discord.ButtonStyle.success,
            emoji=bot.emoji.get('blue_fire'),
            custom_id=f"reopen_{user_id}" if user_id != 0 else "reopen_placeholder"
        )
        reopen_button.callback = self.reopen_callback

        delete_button = Button(
            label="刪除頻道",
            style=discord.ButtonStyle.danger,
            emoji=bot.emoji.get('red_fire'),
            custom_id=f"delete_{user_id}" if user_id != 0 else "delete_placeholder"
        )
        delete_button.callback = self.delete_callback

        self.add_item(reopen_button)
        self.add_item(delete_button)

    async def ensure_db_manager(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        guild_name = interaction.guild.name

        if (self.db_manager is None or
            self.db_manager.guild_id != guild_id):
            self.db_manager = DatabaseManager(guild_id, guild_name)
            await self.db_manager.init_db()

            self.application_category_id = await self.db_manager.get_application_category()

        return self.db_manager

    async def reopen_callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="權限不足",
                description="只有管理員才能重新審核申請。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        applicant = interaction.guild.get_member(self.user_id)
        if applicant:
            overwrites = interaction.channel.overwrites
            overwrites[applicant] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            await interaction.channel.edit(overwrites=overwrites)

            db_manager = DatabaseManager(interaction.guild.id, interaction.guild.name)
            await db_manager.update_application_status(self.user_id, "pending")

            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)

            embed = discord.Embed(
                title="此申請已重新開啟",
                description=f"{applicant.mention}請重新開始申請",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)

            selection_embed = discord.Embed(
                title="身份組申請",
                description=f"請選擇你的身份 {self.emoji.get('loading1')}",
                color=discord.Color.blue()
            )
            await interaction.channel.send(embed=selection_embed, view=RoleSelectionView(self.user_id, self.bot))

    async def delete_callback(self, interaction: discord.Interaction):

        await self.ensure_db_manager(interaction)

        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="權限不足",
                description="只有管理員才能刪除申請頻道。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        embed = discord.Embed(
            title="頻道即將刪除",
            description=f"此頻道將在 3 秒後刪除 {self.emoji.get('loading1')}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)

        await self.db_manager.remove_bot_created_channel(interaction.channel.id)

        await asyncio.sleep(3)
        await interaction.channel.delete()

class ReapplyView(View):
    def __init__(self, user_id: int, bot=None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.bot = bot
        self.emoji = bot.emoji

        close_button = Button(
            label="關閉申請頻道",
            style=discord.ButtonStyle.danger,
            emoji=self.emoji.get('red_fire'),
            custom_id=f"close_{user_id}" if user_id != 0 else "close_placeholder"
        )
        close_button.callback = self.close_callback

        self.add_item(close_button)

    async def extract_user_id_from_interaction(self, interaction: discord.Interaction):
        if self.user_id != 0:
            return True

        custom_id = interaction.data.get("custom_id", "")
        if custom_id.startswith("close_"):
            try:
                self.user_id = int(custom_id.split("_")[-1])
                return True
            except (ValueError, IndexError):
                pass

        channel_name = interaction.channel.name if interaction.channel else ""
        if channel_name.startswith("身份組申請-"):
            for member in interaction.guild.members:
                if member.display_name in channel_name:
                    self.user_id = member.id
                    return True

        self.user_id = interaction.user.id
        return True

    async def close_callback(self, interaction: discord.Interaction):
        await self.extract_user_id_from_interaction(interaction)

        if interaction.user.id != self.user_id and not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="權限不足",
                description="只有申請人或管理員可以關閉申請頻道。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        db_manager = DatabaseManager(interaction.guild.id, interaction.guild.name)
        await db_manager.update_application_status(self.user_id, "closed")

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(view=self)

        embed = discord.Embed(
            title="即將關閉頻道...",
            description="此申請頻道將在 3 秒後關閉",
            color=discord.Color.blue()
        )

        await interaction.followup.send(embed=embed)

        await asyncio.sleep(3)

        applicant = interaction.guild.get_member(self.user_id)
        if applicant:
            overwrites = interaction.channel.overwrites
            if applicant in overwrites:
                del overwrites[applicant]
                await interaction.channel.edit(overwrites=overwrites)

                admin_embed = discord.Embed(
                    title="管理員選項",
                    description="請選擇後續操作：",
                    color=discord.Color.yellow()
                )
                await interaction.followup.send(embed=admin_embed, view=ReopenView(self.user_id, self.bot))

def setup_persistent_views_role(bot):
    try:
        bot.add_view(Verfication_View(bot=bot))

        bot.add_view(RoleSelectionView(0, bot=bot))

        bot.add_view(StudentApplicationForm(0, bot=bot))
        bot.add_view(ElderApplicationForm(0, bot=bot))

        bot.add_view(ApplicationApprovalView(0, "placeholder", bot=bot))

        bot.add_view(ReopenView(0, bot=bot))

        bot.add_view(ReapplyView(0, bot=bot))

        return True
    except Exception as e:
        print(f"設定持久化視圖時發生錯誤: {e}")
        return False
