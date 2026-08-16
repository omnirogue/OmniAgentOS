"""Symmetric encryption for held-out ``expected`` values at rest.

Path secrecy inside a shared same-uid directory is not isolation: a scrubbed
candidate can still enumerate the protected SQLite file, and owner-only file
permissions do not distinguish it from the grader. Encrypting each value makes
that demonstrated on-disk read recover ciphertext instead of the held-out
answer.

This is not OS-level sandboxing. Same-uid access to a live grader worker's
memory, or to its short-lived spawn environment through process inspection, is
a deeper H3 isolation boundary (mount namespace/container/process isolation).
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

EVAL_KEY_ENV = "OMNIAGENTOS_EVAL_KEY"


class DecryptionError(RuntimeError):
    """Raised for an unreadable protected value without exposing key material."""


def generate_key() -> bytes:
    """Return a fresh Fernet key for one ``ProtectedGrader`` run."""
    return Fernet.generate_key()


def encrypt_expected(key: bytes, expected: dict[str, Any]) -> str:
    """Serialize and encrypt one held-out expected value."""
    payload = json.dumps(expected, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return Fernet(key).encrypt(payload).decode("ascii")


def decrypt_expected(key: bytes, ciphertext: str) -> dict[str, Any]:
    """Decrypt one held-out expected value, rejecting corrupt/non-object data."""
    if not isinstance(ciphertext, str):
        raise DecryptionError("could not decrypt protected expected value")
    try:
        payload = Fernet(key).decrypt(ciphertext.encode("ascii"))
        loaded = json.loads(payload)
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise DecryptionError("could not decrypt protected expected value") from exc
    if not isinstance(loaded, dict):
        raise DecryptionError("could not decrypt protected expected value")
    return loaded
