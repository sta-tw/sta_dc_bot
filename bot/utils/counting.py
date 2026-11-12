"""Parsing and formatting helpers for the counting channel.

A message counts as the value of the math expression it starts with, rounded
toward zero: "12", "6*2", "√144", "2^3+4", "１２" and "12.9 換你了" all count
as 12. Anything after the expression is ignored as long as it is separated by
a space or is not math (e.g. "12 換你了", "12換你了").

Besides arithmetic the parser understands common textbook notation: |x|,
⌊x⌋, ⌈x⌉, log₂(x), sin²(x), sums ∑ₖ₌₁¹⁰, products ∏, integrals ∫₀¹ f(x) dx,
limits limₓ→0, derivatives (d/dx)(f)|ₓ₌₂, matrices det([[1,2],[3,4]]) and
number theory functions such as C(n,k), φ(n) and τ(n).

The text is first compiled into closures and then evaluated, so sums, integrals,
limits and derivatives can evaluate their body many times. User text is never
passed to eval(). Sizes and the total amount of work are capped so hostile
input cannot exhaust CPU or memory.
"""

import cmath
import math
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal

type Number = Fraction | float | complex
# Square-bracket lists, used as matrices: [[1,2],[3,4]].
type Value = Number | tuple[Value, ...]
type TokenKind = Literal[
    "num",
    "name",
    "var",
    "ident",
    "op",
    "open",
    "close",
    "comma",
    "root",
    "bar",
    "sub",
    "eq",
    "arrow",
    "inf",
    "big",
    "deriv",
    "end",
]

# A literal or result may have at most this many digits before the point.
MAX_DIGITS: Final = 100
MAX_TOKENS: Final = 256
MAX_DEPTH: Final = 64
# Exact intermediate values are capped at about 6000 digits.
MAX_BITS: Final = 20_000
MAX_EXPONENT_LITERAL: Final = 1_000
MAX_FACTORIAL: Final = 1_000
# Node evaluations allowed inside sums, integrals, limits and derivatives.
MAX_WORK: Final = 100_000
MAX_TERMS: Final = 10_000
MAX_MATRIX: Final = 8
MAX_FACTORIZE: Final = 10**12
MAX_ZETA_NEGATIVE: Final = 200
# Floating point noise below this relative size is ignored (2.9999999999 -> 3).
EPSILON: Final = 1e-9

_SUPERSCRIPT_TABLE: Final = str.maketrans(
    {
        **dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789", strict=True)),
        "⁺": "+",
        "⁻": "-",
        "⁽": "(",
        "⁾": ")",
        "⁼": "=",
        "ⁿ": "n",
        "ⁱ": "i",
        "ˣ": "x",
        "ʸ": "y",
        "ᵏ": "k",
        "ᵗ": "t",
    }
)
_SUBSCRIPT_TABLE: Final = str.maketrans(
    {
        **dict(zip("₀₁₂₃₄₅₆₇₈₉", "0123456789", strict=True)),
        "₊": "+",
        "₋": "-",
        "₌": "=",
        "₍": "(",
        "₎": ")",
        **dict(zip("ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣᵤᵥ", "aeoxhklmnpstijruv", strict=True)),
    }
)


def _run_pattern(table: dict[int, str]) -> re.Pattern[str]:
    characters = "".join(re.escape(chr(code)) for code in table)
    return re.compile(f"[{characters}]+")


_SUPERSCRIPT_RUN: Final = _run_pattern(_SUPERSCRIPT_TABLE)
_SUBSCRIPT_RUN: Final = _run_pattern(_SUBSCRIPT_TABLE)
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
        "∣": "|",
        "⟶": "→",
    }
)
# Full-width punctuation is ordinary Chinese text, not math: "12！" is excitement,
# not a factorial, and "12，換你" is a sentence. Hide it from NFKC.
_TEXT_PUNCTUATION: Final = str.maketrans({"！": "", "，": ""})

# 0x1F, 0b101, 0o17, or a decimal such as 12, 1_000, 12.5, 12., .5 and 1.5e3.
_NUMBER_PATTERN: Final = (
    r"0[xX](?:_?[0-9a-fA-F])+|0[bB](?:_?[01])+|0[oO](?:_?[0-7])+"
    r"|(?P<mantissa>(?P<integer>[0-9](?:_?[0-9])*)"
    r"(?:\.(?P<fraction>(?:[0-9](?:_?[0-9])*)?))?"
    r"|\.(?P<bare_fraction>[0-9](?:_?[0-9])*))"
    r"(?:[eE](?P<exponent>[+-]?[0-9]+))?"
)
_NUMBER: Final = re.compile(_NUMBER_PATTERN)
_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# Bound variables are single lowercase letters other than the constants e and i.
_VARIABLE: Final = re.compile(r"[a-df-hj-z]")
# (d/dx), d/dx, (d²/dx²) after superscripts became ^(2).
_ORDER: Final = r"(?:\^\(?([1-9])\)?)?"
_DERIVATIVE: Final = re.compile(
    rf"(\(\s*)?d{_ORDER}\s*/\s*d([a-df-hj-z]){_ORDER}(?(1)\s*\)|(?![A-Za-z0-9]))"
)
# The differential that closes an integral, or a nested integral sign.
_DIFFERENTIAL: Final = re.compile(r"∫|(?<![A-Za-z])d([a-df-hj-z])(?![A-Za-z0-9])")
_GREEK: Final = {
    "π": "pi",
    "τ": "tau",
    "φ": "phi",
    "ϕ": "phi",
    "Γ": "gamma",
    "ζ": "zeta",
}
# φ(n), τ(n) and σ(n) directly followed by a bracket are number theory functions.
_GREEK_FUNCTIONS: Final = {
    "φ": "totient",
    "ϕ": "totient",
    "τ": "divisors",
    "σ": "sigma",
}
_ROOTS: Final = {"√": "sqrt", "∛": "cbrt"}
_BIG_OPERATORS: Final = {"∑": "sum", "Σ": "sum", "∏": "prod", "Π": "prod", "∫": "int"}
_OPERATORS: Final = ("**", "//", "+", "-", "*", "/", "%", "^", "!")
_CLOSERS: Final = {"(": ")", "[": "]", "{": "}", "⌊": "⌋", "⌈": "⌉"}
# Tokens that may not be glued to the end of an expression ("1..5", "1)", "1,000").
_GLUED_ERRORS: Final = frozenset({"num", "close", "comma"})

# Binding powers: higher binds tighter. Power is right-associative and binds
# tighter than a leading minus, so -2^2 is -4 as in standard notation.
_BAR: Final = 1  # f(x)|ₓ₌₂ applies to everything before it.
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
    "mod": (_MULTIPLICATIVE, _MULTIPLICATIVE + 1),
    "^": (_POWER, _POWER),
    "**": (_POWER, _POWER),
}

# Numerical methods.
_LIMIT_LEVELS: Final = 11
_LIMIT_TOLERANCE: Final = 1e-6
_DERIVATIVE_LEVELS: Final = 7
_DERIVATIVE_TOLERANCE: Final = 1e-6
_QUADRATURE_RANGE: Final = 4
_QUADRATURE_LEVELS: Final = 7
_QUADRATURE_TOLERANCE: Final = 1e-13
_SERIES_TOLERANCE: Final = 1e-17


class ExpressionError(Exception):
    """The text is not a valid expression, or evaluating it is not allowed."""


class _SyntaxError(ExpressionError):
    """The tokens do not form an expression (as opposed to a math failure)."""


# --- number helpers ------------------------------------------------------------


def _scalar(value: Value) -> Number:
    # A single-item square bracket can still group an arithmetic expression.
    # Keep its structure until evaluation so [[1]] also represents a 1×1 matrix.
    while isinstance(value, tuple):
        if len(value) != 1:
            raise ExpressionError("number required")
        value = value[0]
    return value


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
        # Floating point noise such as 1e-17j does not make a number complex.
        if abs(value.imag) > EPSILON * max(1.0, abs(value.real)):
            raise ExpressionError("real number required")
        return value.real
    return value


def _integer(value: Number) -> int:
    real = _real(value)
    if isinstance(real, Fraction):
        if real.denominator != 1:
            raise ExpressionError("integer required")
        return real.numerator
    if not math.isfinite(real):
        raise ExpressionError("integer required")
    nearest = round(real)
    if abs(real - nearest) > EPSILON * max(1.0, abs(real)):
        raise ExpressionError("integer required")
    return int(nearest)


def _inexact(value: Number) -> float | complex:
    return value if isinstance(value, complex) else float(value)


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


def _ln_fraction(value: Fraction) -> float:
    return math.log(value.numerator) - math.log(value.denominator)


def _trigonometric(
    real: Callable[[float], float],
    complex_: Callable[[complex], complex],
    quadrants: tuple[int | None, int | None, int | None, int | None],
) -> Callable[[Number], Number]:
    """Keep recognizable quadrant angles exact, including undefined poles."""
    analytic = _analytic(real, complex_)

    def apply(value: Number) -> Number:
        if isinstance(value, complex) and value.imag != 0:
            return analytic(value)
        angle = value.real if isinstance(value, complex) else float(value)
        multiple = round(angle / (math.pi / 2))
        if angle == multiple * (math.pi / 2):
            # Allow the rounding error to grow with the represented angle,
            # but cap it: huge angles can look like quadrant multiples solely
            # because their floating point spacing is larger than a period.
            residual = math.cos(angle) if multiple % 2 else math.sin(angle)
            tolerance = min(4 * math.ulp(angle), EPSILON)
            if abs(residual) <= tolerance:
                exact = quadrants[multiple % 4]
                if exact is None:
                    raise ExpressionError("trigonometric function is undefined")
                return Fraction(exact)
        return analytic(value)

    return apply


def _exact_log(value: Number, base: Number) -> Fraction | None:
    """log(value, base) when it is an integer, e.g. log₃(81) = 4 exactly."""
    if not (isinstance(value, Fraction) and isinstance(base, Fraction)):
        return None
    if value <= 0 or base <= 0 or base == 1:
        return None
    estimate = _ln_fraction(value) / _ln_fraction(base)
    exponent = round(estimate)
    if abs(exponent) > MAX_EXPONENT_LITERAL or abs(estimate - exponent) > 1e-6:
        return None
    try:
        if _power(base, Fraction(exponent)) == value:
            return Fraction(exponent)
    except ExpressionError:
        pass
    return None


_ln: Final = _analytic(math.log, cmath.log)
_TEN: Final = Fraction(10)


def _log(value: Number, base: Number = _TEN) -> Number:
    exact = _exact_log(value, base)
    if exact is not None:
        return exact
    if base == 10:
        return _analytic(math.log10, cmath.log10)(value)
    if base == 2:
        return _analytic(math.log2, lambda number: cmath.log(number, 2))(value)
    return _ln(value) / _ln(base)


def _reciprocal(function: Callable[[Number], Number]) -> Callable[[Number], Number]:
    return lambda value: _binary("/", Fraction(1), function(value))


def _real_part(value: Number) -> Number:
    return value.real if isinstance(value, complex) else value


def _imaginary_part(value: Number) -> Number:
    return value.imag if isinstance(value, complex) else Fraction(0)


def _gamma(value: Number) -> Number:
    real = _real(value)
    if isinstance(real, Fraction) and real.denominator == 1:
        if real <= 0:
            raise ExpressionError("gamma is undefined at non-positive integers")
        return _factorial(real - 1)
    return math.gamma(float(real))


def _bernoulli(count: int) -> list[Fraction]:
    """B₀ … B_count with B₁ = -1/2."""
    numbers: list[Fraction] = []
    for m in range(count + 1):
        total = sum(
            (math.comb(m + 1, k) * numbers[k] for k in range(m)), Fraction(0)
        )
        numbers.append(Fraction(1) if m == 0 else -total / (m + 1))
    return numbers


def _borwein_coefficients(size: int) -> list[float]:
    coefficients: list[float] = []
    total = Fraction(0)
    for i in range(size + 1):
        total += Fraction(
            math.factorial(size + i - 1) << (2 * i),
            math.factorial(size - i) * math.factorial(2 * i),
        )
        coefficients.append(float(size * total))
    return coefficients


_BORWEIN: Final = _borwein_coefficients(40)


def _zeta(value: Number) -> Number:
    s = _real(value)
    if s == 1:
        raise ExpressionError("zeta has a pole at 1")
    if isinstance(s, Fraction) and s.denominator == 1 and s <= 0:
        # ζ(-n) = (-1)ⁿ Bₙ₊₁ / (n + 1)
        n = -s.numerator
        if n > MAX_ZETA_NEGATIVE:
            raise ExpressionError("zeta argument out of range")
        sign = -1 if n % 2 else 1
        return sign * _bernoulli(n + 1)[n + 1] / (n + 1)
    s = float(s)
    if s < 0.5:
        # Functional equation: ζ(s) = 2ˢ πˢ⁻¹ sin(πs/2) Γ(1-s) ζ(1-s)
        reflected = float(_real(_zeta(1 - s)))
        return (
            math.pow(2, s) * math.pow(math.pi, s - 1) * math.sin(math.pi * s / 2)
        ) * math.gamma(1 - s) * reflected
    # Borwein's algorithm for the alternating zeta function η(s).
    size = len(_BORWEIN) - 1
    last = _BORWEIN[size]
    eta = -sum(
        (-1 if k % 2 else 1) * (_BORWEIN[k] - last) / math.pow(k + 1, s) for k in range(size)
    ) / last
    return eta / (1 - math.pow(2, 1 - s))


def _factorize(value: Number) -> dict[int, int]:
    number = _integer(value)
    if not 1 <= number <= MAX_FACTORIZE:
        raise ExpressionError("argument out of range")
    factors: dict[int, int] = {}
    divisor = 2
    while divisor * divisor <= number:
        while number % divisor == 0:
            factors[divisor] = factors.get(divisor, 0) + 1
            number //= divisor
        divisor += 1 if divisor == 2 else 2
    if number > 1:
        factors[number] = factors.get(number, 0) + 1
    return factors


def _totient(value: Number) -> Number:
    factors = _factorize(value)
    return Fraction(math.prod((p - 1) * p ** (e - 1) for p, e in factors.items()))


def _divisor_count(value: Number) -> Number:
    return Fraction(math.prod(e + 1 for e in _factorize(value).values()))


def _divisor_sum(value: Number) -> Number:
    factors = _factorize(value)
    return Fraction(math.prod((p ** (e + 1) - 1) // (p - 1) for p, e in factors.items()))


def _combinations(n: Number, k: Number, ordered: bool) -> Number:
    total, chosen = _integer(n), _integer(k)
    if total < 0 or chosen < 0:
        raise ExpressionError("argument out of range")
    if chosen > total:
        return Fraction(0)
    if min(chosen, total - chosen) > MAX_FACTORIAL or total > 10**6:
        raise ExpressionError("argument out of range")
    return Fraction(math.perm(total, chosen) if ordered else math.comb(total, chosen))


def _matrix(value: Value) -> list[list[Number]]:
    """A square matrix of numbers."""
    if not isinstance(value, tuple) or not value:
        raise ExpressionError("matrix required")
    rows: list[list[Number]] = []
    for row in value:
        if not isinstance(row, tuple) or len(row) != len(value):
            raise ExpressionError("square matrix required")
        if any(isinstance(item, tuple) for item in row):
            raise ExpressionError("matrix entries must be numbers")
        rows.append([_scalar(item) for item in row])
    if len(rows) > MAX_MATRIX:
        raise ExpressionError("matrix too large")
    return rows


def _determinant(value: Value) -> Number:
    rows = _matrix(value)
    size = len(rows)
    result: Number = Fraction(1)
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        if rows[pivot][column] == 0:
            return Fraction(0)
        if pivot != column:
            rows[column], rows[pivot] = rows[pivot], rows[column]
            result = -result
        result = _checked(result * rows[column][column])
        for row in range(column + 1, size):
            factor = rows[row][column] / rows[column][column]
            rows[row] = [
                _checked(a - factor * b)
                for a, b in zip(rows[row], rows[column], strict=True)
            ]
    return result


def _trace(value: Value) -> Number:
    rows = _matrix(value)
    return sum((rows[i][i] for i in range(len(rows))), Fraction(0))


_UNARY_FUNCTIONS: Final[dict[str, Callable[[Number], Number]]] = {
    "sqrt": _sqrt,
    "cbrt": _cbrt,
    "abs": lambda value: abs(value),
    "exp": _analytic(math.exp, cmath.exp),
    "ln": _ln,
    "log10": lambda value: _log(value, Fraction(10)),
    "log2": lambda value: _log(value, Fraction(2)),
    "sin": _trigonometric(math.sin, cmath.sin, (0, 1, 0, -1)),
    "cos": _trigonometric(math.cos, cmath.cos, (1, 0, -1, 0)),
    "tan": _trigonometric(math.tan, cmath.tan, (0, None, 0, None)),
    "asin": _analytic(math.asin, cmath.asin),
    "acos": _analytic(math.acos, cmath.acos),
    "atan": _analytic(math.atan, cmath.atan),
    "sinh": _analytic(math.sinh, cmath.sinh),
    "cosh": _analytic(math.cosh, cmath.cosh),
    "tanh": _analytic(math.tanh, cmath.tanh),
    "asinh": _analytic(math.asinh, cmath.asinh),
    "acosh": _analytic(math.acosh, cmath.acosh),
    "atanh": _analytic(math.atanh, cmath.atanh),
    "floor": lambda value: Fraction(math.floor(_real(value))),
    "ceil": lambda value: Fraction(math.ceil(_real(value))),
    "trunc": lambda value: Fraction(math.trunc(_real(value))),
    "round": _round_half_up,
    "factorial": _factorial,
    "re": _real_part,
    "im": _imaginary_part,
    "gamma": _gamma,
    "zeta": _zeta,
    "totient": _totient,
    "divisors": _divisor_count,
    "sigma": _divisor_sum,
}
_UNARY_FUNCTIONS.update(
    {
        "arcsin": _UNARY_FUNCTIONS["asin"],
        "arccos": _UNARY_FUNCTIONS["acos"],
        "arctan": _UNARY_FUNCTIONS["atan"],
        "cot": _reciprocal(_UNARY_FUNCTIONS["tan"]),
        "sec": _reciprocal(_UNARY_FUNCTIONS["cos"]),
        "csc": _reciprocal(_UNARY_FUNCTIONS["sin"]),
        "acot": lambda value: _UNARY_FUNCTIONS["atan"](
            _binary("/", Fraction(1), value)
        ),
    }
)
_UNARY_FUNCTIONS["arccot"] = _UNARY_FUNCTIONS["acot"]
# sin⁻¹(x) means arcsin(x), not 1/sin(x).
_INVERSES: Final = {
    "sin": "asin",
    "cos": "acos",
    "tan": "atan",
    "cot": "acot",
    "sinh": "asinh",
    "cosh": "acosh",
    "tanh": "atanh",
}
_MATRIX_FUNCTIONS: Final[dict[str, Callable[[Value], Number]]] = {
    "det": _determinant,
    "tr": _trace,
}
_CONSTANTS: Final[dict[str, Number]] = {
    "pi": math.pi,
    "tau": math.tau,
    "e": math.e,
    "phi": (1 + math.sqrt(5)) / 2,
    "i": 1j,
}
_FUNCTIONS: Final = frozenset(
    {
        *_UNARY_FUNCTIONS,
        *_MATRIX_FUNCTIONS,
        "log",
        "gcd",
        "lcm",
        "max",
        "min",
        "pow",
        "comb",
        "perm",
        "binom",
        "lim",
    }
)
_NAMES: Final = frozenset({*_CONSTANTS, *_FUNCTIONS})
# Names matched exactly. Other single letters are never names, so the words
# "I" and "E" stay text.
_ALIASES: Final = {
    "e": "e",
    "i": "i",
    "C": "comb",
    "P": "perm",
    "Re": "re",
    "Im": "im",
}
_INFINITY_NAMES: Final = frozenset({"inf", "infinity"})


def _lookup(name: str) -> str | None:
    """The canonical name of a constant or function, or None if unknown."""
    if name in _ALIASES:
        return _ALIASES[name]
    lowered = name.lower()
    if len(name) == 1 or lowered not in _NAMES:
        return None
    return lowered


def _call(name: str, arguments: list[Value]) -> Number:
    count = len(arguments)
    if name in _MATRIX_FUNCTIONS:
        if count != 1:
            raise ExpressionError(f"wrong number of arguments for {name}")
        return _MATRIX_FUNCTIONS[name](arguments[0])
    numbers = [_scalar(argument) for argument in arguments]
    if name in _UNARY_FUNCTIONS and count == 1:
        return _UNARY_FUNCTIONS[name](numbers[0])
    if name == "log" and count in (1, 2):
        return _log(*numbers)
    if name == "pow" and count == 2:
        return _power(*numbers)
    if name in ("comb", "binom", "perm") and count == 2:
        return _combinations(*numbers, ordered=name == "perm")
    if name in ("gcd", "lcm"):
        integers = [_integer(number) for number in numbers]
        return Fraction(math.gcd(*integers) if name == "gcd" else math.lcm(*integers))
    if name in ("max", "min"):
        reals = [_real(number) for number in numbers]
        return max(reals) if name == "max" else min(reals)
    raise ExpressionError(f"wrong number of arguments for {name}")


def _binary(operator: str, left: Value, right: Value) -> Number:
    a, b = _scalar(left), _scalar(right)
    match operator:
        case "+":
            result: Number = a + b
        case "-":
            result = a - b
        case "*":
            result = a * b
        case "/":
            result = a / b
        case "//":
            quotient = _real(a) // _real(b)
            result = Fraction(quotient) if isinstance(quotient, int) else quotient
        case "%" | "mod":
            result = _real(a) % _real(b)
        case _:
            result = _power(a, b)
    return _checked(result)


# --- evaluation ----------------------------------------------------------------


class _Env:
    """Variable bindings and the work budget for one evaluation."""

    def __init__(self) -> None:
        self.variables: dict[str, Number] = {}
        self.work: int = 0

    def lookup(self, name: str) -> Number:
        if name not in self.variables:
            raise ExpressionError(f"{name} has no value")
        return self.variables[name]

    def at(self, node: "Node", name: str, value: Number, cost: int = 1) -> Number:
        """Evaluate node with the variable name bound to value."""
        self.work += cost
        if self.work > MAX_WORK:
            raise ExpressionError("too much work")
        missing = name not in self.variables
        saved = self.variables.get(name, Fraction(0))
        self.variables[name] = value
        try:
            return _scalar(node(self))
        finally:
            if missing:
                del self.variables[name]
            else:
                self.variables[name] = saved


type Node = Callable[[_Env], Value]


@dataclass(frozen=True)
class _Const:
    value: Value

    def __call__(self, env: _Env) -> Value:
        return self.value


@dataclass(frozen=True)
class _ListNode:
    items: tuple[Node, ...]

    def __call__(self, env: _Env) -> Value:
        return tuple(item(env) for item in self.items)


def _richardson(values: Sequence[Number], shift: int) -> tuple[Number, float]:
    """Extrapolate T(h₀/2ᵏ) to h = 0; also return the last correction size.

    The error of T(h) is a series in h^shift: 1 for one-sided limits, 2 for
    central differences.
    """
    row = [values[0]]
    error = math.inf
    for value in values[1:]:
        new_row = [value]
        for m, old in enumerate(row, start=1):
            new_row.append(new_row[-1] + (new_row[-1] - old) / ((1 << (shift * m)) - 1))
        error = float(abs(new_row[-1] - row[-1]))
        row = new_row
    return row[-1], error


def _limit(body: Node, env: _Env, name: str, target: Number, cost: int) -> Number:
    point = _real(target)
    if isinstance(point, float) and math.isinf(point):
        sign = 1.0 if point > 0 else -1.0
        sides: list[Callable[[float], float]] = [lambda h: sign / h]
    else:
        center, scale = float(point), max(1.0, abs(float(point)))
        sides = [lambda h: center + scale * h, lambda h: center - scale * h]
    estimates: list[Number] = []
    for side in sides:
        steps = [math.ldexp(1 / 16, -level) for level in range(_LIMIT_LEVELS)]
        try:
            values = [_inexact(env.at(body, name, side(h), cost)) for h in steps]
        except (ArithmeticError, ValueError):
            continue  # Outside the domain on this side, as in √x at 0.
        estimate, error = _richardson(values, 1)
        if not error <= _LIMIT_TOLERANCE * max(1.0, abs(estimate)):
            raise ExpressionError("limit does not converge")
        estimates.append(estimate)
    if not estimates:
        raise ExpressionError("limit does not exist")
    first = estimates[0]
    if any(
        abs(other - first) > _LIMIT_TOLERANCE * max(1.0, abs(first))
        for other in estimates
    ):
        raise ExpressionError("one-sided limits differ")
    return _checked(sum(estimates) / len(estimates))


def _derivative(body: Node, env: _Env, name: str, order: int, cost: int) -> Number:
    """Extrapolate finite differences and check both sides for each derivative.

    Symmetric differences alone can hide corners, as for abs(x) at zero.
    Steps stay exact fractions, so polynomial differences remain exact.
    Finite sampling is a convergence check, not a proof of differentiability.
    """

    def extrapolate(values: list[Number], shift: int) -> Number:
        # Very small steps amplify floating point cancellation. Use the most
        # stable extrapolation instead of always trusting the smallest step.
        best: Number = values[0]
        best_error = math.inf
        for count in range(3, len(values) + 1):
            estimate, error = _richardson(values[:count], shift)
            if error < best_error:
                best, best_error = estimate, error
        if not best_error <= _DERIVATIVE_TOLERANCE * max(1.0, abs(best)):
            raise ExpressionError("derivative does not converge")
        return _checked(best)

    x = _real(env.lookup(name))
    size = abs(x)
    # Small enough to stay inside the domain near 0, as in ln(x) at 0.1.
    step = size / 4 if 0 < size < 0.5 else max(size, Fraction(1)) / 8
    stencil = [
        (Fraction(order - 2 * j, 2), (-1 if j % 2 else 1) * math.comb(order, j))
        for j in range(order + 1)
    ]
    values: list[Number] = []
    for level in range(_DERIVATIVE_LEVELS):
        h = step / (1 << level)
        total = sum(
            (weight * env.at(body, name, x + offset * h, cost) for offset, weight in stencil),
            Fraction(0),
        )
        values.append(total / math.prod([h] * order))
    try:
        estimate = extrapolate(values, 2)
    except ExpressionError:
        # A derivative may exist without the next derivative being smooth,
        # as for x*abs(x) at zero, whose central error is linear in h.
        estimate = extrapolate(values, 1)

    # A higher derivative also requires all preceding derivatives to exist.
    # For example, the symmetric third difference of abs(x) is zero, despite
    # its first derivative not existing at the origin.
    at_point = env.at(body, name, x, cost)
    for degree in range(1, order + 1):
        weights = [
            (-1 if (degree - j) % 2 else 1) * math.comb(degree, j)
            for j in range(degree + 1)
        ]
        sides: list[Number] = []
        exact_constant_sides: list[bool] = []
        for direction in (-1, 1):
            differences: list[Number] = []
            for level in range(_DERIVATIVE_LEVELS):
                h = direction * step / (degree * (1 << level))
                total = weights[0] * at_point + sum(
                    (
                        weights[j] * env.at(body, name, x + j * h, cost)
                        for j in range(1, degree + 1)
                    ),
                    Fraction(0),
                )
                differences.append(total / math.prod([h] * degree))
            sides.append(extrapolate(differences, 1))
            exact_constant_sides.append(
                all(
                    isinstance(value, Fraction) and value == differences[0]
                    for value in differences
                )
            )
        tolerance = _DERIVATIVE_TOLERANCE * max(1.0, *(abs(side) for side in sides))
        # Exact, constant slopes need no floating point tolerance. Otherwise a
        # small multiplier could hide the corner in e.g. 1e-20*abs(x).
        if (all(exact_constant_sides) and sides[0] != sides[1]) or (
            abs(sides[0] - sides[1]) > tolerance
        ):
            raise ExpressionError("one-sided derivatives differ")
        if degree == order and any(abs(side - estimate) > tolerance for side in sides):
            raise ExpressionError("derivative estimates differ")
    return _checked(_inexact(estimate))


def _quadrature_map(
    a: float, b: float
) -> Callable[[float], tuple[float, float]]:
    """Map t ∈ ℝ onto (a, b): tanh-sinh, exp-sinh or sinh-sinh substitution."""
    half_pi = math.pi / 2

    if math.isinf(a) and math.isinf(b):

        def point(t: float) -> tuple[float, float]:
            u = half_pi * math.sinh(t)
            return math.sinh(u), math.cosh(u) * half_pi * math.cosh(t)

    elif math.isinf(b) or math.isinf(a):
        anchor, direction = (a, 1.0) if math.isinf(b) else (b, -1.0)

        def point(t: float) -> tuple[float, float]:
            grow = math.exp(half_pi * math.sinh(t))
            return anchor + direction * grow, grow * half_pi * math.cosh(t)

    else:
        half = (b - a) / 2

        def point(t: float) -> tuple[float, float]:
            u = half_pi * math.sinh(t)
            shrink = math.exp(-2 * abs(u))
            # Distance to the nearer end, computed without cancellation.
            distance = half * 2 * shrink / (1 + shrink)
            x = b - distance if t >= 0 else a + distance
            return x, half * half_pi * math.cosh(t) / math.cosh(u) ** 2

    return point


def _integral(
    body: Node, env: _Env, name: str, lower: Number, upper: Number, cost: int
) -> Number:
    a, b = _real(lower), _real(upper)
    if a == b:
        return Fraction(0)
    if a > b:
        return _checked(-_integral(body, env, name, upper, lower, cost))
    start, stop = float(a), float(b)
    point = _quadrature_map(start, stop)
    total: Number = 0.0
    magnitude = 0.0

    def add(t: float) -> None:
        nonlocal total, magnitude
        x, weight = point(t)
        if not (start < x < stop) or weight == 0 or not math.isfinite(weight):
            return
        term = _inexact(env.at(body, name, x, cost)) * weight
        total += term
        magnitude += abs(term)

    h = 1.0
    for k in range(-_QUADRATURE_RANGE, _QUADRATURE_RANGE + 1):
        add(float(k))
    estimate = total * h
    change = math.inf
    for _ in range(_QUADRATURE_LEVELS):
        h /= 2
        for k in range(1, int(_QUADRATURE_RANGE / h) + 1, 2):
            add(k * h)
            add(-k * h)
        refined = total * h
        change = abs(refined - estimate)
        estimate = refined
        if change <= _QUADRATURE_TOLERANCE * magnitude * h:
            return _checked(estimate)
    if change <= 1e-8 * max(1.0, magnitude * h):
        return _checked(estimate)
    raise ExpressionError("integral does not converge")


def _series(
    body: Node,
    env: _Env,
    name: str,
    lower: Number,
    upper: Number,
    cost: int,
    product: bool,
) -> Number:
    operator = "*" if product else "+"
    first = _integer(lower)
    end = _real(upper)
    if isinstance(end, float) and math.isinf(end) and end > 0:
        # Add terms until they stop making a difference.
        result: Number = 1.0 if product else 0.0
        settled = 0
        for k in range(first, first + MAX_TERMS):
            term = _inexact(env.at(body, name, Fraction(k), cost))
            result = _binary(operator, result, term)
            change = abs(term - 1) if product else abs(term)
            if result == 0 or change <= _SERIES_TOLERANCE * abs(result):
                settled += 1
                if settled == 3:
                    return result
            else:
                settled = 0
        raise ExpressionError("series does not converge")
    last = _integer(end)
    if last - first + 1 > MAX_TERMS:
        raise ExpressionError("too many terms")
    result = Fraction(1 if product else 0)
    for k in range(first, last + 1):
        result = _binary(operator, result, env.at(body, name, Fraction(k), cost))
    return result


# --- tokenizer -----------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: TokenKind
    text: str = ""
    # None for a literal that is too long; only an error if it is used.
    value: Number | None = None
    # Whitespace before the token: "2 pi" is two words, "2pi" is 2π.
    spaced: bool = False
    # Where the whitespace before the token starts, for re-reading it.
    start: int = 0
    # Canonical function or variable name, or the derivative variable.
    name: str = ""
    order: int = 0


def normalize(content: str) -> str:
    """Map full-width, superscript and math symbols to plain ASCII syntax."""
    # Superscripts and subscripts first: NFKC would turn "3²" into "32".
    content = _SUPERSCRIPT_RUN.sub(
        lambda match: f"^({match.group().translate(_SUPERSCRIPT_TABLE)})", content
    )
    content = _SUBSCRIPT_RUN.sub(
        lambda match: f"_({match.group().translate(_SUBSCRIPT_TABLE)})", content
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
        # Variables bound by an enclosing sum, integral, limit or derivative.
        self.scope: frozenset[str] = frozenset()

    def next(self) -> _Token:
        text, start = self.text, self.position
        while self.position < len(text) and text[self.position].isspace():
            self.position += 1
        spaced = self.position > start
        if self.position >= len(text):
            return _Token("end", spaced=spaced, start=start)
        self.count += 1
        if self.count > MAX_TOKENS:
            raise ExpressionError("expression too long")
        token = self._read(text, self.position, spaced, start)
        self.position += len(token.text)
        return token

    def _read(self, text: str, position: int, spaced: bool, start: int) -> _Token:
        def make(kind: TokenKind, length: int = 1, **fields: object) -> _Token:
            source = text[position : position + length]
            return _Token(kind, source, spaced=spaced, start=start, **fields)  # pyright: ignore[reportArgumentType]

        character = text[position]
        if number := _NUMBER.match(text, position):
            value = _parse_literal(number)
            end = number.end()
            # Python-style imaginary literal: 3j
            if text[end : end + 1] in ("j", "J") and not _NAME.match(text, end + 1):
                imaginary = None if value is None else 1j * value
                return make("num", end + 1 - position, value=imaginary)
            return make("num", end - position, value=value)
        if character in "(d" and (derivative := _DERIVATIVE.match(text, position)):
            top, bottom = derivative.group(2), derivative.group(4)
            if top is None or bottom is None or top == bottom:
                order = int(top or bottom or 1)
                length = derivative.end() - position
                return make("deriv", length, name=derivative.group(3), order=order)
        if character in _CLOSERS:
            return make("open")
        if character in _CLOSERS.values():
            return make("close")
        if character == ",":
            return make("comma")
        if character == "→":
            return make("arrow")
        if text.startswith("->", position):
            return make("arrow", 2)
        for operator in _OPERATORS:
            if text.startswith(operator, position):
                return make("op", len(operator))
        simple: dict[str, TokenKind] = {
            "|": "bar",
            "_": "sub",
            "=": "eq",
            "∞": "inf",
        }
        if character in simple:
            return make(simple[character])
        if character in _ROOTS:
            return make("root", name=_ROOTS[character])
        if character in _BIG_OPERATORS:
            return make("big", name=_BIG_OPERATORS[character])
        if character in _GREEK_FUNCTIONS and text.startswith("(", position + 1):
            return make("name", name=_GREEK_FUNCTIONS[character])
        if character in _GREEK:
            return make("name", name=_GREEK[character])
        if name := _NAME.match(text, position):
            word = name.group()
            if word == "mod":
                return make("op", 3)
            if word.lower() in _INFINITY_NAMES:
                return make("inf", len(word))
            if (canonical := _lookup(word)) is not None:
                return make("name", len(word), name=canonical)
            # "3xk" inside ∑ₖ is 3·x·k: split off a bound variable.
            if word[0] in self.scope:
                return make("var", name=word[0])
            return make("ident", len(word))
        # Unknown text ends the expression ("12換你了" counts as 12).
        return _Token("end", spaced=spaced, start=start)


# --- parser --------------------------------------------------------------------


type _State = tuple[int, int, _Token, int, frozenset[str]]


class _Parser:
    """A Pratt parser that compiles the expression into closures."""

    def __init__(self, text: str) -> None:
        self.tokens: _Tokenizer = _Tokenizer(text)
        self.current: _Token = self.tokens.next()
        self.depth: int = 0

    def save(self) -> _State:
        tokens = self.tokens
        return (tokens.position, tokens.count, self.current, self.depth, tokens.scope)

    def restore(self, state: _State) -> None:
        tokens = self.tokens
        tokens.position, tokens.count, self.current, self.depth, tokens.scope = state

    def advance(self) -> _Token:
        token = self.current
        self.current = self.tokens.next()
        return token

    def retokenize(self) -> None:
        """Re-read the lookahead token after the variables in scope changed."""
        self.tokens.position = self.current.start
        self.current = self.tokens.next()

    def peek(self, kind: TokenKind) -> bool:
        """Whether the lookahead token has this kind (never narrowed by checkers)."""
        return self.current.kind == kind

    def expect(self, kind: TokenKind, text: str | None = None) -> _Token:
        if self.current.kind != kind or (text is not None and self.current.text != text):
            raise _SyntaxError(f"expected {text or kind}")
        return self.advance()

    def parse(self) -> Node:
        node = self.expression(0)
        # Math glued to the end, as in "1..5", "1)" or "1,000", is ambiguous.
        if self.current.kind in _GLUED_ERRORS and not self.current.spaced:
            raise _SyntaxError("unexpected text after expression")
        return node

    def expression(self, min_power: int) -> Node:
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
                left = _binary_node(token.text, left, right)
            elif token.kind == "op" and token.text == "!" and not token.spaced:
                if _POSTFIX < min_power:
                    break
                _ = self.advance()
                left = _function_node(_factorial, left)
            elif token.kind == "bar" and _BAR >= min_power:
                # f(x)|ₓ₌₂ evaluates everything before the bar at x = 2.
                state = self.save()
                _ = self.advance()
                try:
                    _ = self.expect("sub")
                    name, value = self.binding()
                except _SyntaxError:
                    self.restore(state)
                    break
                left = _substitute_node(left, name, value)
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
                left = _binary_node("*", left, right)
            else:
                break
        self.depth -= 1
        return left

    @staticmethod
    def starts_implicit_product(token: _Token) -> bool:
        """2pi, 2(3), 2√2 and 3x multiply; "2 pi" and "1..5" do not."""
        return not token.spaced and token.kind in (
            "name",
            "var",
            "open",
            "root",
            "big",
            "deriv",
        )

    def prefix(self) -> Node:
        token = self.advance()
        match token.kind:
            case "num":
                if token.value is None:
                    raise ExpressionError("number literal too long")
                return _Const(token.value)
            case "op" if token.text in ("+", "-"):
                operand = self.expression(_UNARY)
                if token.text == "+":
                    return operand
                if isinstance(operand, _Const) and not isinstance(operand.value, tuple):
                    return _Const(_checked(-operand.value))
                return lambda env: _checked(-_scalar(operand(env)))
            case "open":
                return self.bracket(token.text)
            case "bar":
                inner = self.expression(0)
                _ = self.expect("bar")
                return _function_node(_UNARY_FUNCTIONS["abs"], inner)
            case "root":
                operand = self.expression(_ROOT)
                return _function_node(_UNARY_FUNCTIONS[token.name], operand)
            case "var":
                name = token.name
                return lambda env: env.lookup(name)
            case "name":
                return self.name(token.name)
            case "big":
                return self.integral() if token.name == "int" else self.series(token.name)
            case "deriv":
                return self.derivative(token.name, token.order)
            case _:
                raise _SyntaxError("expected a number")

    def bracket(self, opener: str) -> Node:
        closer = _CLOSERS[opener]
        items = [self.expression(0)]
        while opener == "[" and self.current.kind == "comma":
            _ = self.advance()
            items.append(self.expression(0))
        _ = self.expect("close", closer)
        if opener == "[":
            return _ListNode(tuple(items))
        if opener in "⌊⌈":
            name = "floor" if opener == "⌊" else "ceil"
            return _function_node(_UNARY_FUNCTIONS[name], items[0])
        return items[0]

    def name(self, name: str) -> Node:
        if name in _CONSTANTS:
            return _Const(_CONSTANTS[name])
        if name == "lim":
            return self.limit()
        base: Node | None = None
        if name == "log" and self.current.kind == "sub":
            _ = self.advance()
            base = self.expression(_POWER + 1)  # log₂(8)
        power: Node | None = None
        if self.current.kind == "op" and self.current.text in ("^", "**"):
            if not self.current.spaced:
                _ = self.advance()
                power = self.expression(_POWER + 1)  # sin²(x)
                if (
                    isinstance(power, _Const)
                    and power.value == -1
                    and name in _INVERSES
                ):
                    name, power = _INVERSES[name], None
        opener = self.expect("open").text
        if opener not in "({":
            raise _SyntaxError(f"{name} needs parentheses")
        arguments = [self.expression(0)]
        while self.current.kind == "comma":
            _ = self.advance()
            arguments.append(self.expression(0))
        _ = self.expect("close", _CLOSERS[opener])
        if base is not None:
            arguments.append(base)

        def call(env: _Env) -> Value:
            return _checked(_call(name, [argument(env) for argument in arguments]))

        if power is None:
            return call
        return _binary_node("^", call, power)

    # --- calculus ------------------------------------------------------------

    def variable(self) -> str:
        token = self.current
        if token.kind in ("var", "ident") and _VARIABLE.fullmatch(token.text):
            _ = self.advance()
            return token.text
        raise _SyntaxError("expected a variable")

    def binding(self) -> tuple[str, Node]:
        """The k=1 in ∑ₖ₌₁ or the x=2 in |ₓ₌₂, with or without brackets."""
        closer = None
        if self.current.kind == "open" and self.current.text in "({":
            closer = _CLOSERS[self.advance().text]
        name = self.variable()
        _ = self.expect("eq")
        if closer is None:
            return name, self.expression(_POWER + 1)
        value = self.expression(0)
        _ = self.expect("close", closer)
        return name, value

    def bound(self) -> Node:
        """A sum, integral or limit bound: a tight expression, ∞ or -∞."""
        state = self.save()
        sign = 1.0
        if self.current.kind == "op" and self.current.text in ("+", "-"):
            sign = -1.0 if self.advance().text == "-" else 1.0
        if self.current.kind == "inf":
            _ = self.advance()
            return _Const(sign * math.inf)
        self.restore(state)
        if self.current.kind == "open" and self.current.text in "({":
            closer = _CLOSERS[self.advance().text]
            try:
                inner = self.bound()
                _ = self.expect("close", closer)
                return inner
            except _SyntaxError:
                self.restore(state)
        return self.expression(_POWER + 1)

    def scoped(self, name: str, min_power: int) -> tuple[Node, int]:
        """Parse a body with name bound; also return its evaluation cost."""
        outer = self.tokens.scope
        self.tokens.scope = outer | {name}
        self.retokenize()
        first = self.tokens.count
        try:
            body = self.expression(min_power)
        finally:
            self.tokens.scope = outer
        cost = self.tokens.count - first + 1
        self.retokenize()
        return body, cost

    def series(self, kind: str) -> Node:
        _ = self.expect("sub")
        name, lower = self.binding()
        _ = self.expect("op", "^")
        upper = self.bound()
        body, cost = self.scoped(name, _MULTIPLICATIVE)
        product = kind == "prod"
        return lambda env: _series(
            body, env, name, _scalar(lower(env)), _scalar(upper(env)), cost, product
        )

    def differential(self) -> str:
        """The variable of the dx that closes this integral."""
        pending = 0
        text = self.tokens.text
        for match in _DIFFERENTIAL.finditer(text, self.current.start):
            if match.group() == "∫":
                pending += 1
            elif pending == 0:
                return match.group(1)
            else:
                pending -= 1
        raise _SyntaxError("integral needs dx")

    def integral(self) -> Node:
        name = self.differential()
        _ = self.expect("sub")
        lower = self.bound()
        _ = self.expect("op", "^")
        upper = self.bound()
        body, cost = self.scoped(name, 0)
        _ = self.expect("ident", "d" + name)
        return lambda env: _integral(
            body, env, name, _scalar(lower(env)), _scalar(upper(env)), cost
        )

    def limit(self) -> Node:
        _ = self.expect("sub")
        if self.current.kind == "open" and self.current.text in "({":
            closer = _CLOSERS[self.advance().text]
            name = self.variable()
            if self.peek("arrow"):  # lim_(x→0)
                _ = self.advance()
                target = self.bound()
                _ = self.expect("close", closer)
            else:  # limₓ→0
                _ = self.expect("close", closer)
                _ = self.expect("arrow")
                target = self.bound()
        else:  # lim_x→0
            name = self.variable()
            _ = self.expect("arrow")
            target = self.bound()
        body, cost = self.scoped(name, _MULTIPLICATIVE)
        return lambda env: _limit(body, env, name, _scalar(target(env)), cost)

    def derivative(self, name: str, order: int) -> Node:
        body, cost = self.scoped(name, _MULTIPLICATIVE)
        return lambda env: _derivative(body, env, name, order, cost)


def _binary_node(operator: str, left: Node, right: Node) -> Node:
    return lambda env: _binary(operator, left(env), right(env))


def _function_node(function: Callable[[Number], Number], operand: Node) -> Node:
    return lambda env: _checked(function(_scalar(operand(env))))


def _substitute_node(node: Node, name: str, value: Node) -> Node:
    return lambda env: env.at(node, name, _scalar(value(env)))


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
        node = _Parser(normalize(content)).parse()
        result = _round_toward_zero(_scalar(node(_Env())))
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
