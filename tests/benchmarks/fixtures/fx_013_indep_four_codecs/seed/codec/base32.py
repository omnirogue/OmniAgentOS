"""RFC 4648 Base32 codec implementation."""

from __future__ import annotations

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


class CodecError(ValueError):
    """Raised when encoding or decoding fails."""


def b32_encode(data: bytes) -> str:
    """Encode bytes into RFC 4648 Base32 string."""
    raise NotImplementedError()


def b32_decode(text: str) -> bytes:
    """Decode an RFC 4648 Base32 string back into bytes."""
    raise NotImplementedError()
