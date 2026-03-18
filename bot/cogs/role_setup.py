import discord
from discord import app_commands
from discord.ext import commands
from utils.role_ui import Verfication_View

def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

class Role_Setup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.emoji = self.bot.emoji

    @app_commands.command(name="role_setup", description="建立身份驗證申請面板")
    @is_admin()
    async def setup_buttons(self, interaction: discord.Interaction):
        await interaction.response.defer()

        embed = discord.Embed(
            title=f"{self.emoji.get('!')} 身份驗證",
            description=f"請選擇您需要的操作：\n\n{self.emoji.get('verify_check')} **驗證身份** - 已驗證過的老人或應屆特選生點擊取得身分組\n\n{self.emoji.get('F')} **申請身分組** - 尚未驗證過的老人或應屆特選生按 F 進入申請",
            color=discord.Color.blue()
        )

        view = Verfication_View(bot=self.bot)
        await interaction.followup.send(embed=embed, view=view)

async def setup(bot):
    await bot.add_cog(Role_Setup(bot))
