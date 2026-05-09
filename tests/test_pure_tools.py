"""Unit tests for pure utility tools (calculator, datetime_tool)."""

import re
from datetime import datetime, timezone

import pytest

from utils.tools.calculator import calculator_tool
from utils.tools.datetime_tool import datetime_tool

# ---------------------------------------------------------------------------
# calculator_tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("2 + 2", "4"),
        ("10 * (5 + 3)", "80"),
        ("2 ** 10", "1024"),
        ("100 / 4", "25.0"),
        ("17 % 5", "2"),
        ("-7 + 10", "3"),
    ],
)
def test_calculator_basic_arithmetic(expr, expected):
    assert calculator_tool.invoke({"expression": expr}) == expected


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("abs(-5)", "5"),
        ("max(1, 2, 3)", "3"),
        ("min(1, 2, 3)", "1"),
        ("round(3.7)", "4"),
        ("pow(2, 8)", "256"),
        ("sum([1, 2, 3])", "6"),
    ],
)
def test_calculator_allowed_builtins(expr, expected):
    assert calculator_tool.invoke({"expression": expr}) == expected


@pytest.mark.parametrize(
    "forbidden",
    [
        "__import__('os')",
        "open('/etc/passwd')",
        "exec('print(1)')",
        "eval('1+1')",
    ],
)
def test_calculator_forbidden_returns_error_string(forbidden):
    out = calculator_tool.invoke({"expression": forbidden})
    assert out.startswith("Error evaluating expression")


def test_calculator_syntax_error_returns_error_string():
    out = calculator_tool.invoke({"expression": "2 +"})
    assert out.startswith("Error evaluating expression")


def test_calculator_division_by_zero_returns_error_string():
    out = calculator_tool.invoke({"expression": "1/0"})
    assert out.startswith("Error evaluating expression")


# ---------------------------------------------------------------------------
# datetime_tool
# ---------------------------------------------------------------------------


def test_datetime_default_format_matches_expected_pattern():
    out = datetime_tool.invoke({})
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", out)


def test_datetime_custom_format_year():
    out = datetime_tool.invoke({"format": "%Y"})
    expected_year = str(datetime.now(timezone.utc).year)
    assert out == expected_year


def test_datetime_custom_format_iso_date():
    out = datetime_tool.invoke({"format": "%Y-%m-%d"})
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", out)


def test_datetime_invalid_format_returns_error_string():
    # strftime of bytes (or other non-string) raises TypeError.
    # The @tool wrapper coerces input via the schema; pass a None
    # by going through a custom format with %% loops? Instead, call
    # the underlying function directly to bypass schema validation.
    from utils.tools.datetime_tool import datetime_tool as t

    out = t.func(format=None)  # type: ignore[arg-type]
    assert out.startswith("Error formatting datetime")
