"""FROZEN acceptance check for fx_013_indep_four_codecs.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

from codec.hexdump import hexdump


def test_hexdump_empty() -> None:
    assert hexdump(b"") == ""


def test_hexdump_worked_example() -> None:
    expected = (
        "00000000  68 65 6c 6c  6f 20 77 6f  |hello wo|\n00000008  72 6c 64                  |rld|"
    )
    assert hexdump(b"hello world", width=8) == expected


def test_hexdump_custom_width() -> None:
    # Width 16 (even)
    # "hello world!"
    # 00000000  68 65 6c 6c 6f 20 77 6f  72 6c 64 21              |hello world!|
    data = b"hello world!"
    expected_w16 = "00000000  68 65 6c 6c 6f 20 77 6f  72 6c 64 21              |hello world!|"
    assert hexdump(data, width=16) == expected_w16

    # Width 5 (odd)
    # "hello"
    # 00000000  68 65 6c 6c 6f  |hello|
    expected_w5 = "00000000  68 65 6c 6c 6f  |hello|"
    assert hexdump(b"hello", width=5) == expected_w5


def test_hexdump_non_printable() -> None:
    # Non-printable characters (outside 0x20-0x7e) must be rendered as '.'
    data = b"\x00\x1f\x20\x7e\x7f\x80"
    # width=3 (odd)
    # 00000000  00 1f 20  |.. |
    # 00000003  7e 7f 80  |~..|
    expected = "00000000  00 1f 20  |.. |\n00000003  7e 7f 80  |~..|"
    assert hexdump(data, width=3) == expected


def test_hexdump_errors() -> None:
    # Width < 1 or > 32 should raise ValueError
    for bad_width in (0, -5, 33, 100):
        try:
            hexdump(b"abc", width=bad_width)
            raise AssertionError(f"Should raise ValueError for width {bad_width}")
        except ValueError:
            pass
