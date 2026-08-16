"""INI Configuration Parser

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
    result: dict[str, dict[str, str]] = {}
    current_section: str | None = None

    for idx, raw_line in enumerate(text.splitlines(), 1):
        if _is_ignored_line(raw_line):
            continue

        stripped = raw_line.strip()

        # Check if it is a section header
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip()
            if current_section not in result:
                result[current_section] = {}
            continue

        # It's not ignored and not a section header. It must contain '='.
        if "=" not in stripped:
            raise IniError(
                f"Line {idx}: Line must be a section header or a key-value pair: {raw_line!r}"
            )

        if current_section is None:
            raise IniError(
                f"Line {idx}: Key-value pair found before any section header: {raw_line!r}"
            )

        key, val = stripped.split("=", 1)
        result[current_section][key.strip()] = val.strip()

    return result
