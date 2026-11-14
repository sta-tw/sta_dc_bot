import pytest

from bot.utils.counting import ExpressionError, evaluate, parse_number


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("(d/dx)(x^4-3x^2+7x)|_(x=2)", 27),
        ("(d²/dx²)(x^5)|_(x=1)", 20),
        ("(d³/dx³)(x^5)|_(x=1)", 60),
        ("(d⁴/dx⁴)(x^5)|_(x=1)", 120),
        ("(d/dx)(abs(x))|_(x=2)", 1),
        ("(d/dx)(abs(x))|_(x=-2)", -1),
        ("(d/dx)(x*abs(x))|_(x=0)", 0),
        ("(d/dx)(sin(x))|_(x=0)", 1),
        ("(d²/dx²)(cos(x))|_(x=0)", -1),
        ("(d/dx)(ln(x))|_(x=0.1)", 10),
        ("(d/dx)(floor(x))|_(x=0.5)", 0),
    ],
)
def test_derivatives_preserve_smooth_values(expression: str, expected: int) -> None:
    assert evaluate(expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "(d/dx)(abs(x))|_(x=0)",
        "(d/dx)(|x|)|ₓ₌₀",
        "(d/dx)(abs(x)+3x)|_(x=0)",
        "(d/dx)(1e-8*abs(x))|_(x=0)",
        "(d/dx)(1e-20*abs(x)+3x)|_(x=0)",
        "(d³/dx³)(abs(x))|_(x=0)",
        "(d²/dx²)(x*abs(x))|_(x=0)",
        "(d/dx)(floor(x))|_(x=0)",
        "(d/dx)(ceil(x))|_(x=0)",
        "(d/dx)(sqrt(x))|_(x=0)",
        "(d/dx)(1/x)|_(x=0)",
    ],
)
def test_derivatives_reject_corners_and_discontinuities(expression: str) -> None:
    with pytest.raises(ExpressionError):
        evaluate(expression)
    assert parse_number(expression) is None
