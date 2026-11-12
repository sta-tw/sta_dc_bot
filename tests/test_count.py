import sqlite3
from pathlib import Path
from typing import final

import pytest
import pytest_asyncio

from bot.cogs.count import (
    HELP,
    CountUsageError,
    build_query,
    is_new_magnitude,
    parse_units,
    read_count,
)
from bot.utils.counting import format_units, parse_number
from database.count import (
    PERSONAL_LIMIT,
    SACRIFICE_REWARD,
    SERVER_LIMIT,
    TOTEM_UNITS,
    CountRepository,
    CountResult,
)

CHANNEL = 1
# Monday 00:00 in Asia/Taipei: two claims 13 hours apart stay on the same weekday.
MONDAY = 1_700_409_600.0


def snowflake(milliseconds: int) -> int:
    """Build a message ID whose embedded timestamp is the given offset."""
    return milliseconds << 22


@final
class Counter:
    """Feeds messages with increasing IDs, one second apart by default."""

    def __init__(self, repo: CountRepository) -> None:
        self.repo: CountRepository = repo
        self.last_ms: int = 0

    async def send(
        self, number: int | None, user: int, *, gap_ms: int = 1_000
    ) -> CountResult | None:
        """Send a message; None means the repository ignored it."""
        self.last_ms += gap_ms
        return await self.repo.accept(
            CHANNEL, number, snowflake(self.last_ms), user, MONDAY
        )

    async def say(self, number: int, user: int, *, gap_ms: int = 1_000) -> CountResult:
        """Send a message that must be judged."""
        result = await self.send(number, user, gap_ms=gap_ms)
        assert result is not None
        return result

    async def current(self) -> int:
        state = await self.repo.state(CHANNEL)
        assert state is not None
        return int(state["current_number"])


@pytest_asyncio.fixture
async def repo(tmp_path: Path) -> CountRepository:
    repository = CountRepository(tmp_path / "1.db")
    assert await repository.initialize() == set()
    await repository.configure(CHANNEL, 0, snowflake(0))
    return repository


async def give_totems(repo: CountRepository, user: int, claims: int) -> None:
    for index in range(claims):
        _ = await repo.claim(user, MONDAY + index * 13 * 3600)


# --- parsing helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("1", 1),
        (" 42 ", 42),
        ("007", 7),
        ("12 換你了", 12),
        ("5 + 3", None),
        ("1.5", 1),
        ("12.9 換你了", 12),
        ("0.9", 0),
        ("1.", 1),
        ("12. 換你了", 12),
        ("1.5.3", None),
        ("1.5e3", None),
        ("1.5+2", None),
        (".5", 0),
        (".", None),
        ("1..5", None),
        ("1.e3", None),
        ("5e3", None),
        ("0x10", None),
        ("-5", None),
        ("我來數 12", None),
        ("", None),
        ("9" * 100, int("9" * 100)),
        ("9" * 101, None),
    ],
)
def test_parse_number(content: str, expected: int | None) -> None:
    assert parse_number(content) == expected


@pytest.mark.parametrize(
    ("units", "text"), [(0, "0"), (20, "0.2"), (75, "0.75"), (150, "1.5"), (400, "4")]
)
def test_format_units(units: int, text: str) -> None:
    assert format_units(units) == text


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("12 換你了", 12),
        ("a 1", 0),
        ("", 0),
        ("0", 0),
        ("1.5", 1),
        ("12.9", 12),
        ("007", 7),
    ],
)
def test_read_count_shows_zero_when_nothing_counts(content: str, expected: int) -> None:
    assert read_count(content) == expected


def test_help_fits_in_one_discord_message() -> None:
    assert len(HELP) <= 2000


# --- command helpers ---------------------------------------------------------


def test_build_query_maps_options() -> None:
    query = build_query("global", 123, "30d", 9, personal=False, page=2, sort="errors")
    assert (query.is_global, query.user_id, query.days, query.page, query.sort) == (
        True,
        123,
        30,
        2,
        "errors",
    )
    assert query.label == "全域｜<@123>｜近 30 天"


def test_build_query_defaults_to_self_only_for_personal_stats() -> None:
    assert build_query(None, None, None, 9, personal=True).user_id == 9
    assert build_query(None, None, None, 9, personal=False).user_id is None
    assert build_query("server", None, None, 9, personal=True).user_id is None
    assert build_query(None, None, None, 9, personal=True).label == (
        "此伺服器｜<@9>｜全部期間"
    )


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (1, False),
        (9, False),
        (10, True),
        (11, False),
        (100, True),
        (1000, True),
        (1001, False),
        (2000, False),
        (10**99, True),
    ],
)
def test_is_new_magnitude(number: int, expected: bool) -> None:
    assert is_new_magnitude(number) is expected


@pytest.mark.parametrize(
    ("amount", "units"), [(1, 100), (0.5, 50), (-1, -100), (0.2, 20), (2.25, 225)]
)
def test_parse_units(amount: float, units: int) -> None:
    assert parse_units(amount) == units


@pytest.mark.parametrize("amount", [0, 0.001, 1.234, float("inf"), float("nan")])
def test_parse_units_rejects_invalid_amounts(amount: float) -> None:
    with pytest.raises(CountUsageError):
        _ = parse_units(amount)


# --- counting ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_correct_counts_advance_progress(repo: CountRepository) -> None:
    counter = Counter(repo)
    assert (await counter.say(1, 1)).outcome == "success"
    assert (await counter.say(2, 2)).outcome == "success"
    assert await counter.current() == 2


@pytest.mark.asyncio
async def test_wrong_number_without_totems_resets(repo: CountRepository) -> None:
    counter = Counter(repo)
    _ = await counter.say(1, 1)
    result = await counter.say(5, 2)
    assert (result.outcome, result.expected, result.previous) == ("error", 2, 1)
    assert result.revive_source is None
    assert await counter.current() == 0


@pytest.mark.asyncio
async def test_consecutive_count_is_an_error(repo: CountRepository) -> None:
    counter = Counter(repo)
    _ = await counter.say(1, 1)
    result = await counter.say(2, 1)
    assert (result.outcome, result.consecutive) == ("error", True)


@pytest.mark.asyncio
async def test_repeating_your_own_last_count_is_ignored(repo: CountRepository) -> None:
    counter = Counter(repo)
    _ = await counter.say(1, 1)
    assert await counter.send(1, 1) is None
    assert await counter.current() == 1
    assert (await counter.say(2, 2)).outcome == "success"


@pytest.mark.asyncio
async def test_error_at_zero_does_not_spend_totems(repo: CountRepository) -> None:
    await give_totems(repo, 1, 2)
    result = await Counter(repo).say(5, 1)
    assert (result.outcome, result.revive_source) == ("error", None)
    wallet, _ = await repo.wallet(1)
    assert wallet["units"] == TOTEM_UNITS


@pytest.mark.asyncio
async def test_personal_totem_revives_before_server_quota(
    repo: CountRepository,
) -> None:
    await give_totems(repo, 3, 2)
    counter = Counter(repo)
    _ = await counter.say(1, 1)
    _ = await counter.say(2, 2)
    result = await counter.say(9, 3)
    assert (result.outcome, result.revive_source) == ("revived", "personal")
    assert await counter.current() == 2
    wallet, _ = await repo.wallet(3)
    assert wallet["units"] == 0
    # Progress continues from the kept number.
    assert (await counter.say(3, 3)).outcome == "success"


@pytest.mark.asyncio
async def test_server_quota_revives_when_personal_totems_run_out(
    repo: CountRepository,
) -> None:
    # Five sacrifices give the server exactly one revive.
    for user in range(10, 15):
        await give_totems(repo, user, 2)
        _ = await repo.sacrifice(user, MONDAY)
    _, server_units = await repo.wallet(0)
    assert server_units == 5 * SACRIFICE_REWARD == TOTEM_UNITS

    counter = Counter(repo)
    _ = await counter.say(1, 1)
    result = await counter.say(9, 2)
    assert (result.outcome, result.revive_source) == ("revived", "server")
    _, server_units = await repo.wallet(0)
    assert server_units == 0
    assert (await counter.say(9, 2)).outcome == "error"


@pytest.mark.asyncio
async def test_grace_period_protects_same_number_from_others(
    repo: CountRepository,
) -> None:
    await repo.configure(CHANNEL, 0, snowflake(0), grace_seconds=2)
    counter = Counter(repo)
    _ = await counter.say(1, 1)
    assert (await counter.say(1, 2, gap_ms=500)).outcome == "protected"
    assert (await counter.say(1, 3, gap_ms=5_000)).outcome == "error"


@pytest.mark.asyncio
async def test_old_or_unknown_messages_are_ignored(repo: CountRepository) -> None:
    counter = Counter(repo)
    _ = await counter.say(1, 1)
    assert await repo.accept(CHANNEL, 2, snowflake(1), 2, MONDAY) is None
    assert await repo.accept(999, 1, snowflake(99_000), 2, MONDAY) is None
    assert await repo.accept(CHANNEL, None, snowflake(99_000), 2, MONDAY) is None


# --- totems ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_limits(repo: CountRepository) -> None:
    assert "領取成功" in await repo.claim(1, MONDAY)
    assert "尚未滿" in await repo.claim(1, MONDAY + 3600)
    assert "領取成功" in await repo.claim(1, MONDAY + 13 * 3600)
    wallet, _ = await repo.wallet(1)
    assert (wallet["units"], wallet["claims_today"]) == (TOTEM_UNITS, 2)


@pytest.mark.asyncio
async def test_claim_stops_at_personal_limit(repo: CountRepository) -> None:
    await give_totems(repo, 1, 20)
    wallet, _ = await repo.wallet(1)
    assert wallet["units"] == PERSONAL_LIMIT
    assert "已持有" in await repo.claim(1, MONDAY + 30 * 13 * 3600)


@pytest.mark.asyncio
async def test_sacrifice_needs_a_whole_totem(repo: CountRepository) -> None:
    _ = await repo.claim(1, MONDAY)
    assert "至少需要" in await repo.sacrifice(1, MONDAY)


@pytest.mark.asyncio
async def test_adjust_wallet_clamps_to_limits(repo: CountRepository) -> None:
    added = await repo.adjust_wallet(1, 150, MONDAY)
    assert (added.applied, added.balance) == (150, 150)
    capped = await repo.adjust_wallet(1, 1_000, MONDAY)
    assert (capped.applied, capped.balance) == (PERSONAL_LIMIT - 150, PERSONAL_LIMIT)
    floored = await repo.adjust_wallet(1, -10_000, MONDAY)
    assert (floored.applied, floored.balance) == (-PERSONAL_LIMIT, 0)


@pytest.mark.asyncio
async def test_adjust_server_clamps_to_limits(repo: CountRepository) -> None:
    added = await repo.adjust_server(99, 100, MONDAY)
    assert (added.applied, added.balance) == (100, 100)
    capped = await repo.adjust_server(99, 10_000, MONDAY)
    assert capped.balance == SERVER_LIMIT
    _, server_units = await repo.wallet(0)
    assert server_units == SERVER_LIMIT


@pytest.mark.asyncio
async def test_server_adjustment_enables_revive(repo: CountRepository) -> None:
    _ = await repo.adjust_server(99, TOTEM_UNITS, MONDAY)
    counter = Counter(repo)
    _ = await counter.say(1, 1)
    assert (await counter.say(9, 2)).revive_source == "server"


# --- bans --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banned_members_are_ignored(repo: CountRepository) -> None:
    assert await repo.ban(2, 99, MONDAY) is True
    assert await repo.ban(2, 99, MONDAY) is False
    assert await repo.bans() == [2]
    counter = Counter(repo)
    _ = await counter.say(1, 1)
    assert await counter.send(9, 2) is None
    assert await counter.current() == 1
    assert await repo.unban(2) is True
    assert await repo.unban(2) is False
    assert (await counter.say(2, 2)).outcome == "success"


# --- records -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_record_only_when_passing_the_best(repo: CountRepository) -> None:
    counter = Counter(repo)
    assert (await counter.say(1, 1)).new_record is True
    assert (await counter.say(2, 2)).new_record is True
    _ = await counter.say(9, 1)  # resets to 0
    assert (await counter.say(1, 2)).new_record is False
    assert (await counter.say(2, 1)).new_record is False
    assert (await counter.say(3, 2)).new_record is True


# --- statistics --------------------------------------------------------------


@pytest.mark.asyncio
async def test_statistics_count_revived_mistakes_as_errors(
    repo: CountRepository,
) -> None:
    await give_totems(repo, 2, 2)
    counter = Counter(repo)
    _ = await counter.say(1, 1)
    _ = await counter.say(9, 2)
    [stats] = await repo.statistics(0, 2)
    assert (stats["errors"], stats["revivals"], stats["error_rate"]) == (1, 1, 1.0)


@pytest.mark.asyncio
async def test_global_statistics_skip_databases_without_counting(
    repo: CountRepository, tmp_path: Path
) -> None:
    other = tmp_path / "2.db"
    with sqlite3.connect(other) as db:
        _ = db.execute("CREATE TABLE unrelated (value)")
    _ = await Counter(repo).say(1, 7)
    rows = await CountRepository.global_statistics([other, repo.path])
    assert [(row["user_id"], row["successes"]) for row in rows] == [(7, 1)]
