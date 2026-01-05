from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.config import TicketCategory
from ..utils.cloudflare_ai_client import CloudflareAIClient

PANEL_BUTTON_ID = "ticket:open"
CLOSE_BUTTON_ID = "ticket:close"


class TicketCloseView(discord.ui.View):

    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="關閉客服單",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id=CLOSE_BUTTON_ID,
    )
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.process_close_ticket(interaction)


class TicketModal(discord.ui.Modal):

    def __init__(self, cog: "TicketCog", category: TicketCategory) -> None:
        super().__init__(title=f"建立客服單｜{category.label}")
        self.cog = cog
        self.category = category
        self.summary = discord.ui.TextInput[str](
            label="主旨 (30字以內)",
            style=discord.TextStyle.short,
            max_length=60,
            placeholder="簡述想諮詢的主題",
        )
        self.details = discord.ui.TextInput[str](
            label="詳細描述",
            style=discord.TextStyle.paragraph,
            placeholder="輸入詳細問題描述，有助於客服快速了解情況。",
            max_length=1500,
        )
        self.add_item(self.summary)
        self.add_item(self.details)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_ticket_submission(
            interaction,
            category=self.category,
            summary=self.summary.value,
            details=self.details.value,
        )


class TicketCategorySelect(discord.ui.Select):

    def __init__(self, cog: "TicketCog") -> None:
        self.cog = cog
        self._categories_snapshot = {c.value: c for c in cog.bot.settings.ticket_categories}
        options = [
            discord.SelectOption(
                label=category.label,
                value=category.value,
                description=category.description[:100] or None,
            )
            for category in self._categories_snapshot.values()
        ]
        placeholder = "選擇您要諮詢的分類"
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        category = self._categories_snapshot.get(self.values[0])
        if category is None:
            await interaction.response.send_message("客服分類選項不存在，請通知管理員。", ephemeral=True)
            return
        await interaction.response.send_modal(TicketModal(self.cog, category))


class TicketCategoryView(discord.ui.View):

    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=180)
        if not cog.bot.settings.ticket_categories:
            self.add_item(discord.ui.Button(label="尚未設定分類", disabled=True))
        else:
            self.add_item(TicketCategorySelect(cog))

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message("已取消建立客服單。", ephemeral=True)


class TicketPanelView(discord.ui.View):

    def __init__(self, cog: "TicketCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="聯絡我們",
        style=discord.ButtonStyle.primary,
        emoji="📩",
        custom_id=PANEL_BUTTON_ID,
    )
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("請在伺服器內使用此按鈕。", ephemeral=True)
            return

        if not self.cog.bot.settings.ticket_categories:
            await interaction.response.send_message("未設定客服分類，請通知管理員。", ephemeral=True)
            return

        view = TicketCategoryView(self.cog)
        await interaction.response.send_message("請選擇客服分類：", view=view, ephemeral=True)


class TicketCog(commands.GroupCog, name="ticket"):

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self._lock = asyncio.Lock()
        self._llm_client = CloudflareAIClient(bot.settings)

    @app_commands.command(name="panel", description="重新發布客服面板按鈕")
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("請在伺服器內使用此指令。", ephemeral=True)
            return

        if not self._can_manage_panel(interaction.user):
            await interaction.response.send_message("您沒有權限重新發布面板。", ephemeral=True)
            return

        settings = self.bot.settings
        channel = interaction.guild.get_channel(settings.ticket_panel_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("設定的客服面板頻道不存在。", ephemeral=True)
            return

        await self._cleanup_panel_messages(channel)
        embed = self._build_panel_embed(interaction.guild)
        view = TicketPanelView(self)
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message("客服面板已發布。", ephemeral=True)

    def _build_panel_embed(self, guild: discord.Guild) -> discord.Embed:
        description = (
            "無論您想了解我們的活動、提出合作提案、加入團隊、尋求資源協助，"
            "或是有任何疑問想諮詢，我們都非常樂意聆聽與交流！"
        )
        contact_steps = (
            "與我們聯繫的方式：\n"
            "• 點擊下方「聯絡我們」按鈕\n"
            "• 簡單描述您的需求或問題\n"
            "• 系統會為您建立專屬討論頻道\n"
            "• 在專屬頻道中與我們的團隊成員即時交流"
        )

        embed = discord.Embed(
            title="✨ 聯絡中心 | Contact Hub",
            description=f"👋 需要協助或者有任何想法嗎？\n{description}\n\n{contact_steps}",
            colour=discord.Colour.blurple(),
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="SCIST x SCAICT 2026 聯合寒訓 Team")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        return embed

    async def handle_ticket_submission(
        self,
        interaction: discord.Interaction,
        *,
        category: TicketCategory,
        summary: str,
        details: str,
    ) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("無法開啟客服單，請稍後再試。", ephemeral=True)
            return

        combined_text = f"{summary}\n{details}"
        blocked = self.bot.settings.find_blocked_keyword(combined_text)
        if blocked:
            await interaction.response.send_message(
                f"內容包含禁止詞彙「{blocked}」，請調整描述後再試一次。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        settings = self.bot.settings
        category_channel = interaction.guild.get_channel(settings.ticket_category_id)
        if not isinstance(category_channel, discord.CategoryChannel):
            await interaction.followup.send("客服分類頻道不存在，請通知管理員。", ephemeral=True)
            return

        requester = interaction.user
        if not isinstance(requester, discord.Member):
            await interaction.followup.send("找不到伺服器身分資料，請稍後再試。", ephemeral=True)
            return

        async with self._lock:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                requester: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            for role_id in settings.iter_support_roles():
                role = interaction.guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            short_uuid = str(uuid.uuid4())[:8]
            channel_name = f"ticket-{short_uuid}-{requester.id}"
            channel = await interaction.guild.create_text_channel(
                channel_name,
                category=category_channel,
                overwrites=overwrites,
                topic=f"ticket-owner:{requester.id}|category:{category.value}",
            )

        embed = discord.Embed(
            title=f"新的客服單｜{category.label}",
            description=self._sanitize_user_text(details),
            colour=discord.Colour.blurple(),
            timestamp=datetime.utcnow(),
        )
        embed.set_author(name=str(requester), icon_url=requester.display_avatar.url)
        embed.add_field(name="提問者", value=requester.mention)
        embed.add_field(name="主旨", value=self._sanitize_user_text(summary), inline=False)

        if settings.support_role_ids:
            mentions = " ".join(f"<@&{role_id}>" for role_id in settings.support_role_ids)
            await channel.send(f"{mentions} 有新的客服單！")
        
        view = TicketCloseView(self)
        await channel.send(embed=embed, view=view)

        reference_info = settings.faq_content if settings.faq_content else None

        await self._send_ai_greeting(
            channel=channel,
            requester=requester.display_name,
            category=category,
            summary=summary,
            details=details,
            reference_info=reference_info,
        )

        await interaction.followup.send(f"客服單已建立：{channel.mention}", ephemeral=True)

    @app_commands.command(name="close", description="關閉目前的客服單並保存紀錄")
    async def close_ticket(self, interaction: discord.Interaction) -> None:
        await self.process_close_ticket(interaction)

    async def process_close_ticket(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.category_id != self.bot.settings.ticket_category_id:
            await interaction.response.send_message("此頻道不是客服單頻道。", ephemeral=True)
            return

        owner_id = self._extract_owner(channel.topic)
        if owner_id is None:
            await interaction.response.send_message("無法找到客服單擁有者。", ephemeral=True)
            return

        is_owner = interaction.user.id == owner_id if interaction.user else False
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        has_role = any(role.id in self.bot.settings.support_role_ids for role in getattr(member, "roles", []))
        if not (is_owner or has_role):
            await interaction.response.send_message("您沒有權限關閉此客服單。", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        else:
            pass

        transcript_path = await self._export_transcript(channel)

        owner = channel.guild.get_member(owner_id)
        if owner is not None:
            try:
                await owner.send(
                    content="您的客服單已關閉，以下是對話紀錄。",
                    file=discord.File(transcript_path, filename=transcript_path.name),
                )
            except discord.Forbidden:
                self.bot.logger.warning("無法傳送客服單紀錄給 %s", owner)

        await interaction.followup.send("客服單已關閉，紀錄已寄送給提問者。", ephemeral=True)

        try:
            await channel.delete(reason="Ticket closed")
        except discord.HTTPException as exc:
            self.bot.logger.warning("刪除客服單頻道 %s 失敗: %s", channel.id, exc)

    @app_commands.command(name="refresh", description="刷新（清空）客服面板頻道訊息")
    async def refresh_ticket(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("請在伺服器內使用此指令。", ephemeral=True)
            return

        settings = self.bot.settings
        channel = interaction.guild.get_channel(settings.ticket_panel_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("設定的客服面板頻道不存在。", ephemeral=True)
            return

        if not self._can_manage_panel(interaction.user):
            await interaction.response.send_message("您沒有權限執行此操作。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = 0
        try:
            async for message in channel.history(limit=None, oldest_first=False):
                try:
                    await message.delete()
                    deleted += 1
                    await asyncio.sleep(0.5)
                except discord.HTTPException:
                    continue
        except discord.HTTPException as exc:
            self.bot.logger.warning("清空頻道時發生錯誤: %s", exc)

        try:
            self.bot.logger.info(
                "Ticket panel refreshed | channel=%s deleted=%s by=%s",
                getattr(channel, "id", None), deleted, getattr(interaction.user, "id", None),
            )
        except Exception:
            pass

        await interaction.followup.send(f"已清空面板頻道 {deleted} 則訊息。", ephemeral=True)

    def refresh_settings(self) -> None:
        self._llm_client = CloudflareAIClient(self.bot.settings)

    async def _export_transcript(self, channel: discord.TextChannel) -> Path:
        settings = self.bot.settings
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        file_path = settings.transcript_dir / f"ticket-{channel.id}-{timestamp}.txt"

        lines: list[str] = []
        async for message in channel.history(limit=None, oldest_first=True):
            created = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{created}] {message.author}: {message.clean_content}")
            for attachment in message.attachments:
                lines.append(f"    [附件] {attachment.url}")

        file_path.write_text("\n".join(lines), encoding="utf-8")
        return file_path

    @staticmethod
    def _extract_owner(topic: str | None) -> int | None:
        if not topic or "ticket-owner:" not in topic:
            return None
        try:
            segment = topic.split("ticket-owner:", maxsplit=1)[1]
            value = segment.split("|", maxsplit=1)[0]
            return int(value)
        except ValueError:
            return None

    def _can_manage_panel(self, member: discord.Member) -> bool:
        settings = self.bot.settings
        if member.guild_permissions.manage_guild:
            return True
        return any(role.id in settings.support_role_ids for role in member.roles)

    async def _cleanup_panel_messages(self, channel: discord.TextChannel) -> None:
        async for message in channel.history(limit=25):
            if message.author == channel.guild.me and message.components:
                try:
                    await message.delete()
                except discord.HTTPException:
                    self.bot.logger.debug("刪除舊面板失敗: %s", message.id)

    def _sanitize_user_text(self, text: str) -> str:
        return self._llm_client.sanitize_text(text)

    async def _send_ai_greeting(
        self,
        *,
        channel: discord.TextChannel,
        requester: str,
        category: TicketCategory,
        summary: str,
        details: str,
        reference_info: str | None,
    ) -> None:
        try:
            response = await self._llm_client.generate_ticket_reply(
                requester=requester,
                category_label=category.label,
                summary=summary,
                description=details,
                ai_hint=category.ai_hint,
                reference_info=reference_info,
            )
        except Exception as exc:
            self.bot.logger.exception("Ticket greeting failed", exc_info=exc)
            return
        await channel.send(response)


async def setup(bot: commands.Bot) -> None:
    cog = TicketCog(bot)
    await bot.add_cog(cog)
    bot.add_view(TicketPanelView(cog))
    bot.add_view(TicketCloseView(cog))
