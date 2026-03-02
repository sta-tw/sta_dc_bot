import discord
from discord.ui import Button, View
from database.db_manager import DatabaseManager
from bot.utils.role_helper import get_or_create_role, update_role_id_in_config, get_role_color

class Gay(View):
    def __init__(self, bot=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.emoji = self.bot.emoji
        self.db_manager = None

        role_button = Button(
            label="拿 Gay 身份組",
            style=discord.ButtonStyle.success,
            emoji=self.emoji.get('heart1'),
            custom_id="get_gay_role"
        )
        role_button.callback = self.role_callback

        self.add_item(role_button)

    async def ensure_db_manager(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        guild_name = interaction.guild.name

        if (self.db_manager is None or
            self.db_manager.guild_id != guild_id):
            self.db_manager = DatabaseManager(guild_id, guild_name)
            await self.db_manager.init_db()

        return self.db_manager

    async def role_callback(self, interaction: discord.Interaction):
        await self.ensure_db_manager(interaction)

        role_id = await self.db_manager.get_role_id("gay")
        role_name = "Gay"

        role = await get_or_create_role(
            interaction.guild,
            role_id,
            role_name,
            get_role_color("gay"),
            f"自動創建 {role_name} 身份組"
        )

        if not role:
            embed = discord.Embed(
                title="身份組創建失敗",
                description="機器人沒有權限創建身份組，請聯絡管理員。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if not role_id or role.id != role_id:
            await update_role_id_in_config(self.db_manager, "gay", role.id)

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            embed = discord.Embed(
                title="身份組已移除",
                description=f"已移除 {role.mention} 身份組。",
                color=discord.Color.red()
            )
        else:
            await interaction.user.add_roles(role)
            embed = discord.Embed(
                title="身份組已取得",
                description=f"已取得 {role.mention} 身份組。",
                color=discord.Color.green()
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

class Crown(View):
    def __init__(self, bot=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.emoji = self.bot.emoji
        self.db_manager = None

        role_button = Button(
            label="拿 Crown 身份組",
            style=discord.ButtonStyle.success,
            emoji=self.emoji.get('crown1'),
            custom_id="get_crown_role"
        )
        role_button.callback = self.role_callback

        self.add_item(role_button)

    async def ensure_db_manager(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        guild_name = interaction.guild.name

        if (self.db_manager is None or
            self.db_manager.guild_id != guild_id):
            self.db_manager = DatabaseManager(guild_id, guild_name)
            await self.db_manager.init_db()

        return self.db_manager

    async def role_callback(self, interaction: discord.Interaction):
        await self.ensure_db_manager(interaction)

        role_id = await self.db_manager.get_role_id("crown")
        role_name = "Crown"

        role = await get_or_create_role(
            interaction.guild,
            role_id,
            role_name,
            get_role_color("crown"),
            f"自動創建 {role_name} 身份組"
        )

        if not role:
            embed = discord.Embed(
                title="身份組創建失敗",
                description="機器人沒有權限創建身份組，請聯絡管理員。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if not role_id or role.id != role_id:
            await update_role_id_in_config(self.db_manager, "crown", role.id)

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            embed = discord.Embed(
                title="身份組已移除",
                description=f"已移除 {role.mention} 身份組。",
                color=discord.Color.red()
            )
        else:
            await interaction.user.add_roles(role)
            embed = discord.Embed(
                title="身份組已取得",
                description=f"已取得 {role.mention} 身份組。",
                color=discord.Color.green()
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

class Cat(View):
    def __init__(self, bot=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.emoji = self.bot.emoji
        self.db_manager = None

        role_button = Button(
            label="拿 Cat 身份組",
            style=discord.ButtonStyle.success,
            emoji=self.emoji.get('cat0'),
            custom_id="get_cat_role"
        )
        role_button.callback = self.role_callback

        self.add_item(role_button)

    async def ensure_db_manager(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id
        guild_name = interaction.guild.name

        if (self.db_manager is None or
            self.db_manager.guild_id != guild_id):
            self.db_manager = DatabaseManager(guild_id, guild_name)
            await self.db_manager.init_db()

        return self.db_manager

    async def role_callback(self, interaction: discord.Interaction):
        await self.ensure_db_manager(interaction)

        role_id = await self.db_manager.get_role_id("cat")
        role_name = "Cat"

        role = await get_or_create_role(
            interaction.guild,
            role_id,
            role_name,
            get_role_color("cat"),
            f"自動創建 {role_name} 身份組"
        )

        if not role:
            embed = discord.Embed(
                title="身份組創建失敗",
                description="機器人沒有權限創建身份組，請聯絡管理員。",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if not role_id or role.id != role_id:
            await update_role_id_in_config(self.db_manager, "cat", role.id)

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            embed = discord.Embed(
                title="身份組已移除",
                description=f"已移除 {role.mention} 身份組。",
                color=discord.Color.red()
            )
        else:
            await interaction.user.add_roles(role)
            embed = discord.Embed(
                title="身份組已取得",
                description=f"已取得 {role.mention} 身份組。",
                color=discord.Color.green()
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

def setup_persistent_views_role_button(bot):
    try:
        bot.add_view(Gay(bot=bot))
        bot.add_view(Crown(bot=bot))
        bot.add_view(Cat(bot=bot))
        return True
    except Exception as e:
        print(f"設定持久化視圖時發生錯誤: {e}")
        return False
