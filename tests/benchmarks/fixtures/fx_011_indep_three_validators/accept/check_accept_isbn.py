"""FROZEN acceptance check for fx_011_indep_three_validators.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

from validators.isbn import is_valid_isbn10, is_valid_isbn13, normalize_isbn


def test_normalize_isbn() -> None:
    assert normalize_isbn("  0-306-40615-2  ") == "0306406152"
    assert normalize_isbn("978-3-16-148410-0") == "9783161484100"
    assert normalize_isbn("1-56629-908-x") == "156629908X"
    assert normalize_isbn("a_b\tc\nd") == "A_B\tC\nD"


def test_is_valid_isbn10_valid() -> None:
    assert is_valid_isbn10("0-306-40615-2") is True
    assert is_valid_isbn10("1-56629-908-X") is True
    assert is_valid_isbn10("1-56629-908-x") is True
    assert is_valid_isbn10(" 1 5 6 6 2 9 9 0 8 X ") is True


def test_is_valid_isbn10_invalid() -> None:
    assert is_valid_isbn10("0-306-40615-3") is False
    assert is_valid_isbn10("030640615") is False
    assert is_valid_isbn10("03064061522") is False
    assert is_valid_isbn10("030640615A") is False
    assert is_valid_isbn10("0306X06152") is False
    assert is_valid_isbn10("") is False


def test_is_valid_isbn13_valid() -> None:
    assert is_valid_isbn13("978-3-16-148410-0") is True
    assert is_valid_isbn13("978 3 16 148410 0") is True
    assert is_valid_isbn13("9783161484100") is True


def test_is_valid_isbn13_invalid() -> None:
    assert is_valid_isbn13("978-3-16-148410-1") is False
    assert is_valid_isbn13("978316148410") is False
    assert is_valid_isbn13("97831614841000") is False
    assert is_valid_isbn13("978316148410X") is False
    assert is_valid_isbn13("") is False
