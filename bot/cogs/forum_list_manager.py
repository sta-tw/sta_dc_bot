from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.config_paths import ConfigPaths


def _is_admin_member(member: discord.Member, admin_role_id: int | None) -> bool:
    if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
        return True
    if admin_role_id is None:
        return False
    return any(role.id == admin_role_id for role in member.roles)


class ForumListActionView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="新增", style=discord.ButtonStyle.success, custom_id="forum_list_add")
    async def add_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("ForumListManager")
        if not isinstance(cog, ForumListManager):
            await interaction.response.send_message("功能尚未就緒，請稍後再試。", ephemeral=True)
            return
        await cog.start_add_flow(interaction)

class AddItemModal(discord.ui.Modal, title="新增列表項目"):
    def __init__(self, cog: "ForumListManager", guild_id: int, thread_id: int, author_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.thread_id = thread_id
        self.author_id = author_id

        self.title_text = discord.ui.TextInput(label="Title", placeholder="例如：成大邀請賽", required=True, max_length=100)
        self.link = discord.ui.TextInput(label="Link", placeholder="https://...", required=True, max_length=500)

        self.add_item(self.title_text)
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        proposal = {
            "action": "add",
            "guild_id": self.guild_id,
            "thread_id": self.thread_id,
            "author_id": self.author_id,
            "payload": {
                "title": str(self.title_text.value).strip(),
                "link": str(self.link.value).strip(),
            },
        }
        await self.cog.submit_proposal(interaction, proposal)


class EditItemModal(discord.ui.Modal, title="編輯列表項目"):
    def __init__(
        self,
        cog: "ForumListManager",
        guild_id: int,
        thread_id: int,
        author_id: int,
        *,
        category: str,
        item_id: str,
        current_title: str,
        current_link: str,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.thread_id = thread_id
        self.author_id = author_id
        self.category = category
        self.item_id = item_id

        self.title_text = discord.ui.TextInput(label="Title", required=True, default=current_title, max_length=100)
        self.link = discord.ui.TextInput(label="Link", required=True, default=current_link, max_length=500)

        self.add_item(self.title_text)
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        proposal = {
            "action": "edit",
            "guild_id": self.guild_id,
            "thread_id": self.thread_id,
            "author_id": self.author_id,
            "payload": {
                "category": self.category,
                "item_id": self.item_id,
                "title": str(self.title_text.value).strip(),
                "link": str(self.link.value).strip(),
            },
        }
        await self.cog.submit_proposal(interaction, proposal)


class CategorySelectView(discord.ui.View):
    def __init__(self, cog: "ForumListManager", mode: str, categories: list[str]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.mode = mode

        options = [discord.SelectOption(label=name, value=name) for name in categories[:25]]
        select = discord.ui.Select(placeholder="先選擇類別", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        selected_category = interaction.data["values"][0]
        await self.cog.continue_item_select_flow(interaction, self.mode, selected_category)


class ItemSelectView(discord.ui.View):
    def __init__(self, cog: "ForumListManager", mode: str, category: str, items: list[dict[str, str]]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.mode = mode
        self.category = category

        options = [
            discord.SelectOption(
                label=(item.get("title", "未命名")[:90] or "未命名"),
                value=item.get("id", ""),
                description=(item.get("link", "")[:90] if item.get("link") else None),
            )
            for item in items[:25]
        ]
        select = discord.ui.Select(placeholder="再選擇項目", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        item_id = interaction.data["values"][0]
        await self.cog.finish_item_select_flow(interaction, self.mode, self.category, item_id)


class ReviewDecisionView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="同意", style=discord.ButtonStyle.success, custom_id="forum_list_approve")
    async def approve_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("ForumListManager")
        if not isinstance(cog, ForumListManager):
            await interaction.response.send_message("功能尚未就緒，請稍後再試。", ephemeral=True)
            return
        await cog.begin_review_approval(interaction)

    @discord.ui.button(label="拒絕", style=discord.ButtonStyle.danger, custom_id="forum_list_reject")
    async def reject_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("ForumListManager")
        if not isinstance(cog, ForumListManager):
            await interaction.response.send_message("功能尚未就緒，請稍後再試。", ephemeral=True)
            return
        await cog.handle_review_decision(interaction, approved=False)


class ReviewCategoryModal(discord.ui.Modal, title="新增類別"):
    def __init__(self, cog: "ForumListManager", proposal_id: str) -> None:
        super().__init__()
        self.cog = cog
        self.proposal_id = proposal_id

        self.category_name = discord.ui.TextInput(
            label="類別名稱",
            placeholder="例如：演算法",
            required=True,
            max_length=60,
        )
        self.add_item(self.category_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.open_review_confirmation(interaction, self.proposal_id, str(self.category_name.value).strip())


class ReviewChannelSelectView(discord.ui.View):
    def __init__(self, cog: "ForumListManager", proposal_id: str, channels: list[discord.TextChannel]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.proposal_id = proposal_id

        options = [discord.SelectOption(label=ch.name, value=str(ch.id)) for ch in channels[:24]]
        select = discord.ui.Select(placeholder="選擇要發出的頻道", options=options, min_values=1, max_values=1)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = interaction.data["values"][0]
        await self.cog.open_review_confirmation(interaction, self.proposal_id, value)


class ReviewConfirmView(discord.ui.View):
    def __init__(self, cog: "ForumListManager", proposal_id: str) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.proposal_id = proposal_id

    @discord.ui.button(label="確認發出", style=discord.ButtonStyle.success, custom_id="forum_list_confirm_publish")
    async def confirm_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.finalize_review_publish(interaction, self.proposal_id)

    @discord.ui.button(label="返回選頻道", style=discord.ButtonStyle.secondary, custom_id="forum_list_back_channel")
    async def back_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.restart_channel_choice(interaction, self.proposal_id)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.danger, custom_id="forum_list_cancel_review")
    async def cancel_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message("已取消此次審核。", ephemeral=True)


class ForumListManager(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.bot.add_view(ForumListActionView())
        self.bot.add_view(ReviewDecisionView())

    def _file_path(self, guild_id: int) -> Path:
        ConfigPaths.ensure_directories()
        guild_dir = ConfigPaths.guild_dir(guild_id)
        guild_dir.mkdir(parents=True, exist_ok=True)
        return guild_dir / "forum_lists.json"

    def _default_data(self) -> dict[str, Any]:
        return {
            "forum_channel_id": 0,
            "review_channel_id": 0,
            "admin_role_id": 0,
            "threads": {},
            "proposals": {},
        }

    def _load_data(self, guild_id: int) -> dict[str, Any]:
        path = self._file_path(guild_id)
        if not path.exists():
            data = self._default_data()
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = self._default_data()
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data

    def _save_data(self, guild_id: int, data: dict[str, Any]) -> None:
        path = self._file_path(guild_id)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_proposal(self, data: dict[str, Any], proposal_id: str) -> dict[str, Any] | None:
        return data.setdefault("proposals", {}).get(proposal_id)

    def _has_pending_proposal_for_thread(self, data: dict[str, Any], thread_id: int) -> bool:
        proposals = data.get("proposals", {})
        for proposal in proposals.values():
            if proposal.get("status") != "pending":
                continue
            if int(proposal.get("thread_id", 0) or 0) == thread_id:
                return True
        return False

    def _get_thread_from_proposal(self, guild: discord.Guild, proposal: dict[str, Any]) -> discord.Thread | None:
        thread_id = int(proposal.get("thread_id", 0) or 0)
        thread = guild.get_thread(thread_id)
        return thread if isinstance(thread, discord.Thread) else None

    def _build_proposal_preview_embed(self, proposal: dict[str, Any]) -> discord.Embed:
        payload = proposal.get("payload", {})
        action = proposal.get("action", "unknown")
        action_text = {"add": "新增", "edit": "編輯", "delete": "刪除"}.get(action, str(action))

        embed = discord.Embed(
            title=f"審核確認：{action_text}",
            color=discord.Color.orange(),
        )
        embed.add_field(name="申請者", value=f"<@{proposal.get('author_id', 0)}>" if proposal.get("author_id") else "未知", inline=True)
        title = str(payload.get("title", "-")) or "-"
        link = str(payload.get("link", "-")) or "-"
        embed.add_field(name="預覽", value=f"- [{title}]({link})" if link != "-" else f"- {title}", inline=False)
        if action == "delete":
            embed.description = "確認後將直接刪除這筆項目。"
        else:
            embed.description = "確認後將把訊息發布到選定的頻道並更新列表。"
        return embed

    def _get_thread_entry(self, data: dict[str, Any], thread_id: int, create: bool = False) -> dict[str, Any] | None:
        threads = data.setdefault("threads", {})
        key = str(thread_id)
        if key not in threads and create:
            threads[key] = {"panel_message_id": 0, "categories": {}}
        return threads.get(key)

    def _is_configured_for_thread(self, data: dict[str, Any], thread: discord.Thread) -> bool:
        parent = thread.parent
        if not isinstance(parent, discord.ForumChannel):
            return False
        return parent.id == int(data.get("forum_channel_id", 0))

    def _build_panel_embed(self, thread: discord.Thread, entry: dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title="資源列表管理",
            description="可在本串直接討論；需修改列表時請按下方按鈕送審。",
            color=discord.Color.blue(),
        )

        categories = entry.get("categories", {})
        if not categories:
            embed.add_field(name="目前內容", value="尚無項目，請按「新增」建立。", inline=False)
            return embed

        for category, items in categories.items():
            lines: list[str] = []
            for item in items:
                title = item.get("title", "未命名")
                link = item.get("link", "")
                if link:
                    lines.append(f"• [{title}]({link})")
                else:
                    lines.append(f"• {title}")
            embed.add_field(name=category, value="\n".join(lines)[:1024] or "（空）", inline=False)

        embed.set_footer(text=f"thread_id:{thread.id}")
        return embed

    async def _upsert_panel_message(self, guild_id: int, thread: discord.Thread, data: dict[str, Any]) -> None:
        entry = self._get_thread_entry(data, thread.id, create=True)
        if entry is None:
            return

        panel_message_id = int(entry.get("panel_message_id", 0) or 0)
        embed = self._build_panel_embed(thread, entry)
        view = ForumListActionView()

        message: discord.Message | None = None
        if panel_message_id:
            try:
                message = await thread.fetch_message(panel_message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None

        if message is None:
            message = await thread.send(embed=embed, view=view)
            entry["panel_message_id"] = message.id
        else:
            await message.edit(embed=embed, view=view)

        self._save_data(guild_id, data)

    @app_commands.command(name="forum_list_setup", description="設定論壇列表管理（管理員）")
    @app_commands.describe(
        forum_channel="使用者討論用論壇",
        review_channel="管理員審核用文字頻道",
        admin_role="可審核的管理身分組（可留空）",
    )
    async def forum_list_setup(
        self,
        interaction: discord.Interaction,
        forum_channel: discord.ForumChannel,
        review_channel: discord.TextChannel,
        admin_role: discord.Role | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("需要管理伺服器權限。", ephemeral=True)
            return

        data = self._load_data(interaction.guild.id)
        data["forum_channel_id"] = forum_channel.id
        data["review_channel_id"] = review_channel.id
        data["admin_role_id"] = admin_role.id if admin_role else 0
        self._save_data(interaction.guild.id, data)

        await interaction.response.send_message(
            f"已設定完成。\n論壇：{forum_channel.mention}\n審核頻道：{review_channel.mention}",
            ephemeral=True,
        )

    @app_commands.command(name="forum_list_panel", description="在目前論壇貼文建立/刷新列表管理面板")
    async def forum_list_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("需要管理伺服器權限。", ephemeral=True)
            return
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("請在論壇貼文內使用此指令。", ephemeral=True)
            return

        data = self._load_data(interaction.guild.id)
        if not self._is_configured_for_thread(data, interaction.channel):
            await interaction.response.send_message("此貼文不在已設定論壇內。", ephemeral=True)
            return

        await self._upsert_panel_message(interaction.guild.id, interaction.channel, data)
        await interaction.response.send_message("已建立/刷新列表管理面板。", ephemeral=True)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if thread.guild is None:
            return
        if thread.owner_id == self.bot.user.id if self.bot.user else False:
            return

        data = self._load_data(thread.guild.id)
        if not self._is_configured_for_thread(data, thread):
            return

        try:
            await self._upsert_panel_message(thread.guild.id, thread, data)
            await self._create_admin_discussion_thread(thread.guild, thread, data)
        except Exception as exc:
            self.bot.logger.warning("[ForumList] 初始化論壇貼文失敗(thread=%s): %s", thread.id, exc)

    async def _create_admin_discussion_thread(
        self,
        guild: discord.Guild,
        source_thread: discord.Thread,
        data: dict[str, Any],
    ) -> None:
        review_channel = guild.get_channel(int(data.get("review_channel_id", 0) or 0))
        if not isinstance(review_channel, discord.TextChannel):
            return

        admin_thread = await review_channel.create_thread(
            name=f"管理討論-{source_thread.name[:70]}",
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
        await admin_thread.send("這裡是管理討論串。若有列表異動，請等管理員同意/不同意。")
    async def _validate_user_thread_interaction(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Guild, discord.Thread, dict[str, Any]] | None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("請在目標論壇貼文中使用。", ephemeral=True)
            return None

        data = self._load_data(interaction.guild.id)
        if not self._is_configured_for_thread(data, interaction.channel):
            await interaction.response.send_message("此貼文不在設定論壇內。", ephemeral=True)
            return None

        return interaction.guild, interaction.channel, data

    async def start_add_flow(self, interaction: discord.Interaction) -> None:
        validated = await self._validate_user_thread_interaction(interaction)
        if validated is None:
            return
        guild, thread, _ = validated
        await interaction.response.send_modal(AddItemModal(self, guild.id, thread.id, interaction.user.id))

    async def start_edit_flow(self, interaction: discord.Interaction) -> None:
        validated = await self._validate_user_thread_interaction(interaction)
        if validated is None:
            return
        guild, thread, data = validated

        entry = self._get_thread_entry(data, thread.id, create=False)
        categories = list((entry or {}).get("categories", {}).keys())
        if not categories:
            await interaction.response.send_message("目前沒有可編輯項目。", ephemeral=True)
            return

        await interaction.response.send_message(
            "請先選擇類別：",
            view=CategorySelectView(self, "edit", categories),
            ephemeral=True,
        )

    async def start_delete_flow(self, interaction: discord.Interaction) -> None:
        validated = await self._validate_user_thread_interaction(interaction)
        if validated is None:
            return
        guild, thread, data = validated

        entry = self._get_thread_entry(data, thread.id, create=False)
        categories = list((entry or {}).get("categories", {}).keys())
        if not categories:
            await interaction.response.send_message("目前沒有可刪除項目。", ephemeral=True)
            return

        await interaction.response.send_message(
            "請先選擇類別：",
            view=CategorySelectView(self, "delete", categories),
            ephemeral=True,
        )

    async def continue_item_select_flow(self, interaction: discord.Interaction, mode: str, category: str) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("請在論壇貼文內操作。", ephemeral=True)
            return

        data = self._load_data(interaction.guild.id)
        entry = self._get_thread_entry(data, interaction.channel.id, create=False) or {}
        items = list(entry.get("categories", {}).get(category, []))
        if not items:
            await interaction.response.send_message("此類別沒有項目。", ephemeral=True)
            return

        await interaction.response.send_message(
            "再選擇項目：",
            view=ItemSelectView(self, mode, category, items),
            ephemeral=True,
        )

    async def finish_item_select_flow(self, interaction: discord.Interaction, mode: str, category: str, item_id: str) -> None:
        if interaction.guild is None or not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("請在論壇貼文內操作。", ephemeral=True)
            return

        data = self._load_data(interaction.guild.id)
        entry = self._get_thread_entry(data, interaction.channel.id, create=False) or {}
        items = list(entry.get("categories", {}).get(category, []))
        selected = next((x for x in items if x.get("id") == item_id), None)
        if selected is None:
            await interaction.response.send_message("找不到目標項目。", ephemeral=True)
            return

        if mode == "edit":
            await interaction.response.send_modal(
                EditItemModal(
                    self,
                    interaction.guild.id,
                    interaction.channel.id,
                    interaction.user.id,
                    category=category,
                    item_id=item_id,
                    current_title=str(selected.get("title", "")),
                    current_link=str(selected.get("link", "")),
                )
            )
            return

        if mode == "delete":
            proposal = {
                "action": "delete",
                "guild_id": interaction.guild.id,
                "thread_id": interaction.channel.id,
                "author_id": interaction.user.id,
                "payload": {
                    "category": category,
                    "item_id": item_id,
                    "title": selected.get("title", ""),
                    "link": selected.get("link", ""),
                },
            }
            await self.submit_proposal(interaction, proposal)
            return

        await interaction.response.send_message("無效操作。", ephemeral=True)

    async def submit_proposal(self, interaction: discord.Interaction, proposal: dict[str, Any]) -> None:
        if interaction.guild is None:
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        data = self._load_data(interaction.guild.id)
        thread_id = int(proposal.get("thread_id", 0) or 0)
        if thread_id and self._has_pending_proposal_for_thread(data, thread_id):
            await interaction.followup.send("此討論串已有待審核提案，請等待目前審核完成後再提交。", ephemeral=True)
            return

        admin_role_id = int(data.get("admin_role_id", 0) or 0)
        admin_mention = f"<@&{admin_role_id}> " if admin_role_id else ""
        proposal_id = uuid.uuid4().hex[:12]
        proposal["status"] = "pending"
        proposal["proposal_id"] = proposal_id
        data.setdefault("proposals", {})[proposal_id] = proposal
        self._save_data(interaction.guild.id, data)

        review_channel = interaction.guild.get_channel(int(data.get("review_channel_id", 0) or 0))
        if not isinstance(review_channel, discord.TextChannel):
            await interaction.followup.send("未設定審核頻道，請先執行 /forum_list_setup。", ephemeral=True)
            return

        source_thread = interaction.guild.get_thread(int(proposal.get("thread_id", 0)))
        if source_thread is None:
            await interaction.followup.send("找不到原始論壇貼文。", ephemeral=True)
            return

        payload = proposal.get("payload", {})
        action = proposal.get("action")
        action_text = {"add": "新增", "edit": "編輯", "delete": "刪除"}.get(action, str(action))

        embed = discord.Embed(title=f"列表異動審核：{action_text}", color=discord.Color.orange())
        embed.add_field(name="申請者", value=interaction.user.mention, inline=True)
        embed.add_field(name="來源貼文", value=source_thread.mention, inline=True)
        if payload.get("category"):
            embed.add_field(name="類別", value=str(payload.get("category")), inline=False)
        title = str(payload.get("title", "未命名")).strip() or "未命名"
        link = str(payload.get("link", "")).strip()
        embed.add_field(name="預覽", value=f"- [{title}]({link})" if link else f"- {title}", inline=False)
        embed.set_footer(text=f"proposal_id:{proposal_id}")

        review_view = ReviewDecisionView()
        review_thread = None

        try:
            seed = await review_channel.send(
                f"{admin_mention}有新的列表異動待審核（{source_thread.mention}）",
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )
            review_thread = await seed.create_thread(name=f"審核-{action_text}-{source_thread.name[:60]}")
            await review_thread.send(embed=embed, view=review_view)
        except discord.HTTPException as exc:
            self.bot.logger.warning("[ForumList] 建立審核討論串失敗，改送到審核頻道: %s", exc)
            review_thread = None
            await review_channel.send(
                content=f"{admin_mention}有新的列表異動待審核（{source_thread.mention}）",
                embed=embed,
                view=review_view,
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )
        if review_thread is not None:
            proposal["review_thread_id"] = review_thread.id
        data.setdefault("proposals", {})[proposal_id] = proposal
        self._save_data(interaction.guild.id, data)

        await interaction.followup.send("已送出審核，等待管理員審核。", ephemeral=True)

    async def begin_review_approval(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
            return

        data = self._load_data(interaction.guild.id)
        admin_role_id = int(data.get("admin_role_id", 0) or 0)
        if not _is_admin_member(interaction.user, admin_role_id):
            await interaction.response.send_message("只有管理員可以審核。", ephemeral=True)
            return

        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message("找不到審核資料。", ephemeral=True)
            return

        footer_text = interaction.message.embeds[0].footer.text if interaction.message.embeds[0].footer else ""
        if not footer_text.startswith("proposal_id:"):
            await interaction.response.send_message("審核資料格式錯誤。", ephemeral=True)
            return

        proposal_id = footer_text[len("proposal_id:"):]
        proposal = self._get_proposal(data, proposal_id)
        if not proposal:
            await interaction.response.send_message("找不到提案，可能已失效。", ephemeral=True)
            return

        action = proposal.get("action")
        if action == "add":
            channels = [ch for ch in interaction.guild.text_channels if ch.permissions_for(interaction.guild.me).send_messages]
            if channels:
                await interaction.response.edit_message(
                    content="請先選擇要發出訊息的頻道，再確認後發出。",
                    embed=interaction.message.embeds[0],
                    view=ReviewChannelSelectView(self, proposal_id, channels),
                )
            else:
                await interaction.response.send_message("沒有可用的頻道。", ephemeral=True)
            return

        await self.open_review_confirmation(interaction, proposal_id, None, edit_message=True)

    async def restart_channel_choice(self, interaction: discord.Interaction, proposal_id: str) -> None:
        if interaction.guild is None:
            return
        data = self._load_data(interaction.guild.id)
        proposal = self._get_proposal(data, proposal_id)
        if not proposal:
            await interaction.response.send_message("找不到提案。", ephemeral=True)
            return

        channels = [ch for ch in interaction.guild.text_channels if ch.permissions_for(interaction.guild.me).send_messages]
        if not channels:
            await interaction.response.send_message("沒有可用的頻道。", ephemeral=True)
            return

        await interaction.response.edit_message(
            content="請先選擇要發出訊息的頻道，再確認後發出。",
            embed=interaction.message.embeds[0],
            view=ReviewChannelSelectView(self, proposal_id, channels),
        )

    async def open_review_confirmation(
        self,
        interaction: discord.Interaction,
        proposal_id: str,
        selected_channel_id: str | None,
        *,
        edit_message: bool = False,
    ) -> None:
        if interaction.guild is None:
            return

        data = self._load_data(interaction.guild.id)
        proposal = self._get_proposal(data, proposal_id)
        if not proposal:
            await interaction.response.send_message("找不到提案。", ephemeral=True)
            return

        if selected_channel_id:
            proposal.setdefault("review", {})["selected_channel_id"] = selected_channel_id
            self._save_data(interaction.guild.id, data)

        preview = self._build_proposal_preview_embed(proposal)
        if selected_channel_id:
            try:
                channel = interaction.guild.get_channel(int(selected_channel_id))
                if channel:
                    preview.add_field(name="發布位置", value=channel.mention, inline=False)
            except (ValueError, AttributeError):
                pass

        view = ReviewConfirmView(self, proposal_id)
        if edit_message:
            await interaction.response.edit_message(embed=preview, view=view)
            return

        await interaction.response.send_message(embed=preview, view=view, ephemeral=True)

    async def finalize_review_publish(self, interaction: discord.Interaction, proposal_id: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
            return

        data = self._load_data(interaction.guild.id)
        admin_role_id = int(data.get("admin_role_id", 0) or 0)
        if not _is_admin_member(interaction.user, admin_role_id):
            await interaction.response.send_message("只有管理員可以審核。", ephemeral=True)
            return

        proposal = self._get_proposal(data, proposal_id)
        if not proposal:
            await interaction.response.send_message("找不到提案。", ephemeral=True)
            return

        review_data = proposal.setdefault("review", {})
        selected_channel_id = str(review_data.get("selected_channel_id", "")).strip() or None
        if proposal.get("action") == "add" and not selected_channel_id:
            await interaction.response.send_message("請先選擇頻道。", ephemeral=True)
            return

        if selected_channel_id:
            try:
                channel = interaction.guild.get_channel(int(selected_channel_id))
                if isinstance(channel, discord.TextChannel):
                    payload = proposal.get("payload", {})
                    title = str(payload.get("title", "-")) or "-"
                    link = str(payload.get("link", "-")) or "-"
                    message_content = f"- [{title}]({link})" if link != "-" else f"- {title}"
                    await channel.send(message_content)
            except (ValueError, AttributeError) as e:
                await interaction.response.send_message(f"無法發送到頻道：{e}", ephemeral=True)
                return

        proposal["status"] = "approved"
        proposal["approver_id"] = interaction.user.id
        self._save_data(interaction.guild.id, data)

        source_thread = self._get_thread_from_proposal(interaction.guild, proposal)
        if source_thread:
            await self._upsert_panel_message(interaction.guild.id, source_thread, data)

        await interaction.response.send_message("已確認並發出。", ephemeral=True)

    async def handle_review_decision(self, interaction: discord.Interaction, *, approved: bool) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("請在伺服器內使用。", ephemeral=True)
            return

        data = self._load_data(interaction.guild.id)
        admin_role_id = int(data.get("admin_role_id", 0) or 0)
        if not _is_admin_member(interaction.user, admin_role_id):
            await interaction.response.send_message("只有管理員可以審核。", ephemeral=True)
            return

        if not interaction.message or not interaction.message.embeds:
            await interaction.response.send_message("找不到審核資料。", ephemeral=True)
            return

        footer_text = interaction.message.embeds[0].footer.text if interaction.message.embeds[0].footer else ""
        prefix = "proposal_id:"
        if not footer_text.startswith(prefix):
            await interaction.response.send_message("審核資料格式錯誤。", ephemeral=True)
            return

        proposal_id = footer_text[len(prefix):]
        proposal = data.get("proposals", {}).get(proposal_id)
        if not proposal:
            await interaction.response.send_message("找不到提案，可能已失效。", ephemeral=True)
            return
        if proposal.get("status") != "pending":
            await interaction.response.send_message("此提案已處理。", ephemeral=True)
            return

        if approved:
            await self.begin_review_approval(interaction)
        else:
            proposal["status"] = "rejected"
            proposal["approver_id"] = interaction.user.id
            self._save_data(interaction.guild.id, data)

            source_thread = self._get_thread_from_proposal(interaction.guild, proposal)
            if source_thread:
                await source_thread.send(f"❌ 此次異動未通過審核：{interaction.user.mention}")

            await interaction.response.send_message("已拒絕")

    def _apply_proposal(self, data: dict[str, Any], proposal: dict[str, Any]) -> tuple[bool, str]:
        thread_id = int(proposal.get("thread_id", 0) or 0)
        entry = self._get_thread_entry(data, thread_id, create=True)
        if entry is None:
            return False, "找不到貼文資料"

        categories = entry.setdefault("categories", {})
        payload = proposal.get("payload", {})
        action = proposal.get("action")

        category = str(payload.get("category", "")).strip()
        if not category:
            return False, "缺少類別"

        if action == "add":
            categories.setdefault(category, [])
            categories[category].append(
                {
                    "id": uuid.uuid4().hex[:10],
                    "title": str(payload.get("title", "")).strip() or "未命名",
                    "link": str(payload.get("link", "")).strip(),
                }
            )
            return True, "ok"

        items = categories.get(category, [])
        item_id = str(payload.get("item_id", "")).strip()
        index = next((i for i, item in enumerate(items) if item.get("id") == item_id), None)
        if index is None:
            return False, "找不到目標項目"

        if action == "edit":
            items[index]["title"] = str(payload.get("title", "")).strip() or "未命名"
            items[index]["link"] = str(payload.get("link", "")).strip()
            return True, "ok"

        if action == "delete":
            items.pop(index)
            if not items:
                categories.pop(category, None)
            return True, "ok"

        return False, "未知操作"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ForumListManager(bot))
