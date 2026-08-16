"""Tests for memory module (W2).

Covers: search by signature/title/root-cause, failure/rejection flags, lessons writeback,
skill proposal shape.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate_connection
from omniagentos.reliability.memory import (
    propose_confirmed_fix,
    search_improvements,
    writeback_lessons,
)
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import ImprovementStatus


@pytest.fixture
def tmp_db():
    """Create a temporary SQLite database and run migrations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        conn.close()

        store = SqliteReliabilityStore(str(db_path))
        yield store
        if hasattr(store, "_connection"):
            store._connection.close()


def test_search_improvements_empty(tmp_db):
    """Searching with no matches should return empty list."""
    results = search_improvements(tmp_db, signature="nonexistent_sig")
    assert results == []


def test_search_improvements_by_signature(tmp_db):
    """Search should match improvements by signature in title or root_cause."""
    # Create improvements
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Fix: rate_limit_handler logic",
        root_cause="rate_limit",
    )

    results = search_improvements(tmp_db, signature="rate_limit")

    assert len(results) > 0
    assert any(r["id"] == imp_id for r in results)


def test_search_improvements_by_title_keywords(tmp_db):
    """Search should match improvements by title keywords."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Fix: timeout handling in executor",
        summary="Increase timeout threshold for long-running tasks",
    )

    results = search_improvements(tmp_db, title_keywords=["timeout"])

    assert len(results) > 0
    assert any(r["id"] == imp_id for r in results)


def test_search_improvements_by_root_cause(tmp_db):
    """Search should match improvements by root_cause."""
    imp_id = tmp_db.create_improvement(
        origin="audit",
        kind="fix",
        title="Authentication flow fix",
        root_cause="transient",
    )

    results = search_improvements(tmp_db, root_cause="transient")

    assert len(results) > 0
    assert any(r["id"] == imp_id for r in results)


def test_search_improvements_failed_before_flag(tmp_db):
    """Failed improvements should have failed_before flag."""
    # Create a failed improvement
    imp_rolled_back = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Rolled back fix",
    )
    # Transition to rolled_back status
    tmp_db.transition_improvement(
        imp_rolled_back,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.ROLLED_BACK.value,
        actor="test",
    )

    results = search_improvements(tmp_db, title_keywords=["rolled"])

    assert len(results) > 0
    result = results[0]
    assert result["id"] == imp_rolled_back
    assert result["failed_before"] is True
    assert result["rejected_before"] is False


def test_search_improvements_rejected_before_flag(tmp_db):
    """Rejected improvements should have rejected_before flag."""
    imp_rejected = tmp_db.create_improvement(
        origin="human",
        kind="docs",
        title="Documentation update",
    )
    # Transition to rejected
    tmp_db.transition_improvement(
        imp_rejected,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.REJECTED.value,
        actor="test",
    )

    results = search_improvements(tmp_db, title_keywords=["documentation"])

    assert len(results) > 0
    result = results[0]
    assert result["id"] == imp_rejected
    assert result["rejected_before"] is True
    assert result["failed_before"] is False


def test_search_improvements_multiple_filters(tmp_db):
    """Search with multiple filters should AND them."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Performance optimization: cache layer",
        root_cause="architectural",
    )

    # Search with both signature and root_cause
    results = search_improvements(tmp_db, signature="cache", root_cause="architectural")

    assert len(results) > 0
    assert any(r["id"] == imp_id for r in results)


def test_search_improvements_all_recent(tmp_db):
    """Without filters, search should return recent improvements."""
    tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Fix 1",
    )
    tmp_db.create_improvement(
        origin="audit",
        kind="optimization",
        title="Optimization 1",
    )

    results = search_improvements(tmp_db)

    # Should return recent improvements
    assert len(results) >= 2


def _transition_to_confirmed(tmp_db, imp_id: str) -> None:
    """Walk the improvement through the production status path to CONFIRMED."""
    path = [
        ImprovementStatus.TESTING.value,
        ImprovementStatus.JUDGING.value,
        ImprovementStatus.APPROVED.value,
        ImprovementStatus.APPLYING.value,
        ImprovementStatus.APPLIED.value,
        ImprovementStatus.MONITORING.value,
        ImprovementStatus.CONFIRMED.value,
    ]
    current = ImprovementStatus.PROPOSED.value
    for nxt in path:
        tmp_db.transition_improvement(imp_id, current, nxt, actor="test")
        current = nxt


def test_writeback_lessons_writes_isolated_jsonl(
    tmp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-15/L-11: writeback must persist lessons under isolated OMNIAGENTOS_VAR.

    The previous test only asserted "does not raise", which passed while
    fabricated lessons leaked into the checkout's gitignored var/reflexion
    corpus whenever OMNIAGENTOS_VAR was unset (cwd-relative ``var/``).
    """
    import json

    isolated = tmp_path / "runtime-var"
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(isolated))
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_HOME", raising=False)

    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Confirmed fix",
        summary="A fix that worked",
        root_cause="transient",
    )
    _transition_to_confirmed(tmp_db, imp_id)

    writeback_lessons(tmp_db, imp_id)

    lesson_path = isolated / "reflexion" / "lessons.jsonl"
    assert lesson_path.is_file(), "lessons must be written under OMNIAGENTOS_VAR"
    lines = [ln for ln in lesson_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1

    payload = json.loads(lines[0])
    assert payload["improvement_id"] == imp_id
    assert payload["title"] == "Confirmed fix"
    assert payload["root_cause"] == "transient"
    # Must not have written into the repo-relative default var tree for this test.
    repo_leak = Path(__file__).resolve().parents[2] / "var" / "reflexion" / "lessons.jsonl"
    if repo_leak.exists():
        leaked = repo_leak.read_text(encoding="utf-8")
        assert imp_id not in leaked


def test_propose_confirmed_fix_writes_isolated_skill_proposal(
    tmp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L-15/L-11: propose_confirmed_fix must write under isolated OMNIAGENTOS_VAR.

    Replaces the vacuous "returns a non-empty string" assertion that previously
    leaked fabricated skill proposals into operator/runtime ``var/reflexion``.
    """
    import json

    isolated = tmp_path / "propose-var"
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(isolated))
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_HOME", raising=False)

    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Confirmed fix for submission",
        summary="Isolated proposal body",
        root_cause="transient",
    )
    _transition_to_confirmed(tmp_db, imp_id)

    result = propose_confirmed_fix(tmp_db, imp_id)
    assert result is not None
    assert isinstance(result, str)
    assert result.startswith("skprop_")

    proposal_path = isolated / "reflexion" / "skill_proposals.jsonl"
    assert proposal_path.is_file(), "skill proposals must land under OMNIAGENTOS_VAR"
    lines = [ln for ln in proposal_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["id"] == result
    assert payload["improvement_id"] == imp_id
    assert payload["title"] == "Confirmed fix for submission"
    assert payload["status"] == "pending_review"

    # No leak into the checkout package-anchored var tree.
    repo_leak = Path(__file__).resolve().parents[2] / "var" / "reflexion" / "skill_proposals.jsonl"
    if repo_leak.exists():
        leaked = repo_leak.read_text(encoding="utf-8")
        assert imp_id not in leaked
        assert result not in leaked


def test_propose_confirmed_fix_not_fix_kind(
    tmp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """propose_confirmed_fix should not propose non-fix kinds."""
    isolated = tmp_path / "propose-var"
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(isolated))
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)

    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="docs",  # Not a fix
        title="Documentation update",
    )

    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.CONFIRMED.value,
        actor="test",
    )

    result = propose_confirmed_fix(tmp_db, imp_id)
    assert result is None
    assert not (isolated / "reflexion" / "skill_proposals.jsonl").exists()


def test_propose_confirmed_fix_not_confirmed_status(
    tmp_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """propose_confirmed_fix should only work for confirmed status."""
    isolated = tmp_path / "propose-var"
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(isolated))
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)

    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Fix not yet confirmed",
    )

    # Keep in proposed status
    result = propose_confirmed_fix(tmp_db, imp_id)
    assert result is None
    assert not (isolated / "reflexion" / "skill_proposals.jsonl").exists()


def test_search_improvements_deduplicates_ids(tmp_db):
    """Search results should not duplicate improvement IDs."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Fix with multiple keywords: cache timeout handler",
        root_cause="transient",
    )

    # Search with keywords that both match the same improvement
    results = search_improvements(tmp_db, title_keywords=["cache", "timeout"])

    # Should only appear once
    matching = [r for r in results if r["id"] == imp_id]
    assert len(matching) == 1


def test_search_improvements_status_preserved(tmp_db):
    """Improvement status should be preserved in search results."""
    tmp_db.create_improvement(
        origin="audit",
        kind="optimization",
        title="Performance improvement",
    )

    results = search_improvements(tmp_db, title_keywords=["performance"])

    assert len(results) > 0
    result = results[0]
    assert result["status"] == ImprovementStatus.PROPOSED.value
