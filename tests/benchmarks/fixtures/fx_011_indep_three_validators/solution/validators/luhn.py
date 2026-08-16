"""Luhn and Credit Card validator module."""

from __future__ import annotations


def luhn_checksum(digits: str) -> int:
    """Compute the Luhn checksum modulo 10."""
    total = 0
    for i, char in enumerate(reversed(digits)):
        val = int(char)
        if i % 2 == 1:
            val *= 2
            if val > 9:
                val -= 9
        total += val
    return total % 10


def is_valid_card(raw: str) -> bool:
    """Check if raw represents a valid credit card number."""
    try:
        cleaned = "".join(c for c in raw if c not in (" ", "-"))
        if not (13 <= len(cleaned) <= 19):
            return False
        if not cleaned.isdigit():
            return False
        return luhn_checksum(cleaned) == 0
    except Exception:
        return False


def card_brand(raw: str) -> str:
    """Identify the credit card brand or return invalid/unknown."""
    if not is_valid_card(raw):
        return "invalid"
    cleaned = "".join(c for c in raw if c not in (" ", "-"))
    if cleaned.startswith("4") and len(cleaned) in (13, 16):
        return "visa"
    if len(cleaned) >= 2 and cleaned[:2] in ("51", "52", "53", "54", "55"):
        return "mastercard"
    if len(cleaned) == 15 and (cleaned.startswith("34") or cleaned.startswith("37")):
        return "amex"
    return "unknown"
