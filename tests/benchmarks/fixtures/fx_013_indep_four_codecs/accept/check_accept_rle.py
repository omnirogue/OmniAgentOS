"""FROZEN acceptance check for fx_013_indep_four_codecs.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

from codec.rle import CodecError, rle_decode, rle_encode


def test_rle_encode_basic() -> None:
    assert rle_encode("") == ""
    assert rle_encode("abc") == "abc"
    assert rle_encode("aabbcc") == "2a2b2c"
    assert rle_encode("aaab") == "3a1b" or rle_encode("aaab") == "3ab"


def test_rle_encode_long_runs() -> None:
    # 12 'a's: 9a3a
    assert rle_encode("a" * 12) == "9a3a"
    # 20 'b's: 9b9b2b
    assert rle_encode("b" * 20) == "9b9b2b"


def test_rle_encode_errors() -> None:
    try:
        rle_encode("abc1")
        raise AssertionError("Should raise CodecError for digits in input")
    except CodecError:
        pass


def test_rle_decode_basic() -> None:
    assert rle_decode("") == ""
    assert rle_decode("abc") == "abc"
    assert rle_decode("2a2b2c") == "aabbcc"
    assert rle_decode("9a3a") == "a" * 12
    assert rle_decode("9b9b2b") == "b" * 20


def test_rle_decode_errors() -> None:
    # count with no character
    for bad in ("9", "abc9", "2", "3a1"):
        try:
            rle_decode(bad)
            raise AssertionError(f"Should raise CodecError for: {bad}")
        except CodecError:
            pass

    # consecutive digits
    for bad in ("12a", "abc34b", "23"):
        try:
            rle_decode(bad)
            raise AssertionError(f"Should raise CodecError for consecutive digits: {bad}")
        except CodecError:
            pass

    # invalid count (0 or 1)
    for bad in ("0a", "1b", "a1b"):
        try:
            rle_decode(bad)
            raise AssertionError(f"Should raise CodecError for 0/1 counts: {bad}")
        except CodecError:
            pass


def test_rle_roundtrip() -> None:
    for s in ("", "abc", "a" * 10, "ab" * 5, "hello world", "foo bar baz"):
        assert rle_decode(rle_encode(s)) == s
