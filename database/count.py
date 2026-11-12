import math
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, TypedDict, cast
from zoneinfo import ZoneInfo

import aiosqlite

TAIPEI = ZoneInfo("Asia/Taipei")
COOLDOWN = 12 * 3600
PERSONAL_LIMIT = 400
SERVER_LIMIT = 300
TOTEM_UNITS = 100
SACRIFICE_REWARD = 20
DAILY_CLAIMS = 2
WEEKDAY_REWARD = 50
WEEKEND_REWARD = 75
SATURDAY = 5

MISSING_SERVER_ROW = "缺少伺服器額度資料，請先呼叫 initialize()。"
COUNTERS: Final = ("successes", "errors", "protected", "revivals", "sacrifices")

type SQLValue = int | float | str | None
type Parameters = Sequence[SQLValue]


class ChannelState(TypedDict):
    channel_id: int
    current_number: str
    last_message_id: int
    grace_seconds: float
    last_success_id: int
    last_user_id: int | None
    best_number: str


class Wallet(TypedDict):
    user_id: int
    units: int
    last_claim: float | None
    claim_date: str | None
    claims_today: int


class UserStatistics(TypedDict):
    user_id: int
    successes: int
    errors: int
    protected: int
    revivals: int
    sacrifices: int
    last_active: float
    error_rate: float


type Outcome = Literal["success", "protected", "revived", "error"]
type ReviveSource = Literal["personal", "server"]


class InvalidGraceError(ValueError):
    """A grace period a member typed in; its message is safe to show them."""


@dataclass(frozen=True)
class CountResult:
    outcome: Outcome
    expected: int
    previous: int
    consecutive: bool = False
    # Set when a mistake was absorbed by a totem instead of resetting progress.
    revive_source: ReviveSource | None = None
    # A success that went past the channel's previous best number.
    new_record: bool = False


@dataclass(frozen=True)
class Adjustment:
    applied: int
    balance: int


def _units(units: int) -> str:
    """Format internal 1/100 units as a human-readable totem count."""
    return f"{units / 100:g}"


def _error_rate(stats: UserStatistics) -> float:
    total = stats["successes"] + stats["errors"]
    return stats["errors"] / total if total else 0.0


class CountRepository:
    """One guild per database; all currency and progress mutations are atomic."""

    path: Path
    read_only: bool

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only

    @asynccontextmanager
    async def _connect(self) -> AsyncGenerator[aiosqlite.Connection]:
        target = (
            f"{self.path.resolve().as_uri()}?mode=ro" if self.read_only else self.path
        )
        async with aiosqlite.connect(target, timeout=10, uri=self.read_only) as db:
            db.row_factory = aiosqlite.Row
            yield db

    @asynccontextmanager
    async def _transaction(self) -> AsyncGenerator[aiosqlite.Connection]:
        """Serialize read-modify-write operations and roll back on failure."""
        async with self._connect() as db:
            await self._execute(db, "BEGIN IMMEDIATE")
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @asynccontextmanager
    async def _snapshot(self) -> AsyncGenerator[aiosqlite.Connection]:
        """Run several reads against one consistent snapshot, then release it."""
        async with self._connect() as db:
            await self._execute(db, "BEGIN")
            try:
                yield db
            finally:
                await db.rollback()

    @staticmethod
    async def _execute(
        db: aiosqlite.Connection, query: str, parameters: Parameters = ()
    ) -> None:
        async with db.execute(query, parameters):
            pass

    @staticmethod
    async def _fetchone(
        db: aiosqlite.Connection, query: str, parameters: Parameters = ()
    ) -> dict[str, object] | None:
        async with db.execute(query, parameters) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    async def _fetchall(
        db: aiosqlite.Connection, query: str, parameters: Parameters = ()
    ) -> list[dict[str, object]]:
        async with db.execute(query, parameters) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    @classmethod
    async def _channel(
        cls, db: aiosqlite.Connection, channel_id: int
    ) -> ChannelState | None:
        row = await cls._fetchone(
            db, "SELECT * FROM counting_channels WHERE channel_id = ?", (channel_id,)
        )
        return cast(ChannelState | None, row)

    @classmethod
    async def _wallet(cls, db: aiosqlite.Connection, user_id: int) -> Wallet | None:
        row = await cls._fetchone(
            db, "SELECT * FROM counting_wallets WHERE user_id = ?", (user_id,)
        )
        return cast(Wallet | None, row)

    @classmethod
    async def _server_units(cls, db: aiosqlite.Connection) -> int:
        row = await cls._fetchone(
            db, "SELECT units FROM counting_server WHERE singleton = 1"
        )
        if row is None:
            raise RuntimeError(MISSING_SERVER_ROW)
        return cast(int, row["units"])

    @classmethod
    async def _record_action(
        cls,
        db: aiosqlite.Connection,
        user_id: int,
        now: float,
        action: str,
        personal_delta: int = 0,
        server_delta: int = 0,
    ) -> None:
        await cls._execute(
            db,
            """INSERT INTO counting_actions
                (user_id, occurred_at, action, personal_delta, server_delta)
                VALUES (?, ?, ?, ?, ?)""",
            (user_id, now, action, personal_delta, server_delta),
        )

    @staticmethod
    def validate_grace(seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise InvalidGraceError("免死時間必須為大於或等於 0 的有限秒數。")

    async def initialize(self) -> set[int]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            async with db.executescript(
                """
                CREATE TABLE IF NOT EXISTS counting_channels (
                    channel_id INTEGER PRIMARY KEY,
                    current_number TEXT NOT NULL DEFAULT '0',
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    grace_seconds REAL NOT NULL DEFAULT 0,
                    last_success_id INTEGER NOT NULL DEFAULT 0,
                    last_user_id INTEGER,
                    best_number TEXT NOT NULL DEFAULT '0'
                );
                -- Keep these CHECK bounds in sync with PERSONAL_LIMIT/SERVER_LIMIT.
                CREATE TABLE IF NOT EXISTS counting_wallets (
                    user_id INTEGER PRIMARY KEY,
                    units INTEGER NOT NULL DEFAULT 0 CHECK (units BETWEEN 0 AND 400),
                    last_claim REAL,
                    claim_date TEXT,
                    claims_today INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS counting_server (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    units INTEGER NOT NULL DEFAULT 0 CHECK (units BETWEEN 0 AND 300)
                );
                INSERT OR IGNORE INTO counting_server (singleton, units) VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS counting_events (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    occurred_at REAL NOT NULL,
                    outcome TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS counting_events_time
                    ON counting_events(occurred_at, user_id);
                CREATE TABLE IF NOT EXISTS counting_actions (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    occurred_at REAL NOT NULL,
                    action TEXT NOT NULL,
                    personal_delta INTEGER NOT NULL,
                    server_delta INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS counting_actions_time
                    ON counting_actions(occurred_at, user_id);
                CREATE TABLE IF NOT EXISTS counting_bans (
                    user_id INTEGER PRIMARY KEY,
                    banned_by INTEGER NOT NULL,
                    banned_at REAL NOT NULL
                );
                """
            ):
                pass
            await db.commit()
            rows = await self._fetchall(db, "SELECT channel_id FROM counting_channels")
        return {cast(int, row["channel_id"]) for row in rows}

    async def state(self, channel_id: int) -> ChannelState | None:
        async with self._connect() as db:
            return await self._channel(db, channel_id)

    async def configure(
        self, channel_id: int, number: int, message_id: int, grace_seconds: float = 0
    ) -> None:
        self.validate_grace(grace_seconds)
        async with self._transaction() as db:
            await self._execute(
                db,
                """INSERT INTO counting_channels
                    (channel_id, current_number, last_message_id,
                     grace_seconds, best_number)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE
                    SET grace_seconds = excluded.grace_seconds""",
                (channel_id, str(number), message_id, grace_seconds, str(number)),
            )

    async def settings(self, channel_id: int, grace_seconds: float) -> bool:
        self.validate_grace(grace_seconds)
        async with (
            self._transaction() as db,
            db.execute(
                "UPDATE counting_channels SET grace_seconds = ? WHERE channel_id = ?",
                (grace_seconds, channel_id),
            ) as cursor,
        ):
            return cursor.rowcount > 0

    async def remove(self, channel_id: int) -> None:
        async with self._transaction() as db:
            await self._execute(
                db, "DELETE FROM counting_channels WHERE channel_id = ?", (channel_id,)
            )

    async def accept(
        self,
        channel_id: int,
        number: int | None,
        message_id: int,
        user_id: int,
        now: float,
    ) -> CountResult | None:
        if number is None:
            return None
        async with self._transaction() as db:
            row = await self._channel(db, channel_id)
            if row is None or message_id <= row["last_message_id"]:
                return None
            if await self._fetchone(
                db, "SELECT 1 FROM counting_bans WHERE user_id = ?", (user_id,)
            ):
                return None
            current = int(row["current_number"])
            expected = current + 1
            if current > 0 and number == current and row["last_user_id"] == user_id:
                # A repeat of your own last count (double send) is ignored.
                return None
            last_success_id = row["last_success_id"]
            grace_seconds = row["grace_seconds"]
            # Snowflake timestamps measure send time, not handler/network delay.
            delta_ms = (message_id >> 22) - (last_success_id >> 22)
            protected = (
                grace_seconds > 0
                and current > 0
                and number == current
                and last_success_id > 0
                and 0 <= delta_ms <= grace_seconds * 1000
            )
            consecutive = current > 0 and row["last_user_id"] == user_id
            revive_source: ReviveSource | None = None
            new_record = False
            outcome: Outcome
            if number == expected and not consecutive:
                outcome = "success"
                new_record = number > int(row["best_number"])
                best = number if new_record else int(row["best_number"])
                await self._execute(
                    db,
                    """UPDATE counting_channels SET current_number = ?,
                        last_success_id = ?, last_user_id = ?, best_number = ?
                        WHERE channel_id = ?""",
                    (str(number), message_id, user_id, str(best), channel_id),
                )
            elif protected:
                outcome = "protected"
            elif current > 0 and (
                revive_source := await self._consume_revive(db, user_id, now)
            ):
                # Progress is kept as-is; only the totem or server quota is spent.
                outcome = "revived"
            else:
                outcome = "error"
                await self._execute(
                    db,
                    """UPDATE counting_channels SET current_number = '0',
                        last_success_id = 0 WHERE channel_id = ?""",
                    (channel_id,),
                )
            await self._execute(
                db,
                "UPDATE counting_channels SET last_message_id = ? WHERE channel_id = ?",
                (message_id, channel_id),
            )
            await self._execute(
                db,
                """INSERT INTO counting_events
                    (message_id, channel_id, user_id, occurred_at, outcome)
                    VALUES (?, ?, ?, ?, ?)""",
                (message_id, channel_id, user_id, now, outcome),
            )
            return CountResult(
                outcome,
                expected,
                current,
                consecutive and outcome in ("revived", "error"),
                revive_source,
                new_record,
            )

    @classmethod
    async def _consume_revive(
        cls, db: aiosqlite.Connection, user_id: int, now: float
    ) -> ReviveSource | None:
        """Spend a personal totem first, then server quota; None if neither."""
        wallet = await cls._wallet(db, user_id)
        if wallet is not None and wallet["units"] >= TOTEM_UNITS:
            await cls._execute(
                db,
                "UPDATE counting_wallets SET units = units - ? WHERE user_id = ?",
                (TOTEM_UNITS, user_id),
            )
            await cls._record_action(db, user_id, now, "revive", -TOTEM_UNITS, 0)
            return "personal"
        if await cls._server_units(db) >= TOTEM_UNITS:
            await cls._execute(
                db,
                "UPDATE counting_server SET units = units - ? WHERE singleton = 1",
                (TOTEM_UNITS,),
            )
            await cls._record_action(db, user_id, now, "revive", 0, -TOTEM_UNITS)
            return "server"
        return None

    async def wallet(self, user_id: int) -> tuple[Wallet, int]:
        # Both balances must describe the same database snapshot.
        async with self._snapshot() as db:
            row = await self._wallet(db, user_id)
            server_units = await self._server_units(db)
        wallet: Wallet = row or {
            "user_id": user_id,
            "units": 0,
            "last_claim": None,
            "claim_date": None,
            "claims_today": 0,
        }
        return wallet, server_units

    async def claim(self, user_id: int, now: float) -> str:
        date = datetime.fromtimestamp(now, TAIPEI)
        day = date.date().isoformat()
        reward = WEEKEND_REWARD if date.weekday() >= SATURDAY else WEEKDAY_REWARD
        async with self._transaction() as db:
            row = await self._wallet(db, user_id)
            units = row["units"] if row is not None else 0
            count = (
                row["claims_today"]
                if row is not None and row["claim_date"] == day
                else 0
            )
            last_claim = row["last_claim"] if row is not None else None
            if units >= PERSONAL_LIMIT:
                return (
                    f"已持有 {_units(PERSONAL_LIMIT)} 個不死圖騰，"
                    "請先使用或獻祭；此次不消耗領取次數。"
                )
            if count >= DAILY_CLAIMS:
                return f"今天已領取 {DAILY_CLAIMS} 次不死圖騰。"
            if last_claim is not None and now - last_claim < COOLDOWN:
                next_claim = int(last_claim + COOLDOWN)
                return (
                    f"尚未滿 {COOLDOWN // 3600} 小時，下次可領取：<t:{next_claim}:R>。"
                )
            added = min(reward, PERSONAL_LIMIT - units)
            await self._execute(
                db,
                """INSERT INTO counting_wallets
                    (user_id, units, last_claim, claim_date, claims_today)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET units = excluded.units,
                    last_claim = excluded.last_claim, claim_date = excluded.claim_date,
                    claims_today = excluded.claims_today""",
                (user_id, units + added, now, day, count + 1),
            )
            await self._record_action(db, user_id, now, "claim", added)
            return (
                f"領取成功！獲得 {_units(added)} 個不死圖騰，"
                f"目前持有 {_units(units + added)}／{_units(PERSONAL_LIMIT)} 個。"
            )

    async def sacrifice(self, user_id: int, now: float) -> str:
        async with self._transaction() as db:
            row = await self._wallet(db, user_id)
            if row is None or row["units"] < TOTEM_UNITS:
                return "至少需要 1 個完整的不死圖騰才能獻祭。"
            server_units = await self._server_units(db)
            if server_units + SACRIFICE_REWARD > SERVER_LIMIT:
                return (
                    f"伺服器最多持有 {_units(SERVER_LIMIT)} 次復活額度，"
                    f"目前無法完整增加 {_units(SACRIFICE_REWARD)} 次；未扣除圖騰。"
                )
            await self._execute(
                db,
                "UPDATE counting_wallets SET units = units - ? WHERE user_id = ?",
                (TOTEM_UNITS, user_id),
            )
            await self._execute(
                db,
                "UPDATE counting_server SET units = units + ? WHERE singleton = 1",
                (SACRIFICE_REWARD,),
            )
            await self._record_action(
                db, user_id, now, "sacrifice", -TOTEM_UNITS, SACRIFICE_REWARD
            )
            return (
                "已獻祭 1 個不死圖騰！伺服器復活額度："
                f"{_units(server_units + SACRIFICE_REWARD)}／{_units(SERVER_LIMIT)} 次。"
            )

    async def adjust_wallet(self, user_id: int, units: int, now: float) -> Adjustment:
        """Add or remove totem units, clamped to the personal limit."""
        async with self._transaction() as db:
            row = await self._wallet(db, user_id)
            before = row["units"] if row is not None else 0
            after = min(max(before + units, 0), PERSONAL_LIMIT)
            await self._execute(
                db,
                """INSERT INTO counting_wallets (user_id, units) VALUES (?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET units = excluded.units""",
                (user_id, after),
            )
            if after != before:
                await self._record_action(db, user_id, now, "adjust", after - before)
            return Adjustment(after - before, after)

    async def adjust_server(self, actor_id: int, units: int, now: float) -> Adjustment:
        """Add or remove server revive units, clamped to the server limit."""
        async with self._transaction() as db:
            before = await self._server_units(db)
            after = min(max(before + units, 0), SERVER_LIMIT)
            await self._execute(
                db, "UPDATE counting_server SET units = ? WHERE singleton = 1", (after,)
            )
            if after != before:
                await self._record_action(
                    db, actor_id, now, "adjust_server", 0, after - before
                )
            return Adjustment(after - before, after)

    async def ban(self, user_id: int, banned_by: int, now: float) -> bool:
        """Return False if the member was already banned."""
        async with (
            self._transaction() as db,
            db.execute(
                """INSERT OR IGNORE INTO counting_bans (user_id, banned_by, banned_at)
                    VALUES (?, ?, ?)""",
                (user_id, banned_by, now),
            ) as cursor,
        ):
            return cursor.rowcount > 0

    async def unban(self, user_id: int) -> bool:
        """Return False if the member was not banned."""
        async with (
            self._transaction() as db,
            db.execute(
                "DELETE FROM counting_bans WHERE user_id = ?", (user_id,)
            ) as cursor,
        ):
            return cursor.rowcount > 0

    async def bans(self) -> list[int]:
        async with self._connect() as db:
            rows = await self._fetchall(
                db, "SELECT user_id FROM counting_bans ORDER BY banned_at"
            )
        return [cast(int, row["user_id"]) for row in rows]

    async def has_counting_data(self) -> bool:
        """Guild databases are shared with other features; skip ones never set up."""
        async with self._connect() as db:
            row = await self._fetchone(
                db,
                """SELECT COUNT(*) AS tables FROM sqlite_master WHERE type = 'table'
                    AND name IN ('counting_events', 'counting_actions')""",
            )
        return row is not None and cast(int, row["tables"]) == 2

    async def statistics(
        self, since: float = 0, user_id: int | None = None
    ) -> list[UserStatistics]:
        parameters = (since, user_id, user_id)
        async with self._connect() as db:
            # One query keeps event and action totals in the same snapshot.
            rows = await self._fetchall(
                db,
                """WITH events AS (
                    SELECT user_id, SUM(outcome = 'success') AS successes,
                        SUM(outcome IN ('error', 'revived')) AS errors,
                        SUM(outcome = 'protected') AS protected,
                        MAX(occurred_at) AS last_active
                    FROM counting_events
                    WHERE occurred_at >= ? AND (? IS NULL OR user_id = ?)
                    GROUP BY user_id
                ), actions AS (
                    SELECT user_id, SUM(action = 'revive') AS revivals,
                        SUM(action = 'sacrifice') AS sacrifices
                    FROM counting_actions
                    WHERE occurred_at >= ? AND (? IS NULL OR user_id = ?)
                    GROUP BY user_id
                )
                SELECT events.*, COALESCE(revivals, 0) AS revivals,
                    COALESCE(sacrifices, 0) AS sacrifices, 0.0 AS error_rate
                FROM events LEFT JOIN actions USING (user_id)""",
                parameters + parameters,
            )
        result = cast(list[UserStatistics], rows)
        for stats in result:
            stats["error_rate"] = _error_rate(stats)
        return result

    @classmethod
    async def global_statistics(
        cls, paths: Iterable[Path], since: float = 0, user_id: int | None = None
    ) -> list[UserStatistics]:
        merged: dict[int, UserStatistics] = {}
        for path in paths:
            repository = cls(path, read_only=True)
            if not await repository.has_counting_data():
                continue
            for row in await repository.statistics(since, user_id):
                target = merged.setdefault(
                    row["user_id"],
                    {
                        "user_id": row["user_id"],
                        "successes": 0,
                        "errors": 0,
                        "protected": 0,
                        "revivals": 0,
                        "sacrifices": 0,
                        "last_active": 0.0,
                        "error_rate": 0.0,
                    },
                )
                for key in COUNTERS:
                    target[key] += row[key]
                target["last_active"] = max(target["last_active"], row["last_active"])
        for stats in merged.values():
            stats["error_rate"] = _error_rate(stats)
        return list(merged.values())
