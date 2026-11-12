import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, override

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.config_paths import ConfigPaths
from bot.utils.counting import MAX_DIGITS, format_units, parse_number
from database.count import (
    COOLDOWN,
    COUNTERS,
    DAILY_CLAIMS,
    PERSONAL_LIMIT,
    SACRIFICE_REWARD,
    SERVER_LIMIT,
    TAIPEI,
    WEEKDAY_REWARD,
    WEEKEND_REWARD,
    Adjustment,
    CountRepository,
    InvalidGraceError,
    UserStatistics,
)

log = logging.getLogger(__name__)

type Context = commands.Context[commands.Bot | commands.AutoShardedBot]
type Scope = Literal["global", "server"]
type Period = Literal["7d", "30d", "all"]
type SortKey = Literal["successes", "errors", "error_rate"]

DAY = 86400
PAGE_SIZE = 10
TEST_TIMEOUT = 120
# Anti-spam: at most one mistake reply per channel in this many seconds.
ERROR_REPLY_INTERVAL = 3.0
# Statistics may open every guild database, so they are rate limited.
STATS_COOLDOWN = 10.0
STATS_CONCURRENCY = 2
TEST_COOLDOWN = 3.0
BAN_LIST_LIMIT = 50
NO_MENTIONS: Final = discord.AllowedMentions.none()
# Reactions are the in-channel result markers.
REACTIONS: Final = {
    "success": "✅",
    "protected": "🛡️",
    "revived": "⚠️",
    "error": "❌",
}
REVIVE_SOURCES: Final = {"personal": "1 個不死圖騰", "server": "1 次伺服器復活額度"}
PERIOD_DAYS: Final[dict[Period, int]] = {"7d": 7, "30d": 30, "all": 0}

HELP = f"""**數數規則**
大家輪流在數數頻道從 1 往上數，每則訊息輸入下一個數字。Bot 會讀取訊息開頭的數字或算式，後面空一格就可以接文字；不是以數字或算式開頭的訊息會被忽略，可以照常聊天。
同一個人不能連續數兩次，要等別人接上才能再數；不小心重複送出自己剛數的數字則會被忽略。數錯（包括連續數兩次）會讓進度歸零，下一個要從 1 重新開始，除非觸發下方的自動復活。訊息送出後再編輯或刪除，都不會改變進度，Bot 會提醒大家下一個數字。
自動復活：數錯時如果你有不死圖騰，會自動消耗 1 個保住進度；沒有的話改用伺服器的 1 次復活額度。兩者都沒有才會歸零。復活時仍會記一次錯誤。
免死時間：有人數對後的這段秒數內，其他人送出相同數字不算錯，也不算成功。預設為 0（關閉），由管理員設定。
Bot 會在每則數數訊息加上反應，分別標示數對、免死保護、自動復活和數錯歸零；第一次數到 10、100、1000 這類新位數時會發慶祝訊息。

**不死圖騰**
`/count claim`：領取不死圖騰，平日 {format_units(WEEKDAY_REWARD)} 個、週末 {format_units(WEEKEND_REWARD)} 個。依台北時間每天最多領 {DAILY_CLAIMS} 次，兩次至少間隔 {COOLDOWN // 3600} 小時；每人最多持有 {format_units(PERSONAL_LIMIT)} 個，超過的部分不會入帳。
`/count wallet [成員]`：查看圖騰數量、伺服器復活額度和下次可領取的時間。
`/count donatesave`：獻祭 1 個圖騰，讓伺服器復活額度增加 {format_units(SACRIFICE_REWARD)} 次（上限 {format_units(SERVER_LIMIT)} 次）。

**查詢**
`/count status`：查看這個頻道目前的數字、最高紀錄和免死時間。
`/count stats [範圍] [成員] [期間]`：查看統計；不指定範圍和成員時查看自己在本伺服器的紀錄。
`/count leaderboard [範圍] [期間] [頁數] [排序]`：排行榜，每頁 {PAGE_SIZE} 人。
`/count test`：開啟只有自己看得到的輸入框，用數數頻道的規則讀取你輸入的內容並顯示結果，讀不到數字時顯示 0；不影響任何紀錄。

**管理員**
`/count setup 頻道 [秒數]`：啟用數數頻道，可同時設定免死時間。
`/count settings 頻道 秒數`：修改免死時間，可以用小數，0 為關閉。
`/count remove 頻道`：停用數數；統計和圖騰會保留。
`/count give 成員 數量`、`/count give_server 數量`：增加或扣除（負數）個人圖騰或伺服器額度。
`/count ban 成員`、`/count unban 成員`、`/count bans`：禁止或恢復成員數數，被禁止的人送出的數字會被忽略。

**算式**
訊息開頭可以是算式，依照先乘除後加減計算，結果無條件捨去成整數，例如 `12.9`、`3*4`、`√144` 都算 12。
支援全形與半形字元、`+ - * / // % ^ **`、括號、階乘 `!`、科學記號 `1e3`、`0x` `0b` `0o` 進位、上標 `2²`、`√` `∛`、常數 `pi` `e` `i`，以及 `sqrt` `log` `ln` `sin` `cos` `tan` `abs` `floor` `ceil` `round` 等函式（`log(x)` 以 10 為底，三角函數用弧度）。
也看得懂課本寫法：`|x|` `⌊x⌋` `⌈x⌉`、`log₂(8)`、`sin²(x)`、`mod`、`C(n,k)` `P(n,k)` `φ(n)` `τ(n)` `Γ` `ζ`、`det([[1,2],[3,4]])`、`∑ₖ₌₁¹⁰ k²`、`∫₀¹ 2x dx`、`limₓ→0 sin(x)/x`、`(d/dx)(x²)|ₓ₌₂`。
複數結果取實部，整數部分最多 {MAX_DIGITS} 位；運算子後面接的不是算式時，會當成一般文字。常數 `e`、`i` 只認小寫。以上指令也可以用 `!count 子指令` 呼叫。"""


class CountUsageError(commands.CommandError):
    """A user-facing error whose message is sent back as-is."""


@dataclass(frozen=True)
class StatsQuery:
    is_global: bool = False
    user_id: int | None = None
    days: int = 0
    page: int = 1
    sort: SortKey = "successes"

    @property
    def label(self) -> str:
        parts = ["全域" if self.is_global else "此伺服器"]
        if self.user_id is not None:
            parts.append(f"<@{self.user_id}>")
        parts.append(f"近 {self.days} 天" if self.days else "全部期間")
        return "｜".join(parts)


def build_query(
    scope: Scope | None,
    member_id: int | None,
    period: Period | None,
    author_id: int,
    *,
    personal: bool,
    page: int = 1,
    sort: SortKey | None = None,
) -> StatsQuery:
    """Personal stats default to the author unless a scope or member is given."""
    if personal and scope is None and member_id is None:
        member_id = author_id
    return StatsQuery(
        is_global=scope == "global",
        user_id=member_id,
        days=PERIOD_DAYS[period or "all"],
        page=page,
        sort=sort or "successes",
    )


def is_new_magnitude(number: int) -> bool:
    """True for 10, 100, 1000, ...: the first number with one more digit."""
    digits = str(number)
    return number >= 10 and digits[0] == "1" and not digits[1:].strip("0")


def parse_units(amount: float) -> int:
    """Convert a totem amount such as 0.5 or -1 into 1/100 units."""
    if not math.isfinite(amount):
        raise CountUsageError("數量必須是有限的數字。")
    units = round(amount * 100)
    if units == 0 or abs(amount * 100 - units) > 1e-6:
        raise CountUsageError("數量必須不為 0，且最多兩位小數，例如 1、0.5 或 -1。")
    return units


def _unwrap(error: BaseException) -> BaseException:
    """Hybrid and invoke errors wrap the exception that actually happened."""
    while isinstance(
        error,
        commands.HybridCommandError
        | commands.CommandInvokeError
        | app_commands.CommandInvokeError,
    ):
        error = error.original
    return error


def _guild(ctx: Context) -> discord.Guild:
    if ctx.guild is None:
        raise commands.NoPrivateMessage
    return ctx.guild


async def _send(ctx: Context, content: str, *, ephemeral: bool = False) -> None:
    _ = await ctx.send(content, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS)


def _audit(ctx: Context, action: str, **details: object) -> None:
    """Record who changed counting settings or balances, for later review."""
    guild_id = ctx.guild.id if ctx.guild else None
    extra = " ".join(f"{key}={value}" for key, value in details.items())
    log.info(
        "數數管理操作 guild=%s actor=%s action=%s %s",
        guild_id,
        ctx.author.id,
        action,
        extra,
    )


def _adjustment_note(requested: int, adjustment: Adjustment) -> str:
    if adjustment.applied == requested:
        return ""
    return f"（受上下限限制，實際調整 {format_units(adjustment.applied)}）"


def read_count(content: str) -> int:
    """The number a message counts as in a counting channel; 0 when none."""
    number = parse_number(content)
    return 0 if number is None else number


class CountTestModal(discord.ui.Modal, title="數數測試（不影響任何紀錄）"):
    content: discord.ui.TextInput["CountTestModal"] = discord.ui.TextInput(
        label="輸入要測試的訊息",
        placeholder="會用數數頻道的規則讀取",
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    @override
    async def on_submit(self, interaction: discord.Interaction) -> None:
        _ = await interaction.response.send_message(
            f"結果：**{read_count(self.content.value)}**", ephemeral=True
        )


class CountTestView(discord.ui.View):
    """Prefix commands cannot open a modal directly, so they get a button."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=TEST_TIMEOUT)
        self.owner_id: int = owner_id
        self.message: discord.Message | None = None

    @override
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        _ = await interaction.response.send_message(
            "請自己輸入 `/count test` 開啟測試。", ephemeral=True
        )
        return False

    @override
    async def on_timeout(self) -> None:
        self.start.disabled = True
        self.start.label = "測試已結束，請重新輸入 /count test"
        if self.message is not None:
            try:
                _ = await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="開始測試", style=discord.ButtonStyle.primary)
    async def start(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button["CountTestView"],
    ) -> None:
        _ = await interaction.response.send_modal(CountTestModal())


class Count(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot: commands.Bot = bot
        self._repositories: dict[int, CountRepository] = {}
        self._channels: dict[int, set[int]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        # channel ID -> monotonic time of the last mistake reply.
        self._last_error_reply: dict[int, float] = {}
        # channel ID -> message ID already announced as edited or deleted.
        self._announced: dict[int, int] = {}

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = self._locks[guild_id] = asyncio.Lock()
        return lock

    async def _repository(self, guild_id: int) -> CountRepository:
        """Must be called while holding the guild lock."""
        repository = self._repositories.get(guild_id)
        if repository is None:
            repository = CountRepository(ConfigPaths.guild_database(guild_id))
            self._channels[guild_id] = await repository.initialize()
            self._repositories[guild_id] = repository
        return repository

    async def _remove_channel(self, guild_id: int, channel_id: int) -> None:
        """Must be called while holding the guild lock."""
        repository = await self._repository(guild_id)
        if channel_id in self._channels[guild_id]:
            await repository.remove(channel_id)
            self._channels[guild_id].discard(channel_id)

    @override
    async def cog_command_error(self, ctx: Context, error: Exception) -> None:
        original = _unwrap(error)
        if isinstance(original, CountUsageError | InvalidGraceError):
            text = str(original)
        elif isinstance(original, commands.CommandOnCooldown):
            text = f"操作太頻繁，請在 {math.ceil(original.retry_after)} 秒後再試。"
        elif isinstance(original, commands.MissingPermissions):
            text = "只有伺服器管理員可以使用這個指令。"
        elif isinstance(original, commands.NoPrivateMessage):
            text = "請在伺服器裡使用這個指令。"
        elif isinstance(
            original, commands.UserInputError | app_commands.TransformerError
        ):
            text = "參數格式不對，請用 `/count help` 查看指令用法。"
        else:
            log.error("數數指令執行失敗", exc_info=original)
            text = "執行失敗，請稍後再試；如果一直發生，請聯絡管理員。"
        await _send(ctx, text, ephemeral=True)

    # --- command group ----------------------------------------------------------

    @commands.hybrid_group(
        name="count",
        fallback="help",
        description="查看數數玩法與指令說明",
    )
    @commands.guild_only()
    async def count(self, ctx: Context) -> None:
        await _send(ctx, HELP, ephemeral=True)

    # --- administration ---------------------------------------------------------

    @count.command(name="setup", description="啟用數數頻道並設定免死時間")
    @app_commands.describe(
        channel="要啟用數數的頻道", grace_seconds="免死時間（秒），0 為關閉"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def count_setup(
        self, ctx: Context, channel: discord.TextChannel, grace_seconds: float = 0.0
    ) -> None:
        guild = _guild(ctx)
        CountRepository.validate_grace(grace_seconds)
        permissions = channel.permissions_for(guild.me)
        if not (
            permissions.view_channel
            and permissions.read_message_history
            and permissions.send_messages
            and permissions.add_reactions
        ):
            await _send(
                ctx,
                (
                    f"Bot 在 {channel.mention} 需要「檢視頻道」「讀取訊息歷史」"
                    "「傳送訊息」「新增反應」四項權限。"
                ),
                ephemeral=True,
            )
            return
        # Messages older than this command are ignored; both IDs are snowflakes.
        start_id = ctx.interaction.id if ctx.interaction else ctx.message.id
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            existing = await repository.state(channel.id)
            number = int(existing["current_number"]) if existing else 0
            await repository.configure(channel.id, number, start_id, grace_seconds)
            self._channels[guild.id].add(channel.id)
        _audit(ctx, "setup", channel=channel.id, grace_seconds=grace_seconds)
        await _send(
            ctx,
            (
                f"已在 {channel.mention} 啟用數數，目前是 **{number}**，"
                f"下一個是 **{number + 1}**。\n"
                f"免死時間：{grace_seconds:g} 秒（0 為關閉）。"
                "如果這個頻道之前就啟用過，會沿用原本的進度。"
            ),
        )

    @count.command(name="settings", description="修改免死時間（秒），0 為關閉")
    @app_commands.describe(channel="數數頻道", grace_seconds="免死時間（秒），0 為關閉")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def count_settings(
        self, ctx: Context, channel: discord.TextChannel, grace_seconds: float
    ) -> None:
        guild = _guild(ctx)
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            changed = await repository.settings(channel.id, grace_seconds)
        if changed:
            _audit(ctx, "settings", channel=channel.id, grace_seconds=grace_seconds)
        await _send(
            ctx,
            f"{channel.mention} 的免死時間已改為 {grace_seconds:g} 秒（0 為關閉）。"
            if changed
            else f"{channel.mention} 還沒有啟用數數。",
        )

    @count.command(name="remove", description="停用數數，保留統計與圖騰")
    @app_commands.describe(channel="要停用數數的頻道")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def count_remove(self, ctx: Context, channel: discord.TextChannel) -> None:
        guild = _guild(ctx)
        async with self._lock(guild.id):
            _ = await self._repository(guild.id)
            enabled = channel.id in self._channels[guild.id]
            await self._remove_channel(guild.id, channel.id)
        if enabled:
            _audit(ctx, "remove", channel=channel.id)
        await _send(
            ctx,
            f"已停用 {channel.mention} 的數數，統計和圖騰都會保留。"
            if enabled
            else f"{channel.mention} 還沒有啟用數數。",
        )

    @count.command(name="give", description="增加或扣除成員的不死圖騰")
    @app_commands.describe(
        member="要調整的成員", amount="圖騰數量，負數為扣除，例如 1、0.5、-1"
    )
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def count_give(
        self, ctx: Context, member: discord.Member, amount: float
    ) -> None:
        guild = _guild(ctx)
        units = parse_units(amount)
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            adjustment = await repository.adjust_wallet(member.id, units, time.time())
        _audit(ctx, "give", member=member.id, units=adjustment.applied)
        await _send(
            ctx,
            (
                f"已調整 {member.mention} 的不死圖騰，目前持有 "
                f"**{format_units(adjustment.balance)}／"
                f"{format_units(PERSONAL_LIMIT)}** 個。"
                f"{_adjustment_note(units, adjustment)}"
            ),
        )

    @count.command(name="give_server", description="增加或扣除伺服器復活額度")
    @app_commands.describe(amount="額度，負數為扣除，例如 1、0.2、-1")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def count_give_server(self, ctx: Context, amount: float) -> None:
        guild = _guild(ctx)
        units = parse_units(amount)
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            adjustment = await repository.adjust_server(
                ctx.author.id, units, time.time()
            )
        _audit(ctx, "give_server", units=adjustment.applied)
        await _send(
            ctx,
            (
                "已調整伺服器復活額度，目前為 "
                f"**{format_units(adjustment.balance)}／"
                f"{format_units(SERVER_LIMIT)}** 次。"
                f"{_adjustment_note(units, adjustment)}"
            ),
        )

    @count.command(name="ban", description="禁止成員數數")
    @app_commands.describe(member="要禁止數數的成員")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def count_ban(self, ctx: Context, member: discord.Member) -> None:
        guild = _guild(ctx)
        if member.bot:
            raise CountUsageError("Bot 本來就不會參與數數。")
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            banned = await repository.ban(member.id, ctx.author.id, time.time())
        if banned:
            _audit(ctx, "ban", member=member.id)
        await _send(
            ctx,
            f"已禁止 {member.mention} 數數，之後送出的數字都會被忽略。"
            if banned
            else f"{member.mention} 已經在禁止名單裡。",
        )

    @count.command(name="unban", description="恢復成員數數")
    @app_commands.describe(member="要恢復數數的成員")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def count_unban(self, ctx: Context, member: discord.Member) -> None:
        guild = _guild(ctx)
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            unbanned = await repository.unban(member.id)
        if unbanned:
            _audit(ctx, "unban", member=member.id)
        await _send(
            ctx,
            f"已恢復 {member.mention} 數數。"
            if unbanned
            else f"{member.mention} 不在禁止名單裡。",
        )

    @count.command(name="bans", description="查看禁止數數的成員")
    @commands.guild_only()
    @commands.has_guild_permissions(administrator=True)
    async def count_bans(self, ctx: Context) -> None:
        guild = _guild(ctx)
        async with self._lock(guild.id):
            user_ids = await (await self._repository(guild.id)).bans()
        if not user_ids:
            await _send(ctx, "目前沒有被禁止數數的成員。", ephemeral=True)
            return
        shown = "、".join(f"<@{user_id}>" for user_id in user_ids[:BAN_LIST_LIMIT])
        more = (
            f"，以及另外 {len(user_ids) - BAN_LIST_LIMIT} 人"
            if len(user_ids) > BAN_LIST_LIMIT
            else ""
        )
        await _send(
            ctx, f"禁止數數的成員（{len(user_ids)} 人）：{shown}{more}", ephemeral=True
        )

    # --- members ----------------------------------------------------------------

    @count.command(name="status", description="查看這個頻道的數數進度")
    @commands.guild_only()
    async def count_status(self, ctx: Context) -> None:
        guild = _guild(ctx)
        async with self._lock(guild.id):
            row = await (await self._repository(guild.id)).state(ctx.channel.id)
        if row is None:
            await _send(ctx, "這個頻道還沒有啟用數數。", ephemeral=True)
            return
        current = int(row["current_number"])
        await _send(
            ctx,
            (
                f"目前：**{current}**｜下一個：**{current + 1}**\n"
                f"最高紀錄：**{row['best_number']}**｜"
                f"免死時間：{row['grace_seconds']:g} 秒"
            ),
        )

    @count.command(name="claim", description="領取不死圖騰，每天最多兩次")
    @commands.guild_only()
    async def count_claim(self, ctx: Context) -> None:
        guild = _guild(ctx)
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            text = await repository.claim(ctx.author.id, time.time())
        await _send(ctx, text)

    @count.command(name="wallet", description="查看不死圖騰與伺服器復活額度")
    @app_commands.describe(member="要查看的成員，預設為自己")
    @commands.guild_only()
    async def count_wallet(
        self, ctx: Context, member: discord.Member | None = None
    ) -> None:
        guild = _guild(ctx)
        target = member or ctx.author
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            wallet, server_units = await repository.wallet(target.id)
        now = time.time()
        local = datetime.fromtimestamp(now, TAIPEI)
        claimed = (
            wallet["claims_today"]
            if wallet["claim_date"] == local.date().isoformat()
            else 0
        )
        last_claim = wallet["last_claim"]
        next_claim = last_claim + COOLDOWN if last_claim is not None else now
        if claimed >= DAILY_CLAIMS:
            midnight = datetime.combine(
                local.date() + timedelta(days=1), datetime.min.time(), tzinfo=TAIPEI
            )
            next_claim = max(next_claim, midnight.timestamp())
        if wallet["units"] >= PERSONAL_LIMIT:
            ready = "已達持有上限，請先使用或獻祭"
        elif next_claim > now:
            ready = f"<t:{int(next_claim)}:R>"
        else:
            ready = "現在就可以領取"
        await _send(
            ctx,
            (
                f"{target.mention} 的不死圖騰：**{format_units(wallet['units'])}／"
                f"{format_units(PERSONAL_LIMIT)}** 個\n"
                f"伺服器復活額度：**{format_units(server_units)}／"
                f"{format_units(SERVER_LIMIT)}** 次\n"
                f"今日已領取：{claimed}／{DAILY_CLAIMS} 次｜下次領取：{ready}"
            ),
        )

    @count.command(
        name="donatesave", description="獻祭 1 個不死圖騰，增加伺服器復活額度"
    )
    @commands.guild_only()
    async def count_sacrifice(self, ctx: Context) -> None:
        guild = _guild(ctx)
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            text = await repository.sacrifice(ctx.author.id, time.time())
        await _send(ctx, text)

    async def _statistics(
        self, guild_id: int, query: StatsQuery
    ) -> list[UserStatistics]:
        since = time.time() - query.days * DAY if query.days else 0
        if query.is_global:
            paths = [
                path
                for path in ConfigPaths.DATABASE_DIR.glob("*.db")
                if path.stem.isdecimal()
            ]
            return await CountRepository.global_statistics(paths, since, query.user_id)
        async with self._lock(guild_id):
            repository = await self._repository(guild_id)
        return await repository.statistics(since, query.user_id)

    @commands.cooldown(1, STATS_COOLDOWN, commands.BucketType.user)
    @commands.max_concurrency(STATS_CONCURRENCY, wait=True)
    @count.command(name="stats", description="查看數數統計")
    @app_commands.describe(
        scope="global 為所有伺服器，server 為此伺服器",
        member="只看這位成員",
        period="統計期間",
    )
    @commands.guild_only()
    async def count_stats(
        self,
        ctx: Context,
        scope: Scope | None = None,
        member: discord.Member | None = None,
        period: Period | None = None,
    ) -> None:
        guild = _guild(ctx)
        await ctx.defer()
        query = build_query(
            scope, member.id if member else None, period, ctx.author.id, personal=True
        )
        rows = await self._statistics(guild.id, query)
        if not rows:
            await _send(ctx, f"{query.label}：沒有數數紀錄。")
            return
        total = {key: sum(row[key] for row in rows) for key in COUNTERS}
        attempts = total["successes"] + total["errors"]
        rate = total["errors"] / attempts if attempts else 0
        last = max(row["last_active"] for row in rows)
        await _send(
            ctx,
            (
                f"{query.label}｜參與人數：{len(rows)}\n"
                f"成功：**{total['successes']}**｜錯誤：**{total['errors']}**｜"
                f"錯誤率：**{rate:.1%}**\n"
                f"免死保護：{total['protected']}｜"
                f"復活：{total['revivals']}｜獻祭：{total['sacrifices']}\n"
                f"最後參與：<t:{int(last)}:R>"
            ),
        )

    @commands.cooldown(1, STATS_COOLDOWN, commands.BucketType.user)
    @commands.max_concurrency(STATS_CONCURRENCY, wait=True)
    @count.command(name="leaderboard", description="查看數數排行榜")
    @app_commands.describe(
        scope="global 為所有伺服器，server 為此伺服器",
        period="統計期間",
        page="頁數",
        sort="排序方式",
    )
    @commands.guild_only()
    async def count_leaderboard(
        self,
        ctx: Context,
        scope: Scope | None = None,
        period: Period | None = None,
        page: commands.Range[int, 1] = 1,
        sort: SortKey | None = None,
    ) -> None:
        guild = _guild(ctx)
        await ctx.defer()
        query = build_query(
            scope, None, period, ctx.author.id, personal=False, page=page, sort=sort
        )
        rows = await self._statistics(guild.id, query)
        if query.sort == "error_rate":
            # Lowest error rate first; people with more attempts win ties.
            rows.sort(
                key=lambda r: (
                    r["successes"] + r["errors"] == 0,
                    r["error_rate"],
                    -(r["successes"] + r["errors"]),
                    r["user_id"],
                )
            )
        else:
            sort_key = query.sort
            rows.sort(key=lambda r: (-r[sort_key], r["user_id"]))
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        if query.page > pages:
            await _send(ctx, f"排行榜只有 {pages} 頁。", ephemeral=True)
            return
        start = (query.page - 1) * PAGE_SIZE
        lines = [
            (
                f"**數數排行榜**｜{query.label}｜第 {query.page}／{pages} 頁"
                f"（共 {len(rows)} 人）"
            )
        ]
        lines.extend(
            (
                f"{rank}. <@{row['user_id']}> 成功 {row['successes']}｜"
                f"錯誤 {row['errors']}｜錯誤率 {row['error_rate']:.1%}｜"
                f"免死保護 {row['protected']}"
            )
            for rank, row in enumerate(rows[start : start + PAGE_SIZE], start + 1)
        )
        if not rows:
            lines.append("還沒有任何紀錄。")
        lines.append("錯誤率＝錯誤 ÷（成功＋錯誤），免死保護不計入。")
        await _send(ctx, "\n".join(lines))

    @commands.cooldown(1, TEST_COOLDOWN, commands.BucketType.user)
    @count.command(name="test", description="用數數規則讀取一段文字，不影響任何紀錄")
    @commands.guild_only()
    async def count_test(self, ctx: Context) -> None:
        if ctx.interaction is not None:
            _ = await ctx.interaction.response.send_modal(CountTestModal())
            return
        view = CountTestView(ctx.author.id)
        view.message = await ctx.send(
            "按下按鈕開啟只有你看得到的輸入框。",
            view=view,
            allowed_mentions=NO_MENTIONS,
        )

    # --- counting ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot or message.webhook_id:
            return
        # Skip other channels before evaluating math. A guild whose channels are
        # not loaded yet (e.g. right after a reload) falls through to the lock.
        channels = self._channels.get(guild.id)
        if channels is not None and message.channel.id not in channels:
            return
        number = parse_number(message.content)
        if number is None:
            return
        context = await self.bot.get_context(message)
        if context.prefix is not None:
            return
        async with self._lock(guild.id):
            repository = await self._repository(guild.id)
            if message.channel.id not in self._channels[guild.id]:
                return
            result = await repository.accept(
                message.channel.id,
                number,
                message.id,
                message.author.id,
                message.created_at.timestamp(),
            )
        # Database order is settled; Discord I/O no longer needs the lock.
        if result is None:
            return
        try:
            await message.add_reaction(REACTIONS[result.outcome])
        except discord.HTTPException:
            log.warning("無法為數數訊息 %s 新增反應", message.id)
        if result.outcome == "success":
            if result.new_record and is_new_magnitude(number):
                await self._celebrate(message.channel, number)
            return
        if result.outcome == "protected":
            return
        reason = (
            "不能連續數兩次，請讓其他人接著數。"
            if result.consecutive
            else f"數錯了，應該是 **{result.expected}**。"
        )
        if result.revive_source is not None:
            follow_up = (
                f"已自動消耗{REVIVE_SOURCES[result.revive_source]}，"
                f"進度維持 **{result.previous}**，下一個是 **{result.expected}**。"
            )
        elif result.previous > 0:
            follow_up = (
                "沒有可用的不死圖騰或伺服器復活額度，"
                f"進度從 **{result.previous}** 歸零，請從 **1** 重新開始。"
            )
        else:
            follow_up = "請從 **1** 開始。"
        if not self._error_reply_allowed(message.channel.id):
            return  # The reaction already shows the result.
        try:
            _ = await message.reply(
                f"{message.author.mention} {reason}\n{follow_up}",
                mention_author=True,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=[message.author], roles=False
                ),
            )
        except discord.HTTPException:
            log.warning("無法回覆數數錯誤訊息 %s", message.id)

    def _error_reply_allowed(self, channel_id: int) -> bool:
        now = time.monotonic()
        if now - self._last_error_reply.get(channel_id, -math.inf) < (
            ERROR_REPLY_INTERVAL
        ):
            return False
        self._last_error_reply[channel_id] = now
        return True

    @staticmethod
    async def _celebrate(channel: discord.abc.Messageable, number: int) -> None:
        try:
            _ = await channel.send(
                (
                    f"恭喜！這個頻道數到了 **{number}**，"
                    f"第一次進入 {len(str(number))} 位數！"
                ),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException:
            log.warning("無法發送數數慶祝訊息：%s", number)

    def _is_counting_channel(self, guild_id: int | None, channel_id: int) -> bool:
        """In-memory check so unrelated message events skip the database."""
        return guild_id is not None and channel_id in self._channels.get(
            guild_id, set()
        )

    async def _announce_lost_count(
        self, guild_id: int, channel_id: int, message_ids: set[int], action: str
    ) -> None:
        """Tell the channel the next number when its latest count disappears."""
        async with self._lock(guild_id):
            state = await (await self._repository(guild_id)).state(channel_id)
        if state is None or state["last_success_id"] not in message_ids:
            return
        # Editing the same message back and forth must not spam the channel.
        if self._announced.get(channel_id) == state["last_success_id"]:
            return
        self._announced[channel_id] = state["last_success_id"]
        current = int(state["current_number"])
        channel = self.bot.get_channel(channel_id)
        if current == 0 or not isinstance(channel, discord.TextChannel):
            return
        try:
            _ = await channel.send(
                (
                    f"<@{state['last_user_id']}> {action}了剛數的 **{current}**，"
                    f"下一個是 **{current + 1}**。"
                ),
                allowed_mentions=NO_MENTIONS,
            )
        except discord.HTTPException:
            log.warning("無法提醒數數頻道 %s 的下一個數字", channel_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if self._is_counting_channel(payload.guild_id, payload.channel_id):
            assert payload.guild_id is not None
            await self._announce_lost_count(
                payload.guild_id, payload.channel_id, {payload.message_id}, "刪除"
            )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if self._is_counting_channel(payload.guild_id, payload.channel_id):
            assert payload.guild_id is not None
            await self._announce_lost_count(
                payload.guild_id, payload.channel_id, payload.message_ids, "刪除"
            )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if not self._is_counting_channel(payload.guild_id, payload.channel_id):
            return
        assert payload.guild_id is not None
        content = payload.message.content
        cached = payload.cached_message
        if cached is not None and cached.content == content:
            return  # Embed or pin updates, not a text edit.
        async with self._lock(payload.guild_id):
            repository = await self._repository(payload.guild_id)
            state = await repository.state(payload.channel_id)
        if state is None or parse_number(content) == int(state["current_number"]):
            return
        await self._announce_lost_count(
            payload.guild_id, payload.channel_id, {payload.message_id}, "編輯掉"
        )

    # --- channel housekeeping ----------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            if not guild.unavailable:
                await self.on_guild_available(guild)

    @commands.Cog.listener()
    async def on_guild_available(self, guild: discord.Guild) -> None:
        # Clean up channels that were deleted while the Bot was offline.
        async with self._lock(guild.id):
            _ = await self._repository(guild.id)
            for channel_id in tuple(self._channels[guild.id]):
                if guild.get_channel(channel_id) is None:
                    await self._remove_channel(guild.id, channel_id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        async with self._lock(channel.guild.id):
            await self._remove_channel(channel.guild.id, channel.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Count(bot))
