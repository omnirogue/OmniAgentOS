"""
Duration formatting and parsing utility.
"""

from __future__ import annotations


def format_duration(seconds: int) -> str:
    """
    Formats a non-negative integer of seconds into a string of duration units.
    E.g., 90061 -> "1d 1h 1m 1s".
    """
    raise NotImplementedError("TODO: implement format_duration")


def parse_duration(text: str) -> int:
    """
    Parses a duration string into the total number of seconds.
    E.g., "1d 1h 1m 1s" -> 90061, "1m30s" -> 90.
    """
    raise NotImplementedError("TODO: implement parse_duration")
