"""FROZEN acceptance check for fx_001_greenfield_palindrome.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

import strutil


def test_is_palindrome_true_cases() -> None:
    assert strutil.is_palindrome("") is True
    assert strutil.is_palindrome("   ") is True
    assert strutil.is_palindrome("a") is True
    assert strutil.is_palindrome("aba") is True
    assert strutil.is_palindrome("race car") is True
    assert strutil.is_palindrome("Never odd or even") is True
    assert strutil.is_palindrome("Was it a car or a cat I saw") is True


def test_is_palindrome_false_cases() -> None:
    assert strutil.is_palindrome("abc") is False
    assert strutil.is_palindrome("hello") is False
    # Spaces are ignored, so this collapses to "abc" — still not a mirror.
    assert strutil.is_palindrome("ab c") is False


def test_case_and_space_only_are_ignored() -> None:
    # Punctuation is NOT ignored: the trailing "!" breaks the mirror.
    assert strutil.is_palindrome("aba!") is False


def test_existing_helper_survives() -> None:
    assert strutil.normalize("  x  ") == "x"
