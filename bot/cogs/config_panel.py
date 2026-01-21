from __future__ import annotations

import json
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.config import Settings
from ..utils.logging_config import get_recent_logs


class FAQModal(discord.ui.Modal):

    def __init__(self, cog: "ConfigCog") -> None:
        super().__init__(title="編輯 FAQ")
        self.cog = cog
        default_content = cog.bot.settings.faq_content
        self.content = discord.ui.TextInput[str](
            label="FAQ 內容 (純文字)",
            style=discord.TextStyle.paragraph,
            required=False,
            placeholder="請輸入 FAQ 內容，將直接提供給 LLM 參考。",
            default=default_content,
            max_length=2000,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        new_content = self.content.value.strip()
        await self.cog.update_faq(new_content)
        await interaction.followup.send("FAQ 已更新並重新載入設定。", ephemeral=True)


class LLMSettingsModal(discord.ui.Modal):

    def __init__(self, cog: "ConfigCog") -> None:
        super().__init__(title="編輯 LLM 設定")
        self.cog = cog
        s = cog.bot.settings
        self.persona = discord.ui.TextInput[str](
            label="提示詞（系統提示）",
            style=discord.TextStyle.paragraph,
            required=False,
            default=s.llm_persona_prompt,
        )
        self.max_sentences = discord.ui.TextInput[str](
            label="最大句子數",
            style=discord.TextStyle.short,
            required=False,
            default=str(s.llm_max_sentences),
        )
        self.add_item(self.persona)
        self.add_item(self.max_sentences)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        persona = (self.persona.value or "").strip()
        try:
            max_sentences = int((self.max_sentences.value or "3").strip())
            if max_sentences <= 0:
                max_sentences = 3
        except ValueError:
            max_sentences = 3
        
        await self.cog.update_llm(persona=persona, max_sentences=max_sentences)
        await interaction.followup.send("LLM 設定已更新並重新載入。", ephemeral=True)


class BlockedKeywordModal(discord.ui.Modal):

    def __init__(self, cog: "ConfigCog") -> None:
        super().__init__(title="編輯禁止字詞")
        self.cog = cog
        default_value = ", ".join(cog.bot.settings.blocked_keywords)
        self.keywords = discord.ui.TextInput[str](
            label="禁止字詞 (以逗號分隔)",
            style=discord.TextStyle.paragraph,
            required=False,
            default=default_value,
        )
        self.add_item(self.keywords)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        terms = [token.strip().lower() for token in self.keywords.value.split(",") if token.strip()]
        await self.cog.update_blocked_keywords(terms)
        await interaction.followup.send("禁止字詞已更新並重新載入設定。", ephemeral=True)


class ConfigPanelView(discord.ui.View):

    def __init__(self, cog: "ConfigCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    async def _ensure_permissions(self, interaction: discord.Interaction) -> bool:
        if not self.cog.is_authorized(interaction):
            await interaction.response.send_message("您沒有權限使用此面板。", ephemeral=True)
            return False
        if not self.cog.is_valid_channel(interaction):
            await interaction.response.send_message("請在指定的設定頻道操作。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="編輯 FAQ", style=discord.ButtonStyle.primary, custom_id="config:edit_faq")
    async def edit_faq(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_permissions(interaction):
            return
        await interaction.response.send_modal(FAQModal(self.cog))

    @discord.ui.button(label="編輯禁止字詞", style=discord.ButtonStyle.secondary, custom_id="config:edit_blocked")
    async def edit_blocked(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_permissions(interaction):
            return
        await interaction.response.send_modal(BlockedKeywordModal(self.cog))

    @discord.ui.button(label="編輯 LLM 設定", style=discord.ButtonStyle.secondary, custom_id="config:edit_llm")
    async def edit_llm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_permissions(interaction):
            return
        await interaction.response.send_modal(LLMSettingsModal(self.cog))

    @discord.ui.button(label="重新載入設定", style=discord.ButtonStyle.success, custom_id="config:reload")
    async def reload(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_permissions(interaction):
            return
        await self.cog.reload_settings()
        await interaction.response.send_message("設定已重新載入。", ephemeral=True)

    @discord.ui.button(label="查看最新日誌", style=discord.ButtonStyle.secondary, custom_id="config:logs")
    async def show_logs(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_permissions(interaction):
            return
        logs = get_recent_logs(15)
        if not logs:
            message = "目前沒有可顯示的記錄。"
        else:
            message = "```\n" + "\n".join(logs) + "\n```"
        await interaction.response.send_message(message, ephemeral=True)


class ConfigCog(commands.Cog):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.view = ConfigPanelView(self)

    @app_commands.command(name="config_panel", description="發布設定控制面板")
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("請在伺服器內使用此指令。", ephemeral=True)
            return
        if not self.is_authorized(interaction):
            await interaction.response.send_message("您沒有權限使用此指令。", ephemeral=True)
            return
        if not self.is_valid_channel(interaction):
            await interaction.response.send_message("請在指定的設定頻道操作。", ephemeral=True)
            return
        await interaction.channel.send("Config 面板已更新。", view=self.view)
        await interaction.response.send_message("面板已發布。", ephemeral=True)

    @app_commands.command(name="config_set_channel", description="將目前頻道設為設定面板限定頻道")
    async def set_config_channel(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("請在伺服器內使用此指令。", ephemeral=True)
            return
        if not self.is_authorized(interaction):
            await interaction.response.send_message("您沒有權限使用此指令。", ephemeral=True)
            return
        data = self._load_settings_json()
        data["config_channel_id"] = int(interaction.channel_id or 0)
        self._write_settings_json(data)
        await self.reload_settings()
        await interaction.response.send_message("已更新設定面板指定頻道。", ephemeral=True)

    def is_authorized(self, interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            return False
        if member.guild_permissions.manage_guild:
            return True
        settings = self.bot.settings
        return any(role.id in settings.support_role_ids for role in member.roles)

    def is_valid_channel(self, interaction: discord.Interaction) -> bool:
        channel_id = self.bot.settings.config_channel_id
        if channel_id is None:
            return True
        return interaction.channel_id == channel_id

    async def update_faq(self, content: str) -> None:
        data = self._load_settings_json()
        data["faq_content"] = content
        if "faq_entries" in data:
            del data["faq_entries"]
        self._write_settings_json(data)
        await self.reload_settings()

    async def update_blocked_keywords(self, keywords: list[str]) -> None:
        data = self._load_settings_json()
        data["blocked_keywords"] = keywords
        self._write_settings_json(data)
        await self.reload_settings()

    async def update_llm(self, *, persona: str | None = None, max_sentences: int | None = None) -> None:
        data = self._load_settings_json()
        llm = data.get("llm", {})
        
        if persona:
            llm["persona_prompt"] = persona
        
        if max_sentences is not None:
            llm["max_sentences"] = int(max_sentences)
        
        data["llm"] = llm
        self._write_settings_json(data)
        await self.reload_settings()

    async def reload_settings(self) -> None:
        new_settings = Settings.from_file(self.bot.settings_path)
        self.bot.settings = new_settings
        self.view = ConfigPanelView(self)
        self.bot.add_view(self.view)
        ticket_cog = self.bot.get_cog("ticket")
        if hasattr(ticket_cog, "refresh_settings"):
            ticket_cog.refresh_settings()
        chat_cog = self.bot.get_cog("LLMChat")
        if hasattr(chat_cog, "refresh_settings"):
            chat_cog.refresh_settings()

    def _load_settings_json(self) -> dict:
        path: Path = self.bot.settings_path
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_settings_json(self, data: dict) -> None:
        path: Path = self.bot.settings_path
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def setup(bot: commands.Bot) -> None:
    cog = ConfigCog(bot)
    await bot.add_cog(cog)
    bot.add_view(cog.view)
