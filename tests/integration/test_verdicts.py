"""Fail-closed verdict parsing for the integration stage."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from omniagentos.integration.config import IntegrationConfig, load_integration_config
from omniagentos.integration.verdicts import (
    _LEGACY_ANTHROPIC_PROSE_MARKERS,
    _parse_verdict,
    _reviewer_lineage_ok,
    prose_names_anthropic,
)

REPO = Path(__file__).resolve().parents[2]


def _cfg(**overrides: Any) -> IntegrationConfig:
    base = load_integration_config(REPO / "configs" / "integration.yaml")
    if not overrides:
        return base
    return replace(base, **overrides)


def test_unparseable_is_not_approval() -> None:
    """Decisive: absent/malformed VERDICT is never approval.

    Includes the six named cases sol required: prefix counterfeits stay
    unparseable; case-insensitive exact + surrounding whitespace still work.
    """
    unparseable = [
        "",
        "looks fine to me",
        "I approve this change",
        "VERDICT: MAYBE",
        "VERDICT: LGTM",
        "Status: APPROVE",  # wrong key
        "notes only\nno verdict line",
        # Prefix / near-miss counterfeits — exact match only (fail-closed).
        "VERDICT: APPROVED",
        "VERDICT: APPROVE pending fixes",
        "VERDICT: APPROVE-WITH-NOTES",
        "VERDICT:",  # nothing after the colon
    ]
    for text in unparseable:
        v = _parse_verdict(text)
        assert v.decision == "unparseable", text
        assert v.decision != "approve"
        assert v.decision not in {"approve", "approve_with_notes"}

    # Contract: case-insensitive exact + surrounding whitespace ok.
    assert _parse_verdict("VERDICT: approve").decision == "approve"
    assert _parse_verdict("VERDICT:  REJECT ").decision == "reject"


def test_prefix_counterfeit_verdicts_are_unparseable() -> None:
    """Named counterfeits that prefix-matching would fail-open as approval."""
    counterfeits = [
        "VERDICT: APPROVED",
        "VERDICT: APPROVE pending fixes",
        "VERDICT: APPROVE-WITH-NOTES",
        "VERDICT:",  # empty payload
    ]
    for text in counterfeits:
        v = _parse_verdict(text)
        assert v.decision == "unparseable", text
        assert v.decision not in {"approve", "approve_with_notes"}

    # Case-insensitive exact still works.
    assert _parse_verdict("VERDICT: approve").decision == "approve"
    # Surrounding whitespace ok.
    assert _parse_verdict("VERDICT:  REJECT ").decision == "reject"
    # FAILED is outside the three named decisions → unparseable (not "failed").
    assert _parse_verdict("VERDICT: FAILED").decision == "unparseable"


def test_approve_with_notes() -> None:
    text = "VERDICT: APPROVE WITH NOTES\n\nShip it, but fix the log line later.\n"
    v = _parse_verdict(text)
    assert v.decision == "approve_with_notes"

    # Case-insensitive + markdown decoration (as shell consumers strip).
    v2 = _parse_verdict("**VERDICT:** approve with notes\n")
    assert v2.decision == "approve_with_notes"

    v3 = _parse_verdict("VERDICT: APPROVE\n")
    assert v3.decision == "approve"

    v4 = _parse_verdict("VERDICT: REJECT\nreason: no\n")
    assert v4.decision == "reject"


def test_reviewer_model_trailer_wins_over_prose() -> None:
    """Trailer lineage is authoritative even when prose names anthropic models."""
    # Trailer is openai/sol — must fail the anthropic requirement despite prose.
    text = (
        "VERDICT: APPROVE\n"
        "Reviewed by claude opus fable sonnet haiku in the narrative.\n"
        "Reviewer-Model: gpt-5.6-sol\n"
    )
    v = _parse_verdict(text)
    assert v.reviewer_model == "gpt-5.6-sol"
    # Two-argument surface only (contract).
    assert _reviewer_lineage_ok(v, _cfg()) is False

    # Trailer is anthropic — ok even without prose markers.
    text_ok = "VERDICT: APPROVE\nReviewer-Model: claude-opus-5\n"
    v_ok = _parse_verdict(text_ok)
    assert _reviewer_lineage_ok(v_ok, _cfg()) is True


def test_prose_fallback_flag_off_requires_trailer() -> None:
    """Two-arg surface: prose fallback uses v.source_text from _parse_verdict."""
    text = "VERDICT: APPROVE\nThis was reviewed by claude opus.\n"
    v = _parse_verdict(text)
    assert v.reviewer_model is None
    assert v.source_text == text
    # Contracted call: _reviewer_lineage_ok(v, cfg) — no third argument.
    assert _reviewer_lineage_ok(v, _cfg(prose_fallback=True)) is True
    assert _reviewer_lineage_ok(v, _cfg(prose_fallback=False)) is False


def test_two_arg_prose_fallback_without_trailer() -> None:
    """DECISIVE: two-arg _reviewer_lineage_ok performs prose fallback from source_text."""
    text = "VERDICT: APPROVE\nReviewed tonight by claude opus on the fleet.\n"
    v = _parse_verdict(text)
    assert v.reviewer_model is None
    assert "claude" in v.source_text.lower()
    # Exactly the brief surface — two arguments, no text= kwarg.
    assert _reviewer_lineage_ok(v, _cfg(prose_fallback=True)) is True
    assert _reviewer_lineage_ok(v, _cfg(prose_fallback=False)) is False
    # No anthropic marker → fallback still False under prose_fallback=True.
    cold = _parse_verdict("VERDICT: APPROVE\nsol reviewed this\n")
    assert _reviewer_lineage_ok(cold, _cfg(prose_fallback=True)) is False


def test_candidate_sha_parsed() -> None:
    text = (
        "VERDICT: APPROVE\n"
        "Reviewer-Model: claude-opus-5\n"
        "Candidate-Sha: deadbeefcafebabe0123456789abcdef01234567\n"
    )
    v = _parse_verdict(text)
    assert v.candidate_sha == "deadbeefcafebabe0123456789abcdef01234567"
    assert v.reviewer_model == "claude-opus-5"


def test_prose_fallback_matches_legacy_fleet_ledger_set() -> None:
    """Substring set must be EXACTLY fleet-ledger.py's tuple (transition parity)."""
    assert _LEGACY_ANTHROPIC_PROSE_MARKERS == (
        "opus",
        "claude",
        "sonnet",
        "fable",
        "haiku",
    )
    # Positive: each marker alone is enough (fleet-ledger uses `any(m in text.lower())`).
    for marker in _LEGACY_ANTHROPIC_PROSE_MARKERS:
        assert prose_names_anthropic(f"verifier was {marker} tonight") is True
    # Negative: no marker → not ok under prose fallback.
    assert prose_names_anthropic("verifier was sol / grok only") is False
    v = _parse_verdict("VERDICT: APPROVE\nsol reviewed this\n")
    assert _reviewer_lineage_ok(v, _cfg(prose_fallback=True)) is False


def test_unwired_parsing_surface_stays_private_until_wired() -> None:
    """The T5 seam is PRIVATE until its first production caller lands.

    The reachability gate refuses new public symbols with no production
    caller, and devtasks/REACHABILITY-EXEMPT.txt bans "will be wired later"
    as an exemption reason — so the parser and the lineage check keep their
    underscores until the change that wires the real consumer (the T5
    rewrites of integrate.sh / fleet-ledger.py, or the verdict-queue UI)
    drops them IN THE SAME COMMIT as the caller. Falsifiable term, in the
    spirit of the JG3 block in REACHABILITY-EXEMPT.txt: whoever wires the
    caller deletes this test in that same change.
    """
    from omniagentos.integration import verdicts

    # No public unwired surface.
    assert not hasattr(verdicts, "parse_verdict")
    assert not hasattr(verdicts, "reviewer_lineage_ok")
    assert set(verdicts.__all__) == {
        "Decision",
        "ParsedVerdict",
        "prose_names_anthropic",
        "_LEGACY_ANTHROPIC_PROSE_MARKERS",
    }
    # The capability itself stays, behaviour pinned, ready for T5.
    assert callable(verdicts._parse_verdict)
    assert callable(verdicts._reviewer_lineage_ok)
