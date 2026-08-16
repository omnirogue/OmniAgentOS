"""Unsigned LEB128 varint codec implementation."""

from __future__ import annotations


class CodecError(ValueError):
    """Raised when encoding or decoding fails."""


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer using unsigned LEB128 varint encoding."""
    if value < 0:
        raise CodecError("Negative value not allowed for unsigned varint.")
    if value == 0:
        return b"\x00"
    out = bytearray()
    while value > 0:
        temp = value & 0x7F
        value >>= 7
        if value > 0:
            temp |= 0x80
        out.append(temp)
    return bytes(out)


def decode_varint(buf: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a single varint from buf starting at offset."""
    if offset < 0 or offset >= len(buf):
        raise CodecError("Offset out of range.")

    value = 0
    shift = 0
    i = offset
    n = len(buf)

    while True:
        if i >= n:
            raise CodecError("Truncated varint: buffer ended without terminal byte.")
        byte = buf[i]
        value |= (byte & 0x7F) << shift
        i += 1
        if not (byte & 0x80):
            break
        shift += 7

    return value, i


def encode_all(values: list[int]) -> bytes:
    """Encode a list of non-negative integers by concatenating their varint encodings."""
    out = bytearray()
    for val in values:
        out.extend(encode_varint(val))
    return bytes(out)


def decode_all(buf: bytes) -> list[int]:
    """Decode all varints from buf and return them as a list."""
    if not buf:
        return []
    values = []
    offset = 0
    n = len(buf)
    while offset < n:
        val, next_offset = decode_varint(buf, offset)
        values.append(val)
        offset = next_offset
    if offset != n:
        raise CodecError("Trailing garbage or incomplete decode.")
    return values
