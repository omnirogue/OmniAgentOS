"""INI Configuration Parser Stub

This module handles parsing INI files into structured Python dicts.
"""

from __future__ import annotations


class IniError(ValueError):
    """Raised when there is an error parsing the INI structure."""

    pass


def _is_ignored_line(line: str) -> bool:
    """Check if a line is a blank line or a comment line starting with '#' or ';'."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#") or stripped.startswith(";")


def parse_ini(text: str) -> dict[str, dict[str, str]]:
    """Parse the given INI string and return a dictionary mapping sections to key-value pairs.

    Args:
        text: The raw INI content.

    Returns:
        A dict of dicts: {section_name: {key: value}}.

    Raises:
        IniError: If parsing fails.
    """
    raise NotImplementedError("parse_ini is not implemented yet")
