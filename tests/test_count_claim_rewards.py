import sqlite3
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest

import database.count as count_store
from database.count import COOLDOWN, PERSONAL_LIMIT, CountRepository

MONDAY = 1_700_409_600.0


@pytest.mark.parametrize(
    ("bucket", "reward", "bounds"),
    [
        (0, 20, (20, 80)),
        (0, 80, (20, 80)),
        (89, 20, (20, 80)),
        (89, 80, (20, 80)),
        (90, 100, (100, 200)),
        (90, 200, (100, 200)),
        (97, 100, (100, 200)),
        (97, 200, (100, 200)),
        (98, PERSONAL_LIMIT, None),
        (99, PERSONAL_LIMIT, None),
    ],
)
def test_claim_reward_bucket_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    bucket: int,
    reward: int,
    bounds: tuple[int, int] | None,
) -> None:
    draw = Mock(return_value=bucket)
    amount = Mock(return_value=reward)
    monkeypatch.setattr(count_store.random, "randrange", draw)
    monkeypatch.setattr(count_store.random, "randint", amount)

    assert count_store.roll_claim_reward() == reward

    draw.assert_called_once_with(100)
    if bounds is None:
        amount.assert_not_called()
    else:
        amount.assert_called_once_with(*bounds)


def test_claim_reward_probabilities_cover_every_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(count_store.random, "randrange", Mock(side_effect=range(100)))
    monkeypatch.setattr(count_store.random, "randint", lambda low, high: low)

    rewards = Counter(count_store.roll_claim_reward() for _ in range(100))

    assert rewards == {20: 90, 100: 8, PERSONAL_LIMIT: 2}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("balance", "reward", "added"),
    [
        (0, 20, 20),
        (0, 80, 80),
        (0, 100, 100),
        (0, 200, 200),
        (0, PERSONAL_LIMIT, PERSONAL_LIMIT),
        (350, 80, 50),
        (350, 200, 50),
        (350, PERSONAL_LIMIT, 50),
        (399, PERSONAL_LIMIT, 1),
    ],
)
async def test_claim_applies_actual_reward_and_records_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    balance: int,
    reward: int,
    added: int,
) -> None:
    path = tmp_path / "count.db"
    repository = CountRepository(path)
    await repository.initialize()
    if balance:
        await repository.adjust_wallet(1, balance, MONDAY - 1)
    draw = Mock(return_value=reward)
    monkeypatch.setattr(count_store, "roll_claim_reward", draw)

    text = await repository.claim(1, MONDAY)
    wallet, server_units = await repository.wallet(1)

    assert "領取成功" in text
    assert f"獲得 {added / 100:g} 個不死圖騰" in text
    assert ("幸運補滿" in text) == (reward == PERSONAL_LIMIT)
    assert wallet["units"] == balance + added
    assert wallet["last_claim"] == MONDAY
    assert wallet["claim_date"] == "2023-11-20"
    assert wallet["claims_today"] == 1
    assert server_units == 0
    draw.assert_called_once_with()
    with sqlite3.connect(path) as db:
        actions = db.execute(
            "SELECT personal_delta, server_delta FROM counting_actions WHERE action='claim'"
        ).fetchall()
    assert actions == [(added, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("balance", "claimed", "last_claim", "message"),
    [
        (PERSONAL_LIMIT, 0, None, "已持有"),
        (20, 1, MONDAY - 3600, "尚未滿"),
        (40, 2, MONDAY - COOLDOWN, "今天已領取"),
    ],
)
async def test_rejected_claim_does_not_draw_or_change_balance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    balance: int,
    claimed: int,
    last_claim: float | None,
    message: str,
) -> None:
    path = tmp_path / "count.db"
    repository = CountRepository(path)
    await repository.initialize()
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO counting_wallets
               (user_id, units, last_claim, claim_date, claims_today)
               VALUES (1, ?, ?, '2023-11-20', ?)""",
            (balance, last_claim, claimed),
        )
    before = await repository.wallet(1)
    draw = Mock(return_value=PERSONAL_LIMIT)
    monkeypatch.setattr(count_store, "roll_claim_reward", draw)

    assert message in await repository.claim(1, MONDAY)

    assert await repository.wallet(1) == before
    draw.assert_not_called()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM counting_actions").fetchone() == (0,)


@pytest.mark.asyncio
async def test_claim_draws_again_after_cooldown_and_date_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = CountRepository(tmp_path / "count.db")
    await repository.initialize()
    draw = Mock(side_effect=[20, 150, PERSONAL_LIMIT])
    monkeypatch.setattr(count_store, "roll_claim_reward", draw)

    for offset in (0, 13 * 3600, 26 * 3600):
        assert "領取成功" in await repository.claim(1, MONDAY + offset)

    wallet, _ = await repository.wallet(1)
    assert wallet["units"] == PERSONAL_LIMIT
    assert wallet["claims_today"] == 1
    assert wallet["claim_date"] == "2023-11-21"
    assert draw.call_count == 3
