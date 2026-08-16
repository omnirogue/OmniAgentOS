"""Per-run provider allowlist enforcement (D5 / F9).

A dispatch or swarm run may pin ``allowed_providers=[codex, grok, gemini]``.
That pin is only meaningful if **every** provider pick site honours it:
worker rotation and the planner fallback chain. This module is the single
shared predicate; call sites must not re-implement the filter.

Mode flag (default **off**):

``OMNIAGENTOS_ALLOWED_PROVIDERS_MODE`` ∈ {``off``, ``shadow``, ``enforce``}

* ``off``     — ignore any allowlist; baseline behaviour.
* ``shadow``  — when an allowlist is set, log violations but still return the
  full candidate set (would-be filter is recorded).
* ``enforce`` — drop disallowed candidates; if none remain, raise
  :class:`ProviderNotAllowed` rather than falling through to a banned provider.

When ``allowed_providers`` is ``None`` or empty the filter is a no-op in every
mode (the off-by-default posture for the *constraint* itself).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Sequence
from typing import Any, Literal

LOG = logging.getLogger(__name__)

AllowedProvidersMode = Literal["off", "shadow", "enforce"]

ALLOWED_PROVIDERS_MODE_ENV = "OMNIAGENTOS_ALLOWED_PROVIDERS_MODE"
ALLOWED_PROVIDERS_MODES: tuple[AllowedProvidersMode, ...] = ("off", "shadow", "enforce")
DEFAULT_ALLOWED_PROVIDERS_MODE: AllowedProvidersMode = "off"

# Provider aliases seen in planner rungs / worker endpoints / account pool.
_PROVIDER_ALIASES: dict[str, str] = {
    "claude": "claude",
    "anthropic": "claude",
    "fable": "claude",
    "opus": "claude",
    "codex": "codex",
    "openai": "codex",
    "sol": "codex",
    "terra": "codex",
    "gpt": "codex",
    "grok": "grok",
    "xai": "grok",
    "gemini": "gemini",
    "google": "gemini",
    "kimi": "kimi",
    "moonshot": "kimi",
    "openrouter": "openrouter",
    "api": "api",
    "api-litellm": "api",
    "api-kimi-k3": "fireworks",
    "api-openrouter": "openrouter",
    "fireworks": "fireworks",
}


class ProviderNotAllowed(RuntimeError):
    """Raised when every candidate was filtered out by an active allowlist."""

    def __init__(
        self,
        *,
        stage: str,
        allowed: Sequence[str],
        rejected: Sequence[str],
        detail: str = "",
    ) -> None:
        self.stage = stage
        self.allowed = tuple(allowed)
        self.rejected = tuple(rejected)
        msg = (
            f"ProviderNotAllowed at stage={stage!r}: "
            f"allowed={list(allowed)!r} rejected={list(rejected)!r}"
        )
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


def allowed_providers_mode() -> AllowedProvidersMode:
    raw = os.environ.get(ALLOWED_PROVIDERS_MODE_ENV, DEFAULT_ALLOWED_PROVIDERS_MODE)
    value = str(raw or DEFAULT_ALLOWED_PROVIDERS_MODE).strip().lower()
    if value in ALLOWED_PROVIDERS_MODES:
        return value  # type: ignore[return-value]
    LOG.warning(
        "allowed_providers: unknown mode %r; treating as %s",
        raw,
        DEFAULT_ALLOWED_PROVIDERS_MODE,
    )
    return DEFAULT_ALLOWED_PROVIDERS_MODE


def normalize_provider(name: str | None) -> str:
    """Map a provider / rung / model lineage name to a canonical allowlist key."""
    if not name:
        return ""
    key = str(name).strip().lower()
    if key in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[key]
    # Harness forms like "cli-claude" / "cli-codex".
    if key.startswith("cli-"):
        return normalize_provider(key[4:])
    # Model-ish ids: "claude-opus-5", "gpt-5.6-sol", "gemini-3.6-flash".
    for prefix, canon in (
        ("claude", "claude"),
        ("anthropic", "claude"),
        ("gpt", "codex"),
        ("o1", "codex"),
        ("o3", "codex"),
        ("o4", "codex"),
        ("grok", "grok"),
        ("gemini", "gemini"),
        ("kimi", "kimi"),
    ):
        if key.startswith(prefix):
            return canon
    return key


def provider_of(candidate: Any) -> str:
    """Extract a provider name from a rung, endpoint, string, or mapping."""
    if candidate is None:
        return ""
    if isinstance(candidate, str):
        return normalize_provider(candidate)
    if isinstance(candidate, dict):
        for key in ("provider", "name", "cli", "harness", "model"):
            if candidate.get(key) is not None:
                return normalize_provider(str(candidate[key]))
        return ""
    for attr in ("provider", "name", "cli"):
        value = getattr(candidate, attr, None)
        if value:
            return normalize_provider(str(value))
    harness = getattr(candidate, "harness", None)
    if harness is not None:
        return normalize_provider(str(harness))
    model = getattr(candidate, "model", None)
    if model:
        return normalize_provider(str(model))
    return normalize_provider(str(candidate))


def filter_allowed[T](
    candidates: Iterable[T],
    allowed_providers: Sequence[str] | None,
    *,
    stage: str,
    mode: AllowedProvidersMode | None = None,
) -> list[T]:
    """Filter *candidates* under the active allowlist mode.

    Returns a list (never mutates the input). In ``shadow`` mode the original
    candidates are returned after logging any that would have been dropped.
    In ``enforce`` mode disallowed candidates are dropped; an empty result
    after a non-empty input raises :class:`ProviderNotAllowed`.
    """
    items = list(candidates)
    resolved_mode = mode or allowed_providers_mode()

    if resolved_mode == "off" or not allowed_providers:
        return items

    allowed = {normalize_provider(p) for p in allowed_providers if str(p).strip()}
    if not allowed:
        return items

    kept: list[T] = []
    rejected: list[str] = []
    for item in items:
        prov = provider_of(item)
        if prov in allowed:
            kept.append(item)
        else:
            rejected.append(prov or "<unknown>")
            LOG.info(
                "allowed_providers violation stage=%s mode=%s provider=%s allowed=%s",
                stage,
                resolved_mode,
                prov or "<unknown>",
                sorted(allowed),
            )

    if resolved_mode == "shadow":
        # Baseline behaviour: keep everyone, but the log lines are the dry-run trace.
        return items

    # enforce
    if not kept and items:
        raise ProviderNotAllowed(
            stage=stage,
            allowed=sorted(allowed),
            rejected=rejected,
            detail="no candidates remain after allowlist filter",
        )
    return kept


def assert_provider_allowed(
    provider: str | None,
    allowed_providers: Sequence[str] | None,
    *,
    stage: str,
    mode: AllowedProvidersMode | None = None,
) -> None:
    """Raise or log when a single chosen provider violates the allowlist."""
    resolved_mode = mode or allowed_providers_mode()
    if resolved_mode == "off" or not allowed_providers:
        return
    allowed = {normalize_provider(p) for p in allowed_providers if str(p).strip()}
    if not allowed:
        return
    prov = normalize_provider(provider)
    if prov in allowed:
        return
    LOG.info(
        "allowed_providers violation stage=%s mode=%s provider=%s allowed=%s",
        stage,
        resolved_mode,
        prov or "<unknown>",
        sorted(allowed),
    )
    if resolved_mode == "enforce":
        raise ProviderNotAllowed(
            stage=stage,
            allowed=sorted(allowed),
            rejected=[prov or "<unknown>"],
            detail="chosen provider is not on the allowlist",
        )
