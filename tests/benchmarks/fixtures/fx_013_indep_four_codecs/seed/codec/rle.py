"""Run-length encoding codec implementation."""

from __future__ import annotations


class CodecError(ValueError):
    """Raised when encoding or decoding fails."""


def rle_encode(data: str) -> str:
    """Encode string using run-length encoding."""
    raise NotImplementedError()


def rle_decode(text: str) -> str:
    """Decode an RLE encoded string."""
    raise NotImplementedError()
