"""Luhn and Credit Card validator module."""

from __future__ import annotations


def luhn_checksum(digits: str) -> int:
    """Compute the Luhn checksum modulo 10."""
    raise NotImplementedError()


def is_valid_card(raw: str) -> bool:
    """Check if raw represents a valid credit card number."""
    raise NotImplementedError()


def card_brand(raw: str) -> str:
    """Identify the credit card brand or return invalid/unknown."""
    raise NotImplementedError()
