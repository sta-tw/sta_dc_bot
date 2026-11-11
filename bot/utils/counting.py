"""Parsing and formatting helpers for the counting channel."""

import re

MAX_DIGITS = 100

_NUMBER = re.compile(rf"[0-9]{{1,{MAX_DIGITS}}}(?![0-9])")
_EXPONENT = re.compile(r"[eE][+-]?[0-9]")

_OPERATORS = frozenset(".+-*/%()^&|<>=!")
_BASE_PREFIXES = frozenset("xXbBoO")


def parse_number(content: str) -> int | None:
    """Return the number a message starts with, or None if it is not a plain count.

    Reserved syntax (decimals, formulas, exponents, base prefixes) is rejected
    instead of being truncated to its leading digits.
    """
    content = content.strip()
    match = _NUMBER.match(content)
    if match is None:
        return None
    digits = match.group()
    rest = content[match.end() :]
    suffix = rest.lstrip()
    if suffix and suffix[0] in _OPERATORS: return None

    if _EXPONENT.match(rest) or (digits == "0" and rest[:1] in _BASE_PREFIXES): return None
    return int(digits)


def format_units(units: int) -> str:
    """Format 1/100 units as a short decimal, e.g. 150 -> "1.5", 400 -> "4"."""
    return f"{units / 100:.2f}".rstrip("0").rstrip(".")
