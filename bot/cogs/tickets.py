from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

PANEL_BUTTON_ID = "ticket:open"

class TicketModal(discord.ui.Modal, title="建立客服單"):
    def __init__(self, cog: "TicketCog") -> None:
        super().__init__()
        self.cog = cog
        self.question: discord.ui.TextInput[str] = discord.ui.TextInput(
            label="請描述您的問題",
            style=discord.TextStyle.paragraph,
            placeholder="輸入詳細問題描述，有助於客服快速了解情況。",
            max_length=1000,
        )
        self.add_item(self.question)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_ticket_submission(interaction, self.question.value)


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

        await interaction.response.send_modal(TicketModal(self.cog))


class TicketCog(commands.GroupCog, name="ticket"):

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self.bot = bot
        self._lock = asyncio.Lock()

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

    async def handle_ticket_submission(self, interaction: discord.Interaction, question: str) -> None:
        if interaction.guild is None or interaction.user is None:
            await interaction.response.send_message("無法開啟客服單，請稍後再試。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        settings = self.bot.settings
        category = interaction.guild.get_channel(settings.ticket_category_id)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send("客服分類頻道不存在，請通知管理員。", ephemeral=True)
            return

        requester = interaction.user
        if not isinstance(requester, discord.Member):
            await interaction.followup.send("找不到伺服器身分資料，請稍後再試。", ephemeral=True)
            return

        async with self._lock:
            existing = discord.utils.find(
                lambda c: isinstance(c, discord.TextChannel)
                and self._extract_owner(c.topic) == requester.id,
                category.text_channels,
            )
            if existing is not None:
                await interaction.followup.send(
                    f"您已有開啟的客服單：{existing.mention}", ephemeral=True
                )
                return

            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                requester: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            }
            for role_id in settings.support_role_ids:
                role = interaction.guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            discriminator = requester.discriminator or "0000"
            channel_name = f"ticket-{requester.name.lower()}-{discriminator}"
            channel = await interaction.guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"ticket-owner:{requester.id}",
            )

        embed = discord.Embed(
            title="新的客服單",
            description=question,
            colour=discord.Colour.blurple(),
            timestamp=datetime.utcnow(),
        )
        embed.set_author(name=str(requester), icon_url=requester.display_avatar.url)
        embed.add_field(name="提問者", value=requester.mention)

        if settings.support_role_ids:
            mentions = " ".join(f"<@&{role_id}>" for role_id in settings.support_role_ids)
            await channel.send(f"{mentions} 有新的客服單！")
        await channel.send(embed=embed)
        await interaction.followup.send(f"客服單已建立：{channel.mention}", ephemeral=True)

    @app_commands.command(name="close", description="關閉目前的客服單並保存紀錄")
    async def close_ticket(self, interaction: discord.Interaction) -> None:
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

        await interaction.response.defer(ephemeral=True, thinking=True)
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
            return int(topic.split("ticket-owner:", maxsplit=1)[1])
        except ValueError:
            return None

    def _can_manage_panel(self, member: discord.Member) -> bool:
        settings = self.bot.settings
        if member.guild_permissions.manage_guild:
            return True
        return any(role.id in settings.support_role_ids for role in member.roles)


async def setup(bot: commands.Bot) -> None:
    cog = TicketCog(bot)
    await bot.add_cog(cog)
    bot.add_view(TicketPanelView(cog))
