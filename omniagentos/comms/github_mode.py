"""Mode flag + webhook signature verification for the GitHub comms adapter.

Mode: ``OMNIAGENTOS_GITHUB_COMMS_MODE`` (off → shadow → enforce, default off).

Signature: GitHub ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body). The
ingress path uses this so production delivery is not a tests-only claim.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from typing import Literal

__all__ = [
    "GITHUB_COMMS_ENV",
    "GITHUB_COMMS_MODES",
    "GithubCommsMode",
    "github_comms_enabled",
    "github_comms_enforcing",
    "github_comms_mode",
    "verify_github_signature",
]

GithubCommsMode = Literal["off", "shadow", "enforce"]
GITHUB_COMMS_MODES: tuple[GithubCommsMode, ...] = ("off", "shadow", "enforce")
GITHUB_COMMS_ENV = "OMNIAGENTOS_GITHUB_COMMS_MODE"
DEFAULT_MODE: GithubCommsMode = "off"


def github_comms_mode(env: Mapping[str, str] | None = None) -> GithubCommsMode:
    source = env if env is not None else os.environ
    raw = str(source.get(GITHUB_COMMS_ENV, DEFAULT_MODE)).strip().lower()
    if raw in GITHUB_COMMS_MODES:
        return raw  # type: ignore[return-value]
    return DEFAULT_MODE


def github_comms_enabled(env: Mapping[str, str] | None = None) -> bool:
    return github_comms_mode(env) != "off"


def github_comms_enforcing(env: Mapping[str, str] | None = None) -> bool:
    return github_comms_mode(env) == "enforce"


def verify_github_signature(
    body: bytes,
    secret: str,
    signature_header: str | None,
) -> bool:
    """Validate GitHub ``X-Hub-Signature-256`` (``sha256=<hex>``).

    Fail-closed: empty secret, missing header, or wrong digest → False.
    """
    if not secret or not signature_header:
        return False
    header = signature_header.strip()
    if not header.lower().startswith("sha256="):
        return False
    provided = header.split("=", 1)[1].strip()
    if not provided:
        return False
    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, provided)
