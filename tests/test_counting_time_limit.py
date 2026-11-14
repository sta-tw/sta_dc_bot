# Time limit tests: one hostile expression must not keep the evaluator busy for minutes.
import time

import pytest

from bot.utils import counting
from bot.utils.counting import ExpressionError, evaluate


def test_slow_sum_of_factorizations_is_abandoned_at_the_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Breaks if factorization loops never check the clock. Ten factorizations of a
    # prime near 10**12 take about 0.4 s, so a 50 ms limit must cut them short.
    monkeypatch.setattr(counting, "EVALUATION_TIME_LIMIT", 0.05, raising=False)
    started = time.monotonic()
    with pytest.raises(ExpressionError):
        evaluate("∑ₖ₌₁¹⁰ totient(999999999989)")
    assert time.monotonic() - started < 0.3


def test_one_slow_factorization_is_abandoned_at_the_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Breaks if a single built-in call cannot be interrupted. No sum or integral is
    # involved here, so only the factorization loop can notice the clock.
    monkeypatch.setattr(counting, "EVALUATION_TIME_LIMIT", 0.001, raising=False)
    with pytest.raises(ExpressionError):
        evaluate("totient(999999999989)")


def test_long_arithmetic_sum_is_abandoned_at_the_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Breaks if loop bodies (sums, integrals, limits, derivatives) skip the clock check.
    monkeypatch.setattr(counting, "EVALUATION_TIME_LIMIT", 0.001, raising=False)
    with pytest.raises(ExpressionError):
        evaluate("∑ₖ₌₁¹⁰⁰⁰⁰ k²")


def test_ordinary_long_sum_finishes_under_the_default_limit() -> None:
    # Breaks if the default limit is so tight that legitimate long sums are rejected.
    assert evaluate("∑ₖ₌₁¹⁰⁰⁰⁰ k²") == 333383335000
