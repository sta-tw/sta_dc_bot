"""Parsing and formatting helpers for the counting channel."""

import re

MAX_DIGITS = 100

# A decimal number such as 12, 12.75, 12. or .75 (either side of the point may
# be omitted, but not both).
_NUMBER_PATTERN = (
    rf"(?:(?P<integer>[0-9]{{1,{MAX_DIGITS}}})(?:\.[0-9]{{0,{MAX_DIGITS}}})?"
    rf"|\.[0-9]{{1,{MAX_DIGITS}}})(?![0-9])"
)
_NUMBER = re.compile(_NUMBER_PATTERN)
_EXPONENT = re.compile(r"[eE][+-]?[0-9]")
# Characters that may start a formula; reserved for future parsing modes.
_OPERATORS = frozenset(".+-*/%()^&|<>=!")
_BASE_PREFIXES = frozenset("xXbBoO")


def parse_number(content: str) -> int | None:
    """Return the number a message starts with, or None if it is not a count.

    Decimals are accepted and rounded down (12.9 counts as 12). Formulas,
    exponents and base prefixes are still reserved and rejected rather than
    being read as their leading digits.
    """
    content = content.strip()
    match = _NUMBER.match(content)
    if match is None:
        return None
    # ".75" has no integer part, which rounds down to 0.
    integer = match.group("integer") or "0"
    rest = content[match.end() :]
    suffix = rest.lstrip()
    if suffix and suffix[0] in _OPERATORS:
        return None
    # Exponents and base prefixes only count when attached to the number.
    if _EXPONENT.match(rest) or (match.group() == "0" and rest[:1] in _BASE_PREFIXES):
        return None
    # The number is never negative, so dropping the fraction rounds down.
    return int(integer)


def format_units(units: int) -> str:
    """Format 1/100 units as a short decimal, e.g. 150 -> "1.5", 400 -> "4"."""
    return f"{units / 100:.2f}".rstrip("0").rstrip(".")
