"""Fail-closed model-lineage checks for independent review assignment.

The public lineage map mirrors ``devtasks/LANE-DISCIPLINE-DESIGN.md``.  Model
resolution is deliberately an allowlist: an unregistered name raises instead
of becoming its own lineage and therefore looking independent by accident.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, Literal, cast

Lineage = Literal[
    "anthropic",
    "openai",
    "xai",
    "google",
    "alibaba",
    "deepseek",
    "moonshot",
]
ReviewSurface = Literal["standard", "security", "verification"]

LINEAGE_MAP: Final[Mapping[Lineage, tuple[str, ...]]] = MappingProxyType(
    {
        "anthropic": ("claude", "opus", "fable", "sonnet", "haiku"),
        "openai": ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "codex"),
        "xai": ("grok-4.5", "grok-4.3", "grok-build"),
        "google": ("gemini-*", "gemma-*"),
        "alibaba": ("qwen-*",),
        "deepseek": ("deepseek-*",),
        "moonshot": ("kimi-*",),
    }
)

# Explicit aliases already used by runtime/configuration surfaces in this
# repository.  Keeping them enumerated preserves fail-closed behavior: adding a
# new shorthand requires a code review instead of a fuzzy vendor guess.
# DEFECT #3 (finding #3b, mandatory for reviewer failover): "cli-kimi" added
# to allow KimiAdapter in the reviewer pool (scheduler.py:982,6460).
_EXACT_ALIASES: Final[dict[str, Lineage]] = {
    "anthropic": "anthropic",
    "cli-claude": "anthropic",
    "openai": "openai",
    "cli-codex": "openai",
    "sol": "openai",
    "luna": "openai",
    "terra": "openai",
    "xai": "xai",
    "grok": "xai",
    "cli-grok": "xai",
    "google": "google",
    "gemini": "google",
    "gemma": "google",
    "cli-gemini": "google",
    "alibaba": "alibaba",
    "qwen": "alibaba",
    "deepseek": "deepseek",
    "moonshot": "moonshot",
    "kimi": "moonshot",
    "cli-kimi": "moonshot",
}

_PROVIDER_PREFIXES: Final[dict[str, Lineage]] = {
    "anthropic/": "anthropic",
    "openai/": "openai",
    "xai/": "xai",
    "x-ai/": "xai",
    "google/": "google",
    "alibaba/": "alibaba",
    "qwen/": "alibaba",
    "deepseek/": "deepseek",
    "moonshot/": "moonshot",
    "moonshotai/": "moonshot",
}


class LineageAssignmentError(ValueError):
    """Base class for a refused lineage-sensitive assignment."""


class UnknownModelLineageError(LineageAssignmentError):
    """A model name is not present in the explicit lineage allowlist."""


class ReviewerAssignmentError(LineageAssignmentError):
    """Known models cannot satisfy the requested independent-review policy."""


def _member_matches(model: str, member: str) -> bool:
    if member.endswith("*"):
        return model.startswith(member[:-1])
    # Named members are also model families in the design.  A separator is
    # required, so ``solstice`` cannot counterfeit ``sol``.
    return model == member or model.startswith(member + "-")


def _lineage_for_unqualified(model: str) -> Lineage | None:
    alias = _EXACT_ALIASES.get(model)
    if alias is not None:
        return alias
    for lineage, members in LINEAGE_MAP.items():
        if any(_member_matches(model, member) for member in members):
            return lineage
    return None


def _normalize_surface(surface: str | None) -> ReviewSurface:
    """Coerce a surface name to the canonical enum."""
    s = str(surface or "").strip().lower()
    if s in {"security", "verification"}:
        return cast(ReviewSurface, s)
    return "standard"


def lineage_for_model(model: str) -> Lineage:
    """Resolve a registered model/family name or refuse the unknown name."""
    normalized = str(model or "").strip().lower()
    if not normalized:
        raise UnknownModelLineageError("unknown model lineage for empty model name; refusing")

    direct = _lineage_for_unqualified(normalized)
    if direct is not None:
        return direct

    for prefix, provider_lineage in _PROVIDER_PREFIXES.items():
        if not normalized.startswith(prefix):
            continue
        resolved = _lineage_for_unqualified(normalized[len(prefix) :])
        if resolved == provider_lineage:
            return resolved
        break

    raise UnknownModelLineageError(
        f"unknown model lineage for {model!r}; register it before assigning independent review"
    )


def lineage_for_execution(*, model: str, provider: str = "") -> Lineage:
    """Lineage of the thing that ACTUALLY RAN — model first, provider as fallback.

    A bare ``lineage_for_model`` refuses any unregistered model name. That is the
    correct posture when *choosing* a reviewer, but it is too blunt when
    *identifying* an implementer that has already run: an unrecognised model name
    then hard-blocks the task. Measured in the simharness, where a stub model named
    ``stub-simple`` produced ``seq=0 crashed -> seq=1 blocked``, turning a
    recoverable malformed-JSON run into a failed one.

    The provider is the stronger signal anyway — it is the harness that executed the
    work, so ``provider='codex'`` IS openai regardless of what the model is called.
    Falling back to it makes the answer more accurate, not more permissive: the
    cross-lineage rule still compares two resolved lineages, and a genuinely
    unknown provider still refuses.
    """
    try:
        return lineage_for_model(model)
    except UnknownModelLineageError:
        pass
    normalized = str(provider or "").strip().lower()
    if not normalized:
        raise UnknownModelLineageError(
            f"unknown lineage for execution: model={model!r}, provider={provider!r}"
        )
    return lineage_for_model(f"{normalized}/filler")  # provider-prefix fallback


def assign_reviewer(
    *,
    implementer: str,
    candidates: Sequence[str],
    surface: str = "standard",
) -> tuple[str, ...]:
    """Choose independent reviewers or raise when policy cannot be met.

    All names are resolved before selection.  Consequently, an unknown later
    candidate cannot hide behind an earlier valid candidate.
    """
    implementer_lineage = lineage_for_model(implementer)
    normalized_surface = _normalize_surface(surface)
    required = 2 if normalized_surface in {"security", "verification"} else 1

    resolved_candidates = [
        (str(candidate), lineage_for_model(str(candidate))) for candidate in candidates
    ]
    selected: list[str] = []
    selected_lineages: set[Lineage] = set()
    same_lineage_candidates: list[str] = []
    for candidate, candidate_lineage in resolved_candidates:
        if candidate_lineage == implementer_lineage:
            same_lineage_candidates.append(candidate)
            continue
        if candidate_lineage in selected_lineages:
            continue
        selected.append(candidate)
        selected_lineages.add(candidate_lineage)
        if len(selected) == required:
            break

    if len(selected) == required:
        return tuple(selected)

    if required == 1 and same_lineage_candidates:
        joined = ", ".join(repr(name) for name in same_lineage_candidates)
        raise ReviewerAssignmentError(
            f"refused same lineage {implementer_lineage!r}: implementer "
            f"{implementer!r} cannot be reviewed by {joined}"
        )

    raise ReviewerAssignmentError(
        f"{normalized_surface} surface requires {required} reviewers with distinct "
        f"reviewer lineages, each different from implementer lineage "
        f"{implementer_lineage!r}; only {len(selected)} eligible lineage(s) supplied"
    )


def assign_verifier(*, finder: str, candidates: Sequence[str]) -> str:
    """Choose a finding verifier whose lineage differs from the finder."""
    return assign_reviewer(
        implementer=finder,
        candidates=candidates,
        surface="standard",
    )[0]


def _are_independent(*, implementer: str, reviewer: str) -> bool:
    """True iff implementer and reviewer have different lineages.

    Used for validation only; does not raise. Both names must be registered or
    the answer is False (safe default for "is this safe?").
    """
    try:
        impl_lineage = lineage_for_model(implementer)
        review_lineage = lineage_for_model(reviewer)
        return impl_lineage != review_lineage
    except UnknownModelLineageError:
        return False


__all__ = [
    "LINEAGE_MAP",
    "Lineage",
    "LineageAssignmentError",
    "ReviewSurface",
    "ReviewerAssignmentError",
    "UnknownModelLineageError",
    "assign_reviewer",
    "assign_verifier",
    "lineage_for_execution",
    "lineage_for_model",
]
