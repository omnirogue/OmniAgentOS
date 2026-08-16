"""Diff-only UPDATE proposals must actually mutate content on approval.

Regression coverage for the favourable-absence defect: for an UPDATE proposal
carrying ``proposed_diff`` but ``proposed_content=None`` (a legal shape —
:func:`propose_update` only raises when BOTH are ``None``), approval used to
copy ``content_snapshot`` through UNCHANGED and archive the diff merely as
evidence — a versioned, approval-stamped no-op.
"""

from __future__ import annotations

import difflib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Imported at MODULE level on purpose: tests/conftest.py::_bypass_global_auth
# can only install its session-token override when importing this module has
# already loaded the ASGI app (a function-scoped import lands too late and the
# request 401s instead of exercising the route).
from omniagentos.api.main import app
from omniagentos.skills import (
    decide_proposal,
    get_skill,
    list_proposals,
    propose_update,
    upsert_skill,
)

CONTENT_V1 = "line one\nline two\nline three\n"
CONTENT_V2 = "line one\nline TWO CHANGED\nline three\n"

# A clean unified diff turning CONTENT_V1 into CONTENT_V2.
DIFF_V1_TO_V2 = (
    "--- a/skill.md\n"
    "+++ b/skill.md\n"
    "@@ -1,3 +1,3 @@\n"
    " line one\n"
    "-line two\n"
    "+line TWO CHANGED\n"
    " line three\n"
)

# A diff whose context/removal lines no longer match CONTENT_V1 (drifted).
DIFF_STALE_CONTEXT = (
    "--- a/skill.md\n"
    "+++ b/skill.md\n"
    "@@ -1,3 +1,3 @@\n"
    " line one\n"
    "-line TWO NO LONGER PRESENT\n"
    "+line TWO CHANGED\n"
    " line three\n"
)


def _pending_ids() -> set[str]:
    return {proposal["id"] for proposal in list_proposals(state="pending")}


def _seed_skill(slug: str, content: str = CONTENT_V1) -> str:
    return upsert_skill(
        {
            "slug": slug,
            "category": "Testing",
            "subcategory": "DiffOnly",
            "title": slug,
            "content_snapshot": content,
        }
    )


def test_diff_only_update_actually_mutates_content(
    skills_environment: tuple[Path, Path],
) -> None:
    """Headline regression: approving a diff-only proposal must land the diff."""
    skill_id = _seed_skill("diff-only-headline")
    assert get_skill(skill_id)["version"]["content_snapshot"] == CONTENT_V1

    proposal_id = propose_update(
        skill_id,
        proposed_content=None,
        proposed_diff=DIFF_V1_TO_V2,
        risk="major",
        created_by="test",
    )
    result = decide_proposal(
        proposal_id, approve=True, note="approve diff-only", decided_by="tester"
    )

    assert result["state"] == "approved"
    assert result["new_version"] == 2

    skill = get_skill(skill_id)
    assert skill["current_version"] == 2
    assert skill["version"]["content_snapshot"] == CONTENT_V2, (
        "approving a diff-only proposal must apply the diff, not copy the "
        "unchanged current snapshot through as a new version"
    )
    # Provenance: the applied diff's digest is recorded on the version.
    assert skill["version"]["evidence"]["applied_diff_sha256"]


def test_identity_diff_refuses_instead_of_minting_a_no_op(
    skills_environment: tuple[Path, Path],
) -> None:
    """A diff that applies cleanly but changes nothing is still a no-op — refuse it.

    An applier that "succeeded" is not evidence that anything improved. The
    invariant is about the RESULT, not about which code path produced it.
    """
    skill_id = _seed_skill("diff-only-identity", content="unchanged\n")
    identity_diff = (
        "--- a/skill.md\n"
        "+++ b/skill.md\n"
        "@@ -1,1 +1,1 @@\n"
        "-unchanged\n"
        "+unchanged\n"
    )
    proposal_id = propose_update(
        skill_id,
        proposed_content=None,
        proposed_diff=identity_diff,
        risk="major",
        created_by="test",
    )
    with pytest.raises(ValueError, match="identical to the current version"):
        decide_proposal(proposal_id, approve=True, decided_by="tester")

    skill = get_skill(skill_id)
    assert skill["current_version"] == 1
    assert len(skill["versions"]) == 1
    assert _pending_ids() == {proposal_id}


def test_identical_proposed_content_refuses_instead_of_minting_a_no_op(
    skills_environment: tuple[Path, Path],
) -> None:
    """The invariant holds for the proposed_content path too, not just for diffs."""
    skill_id = _seed_skill("identical-content")
    proposal_id = propose_update(
        skill_id,
        proposed_content=CONTENT_V1,
        risk="major",
        created_by="test",
    )
    with pytest.raises(ValueError, match="identical to the current version"):
        decide_proposal(proposal_id, approve=True, decided_by="tester")

    skill = get_skill(skill_id)
    assert skill["current_version"] == 1
    assert len(skill["versions"]) == 1


def test_blank_diff_with_no_content_is_refused_at_submission(
    skills_environment: tuple[Path, Path],
) -> None:
    """Nothing proposed, nothing queued: the junk never reaches the approval feed."""
    skill_id = _seed_skill("blank-diff-submission")
    for blank in ("", "   \n\t "):
        with pytest.raises(ValueError, match="non-empty proposed_diff"):
            propose_update(
                skill_id,
                proposed_content=None,
                proposed_diff=blank,
                risk="low",
                created_by="test",
            )
    assert list_proposals() == []
    skill = get_skill(skill_id)
    assert skill["current_version"] == 1
    assert len(skill["versions"]) == 1


def test_curator_no_change_shape_never_mints_a_version(
    skills_environment: tuple[Path, Path],
) -> None:
    """The production shape, spelled out.

    ``selfimprove/curator.py::_proposed_diff`` is ``difflib.unified_diff``,
    which returns the EMPTY STRING when the learning content already equals the
    skill's content. That went to ``_risk_for_update`` -> ``low`` -> auto-approve,
    and minted an approval-stamped version identical to the previous one on
    every curator pass over an already-current skill.
    """
    skill_id = _seed_skill("curator-no-change")
    curator_diff = "".join(
        difflib.unified_diff(
            CONTENT_V1.splitlines(keepends=True),
            CONTENT_V1.splitlines(keepends=True),
            fromfile="current",
            tofile="proposed",
        )
    )
    assert curator_diff == ""

    with pytest.raises(ValueError, match="non-empty proposed_diff"):
        propose_update(
            skill_id,
            proposed_content=None,
            proposed_diff=curator_diff,
            risk="low",
            created_by="curator",
        )

    skill = get_skill(skill_id)
    assert skill["current_version"] == 1
    assert len(skill["versions"]) == 1


def test_legacy_blank_diff_row_is_refused_at_decision(
    skills_environment: tuple[Path, Path],
) -> None:
    """Rows written before the submission check still cannot approve into a no-op."""
    skill_id = _seed_skill("legacy-blank-diff")
    db_path = skills_environment[0]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO update_proposals "
            "(id, skill_id, proposed_diff, proposed_content, evidence_files_json, "
            "linked_execution_id, risk, state, created_by, created_at) "
            "VALUES ('upd_legacy_blank', ?, '', NULL, '[]', NULL, 'low', 'pending', "
            "'curator', '2026-01-01T00:00:00Z')",
            (skill_id,),
        )
        connection.commit()

    with pytest.raises(ValueError, match="nothing to apply"):
        decide_proposal("upd_legacy_blank", approve=True, decided_by="tester")

    skill = get_skill(skill_id)
    assert skill["current_version"] == 1
    assert len(skill["versions"]) == 1
    assert _pending_ids() == {"upd_legacy_blank"}


def test_low_risk_auto_approval_refusal_leaves_proposal_pending_and_visible(
    skills_environment: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused auto-approval must not approve, and must not vanish either.

    ``propose_update`` commits the proposal before auto-approving, so letting
    the refusal escape would abandon a pending row whose id the caller never
    learns. It stays pending and the refusal is announced in the same approval
    feed the proposal was announced in.
    """
    skill_id = _seed_skill("low-risk-refusal")
    with caplog.at_level("WARNING"):
        proposal_id = propose_update(
            skill_id,
            proposed_content=None,
            proposed_diff=DIFF_STALE_CONTEXT,
            risk="low",
            created_by="curator",
        )

    assert _pending_ids() == {proposal_id}
    assert "automatic low-risk approval refused" in caplog.text

    skill = get_skill(skill_id)
    assert skill["current_version"] == 1
    assert skill["version"]["content_snapshot"] == CONTENT_V1
    assert len(skill["versions"]) == 1

    with sqlite3.connect(skills_environment[0]) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT title, severity, payload_json FROM notifications WHERE ref_id = ? "
            "ORDER BY created_at",
            (proposal_id,),
        ).fetchall()
    refusals = [row for row in rows if row["title"] == "Automatic approval refused"]
    assert len(refusals) == 1
    assert refusals[0]["severity"] == "warning"
    assert "does not apply cleanly" in refusals[0]["payload_json"]


def test_diff_only_update_refuses_when_diff_does_not_apply_cleanly(
    skills_environment: tuple[Path, Path],
) -> None:
    """Fail-closed branch (a): context drift since the proposal was filed."""
    skill_id = _seed_skill("diff-only-stale-context")
    proposal_id = propose_update(
        skill_id,
        proposed_content=None,
        proposed_diff=DIFF_STALE_CONTEXT,
        risk="major",
        created_by="test",
    )

    with pytest.raises(ValueError, match="does not apply cleanly"):
        decide_proposal(proposal_id, approve=True, decided_by="tester")

    # No version minted, skill content untouched, proposal stays undecided.
    skill = get_skill(skill_id)
    assert skill["current_version"] == 1
    assert skill["version"]["content_snapshot"] == CONTENT_V1
    assert len(skill["versions"]) == 1
    pending = {p["id"]: p for p in list_proposals(state="pending")}
    assert proposal_id in pending
    assert pending[proposal_id]["decided_at"] is None


def test_diff_only_update_refuses_when_current_snapshot_is_none(
    skills_environment: tuple[Path, Path], tmp_path: Path
) -> None:
    """Fail-closed branch (b): nothing to patch against.

    ``skill_versions.content_snapshot`` is NOT NULL by schema, so the only
    reachable "nothing to patch against" state is a ``skills.current_version``
    pointer with no matching ``skill_versions`` row (the JOIN in
    ``decide_proposal`` then resolves ``current`` to ``None``) — e.g. a row
    corrupted independently of the sanctioned write paths.
    """
    skill_id = _seed_skill("diff-only-no-snapshot")

    db_path = skills_environment[0]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DELETE FROM skill_versions WHERE skill_id = ? AND version = 1",
            (skill_id,),
        )
        connection.commit()

    proposal_id = propose_update(
        skill_id,
        proposed_content=None,
        proposed_diff=DIFF_V1_TO_V2,
        risk="major",
        created_by="test",
    )

    with pytest.raises(ValueError, match="no current content_snapshot"):
        decide_proposal(proposal_id, approve=True, decided_by="tester")

    assert _pending_ids() == {proposal_id}


def test_both_proposed_content_and_diff_prefers_content_and_logs_mismatch(
    skills_environment: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """Fail-closed branch (c): proposed_content wins; a mismatch is logged, not silent."""
    skill_id = _seed_skill("diff-only-both-present")
    mismatched_content = "totally different content, unrelated to the diff\n"

    proposal_id = propose_update(
        skill_id,
        proposed_content=mismatched_content,
        proposed_diff=DIFF_V1_TO_V2,
        risk="major",
        created_by="test",
    )
    with caplog.at_level("WARNING"):
        result = decide_proposal(proposal_id, approve=True, decided_by="tester")

    assert result["state"] == "approved"
    skill = get_skill(skill_id)
    # proposed_content wins per existing precedence.
    assert skill["version"]["content_snapshot"] == mismatched_content
    assert "does not reconcile" in caplog.text


def test_both_proposed_content_and_diff_agree_no_mismatch_logged(
    skills_environment: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """When content and diff agree, no mismatch warning fires."""
    skill_id = _seed_skill("diff-only-both-agree")

    proposal_id = propose_update(
        skill_id,
        proposed_content=CONTENT_V2,
        proposed_diff=DIFF_V1_TO_V2,
        risk="major",
        created_by="test",
    )
    with caplog.at_level("WARNING"):
        result = decide_proposal(proposal_id, approve=True, decided_by="tester")

    assert result["state"] == "approved"
    assert get_skill(skill_id)["version"]["content_snapshot"] == CONTENT_V2
    assert "does not reconcile" not in caplog.text


def test_api_seam_refuses_blank_diff_and_no_op_approval(
    skills_environment: tuple[Path, Path],
) -> None:
    """The HTTP seam inherits the refusal: 400, not a 500 and not a silent no-op.

    ``omniagentos/skills/models.py::UpdateProposalCreate`` still admits
    ``proposed_diff=""`` (it only rejects BOTH fields being ``None``), so the
    DAL is the chokepoint that has to hold — this is the test that says so by
    execution rather than by reading the model.
    """
    skill_id = _seed_skill("api-seam-diff-only")
    with TestClient(app) as client:
        blank = client.post(
            f"/api/skills/{skill_id}/propose",
            json={"proposed_diff": "", "risk": "low"},
        )
        assert blank.status_code == 400, blank.text
        assert "non-empty proposed_diff" in blank.text

        identical = client.post(
            f"/api/skills/{skill_id}/propose",
            json={"proposed_content": CONTENT_V1, "risk": "major"},
        )
        assert identical.status_code == 200, identical.text
        decision = client.post(
            f"/api/updates/{identical.json()}/decide",
            json={"approve": True, "decided_by": "tester"},
        )
        assert decision.status_code == 400, decision.text
        assert "identical to the current version" in decision.text

    skill = get_skill(skill_id)
    assert skill["current_version"] == 1
    assert len(skill["versions"]) == 1
