"""Configuration Pipeline CLI

This module acts as the CLI entry point for parsing and resolving configuration files.
"""

from __future__ import annotations

import sys

from iniparse import IniError, parse_ini
from resolve import CircularReference, MissingReference, resolve


def main(argv: list[str]) -> int:
    """CLI entry point for the sequence configuration pipeline.

    Args:
        argv: List of arguments (without the script name).

    Returns:
        An integer exit code.
    """
    if len(argv) != 1:
        print("usage: cli.py <config-path>")
        return 2

    path = argv[0]
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, FileNotFoundError):
        print(f"error: cannot read {path}")
        return 2

    try:
        parsed = parse_ini(content)
        resolved = resolve(parsed)
    except (IniError, MissingReference, CircularReference) as e:
        print(f"error: {e}")
        return 1

    outputs = []
    for section, keys in resolved.items():
        for key, value in keys.items():
            outputs.append((f"{section}.{key}", value))

    outputs.sort(key=lambda item: item[0])

    for full_key, value in outputs:
        print(f"{full_key}={value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
