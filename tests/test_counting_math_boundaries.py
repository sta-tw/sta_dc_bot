import pytest

from bot.utils.counting import parse_number


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("ceil(sin(π))", 0),
        ("floor(sin(-π))", 0),
        ("ceil(cos(π/2))", 0),
        ("floor(cos(3π/2))", 0),
        ("ceil(sin(11π))", 0),
        ("floor(sin(-11π))", 0),
        ("sin(π)*10^16", 0),
        ("sin(1000000π)*10^16", 0),
        ("cos(π/2)*10^16", 0),
        ("sin(-π/2)", -1),
        ("cos(π)", -1),
        ("tan(π)", 0),
        # Small nonzero inputs must not be snapped to zero.
        ("ceil(sin(1e-20))", 1),
        ("floor(sin(-1e-20))", -1),
        ("ceil(cos(π/2-1e-8))", 1),
        ("floor(cos(π/2+1e-8))", -1),
        # Preserve exact rational values on either side of an integer.
        ("ceil(1+1e-20)", 2),
        ("floor(1-1e-20)", 0),
        # Complex arguments continue to use complex trigonometric functions.
        ("cos(i)", 1),
        ("Im(sin(i))", 1),
        ("Im(tan(i))", 0),
        ("det([[1]])", 1),
        ("tr([[7]])", 7),
        ("det([[0]])", 0),
        ("det([[-3]])", -3),
        ("det([[1+2]])", 3),
        ("tr([[1/2]])*2", 1),
        ("det([[1,2],[3,4]])", -2),
        ("tr([[1,2],[3,4]])", 5),
        ("[1+2]*3", 9),
        ("[[1+2]]*3", 9),
    ],
)
def test_math_boundaries_preserve_valid_values(content: str, expected: int) -> None:
    assert parse_number(content) == expected


@pytest.mark.parametrize(
    "content",
    [
        "tan(π/2)",
        "tan(-π/2)",
        "tan(3π/2)",
        "tan(11π/2)",
        "tan(13π/2)",
        "tan(-11π/2)",
        "sec(π/2)",
        "cot(π)",
        "csc(π)",
        "det(1)",
        "det([1])",
        "det([[1,2]])",
        "det([[1],[2]])",
        "det([[[1]]])",
        "tr([[[1]]])",
        "[1,2]",
    ],
)
def test_math_boundaries_reject_undefined_values(content: str) -> None:
    assert parse_number(content) is None


@pytest.mark.parametrize("offset", ["-1e-8", "+1e-8", "-1e-12", "+1e-12"])
def test_tangent_near_pole_is_still_valid(offset: str) -> None:
    result = parse_number(f"tan(π/2{offset})")
    assert result is not None
    assert abs(result) > 1_000_000
