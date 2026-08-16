"""Canonical Hexdump utility."""

from __future__ import annotations


def hexdump(data: bytes, width: int = 16) -> str:
    """Generate a canonical hexdump -C style string of data."""
    if not isinstance(width, int) or width < 1 or width > 32:
        raise ValueError("Width must be between 1 and 32 inclusive.")
    if not data:
        return ""

    lines = []
    for i in range(0, len(data), width):
        chunk = data[i : i + width]
        offset_str = f"{i:08x}"

        hex_parts = []
        for j in range(width):
            if j < len(chunk):
                hex_parts.append(f"{chunk[j]:02x}")
            else:
                hex_parts.append("  ")

            if j < width - 1:
                if width % 2 == 0 and j == (width // 2) - 1:
                    hex_parts.append("  ")
                else:
                    hex_parts.append(" ")

        hex_str = "".join(hex_parts)

        gutter_chars = []
        for byte in chunk:
            if 0x20 <= byte <= 0x7E:
                gutter_chars.append(chr(byte))
            else:
                gutter_chars.append(".")
        gutter_str = "".join(gutter_chars)

        lines.append(f"{offset_str}  {hex_str}  |{gutter_str}|")

    return "\n".join(lines)
