"""FROZEN acceptance check for fx_013_indep_four_codecs.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

from codec.varint import CodecError, decode_all, decode_varint, encode_all, encode_varint


def test_encode_single() -> None:
    assert encode_varint(0) == b"\x00"
    assert encode_varint(1) == b"\x01"
    assert encode_varint(127) == b"\x7f"
    assert encode_varint(128) == b"\x80\x01"
    assert encode_varint(300) == b"\xac\x02"


def test_decode_single() -> None:
    assert decode_varint(b"\x00") == (0, 1)
    assert decode_varint(b"\x01") == (1, 1)
    assert decode_varint(b"\x7f") == (127, 1)
    assert decode_varint(b"\x80\x01") == (128, 2)
    assert decode_varint(b"\xac\x02") == (300, 2)

    # Test decoding with offset
    buf = b"\xff\xff" + b"\xac\x02" + b"\x00"
    assert decode_varint(buf, 2) == (300, 4)


def test_encode_all_decode_all() -> None:
    values = [0, 1, 127, 128, 300, 16384, 1000000]
    encoded = encode_all(values)
    decoded = decode_all(encoded)
    assert decoded == values
    assert decode_all(b"") == []


def test_varint_errors() -> None:
    # Negative inputs
    for val in (-1, -100):
        try:
            encode_varint(val)
            raise AssertionError("Should raise CodecError for negative values")
        except CodecError:
            pass

    try:
        encode_all([1, 2, -3])
        raise AssertionError("Should raise CodecError for list containing negative value")
    except CodecError:
        pass

    # Truncated varint
    for buf in (b"\x80", b"\xac\x82\x80"):
        try:
            decode_varint(buf)
            raise AssertionError(f"Should raise CodecError for truncated varint: {buf}")
        except CodecError:
            pass

    # Out of range offset
    for offset in (-1, 5):
        try:
            decode_varint(b"\x00", offset)
            raise AssertionError(f"Should raise CodecError for out of range offset: {offset}")
        except CodecError:
            pass

    # Trailing garbage in decode_all
    try:
        decode_all(b"\x00\x80")
        raise AssertionError("Should raise CodecError for trailing garbage or incomplete varint")
    except CodecError:
        pass
