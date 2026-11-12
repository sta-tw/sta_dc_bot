"""Parsing and formatting helpers for the counting channel.

A message counts as the value of the math expression it starts with, rounded
toward zero: "12", "6*2", "√144", "2^3+4", "１２" and "12.9 換你了" all count
as 12. Anything after the expression is ignored as long as it is separated by
a space or is not math (e.g. "12 換你了", "12換你了").

The evaluator is a small hand-written parser; user text is never passed to
eval(). Sizes are capped so hostile input cannot exhaust CPU or memory.
"""

import cmath
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal

type Number = Fraction | float | complex
type TokenKind = Literal["num", "name", "op", "open", "close", "comma", "root", "end"]

# A literal or result may have at most this many digits before the point.
MAX_DIGITS: Final = 100
MAX_TOKENS: Final = 256
MAX_DEPTH: Final = 64
# Exact intermediate values are capped at about 6000 digits.
MAX_BITS: Final = 20_000
MAX_EXPONENT_LITERAL: Final = 1_000
MAX_FACTORIAL: Final = 1_000
# Floating point noise below this relative size is ignored (2.9999999999 -> 3).
EPSILON: Final = 1e-9

_SUPERSCRIPTS: Final = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁽⁾"
_SUPERSCRIPT_RUN: Final = re.compile(f"[{_SUPERSCRIPTS}]+")
_SUPERSCRIPT_TABLE: Final = str.maketrans(_SUPERSCRIPTS, "0123456789+-()")
# Symbols that NFKC normalization leaves alone.
_SYMBOL_TABLE: Final = str.maketrans(
    {
        "×": "*",
        "✕": "*",
        "✖": "*",
        "⋅": "*",
        "·": "*",
        "∙": "*",
        "÷": "/",
        "∕": "/",
        "−": "-",
        "⁄": "/",
    }
)
# Full-width punctuation is ordinary Chinese text, not math: "12！" is excitement,
# not a factorial, and "12，換你" is a sentence. Hide it from NFKC.
_TEXT_PUNCTUATION: Final = str.maketrans({"！": "\ue000", "，": "\ue001"})

# 0x1F, 0b101, 0o17, or a decimal such as 12, 1_000, 12.5, 12., .5 and 1.5e3.
_NUMBER_PATTERN: Final = (
    r"0[xX](?:_?[0-9a-fA-F])+|0[bB](?:_?[01])+|0[oO](?:_?[0-7])+"
    r"|(?P<mantissa>(?P<integer>[0-9](?:_?[0-9])*)"
    r"(?:\.(?P<fraction>(?:[0-9](?:_?[0-9])*)?))?"
    r"|\.(?P<bare_fraction>[0-9](?:_?[0-9])*))"
    r"(?:[eE](?P<exponent>[+-]?[0-9]+))?"
)
_NUMBER: Final = re.compile(_NUMBER_PATTERN)
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GREEK: Final = {"π": "pi", "τ": "tau", "φ": "phi", "ϕ": "phi"}
_ROOTS: Final = {"√": "sqrt", "∛": "cbrt"}
_OPERATORS: Final = ("**", "//", "+", "-", "*", "/", "%", "^", "!")
# Tokens that may not be glued to the end of an expression ("1..5", "1)", "1,000").
_GLUED_ERRORS: Final = frozenset({"num", "close", "comma"})

# Binding powers: higher binds tighter. Power is right-associative and binds
# tighter than a leading minus, so -2^2 is -4 as in standard notation.
_ADDITIVE: Final = 10
_MULTIPLICATIVE: Final = 20
_UNARY: Final = 30
_POWER: Final = 40
_ROOT: Final = 45
_POSTFIX: Final = 50
_BINARY: Final[dict[str, tuple[int, int]]] = {
    "+": (_ADDITIVE, _ADDITIVE + 1),
    "-": (_ADDITIVE, _ADDITIVE + 1),
    "*": (_MULTIPLICATIVE, _MULTIPLICATIVE + 1),
    "/": (_MULTIPLICATIVE, _MULTIPLICATIVE + 1),
    "//": (_MULTIPLICATIVE, _MULTIPLICATIVE + 1),
    "%": (_MULTIPLICATIVE, _MULTIPLICATIVE + 1),
    "^": (_POWER, _POWER),
    "**": (_POWER, _POWER),
}


class ExpressionError(Exception):
    """The text is not a valid expression, or evaluating it is not allowed."""


class _SyntaxError(ExpressionError):
    """The tokens do not form an expression (as opposed to a math failure)."""


# --- number helpers ------------------------------------------------------------


def _checked(value: Number) -> Number:
    if isinstance(value, Fraction):
        size = max(value.numerator.bit_length(), value.denominator.bit_length())
        if size > MAX_BITS:
            raise ExpressionError("number too large")
    elif isinstance(value, complex):
        if not (math.isfinite(value.real) and math.isfinite(value.imag)):
            raise ExpressionError("number too large")
    elif not math.isfinite(value):
        raise ExpressionError("number too large")
    return value


def _real(value: Number) -> Fraction | float:
    if isinstance(value, complex):
        if value.imag:
            raise ExpressionError("real number required")
        return value.real
    return value


def _integer(value: Number) -> int:
    real = _real(value)
    if isinstance(real, Fraction):
        if real.denominator != 1:
            raise ExpressionError("integer required")
        return real.numerator
    if not real.is_integer():
        raise ExpressionError("integer required")
    return int(real)


def _math_arg(value: Fraction | float) -> int | float:
    """Whole numbers stay ints so math.log() works beyond the float range."""
    if isinstance(value, Fraction) and value.denominator == 1:
        return value.numerator
    return float(value)


def _integer_root(number: int, degree: int) -> int:
    """Floor of the degree-th root of a non-negative integer (Newton's method)."""
    if number < 2:
        return number
    guess = 1 << -(-number.bit_length() // degree)
    while True:
        # math.prod keeps the int type; int ** int is typed as Any.
        divisor = math.prod([guess] * (degree - 1))
        better = ((degree - 1) * guess + number // divisor) // degree
        if better >= guess:
            return guess
        guess = better


def _exact_root(value: Fraction, degree: int) -> Fraction | None:
    """The root of a non-negative fraction if it is itself a fraction."""
    numerator = _integer_root(value.numerator, degree)
    denominator = _integer_root(value.denominator, degree)
    if numerator**degree == value.numerator and denominator**degree == (
        value.denominator
    ):
        return Fraction(numerator, denominator)
    return None


def _power(base: Number, exponent: Number) -> Number:
    if (
        isinstance(base, Fraction)
        and isinstance(exponent, Fraction)
        and exponent.denominator == 1
    ):
        power = exponent.numerator
        if abs(base) not in (0, 1):
            size = max(base.numerator.bit_length(), base.denominator.bit_length())
            if abs(power) * size > MAX_BITS:
                raise ExpressionError("number too large")
        return base**power
    result: Number = base**exponent
    return result


def _sqrt(value: Number) -> Number:
    if isinstance(value, complex):
        return cmath.sqrt(value)
    if value < 0:
        magnitude = _sqrt(-value)
        return complex(0, float(_real(magnitude)))
    if isinstance(value, Fraction):
        root = _exact_root(value, 2)
        return root if root is not None else math.sqrt(_math_arg(value))
    return math.sqrt(value)


def _cbrt(value: Number) -> Number:
    if isinstance(value, complex):
        result: Number = value ** (1 / 3)
        return result
    if isinstance(value, Fraction):
        root = _exact_root(abs(value), 3)
        if root is not None:
            return root if value >= 0 else -root
    return math.cbrt(float(value))


def _factorial(value: Number) -> Number:
    number = _integer(value)
    if not 0 <= number <= MAX_FACTORIAL:
        raise ExpressionError("factorial out of range")
    return Fraction(math.factorial(number))


def _round_half_up(value: Number) -> Number:
    return Fraction(math.floor(_real(value) + Fraction(1, 2)))


def _analytic(
    real: Callable[[float], float], complex_: Callable[[complex], complex]
) -> Callable[[Number], Number]:
    """Use the real function, falling back to complex outside its domain."""

    def apply(value: Number) -> Number:
        if not isinstance(value, complex):
            try:
                return real(_math_arg(value))
            except ValueError:
                value = complex(value)
        return complex_(value)

    return apply


def _log(value: Number, base: Number | None = None) -> Number:
    if base is None:
        return _analytic(math.log10, cmath.log10)(value)
    numerator = _analytic(math.log, cmath.log)(value)
    denominator = _analytic(math.log, cmath.log)(base)
    return numerator / denominator


_UNARY_FUNCTIONS: Final[dict[str, Callable[[Number], Number]]] = {
    "sqrt": _sqrt,
    "cbrt": _cbrt,
    "abs": lambda value: abs(value),
    "exp": _analytic(math.exp, cmath.exp),
    "ln": _analytic(math.log, cmath.log),
    "log10": _analytic(math.log10, cmath.log10),
    "log2": _analytic(math.log2, lambda value: cmath.log(value, 2)),
    "sin": _analytic(math.sin, cmath.sin),
    "cos": _analytic(math.cos, cmath.cos),
    "tan": _analytic(math.tan, cmath.tan),
    "asin": _analytic(math.asin, cmath.asin),
    "acos": _analytic(math.acos, cmath.acos),
    "atan": _analytic(math.atan, cmath.atan),
    "arcsin": _analytic(math.asin, cmath.asin),
    "arccos": _analytic(math.acos, cmath.acos),
    "arctan": _analytic(math.atan, cmath.atan),
    "sinh": _analytic(math.sinh, cmath.sinh),
    "cosh": _analytic(math.cosh, cmath.cosh),
    "tanh": _analytic(math.tanh, cmath.tanh),
    "floor": lambda value: Fraction(math.floor(_real(value))),
    "ceil": lambda value: Fraction(math.ceil(_real(value))),
    "trunc": lambda value: Fraction(math.trunc(_real(value))),
    "round": _round_half_up,
    "factorial": _factorial,
}
_CONSTANTS: Final[dict[str, Number]] = {
    "pi": math.pi,
    "tau": math.tau,
    "e": math.e,
    "phi": (1 + math.sqrt(5)) / 2,
    "i": 1j,
}
_FUNCTIONS: Final = frozenset(
    {*_UNARY_FUNCTIONS, "log", "gcd", "lcm", "max", "min", "pow"}
)
_NAMES: Final = frozenset({*_CONSTANTS, *_FUNCTIONS})
# Single letters only match in lowercase, so the words "I" and "E" stay text.
_CASE_SENSITIVE: Final = frozenset({"e", "i"})


def _lookup(name: str) -> str | None:
    """The canonical name of a constant or function, or None if unknown."""
    if name in _CASE_SENSITIVE:
        return name
    lowered = name.lower()
    if lowered in _CASE_SENSITIVE or lowered not in _NAMES:
        return None
    return lowered


def _call(name: str, arguments: list[Number]) -> Number:
    count = len(arguments)
    if name in _UNARY_FUNCTIONS and count == 1:
        return _UNARY_FUNCTIONS[name](arguments[0])
    if name == "log" and count in (1, 2):
        return _log(*arguments)
    if name == "pow" and count == 2:
        return _power(*arguments)
    if name in ("gcd", "lcm"):
        integers = [_integer(argument) for argument in arguments]
        return Fraction(math.gcd(*integers) if name == "gcd" else math.lcm(*integers))
    if name in ("max", "min"):
        reals = [_real(argument) for argument in arguments]
        return max(reals) if name == "max" else min(reals)
    raise ExpressionError(f"wrong number of arguments for {name}")


def _binary(operator: str, left: Number, right: Number) -> Number:
    match operator:
        case "+":
            result: Number = left + right
        case "-":
            result = left - right
        case "*":
            result = left * right
        case "/":
            result = left / right
        case "//":
            quotient = _real(left) // _real(right)
            result = Fraction(quotient) if isinstance(quotient, int) else quotient
        case "%":
            result = _real(left) % _real(right)
        case _:
            result = _power(left, right)
    return _checked(result)


# --- tokenizer -----------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: TokenKind
    text: str = ""
    # None for a literal that is too long; only an error if it is used.
    value: Number | None = None
    # Whitespace before the token: "2 pi" is two words, "2pi" is 2π.
    spaced: bool = False


def normalize(content: str) -> str:
    """Map full-width, superscript and math symbols to plain ASCII syntax."""
    # Superscripts first: NFKC would turn "3²" into "32".
    content = _SUPERSCRIPT_RUN.sub(
        lambda match: f"^({match.group().translate(_SUPERSCRIPT_TABLE)})", content
    )
    content = content.translate(_TEXT_PUNCTUATION)
    return unicodedata.normalize("NFKC", content).translate(_SYMBOL_TABLE)


def _parse_literal(match: re.Match[str]) -> Number | None:
    text = match.group().replace("_", "")
    if match.group("mantissa") is None and match.group("bare_fraction") is None:
        return Fraction(int(text, 0))  # 0x, 0b or 0o
    integer = match.group("integer") or ""
    fraction = match.group("fraction") or match.group("bare_fraction") or ""
    exponent = match.group("exponent")
    if (
        len(integer.replace("_", "")) > MAX_DIGITS
        or len(fraction.replace("_", "")) > MAX_DIGITS
        or (exponent is not None and abs(int(exponent)) > MAX_EXPONENT_LITERAL)
    ):
        return None
    return Fraction(text)


class _Tokenizer:
    """Reads tokens on demand, so text after the expression is never scanned."""

    def __init__(self, text: str) -> None:
        self.text: str = text
        self.position: int = 0
        self.count: int = 0

    def next(self) -> _Token:
        text, start = self.text, self.position
        while self.position < len(text) and text[self.position].isspace():
            self.position += 1
        spaced = self.position > start
        if self.position >= len(text):
            return _Token("end", spaced=spaced)
        self.count += 1
        if self.count > MAX_TOKENS:
            raise ExpressionError("expression too long")
        token = self._read(text, self.position, spaced)
        self.position += len(token.text)
        return token

    def _read(self, text: str, position: int, spaced: bool) -> _Token:
        character = text[position]
        if number := _NUMBER.match(text, position):
            value = _parse_literal(number)
            end = number.end()
            # Python-style imaginary literal: 3j
            if text[end : end + 1] in ("j", "J") and not _NAME.match(text, end + 1):
                imaginary = None if value is None else 1j * value
                return _Token("num", text[position : end + 1], imaginary, spaced)
            return _Token("num", number.group(), value, spaced)
        if character in "([{":
            return _Token("open", character, spaced=spaced)
        if character in ")]}":
            return _Token("close", character, spaced=spaced)
        if character == ",":
            return _Token("comma", character, spaced=spaced)
        for operator in _OPERATORS:
            if text.startswith(operator, position):
                return _Token("op", operator, spaced=spaced)
        if character in _ROOTS:
            return _Token("root", character, spaced=spaced)
        if character in _GREEK:
            return _Token("name", character, spaced=spaced)
        name = _NAME.match(text, position)
        if name and (canonical := _lookup(name.group())) is not None:
            return _Token("name", canonical, spaced=spaced)
        # Unknown text ends the expression ("12換你了" counts as 12).
        return _Token("end", spaced=spaced)


# --- parser --------------------------------------------------------------------


class _Parser:
    """A Pratt parser that evaluates while it parses."""

    def __init__(self, text: str) -> None:
        self.tokens: _Tokenizer = _Tokenizer(text)
        self.current: _Token = self.tokens.next()
        self.depth: int = 0

    def save(self) -> tuple[int, int, _Token, int]:
        return (self.tokens.position, self.tokens.count, self.current, self.depth)

    def restore(self, state: tuple[int, int, _Token, int]) -> None:
        self.tokens.position, self.tokens.count, self.current, self.depth = state

    def advance(self) -> _Token:
        token = self.current
        self.current = self.tokens.next()
        return token

    def expect_close(self) -> None:
        if self.current.kind != "close":
            raise _SyntaxError("missing closing bracket")
        _ = self.advance()

    def parse(self) -> Number:
        value = self.expression(0)
        # Math glued to the end, as in "1..5", "1)" or "1,000", is ambiguous.
        if self.current.kind in _GLUED_ERRORS and not self.current.spaced:
            raise _SyntaxError("unexpected text after expression")
        return value

    def expression(self, min_power: int) -> Number:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExpressionError("expression nested too deeply")
        left = self.prefix()
        while True:
            token = self.current
            if token.kind == "op" and token.text in _BINARY:
                left_power, right_power = _BINARY[token.text]
                if left_power < min_power:
                    break
                state = self.save()
                _ = self.advance()
                try:
                    right = self.expression(right_power)
                except _SyntaxError:
                    # "12 - 換你" and "1+": the operator starts text, not math.
                    self.restore(state)
                    break
                left = _binary(token.text, left, right)
            elif token.kind == "op" and token.text == "!" and not token.spaced:
                if _POSTFIX < min_power:
                    break
                _ = self.advance()
                left = _factorial(left)
            elif self.starts_implicit_product(token):
                if _MULTIPLICATIVE < min_power:
                    break
                state = self.save()
                try:
                    right = self.expression(_MULTIPLICATIVE + 1)
                except _SyntaxError:
                    # "12(換你)": the bracket starts text, not a product.
                    self.restore(state)
                    break
                left = _binary("*", left, right)
            else:
                break
        self.depth -= 1
        return left

    @staticmethod
    def starts_implicit_product(token: _Token) -> bool:
        """2pi, 2(3) and 2√2 multiply; "2 pi" and "1..5" do not."""
        return not token.spaced and token.kind in ("name", "open", "root")

    def prefix(self) -> Number:
        token = self.advance()
        match token.kind:
            case "num":
                if token.value is None:
                    raise ExpressionError("number literal too long")
                return token.value
            case "op" if token.text in ("+", "-"):
                operand = self.expression(_UNARY)
                return _checked(-operand) if token.text == "-" else operand
            case "open":
                value = self.expression(0)
                self.expect_close()
                return value
            case "root":
                return _checked(_call(_ROOTS[token.text], [self.expression(_ROOT)]))
            case "name":
                return self.name(_GREEK.get(token.text, token.text))
            case _:
                raise _SyntaxError("expected a number")

    def name(self, name: str) -> Number:
        if name in _CONSTANTS:
            return _CONSTANTS[name]
        if self.advance().kind != "open":
            raise _SyntaxError(f"{name} needs parentheses")
        arguments = [self.expression(0)]
        while self.current.kind == "comma":
            _ = self.advance()
            arguments.append(self.expression(0))
        self.expect_close()
        return _checked(_call(name, arguments))


# --- public API ----------------------------------------------------------------


def _round_toward_zero(value: Number) -> int:
    if isinstance(value, complex):
        value = value.real  # A complex result counts as its real part: 3+4i -> 3.
    if isinstance(value, float):
        nearest = round(value)
        if abs(value - nearest) <= EPSILON * max(1.0, abs(value)):
            return nearest
    return math.trunc(value)


def evaluate(content: str) -> int:
    """Evaluate the expression a message starts with, rounded toward zero.

    Raises ExpressionError if the message does not start with valid math.
    """
    try:
        result = _round_toward_zero(_Parser(normalize(content)).parse())
    except (ArithmeticError, ValueError, TypeError) as error:
        # Division by zero, overflow, math domain errors and the like.
        raise ExpressionError(str(error)) from error
    if abs(result) >= 10**MAX_DIGITS:
        raise ExpressionError("result too large")
    return result


def parse_number(content: str) -> int | None:
    """The number a message counts as, or None if it is not a count."""
    try:
        return evaluate(content)
    except ExpressionError:
        return None


def format_units(units: int) -> str:
    """Format 1/100 units as a short decimal, e.g. 150 -> "1.5", 400 -> "4"."""
    return f"{units / 100:.2f}".rstrip("0").rstrip(".")
