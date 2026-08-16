"""
Custom CSV serializing utility without using the standard csv library.
"""

from __future__ import annotations


def quote_field(value: str) -> str:
    """
    Quotes a field if it contains a comma, double quote, newline, carriage return,
    or has leading/trailing ASCII spaces. Double quotes inside are escaped by doubling.
    """
    raise NotImplementedError("TODO: implement quote_field")


def to_csv(rows: list[list[str]]) -> str:
    """
    Serializes rows of strings to RFC-4180 style CSV.
    """
    raise NotImplementedError("TODO: implement to_csv")
