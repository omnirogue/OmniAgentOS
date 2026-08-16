"""FROZEN acceptance check for fx_011_indep_three_validators.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

from validators.semver import (
    InvalidVersion,
    compare_versions,
    latest,
    parse_version,
)


def test_parse_version_valid() -> None:
    assert parse_version("0.4.1") == (0, 4, 1, ())
    assert parse_version("10.0.0-alpha") == (10, 0, 0, ("alpha",))
    assert parse_version("1.0.0-alpha.1") == (1, 0, 0, ("alpha", "1"))
    assert parse_version("1.0.0-0.3.7") == (1, 0, 0, ("0", "3", "7"))
    assert parse_version("1.0.0-x.y.z--1") == (1, 0, 0, ("x", "y", "z--1"))


def test_parse_version_invalid() -> None:
    invalid_cases = [
        "1",
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-alpha.01",
        "1.2.3-alpha..1",
        "1.2.3+",
        "1.2.3+build",
        "1.2.3-alpha+b",
        "1.2.3-al_pha",
        "1.2.3-",
        "",
    ]
    for case in invalid_cases:
        try:
            parse_version(case)
            raise AssertionError(f"Expected InvalidVersion for: {case!r}")
        except InvalidVersion:
            pass
        except Exception as e:
            raise AssertionError(
                f"Expected InvalidVersion, got {type(e).__name__} for: {case!r}"
            ) from None


def test_compare_versions() -> None:
    assert compare_versions("1.0.0", "2.0.0") == -1
    assert compare_versions("2.0.0", "1.0.0") == 1
    assert compare_versions("1.1.0", "1.1.0") == 0
    assert compare_versions("1.1.1", "1.1.0") == 1

    assert compare_versions("1.0.0-alpha", "1.0.0") == -1
    assert compare_versions("1.0.0", "1.0.0-alpha") == 1

    assert compare_versions("1.0.0-alpha.1", "1.0.0-alpha.2") == -1
    assert compare_versions("1.0.0-alpha.10", "1.0.0-alpha.beta") == -1
    assert compare_versions("1.0.0-alpha", "1.0.0-beta") == -1
    assert compare_versions("1.0.0-alpha", "1.0.0-alpha.1") == -1


def test_latest() -> None:
    versions = ["1.0.0-alpha.1", "1.0.0", "1.0.0-beta", "0.9.9", "1.0.0-alpha"]
    assert latest(versions) == "1.0.0"

    versions_pre = ["1.0.0-alpha.1", "1.0.0-alpha.2", "1.0.0-alpha"]
    assert latest(versions_pre) == "1.0.0-alpha.2"

    try:
        latest([])
        raise AssertionError("Expected ValueError for empty list")
    except ValueError:
        pass

    try:
        latest(["1.0.0", "invalid"])
        raise AssertionError("Expected InvalidVersion for invalid version in list")
    except InvalidVersion:
        pass
