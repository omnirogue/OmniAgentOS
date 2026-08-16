"""FROZEN acceptance check for fx_011_indep_three_validators.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

from validators.luhn import card_brand, is_valid_card, luhn_checksum


def test_luhn_checksum() -> None:
    # 4539148000006 is valid (0)
    assert luhn_checksum("4539148000006") == 0
    # 4539148803436468 checksum fails (returns 1)
    assert luhn_checksum("4539148803436468") == 1


def test_is_valid_card() -> None:
    # Test valid cards, including formatted ones
    assert is_valid_card("4539148000006") is True
    assert is_valid_card("4556737586899004") is True
    assert is_valid_card("4556 7375 8689 9004") is True
    assert is_valid_card("4539-1480-0000-6") is True
    assert is_valid_card("5105105105105001") is True
    assert is_valid_card("5555555555554006") is True
    assert is_valid_card("343434343430002") is True
    assert is_valid_card("378282246310005") is True
    assert is_valid_card("415623890120007") is True
    assert is_valid_card("6011000990139000007") is True


def test_is_valid_card_invalid() -> None:
    # Test invalid card values
    assert is_valid_card("4539148803436468") is False
    assert is_valid_card("5105105105105101") is False
    assert is_valid_card("340000000000001") is False
    assert is_valid_card("4992739871") is False
    assert is_valid_card("45391480000O6") is False
    assert is_valid_card("") is False


def test_card_brand() -> None:
    # Expected brands for valid cards
    assert card_brand("4539148000006") == "visa"
    assert card_brand("4556737586899004") == "visa"
    assert card_brand("5105105105105001") == "mastercard"
    assert card_brand("5555555555554006") == "mastercard"
    assert card_brand("343434343430002") == "amex"
    assert card_brand("378282246310005") == "amex"
    assert card_brand("415623890120007") == "unknown"
    assert card_brand("6011000990139000007") == "unknown"

    # Brand for invalid card values must be "invalid"
    assert card_brand("4539148803436468") == "invalid"
    assert card_brand("5105105105105101") == "invalid"
    assert card_brand("340000000000001") == "invalid"
    assert card_brand("4992739871") == "invalid"
    assert card_brand("45391480000O6") == "invalid"
    assert card_brand("") == "invalid"
