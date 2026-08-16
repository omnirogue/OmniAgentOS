"""Direct tests for the operator-decision parser's authority semantics.

Each case is a reviewer-reproduced fail-open from PR #236: a first-verb-wins
classification let an approval sentence swallow a rejection or a prohibition
phrased in the same sentence.
"""

from __future__ import annotations

import hashlib

from omniagentos.packgovernance.checksum import bind_bytes
from omniagentos.packgovernance.contracts import (
    ArtifactKind,
    ConstraintKind,
    DispositionKind,
    SideEffect,
)
from omniagentos.packgovernance.decision import parse_operator_decision


def _decision(directives: str):
    text = (
        "Target repository: `Globex/OmniAgentOS`\n\nDecision:\n"
        + directives
        + "\n"
    )
    payload = text.encode()
    artifact = bind_bytes(
        payload,
        path="decision.md",
        kind=ArtifactKind.OPERATOR_AUTHORITY,
        declared_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return parse_operator_decision(artifact)


def test_rejection_inside_approval_sentence_still_rejects() -> None:
    decision = _decision("- Approve A1, but reject A2.")
    approved = decision.disposition_for("A1")
    rejected = decision.disposition_for("A2")
    assert approved is not None and approved.kind is DispositionKind.APPROVED
    assert rejected is not None and rejected.kind is DispositionKind.REJECTED


def test_prohibition_inside_approval_sentence_is_retained() -> None:
    decision = _decision("- Approve A1, but A1 must not push.")
    approved = decision.disposition_for("A1")
    assert approved is not None and approved.kind is DispositionKind.APPROVED
    assert SideEffect.REPO_BRANCH_PUSH in decision.forbidden_side_effects_for("A1")
    assert decision.constraints_of(ConstraintKind.ITEM_PROHIBITION)


def test_approve_and_reject_same_item_conflicts_and_fails_closed() -> None:
    decision = _decision("- Approve A1, but reject A1.")
    assert decision.has_conflict("A1")
    resolved = decision.disposition_for("A1")
    assert resolved is not None and resolved.kind is DispositionKind.REJECTED


def test_plain_conjunction_of_items_is_not_split() -> None:
    decision = _decision("- Reject A1 and A2.")
    for item_id in ("A1", "A2"):
        entry = decision.disposition_for(item_id)
        assert entry is not None and entry.kind is DispositionKind.REJECTED
    assert not decision.unclassified_directives


def test_unrecognised_clause_stays_visible() -> None:
    decision = _decision("- Approve A1, but proceed carefully.")
    approved = decision.disposition_for("A1")
    assert approved is not None and approved.kind is DispositionKind.APPROVED
    assert any("proceed carefully" in entry for entry in decision.unclassified_directives)
