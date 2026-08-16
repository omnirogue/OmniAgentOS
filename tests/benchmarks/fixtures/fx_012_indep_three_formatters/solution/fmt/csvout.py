"""
Custom CSV serializing utility without using the standard csv library.
"""

from __future__ import annotations


def quote_field(value: str) -> str:
    """
    Quotes a field if it contains a comma, double quote, newline, carriage return,
    or has leading/trailing ASCII spaces. Double quotes inside are escaped by doubling.
    """
    needs_quoting = (
        "," in value
        or '"' in value
        or "\r" in value
        or "\n" in value
        or value.startswith(" ")
        or value.endswith(" ")
    )
    if needs_quoting:
        escaped = value.replace('"', '""')
        return f'"{escaped}"'
    return value


def to_csv(rows: list[list[str]]) -> str:
    """
    Serializes rows of strings to RFC-4180 style CSV.
    """
    if not rows:
        return ""
    lines = []
    for row in rows:
        quoted_row = [quote_field(field) for field in row]
        lines.append(",".join(quoted_row))
    return "\r\n".join(lines) + "\r\n"
