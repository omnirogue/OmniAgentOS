"""RFC 4648 Base32 codec implementation."""

from __future__ import annotations

ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


class CodecError(ValueError):
    """Raised when encoding or decoding fails."""


def b32_encode(data: bytes) -> str:
    """Encode bytes into RFC 4648 Base32 string."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("Expected bytes or bytearray.")

    out = []
    buffer = 0
    buffer_bits = 0

    for byte in data:
        buffer = (buffer << 8) | byte
        buffer_bits += 8
        while buffer_bits >= 5:
            val = (buffer >> (buffer_bits - 5)) & 31
            out.append(ALPHABET[val])
            buffer_bits -= 5

    if buffer_bits > 0:
        val = (buffer << (5 - buffer_bits)) & 31
        out.append(ALPHABET[val])

    pad_len = (8 - (len(out) % 8)) % 8
    out.append("=" * pad_len)

    return "".join(out)


def b32_decode(text: str) -> bytes:
    """Decode an RFC 4648 Base32 string back into bytes."""
    if not isinstance(text, str):
        raise TypeError("Expected str.")

    if len(text) % 8 != 0:
        raise CodecError("Invalid Base32 length (must be multiple of 8).")

    if not text:
        return b""

    for char in text:
        if char.islower():
            raise CodecError("Lower case letters are not accepted.")

    stripped = text.rstrip("=")
    pad_count = len(text) - len(stripped)

    if pad_count not in (0, 1, 3, 4, 6):
        raise CodecError("Invalid number of padding characters.")

    if "=" in stripped:
        raise CodecError("Padding character '=' found in the middle of the string.")

    vals = []
    char_map = {char: idx for idx, char in enumerate(ALPHABET)}
    for char in stripped:
        if char not in char_map:
            raise CodecError(f"Invalid character in Base32 string: {char}")
        vals.append(char_map[char])

    if vals:
        total_bits = len(vals) * 5
        leftover_bits = total_bits % 8
        if leftover_bits > 0:
            mask = (1 << leftover_bits) - 1
            if (vals[-1] & mask) != 0:
                raise CodecError("Non-zero leftover bits in the final byte representation.")

    out = bytearray()
    buffer = 0
    buffer_bits = 0
    for val in vals:
        buffer = (buffer << 5) | val
        buffer_bits += 5
        if buffer_bits >= 8:
            byte = (buffer >> (buffer_bits - 8)) & 255
            out.append(byte)
            buffer_bits -= 8

    return bytes(out)
