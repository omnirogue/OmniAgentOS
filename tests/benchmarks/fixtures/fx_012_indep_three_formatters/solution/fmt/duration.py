"""
Duration formatting and parsing utility.
"""

from __future__ import annotations

import re


def format_duration(seconds: int) -> str:
    """
    Formats a non-negative integer of seconds into a string of duration units.
    E.g., 90061 -> "1d 1h 1m 1s".
    """
    if seconds < 0:
        raise ValueError("Duration cannot be negative")

    if seconds == 0:
        return "0s"

    parts = []

    d = seconds // 86400
    rem = seconds % 86400
    if d > 0:
        parts.append(f"{d}d")

    h = rem // 3600
    rem = rem % 3600
    if h > 0:
        parts.append(f"{h}h")

    m = rem // 60
    s = rem % 60
    if m > 0:
        parts.append(f"{m}m")
    if s > 0:
        parts.append(f"{s}s")

    return " ".join(parts)


def parse_duration(text: str) -> int:
    """
    Parses a duration string into the total number of seconds.
    E.g., "1d 1h 1m 1s" -> 90061, "1m30s" -> 90.
    """
    if not text:
        raise ValueError("Empty duration string")

    if not re.match(r"^[0-9dhms\s]+$", text):
        raise ValueError("Invalid characters in duration string")

    comp_pattern = re.compile(r"\s*(\d+)\s*([dhms])\s*")
    matches = list(comp_pattern.finditer(text))
    if not matches:
        raise ValueError("No valid duration components found")

    last_end = 0
    seen_units = set()
    total_seconds = 0

    unit_multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}

    for match in matches:
        if match.start() != last_end:
            raise ValueError("Invalid format or missing unit/number")

        num_str, unit = match.groups()
        if unit in seen_units:
            raise ValueError(f"Repeated unit: {unit}")
        seen_units.add(unit)

        total_seconds += int(num_str) * unit_multipliers[unit]
        last_end = match.end()

    if last_end != len(text):
        raise ValueError("Trailing invalid characters")

    return total_seconds
