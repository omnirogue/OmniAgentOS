"""Single-trait genome mutations used for clean tournament ablations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from omniagentos.lab.contracts import GenomeSpec


def _other_model(model: str | None) -> str:
    """Return a stable alternate model without changing any other role field."""
    if model == "claude-opus":
        return "claude-sonnet"
    if model == "claude-sonnet":
        return "claude-opus"
    return "claude-sonnet" if model is None else "claude-opus"


def _other_agent(agent: str) -> str:
    if agent == "cli-codex":
        return "claude"
    return "cli-codex"


def _validated_variant(candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate while preserving omitted default fields from the source genome."""
    GenomeSpec.model_validate(candidate)
    return candidate


def mutate_single_trait(genome: dict[str, Any]) -> list[dict[str, Any]]:
    """Return three to five valid genomes, each with one atomic trait changed.

    A role assignment, a stage iteration count, and a judge/review flag are all
    independent traits.  The function deliberately does not combine mutations,
    which makes a subsequent tournament result attributable to one change.
    """
    GenomeSpec.model_validate(genome)
    original = deepcopy(genome)
    variants: list[dict[str, Any]] = []

    def add(change: Any) -> None:
        candidate = deepcopy(original)
        change(candidate)
        validated = _validated_variant(candidate)
        if validated != original and validated not in variants:
            variants.append(validated)

    if original["roles"]:
        add(
            lambda value: value["roles"][0].__setitem__(
                "model", _other_model(value["roles"][0].get("model"))
            )
        )
        add(
            lambda value: value["roles"][0].__setitem__(
                "agent", _other_agent(value["roles"][0]["agent"])
            )
        )
    if original["flow"]:
        add(
            lambda value: value["flow"][0].__setitem__(
                "iterations", int(value["flow"][0].get("iterations", 1)) + 1
            )
        )

    def flip_blind(value: dict[str, Any]) -> None:
        judge = value.setdefault("judge", {})
        judge["blind"] = not bool(judge.get("blind", False))

    def flip_adversarial(value: dict[str, Any]) -> None:
        review = value.setdefault("review", {})
        review["adversarial"] = not bool(review.get("adversarial", False))

    add(flip_blind)
    add(flip_adversarial)

    # Sparse-but-valid genomes may have no roles or stages.  This still gives
    # exactly three isolated traits to ablate.
    if len(variants) < 3:
        add(
            lambda value: value.__setitem__(
                "archetype",
                "panel" if value.get("archetype", "pipeline") != "panel" else "pipeline",
            )
        )

    return variants[:5]
