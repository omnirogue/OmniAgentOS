"""Unsigned LEB128 varint codec implementation."""

from __future__ import annotations


class CodecError(ValueError):
    """Raised when encoding or decoding fails."""


def encode_varint(value: int) -> bytes:
    """Encode a non-negative integer using unsigned LEB128 varint encoding."""
    raise NotImplementedError()


def decode_varint(buf: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a single varint from buf starting at offset."""
    raise NotImplementedError()


def encode_all(values: list[int]) -> bytes:
    """Encode a list of non-negative integers by concatenating their varint encodings."""
    raise NotImplementedError()


def decode_all(buf: bytes) -> list[int]:
    """Decode all varints from buf and return them as a list."""
    raise NotImplementedError()
