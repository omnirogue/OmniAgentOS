"""
FROZEN acceptance check for fx_012_indep_three_formatters.
This check file is copied over the workspace AFTER the agent runs to prevent tampering.
It verifies the duration formatter and parser implementation.
"""

from __future__ import annotations

from fmt.duration import format_duration, parse_duration


def test_format_duration_basic() -> None:
    assert format_duration(0) == "0s"
    assert format_duration(1) == "1s"
    assert format_duration(59) == "59s"
    assert format_duration(60) == "1m"
    assert format_duration(61) == "1m 1s"
    assert format_duration(3600) == "1h"
    assert format_duration(3601) == "1h 1s"
    assert format_duration(86400) == "1d"
    assert format_duration(86401) == "1d 1s"
    assert format_duration(90061) == "1d 1h 1m 1s"


def test_format_duration_errors() -> None:
    try:
        format_duration(-1)
        raise AssertionError("Should raise ValueError for negative duration")
    except ValueError:
        pass

    try:
        format_duration(-9999)
        raise AssertionError("Should raise ValueError for negative duration")
    except ValueError:
        pass


def test_parse_duration_basic() -> None:
    # Standard format match
    assert parse_duration("0s") == 0
    assert parse_duration("1s") == 1
    assert parse_duration("1d 1h 1m 1s") == 90061

    # Any order
    assert parse_duration("1s 1m 1h 1d") == 90061
    assert parse_duration("1m 1s") == 61
    assert parse_duration("1h 1s") == 3601

    # With or without spaces
    assert parse_duration("1m30s") == 90
    assert parse_duration("1m   30s") == 90
    assert parse_duration("  1d1h1m1s  ") == 90061
    assert parse_duration("10d") == 864000


def test_parse_duration_errors() -> None:
    invalid_cases = [
        "",
        "   ",
        "1",  # missing unit
        "s",  # missing number
        "1x",  # unknown unit
        "1s 1s",  # repeated unit
        "1m 1m",  # repeated unit
        "1s s",  # missing number for second component
        "1s 2",  # missing unit for second component
        "1.5s",  # invalid character '.'
        "-1s",  # invalid character '-'
        "1d 1h 1m 1s extra",  # trailing garbage
        "extra 1s",  # leading garbage
        "1 0s",  # digit separation without unit
    ]
    for case in invalid_cases:
        try:
            parse_duration(case)
            raise AssertionError(f"Should raise ValueError for {case!r}")
        except ValueError:
            pass


def test_duration_roundtrip() -> None:
    # Test a set of deterministic and random non-negative values
    test_values = [
        0,
        1,
        15,
        60,
        61,
        119,
        120,
        3599,
        3600,
        3601,
        86399,
        86400,
        86401,
        90061,
        1000000,
    ]

    # Simple deterministic random-like generation to remain completely deterministic
    # without using external random seeds
    for val in test_values:
        formatted = format_duration(val)
        parsed = parse_duration(formatted)
        assert parsed == val, f"Roundtrip failed for {val}: got {parsed} from {formatted!r}"

    # Verify a series of sequential values
    for val in range(1000, 2000):
        formatted = format_duration(val)
        parsed = parse_duration(formatted)
        assert parsed == val
