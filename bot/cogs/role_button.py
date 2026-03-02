import discord
from discord import app_commands
from discord.ext import commands
from utils.role_button_ui import Gay, Crown, Cat
import asyncio
import colorsys

def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

class Role_Button(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.emoji = self.bot.emoji

    role_choices = [
        app_commands.Choice(name="Gay", value="gay"),
        app_commands.Choice(name="Crown", value="crown"),
        app_commands.Choice(name="Cat", value="cat")
    ]

    @app_commands.command(name="role_button", description="建立領取身份組按鈕")
    @app_commands.choices(category=role_choices)
    @is_admin()
    async def setup_buttonss(self, interaction: discord.Interaction, category: str):

        if category == "gay":
            embed_title = f"{self.emoji.get('gay1')} 多元身份認同"
            embed_description = f"我們支持多元身份認同\n\n歡迎 {self.emoji.get('gay2')}"
            view = Gay(bot=self.bot)
        elif category == "crown":
            embed_title = f"{self.emoji.get('gay1')} 多元身份認同"
            embed_description = f"我們支持多元身份認同\n\n歡迎 {self.emoji.get('crown1')}"
            view = Crown(bot=self.bot)
        elif category == "cat":
            embed_title = f"{self.emoji.get('cat0')} 加入貓貓教"
            embed_description = f"我們歡迎任何物種入教\n\n歡迎 {self.emoji.get('cat4')}"
            view = Cat(bot=self.bot)
        else:
            await interaction.response.send_message("無效的選項！", ephemeral=True)
            return

        embed = discord.Embed(
            title=embed_title,
            description=embed_description,
            color=0xFF0000
        )

        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        try:
            while True:
                for h in range(0, 361, 5):
                    r, g, b = colorsys.hsv_to_rgb(h / 360, 1, 1)
                    hex_color = (int(r * 255) << 16) + (int(g * 255) << 8) + int(b * 255)
                    await message.edit(embed=discord.Embed(
                        title=embed_title,
                        description=embed_description,
                        color=hex_color
                    ))
                    await asyncio.sleep(0.1)
        except discord.errors.NotFound:
            pass

async def setup(bot):
    await bot.add_cog(Role_Button(bot))
