"""Run-length encoding codec implementation."""

from __future__ import annotations


class CodecError(ValueError):
    """Raised when encoding or decoding fails."""


def rle_encode(data: str) -> str:
    """Encode string using run-length encoding."""
    for char in data:
        if char.isdigit():
            raise CodecError("Input contains a digit.")

    if not data:
        return ""

    parts = []
    current_char = data[0]
    count = 1
    for char in data[1:]:
        if char == current_char:
            count += 1
        else:
            while count > 9:
                parts.append(f"9{current_char}")
                count -= 9
            if count >= 2:
                parts.append(f"{count}{current_char}")
            elif count == 1:
                parts.append(current_char)
            current_char = char
            count = 1

    while count > 9:
        parts.append(f"9{current_char}")
        count -= 9
    if count >= 2:
        parts.append(f"{count}{current_char}")
    elif count == 1:
        parts.append(current_char)

    return "".join(parts)


def rle_decode(text: str) -> str:
    """Decode an RLE encoded string."""
    i = 0
    n = len(text)
    decoded = []
    while i < n:
        char = text[i]
        if char.isdigit():
            count_val = int(char)
            if count_val < 2:
                raise CodecError("Invalid count value in RLE.")
            if i + 1 >= n:
                raise CodecError("Count with no following character.")
            next_char = text[i + 1]
            if next_char.isdigit():
                raise CodecError("Consecutive digits or multi-digit count.")
            decoded.append(next_char * count_val)
            i += 2
        else:
            decoded.append(char)
            i += 1
    return "".join(decoded)
