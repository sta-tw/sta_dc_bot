from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class FlagModal(discord.ui.Modal):
    def __init__(self, problem_num: int, flag_format: str) -> None:
        super().__init__(title=f"提交第{problem_num}題的Flag")
        self.problem_num = problem_num
        
        self.flag_input = discord.ui.TextInput(
            label=f"第{problem_num}題 Flag",
            placeholder=f"格式：{flag_format}",
            required=True,
            max_length=200,
        )
        self.add_item(self.flag_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        flag = self.flag_input.value.strip()
        
        cog: FlagSubmission = interaction.client.get_cog("FlagSubmission")  # type: ignore
        if not cog:
            await interaction.response.send_message(
                "系統錯誤，請稍後再試。",
                ephemeral=True
            )
            return

        await cog.verify_flag(interaction, self.problem_num, flag)


class ProblemSelectView(discord.ui.View):
    def __init__(self, cog: FlagSubmission, user_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = user_id

        options = []
        for problem_num in sorted(cog.bot.settings.flags.keys()):
            if cog.has_submitted(user_id, problem_num):
                label = f"第{problem_num}題 (已完成)"
                options.append(
                    discord.SelectOption(
                        label=label,
                        value=str(problem_num),
                        description="你已經成功提交過此題",
                    )
                )
            else:
                options.append(
                    discord.SelectOption(
                        label=f"第{problem_num}題",
                        value=str(problem_num),
                        description="點擊選擇此題進行提交",
                    )
                )

        self.problem_select = discord.ui.Select(
            placeholder="請選擇要提交的題目...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.problem_select.callback = self.select_callback
        self.add_item(self.problem_select)

    async def select_callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "這不是你的提交界面！",
                ephemeral=True
            )
            return

        problem_num = int(self.problem_select.values[0])
        
        if self.cog.has_submitted(self.user_id, problem_num):
            await interaction.response.send_message(
                f"你已經成功提交過第{problem_num}題了！每題只能提交一次。",
                ephemeral=True
            )
            return
        flag_format = self.cog.get_flag_format()
        modal = FlagModal(problem_num, flag_format)
        await interaction.response.send_modal(modal)


class FlagSubmission(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.submitted_flags: dict[int, set[int]] = {}

    def has_submitted(self, user_id: int, problem_num: int) -> bool:
        return problem_num in self.submitted_flags.get(user_id, set())

    def mark_submitted(self, user_id: int, problem_num: int) -> None:
        if user_id not in self.submitted_flags:
            self.submitted_flags[user_id] = set()
        self.submitted_flags[user_id].add(problem_num)

    def get_flag_format(self) -> str:
        if self.bot.settings.flags:
            first_flag = next(iter(self.bot.settings.flags.values()))
            if first_flag.startswith("FLAG{") and first_flag.endswith("}"):
                return "FLAG{...}"
        return "FLAG{...}"

    @app_commands.command(name="flag", description="提交flag")
    async def submit_flag(self, interaction: discord.Interaction) -> None:
        if not self.bot.settings.flags:
            await interaction.response.send_message(
                "❌ 目前沒有可提交的題目。",
                ephemeral=True
            )
            return

        view = ProblemSelectView(self, interaction.user.id)
        await interaction.response.send_message(
            "請選擇要提交的題目：",
            view=view,
            ephemeral=True
        )

    async def verify_flag(
        self, 
        interaction: discord.Interaction, 
        problem_num: int, 
        flag: str
    ) -> None:
        if self.has_submitted(interaction.user.id, problem_num):
            await interaction.response.send_message(
                f"你已經成功提交過第{problem_num}題了！每題只能提交一次。",
                ephemeral=True
            )
            return

        correct_flag = self.bot.settings.flags.get(problem_num)
        if not correct_flag:
            await interaction.response.send_message(
                "❌ 題目不存在。",
                ephemeral=True
            )
            return

        if flag != correct_flag:
            await interaction.response.send_message(
                "❌ Flag錯誤！請再試試看。",
                ephemeral=True
            )
            return

        self.mark_submitted(interaction.user.id, problem_num)

        await interaction.response.send_message(
            f"✅ 恭喜！你成功找到了第{problem_num}題的flag！",
            ephemeral=True
        )

        announcement_channel_id = self.bot.settings.flag_announcement_channel_id
        if announcement_channel_id:
            announcement_channel = self.bot.get_channel(announcement_channel_id)
            if announcement_channel and isinstance(
                announcement_channel, 
                (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)
            ):
                try:
                    await announcement_channel.send(
                        f"🎉 恭喜 {interaction.user.mention} 找到了第{problem_num}題的flag！"
                    )
                except discord.Forbidden:
                    self.bot.logger.error(
                        "無法在頻道 %s 發送訊息：權限不足",
                        announcement_channel_id
                    )
                except Exception as e:
                    self.bot.logger.error(
                        "發送公告訊息時發生錯誤：%s",
                        e
                    )
            else:
                self.bot.logger.warning(
                    "Flag公告頻道 %s 未找到或類型不正確",
                    announcement_channel_id
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FlagSubmission(bot))
