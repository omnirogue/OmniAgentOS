from __future__ import annotations

import pytest

from omniagentos.formation.lineage import (
    ReviewerAssignmentError,
    UnknownModelLineageError,
    assign_reviewer,
    assign_verifier,
    lineage_for_model,
)


def test_sol_cannot_assign_codex_as_reviewer_but_can_assign_opus() -> None:
    """Catch the exact-name counterfeit: different names can share a lineage."""
    with pytest.raises(ReviewerAssignmentError, match="openai"):
        assign_reviewer(implementer="gpt-5.6-sol", candidates=["codex"])

    assert assign_reviewer(
        implementer="gpt-5.6-sol",
        candidates=["opus"],
    ) == ("opus",)


@pytest.mark.parametrize("surface", ["security", "verification"])
def test_high_assurance_surface_requires_two_cross_lineage_reviewers(
    surface: str,
) -> None:
    with pytest.raises(ReviewerAssignmentError, match="requires 2"):
        assign_reviewer(
            implementer="gpt-5.6-sol",
            candidates=["opus"],
            surface=surface,
        )

    with pytest.raises(ReviewerAssignmentError, match="distinct reviewer lineages"):
        assign_reviewer(
            implementer="gpt-5.6-sol",
            candidates=["opus", "sonnet"],
            surface=surface,
        )

    assert assign_reviewer(
        implementer="gpt-5.6-sol",
        candidates=["opus", "grok-4.5"],
        surface=surface,
    ) == ("opus", "grok-4.5")


def test_verifier_must_differ_in_lineage_from_finder() -> None:
    with pytest.raises(ReviewerAssignmentError, match="google"):
        assign_verifier(
            finder="gemini-3.6-flash",
            candidates=["gemini-3.1-pro"],
        )

    assert (
        assign_verifier(
            finder="gemini-3.6-flash",
            candidates=["gpt-5.6-terra"],
        )
        == "gpt-5.6-terra"
    )


def test_unknown_implementer_fails_closed() -> None:
    with pytest.raises(UnknownModelLineageError, match="some-new-model-9"):
        assign_reviewer(
            implementer="some-new-model-9",
            candidates=["opus"],
        )


def test_unknown_candidate_fails_closed_even_when_an_earlier_candidate_is_valid() -> None:
    with pytest.raises(UnknownModelLineageError, match="some-new-model-9"):
        assign_reviewer(
            implementer="gpt-5.6-sol",
            candidates=["opus", "some-new-model-9"],
        )


@pytest.mark.parametrize(
    ("model", "lineage"),
    [
        ("claude", "anthropic"),
        ("fable", "anthropic"),
        ("gpt-5.6-luna", "openai"),
        ("codex", "openai"),
        ("grok-build", "xai"),
        ("gemini-3.6-flash", "google"),
        ("gemma-3", "google"),
        ("qwen-coder", "alibaba"),
        ("deepseek-v4", "deepseek"),
        ("kimi-k3", "moonshot"),
    ],
)
def test_design_lineage_map(model: str, lineage: str) -> None:
    assert lineage_for_model(model) == lineage
