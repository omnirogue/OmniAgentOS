"""
Basic visible verification check for store setup and basic helpers.
This check file is part of the seed workspace and should always pass.
"""

from __future__ import annotations

from store import lookup, new_store, terms_of


def check_terms_of() -> None:
    text = "The, quick! Brown... fox jumps?"
    terms = terms_of(text)
    # lowercase, drop tokens < 2 chars, split non-alphanumeric
    assert "the" in terms
    assert "quick" in terms
    assert "brown" in terms
    assert "fox" in terms
    assert "jumps" in terms
    assert len(terms) == 5


def check_lookup_empty() -> None:
    store = new_store()
    assert lookup(store, "anything") == ()


if __name__ == "__main__":
    check_terms_of()
    check_lookup_empty()
    print("ALL OK")
