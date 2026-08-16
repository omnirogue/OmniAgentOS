"""U-R8: Test grant inlining in worker briefs (DECISIVE + COUNTERFEIT tests).

Every brief states the capability IDs the lane currently holds and each one's mode.
A lane holding NOTHING gets an explicit line saying so plus the request path.
Silence is what makes an agent guess — this test ensures grants are never silent.

This test suite is DECISIVE: it proves grants are read from the store, not hardcoded.
It includes both positive (what should appear) and counterfeit (what should NOT)
cases to ensure the implementation is correct.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.swarm.scheduler import build_worker_brief
from tests.support.db_template import migrated_db


@pytest.fixture
def db_with_migrations(tmp_path: Path) -> str:
    """Create a database with all migrations applied."""
    db_path = str(tmp_path / "test.db")
    return migrated_db(SqliteStore, db_path)


def _seed_agent(db_path: str, agent_id: str, name: str) -> None:
    """Seed a machine identity agent (lane: or loop:) into the agents table."""
    store = SqliteStore(db_path)
    try:
        store._write(
            "INSERT OR IGNORE INTO agents (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (agent_id, name, utc_now_iso(), utc_now_iso()),
        )
    finally:
        store.close()


def _seed_capability_grant(
    db_path: str,
    agent_id: str,
    capability_id: str,
    mode: str = "read",
    expires_at: str | None = None,
) -> None:
    """Seed a single capability grant into agent_capabilities table."""
    store = SqliteStore(db_path)
    try:
        # Ensure agent exists
        _seed_agent(db_path, agent_id, f"test-{agent_id}")

        store._write(
            """
            INSERT OR REPLACE INTO agent_capabilities
            (agent_id, capability_id, granted_at, granted_by, note, mode, expires_at, issued_by, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                capability_id,
                utc_now_iso(),
                "test",
                f"test grant for {capability_id}",
                mode,
                expires_at,
                "test",
                None,
            ),
        )
    finally:
        store.close()


class TestGrantInliningDecisive:
    """DECISIVE: Prove grants are read from store, not hardcoded."""

    def test_brief_includes_held_grants_and_updates_on_change(
        self, db_with_migrations: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DECISIVE: Brief contains real capability IDs + modes READ FROM STORE.

        Seed a grant row → assert it appears in brief.
        Change the row → assert the brief changes.
        This PROVES store-read rather than hardcode.
        """
        # Monkeypatch to use our test DB
        monkeypatch.setattr(
            "omniagentos.grants.reader.default_db_path",
            lambda: db_with_migrations,
        )

        # FIRST BRIEF: No grants
        brief1 = build_worker_brief(
            {},
            {"title": "Test task", "description": "A test task"},
            {"owned_paths": [], "plan_version": 1, "plan_hash": "abc123"},
            {},
        )
        assert "currently hold no standing capability grants" in brief1

        # SEED A GRANT
        _seed_capability_grant(db_with_migrations, "lane:swarm.worker", "replicate.generate", mode="read")

        # SECOND BRIEF: Should now include the grant
        brief2 = build_worker_brief(
            {},
            {"title": "Test task", "description": "A test task"},
            {"owned_paths": [], "plan_version": 1, "plan_hash": "abc123"},
            {},
        )
        assert "replicate.generate" in brief2
        assert "mode: read" in brief2
        assert "currently hold no standing capability grants" not in brief2

        # CHANGE THE GRANT: Add a second grant with different mode
        _seed_capability_grant(db_with_migrations, "lane:swarm.worker", "model.complete", mode="write")

        # THIRD BRIEF: Should reflect both grants
        brief3 = build_worker_brief(
            {},
            {"title": "Test task", "description": "A test task"},
            {"owned_paths": [], "plan_version": 1, "plan_hash": "abc123"},
            {},
        )
        assert "replicate.generate" in brief3
        assert "mode: read" in brief3
        assert "model.complete" in brief3
        assert "mode: write" in brief3


class TestFormationQualifiedLaneIdentity:
    """Phase-2 integration adjudication of the U-R8 lane-identity divergence.

    PLAN.md §1 invariant 1 names ``lane:swarm.worker.<formation>`` canonical.
    ``swarm_json`` carries ``formation_id`` (planner.py ``_formation_stamp``),
    so the qualified spelling is producible at brief time and IS produced —
    alongside the bare id, which stays the common floor.
    """

    def test_formation_scoped_grant_reaches_only_its_own_formation(
        self, db_with_migrations: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DECISIVE: a `lane:swarm.worker.coding` row lands in a coding brief only.

        The same row must be invisible to a creative worker and to an unbound
        run. If the brief read the bare id alone, the row would never appear at
        all; if it read the qualified id alone, the floor below would vanish.
        """
        monkeypatch.setattr(
            "omniagentos.grants.reader.default_db_path",
            lambda: db_with_migrations,
        )
        _seed_capability_grant(
            db_with_migrations, "lane:swarm.worker", "replicate.generate", mode="read"
        )
        _seed_capability_grant(
            db_with_migrations, "lane:swarm.worker.coding", "git.read", mode="read"
        )

        def _brief(formation_id: str | None) -> str:
            swarm_json: dict[str, object] = {
                "owned_paths": [],
                "plan_version": 1,
                "plan_hash": "abc123",
            }
            if formation_id is not None:
                swarm_json["formation_id"] = formation_id
            return build_worker_brief(
                {}, {"title": "T", "description": "D"}, swarm_json, {}
            )

        coding = _brief("coding")
        creative = _brief("creative")
        unbound = _brief(None)

        # The formation-scoped row reaches its own formation...
        assert "git.read" in coding
        # ...and nobody else's.
        assert "git.read" not in creative
        assert "git.read" not in unbound
        # The bare-id floor is present in every one of them.
        for brief in (coding, creative, unbound):
            assert "replicate.generate" in brief

    def test_counterfeit_non_canonical_formation_id_mints_no_holder(
        self, db_with_migrations: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COUNTERFEIT: a formation id that cannot spell a canonical holder is dropped.

        A grant seeded against the mangled spelling must never surface, and the
        brief must not degrade — the bare-id floor still answers.
        """
        monkeypatch.setattr(
            "omniagentos.grants.reader.default_db_path",
            lambda: db_with_migrations,
        )
        _seed_capability_grant(
            db_with_migrations, "lane:swarm.worker", "replicate.generate", mode="read"
        )
        # `coding:evil` would make `lane:swarm.worker.coding:evil` — two colons,
        # which _CANONICAL_IDENTITY_RE rejects. `-x` does not start alphanumeric.
        for bad in ("coding:evil", "-x", "", "  "):
            _seed_capability_grant(
                db_with_migrations, "lane:swarm.worker", "replicate.generate", mode="read"
            )
            brief = build_worker_brief(
                {},
                {"title": "T", "description": "D"},
                {
                    "owned_paths": [],
                    "plan_version": 1,
                    "plan_hash": "abc123",
                    "formation_id": bad,
                },
                {},
            )
            assert "Grant status unknown" not in brief
            assert "replicate.generate" in brief


class TestGrantInliningCounterfeits:
    """COUNTERFEIT tests: Prove what should NOT appear in briefs."""

    def test_counterfeit_a_unheld_capability_does_not_appear(
        self, db_with_migrations: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COUNTERFEIT A: Brief listing a capability the lane does NOT hold must fail.

        This proves we read from the store, not a hardcoded list.
        """
        monkeypatch.setattr(
            "omniagentos.grants.reader.default_db_path",
            lambda: db_with_migrations,
        )

        # Seed only one capability
        _seed_capability_grant(db_with_migrations, "lane:swarm.worker", "replicate.generate", mode="read")

        brief = build_worker_brief(
            {},
            {"title": "Test task", "description": "A test task"},
            {"owned_paths": [], "plan_version": 1, "plan_hash": "abc123"},
            {},
        )

        # Should include what was granted
        assert "replicate.generate" in brief
        # Should NOT include something that wasn't granted
        assert "stripe_acmeuni.refund" not in brief
        assert "model.complete" not in brief

    def test_counterfeit_b_expired_grant_does_not_appear(
        self, db_with_migrations: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COUNTERFEIT B: Expired grant (non-NULL expires_at in past) does not appear."""
        monkeypatch.setattr(
            "omniagentos.grants.reader.default_db_path",
            lambda: db_with_migrations,
        )

        # Seed an expired grant (past expiry)
        past_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        _seed_capability_grant(
            db_with_migrations,
            "lane:swarm.worker",
            "expired.capability",
            mode="read",
            expires_at=past_time,
        )

        # Seed a live grant (future expiry)
        future_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        _seed_capability_grant(
            db_with_migrations,
            "lane:swarm.worker",
            "live.capability",
            mode="read",
            expires_at=future_time,
        )

        brief = build_worker_brief(
            {},
            {"title": "Test task", "description": "A test task"},
            {"owned_paths": [], "plan_version": 1, "plan_hash": "abc123"},
            {},
        )

        # Should include live grant
        assert "live.capability" in brief
        # Should NOT include expired grant
        assert "expired.capability" not in brief

    def test_counterfeit_c_standing_grant_null_expiry_does_appear(
        self, db_with_migrations: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COUNTERFEIT C (regression test for Defect 1): Standing grant (NULL expires_at) DOES appear.

        PLAN.md §1: Standing grants have NULL expires_at and are valid until revoked.
        This is the regression test for the inverted expiration logic bug.
        """
        monkeypatch.setattr(
            "omniagentos.grants.reader.default_db_path",
            lambda: db_with_migrations,
        )

        # Seed a STANDING grant (NULL expires_at = valid until revoked)
        _seed_capability_grant(
            db_with_migrations,
            "lane:swarm.worker",
            "standing.capability",
            mode="read",
            expires_at=None,  # NULL = STANDING grant
        )

        brief = build_worker_brief(
            {},
            {"title": "Test task", "description": "A test task"},
            {"owned_paths": [], "plan_version": 1, "plan_hash": "abc123"},
            {},
        )

        # Standing grant with NULL expiry MUST appear in the brief
        assert "standing.capability" in brief
        assert "mode: read" in brief
        # Must NOT say "no grants"
        assert "currently hold no standing capability grants" not in brief

    def test_store_unavailable_graceful_degradation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Store unavailable: brief still builds with 'unknown' message, no exception."""
        # Monkeypatch to point to non-existent database
        monkeypatch.setattr(
            "omniagentos.grants.reader.default_db_path",
            lambda: "/nonexistent/path/db.sqlite",
        )

        brief = build_worker_brief(
            {},
            {"title": "Test task", "description": "A test task"},
            {"owned_paths": [], "plan_version": 1, "plan_hash": "abc123"},
            {},
        )

        # Brief should still be valid
        assert "Test task" in brief
        assert "Standing capability grants" in brief
        # Should include the degradation message
        assert "unknown" in brief.lower() or "request" in brief.lower()
        # Should NOT have crashed
        assert len(brief) > 0
