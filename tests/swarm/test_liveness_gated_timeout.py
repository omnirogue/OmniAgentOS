"""Tests for liveness-gated timeout: distinguishing SLOW from STALLED.

Liveness signal allows the scheduler to differentiate between:
- SLOW: work is still emitting output (keep alive, however long it takes)
- STALLED: no output within idle threshold (kill is justified, escalate to costlier tier)

Unlike wall-clock timeout which treats all elapsed time equally, liveness-gated
timeout only kills when there is genuine evidence of stalling (no progress).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import new_id
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.provider_exec import is_making_progress
from tests.support.db_template import migrated_db


def _iso_ts(dt: datetime) -> str:
    """Convert datetime to ISO 8601 string."""
    return dt.isoformat()


def _now_utc() -> datetime:
    """Return current UTC time."""
    return datetime.now(UTC)


def _add_seconds(dt: datetime, seconds: float) -> datetime:
    """Add seconds to a datetime."""
    return dt + timedelta(seconds=seconds)


@pytest.fixture
def harness(tmp_path: Path) -> tuple[SessionsDal, str]:
    """Create a test database and SessionsDal."""
    db = str(tmp_path / "liveness-test.db")
    db = migrated_db(CollabStore, db)
    dal = SessionsDal(db)
    return dal, db


class TestLivenessBasics:
    """Core liveness distinction tests."""

    def test_recently_active_session_is_not_stalled(self, harness: tuple[SessionsDal, str]) -> None:
        """A session that emitted within the idle window is SLOW, not STALLED."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        # Create a session that was last active 5 seconds ago.
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test",
                "last_activity_at": _iso_ts(_add_seconds(now, -5)),
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        # Check with a 30-second idle threshold. Session is within threshold.
        result = is_making_progress(
            session_id,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        assert result["is_making_progress"] is True
        assert result["status"] == "slow"
        assert result["last_activity_seconds_ago"] == 5.0
        assert "keep alive" in result["details"].lower()

    def test_stalled_session_within_threshold(self, harness: tuple[SessionsDal, str]) -> None:
        """A session with no activity for longer than threshold is STALLED."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        # Create a session that was last active 45 seconds ago.
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "grok",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test",
                "last_activity_at": _iso_ts(_add_seconds(now, -45)),
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        # Check with a 30-second idle threshold. Session has stalled.
        result = is_making_progress(
            session_id,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        assert result["is_making_progress"] is False
        assert result["status"] == "stalled"
        assert result["last_activity_seconds_ago"] == 45.0
        assert "stalled" in result["details"].lower()
        assert "kill justified" in result["details"].lower()

    def test_activity_at_exact_threshold_is_not_stalled(self, harness: tuple[SessionsDal, str]) -> None:
        """A session active exactly at the threshold is still SLOW, not STALLED."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        # Create a session last active exactly 30 seconds ago.
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "gemini",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test",
                "last_activity_at": _iso_ts(_add_seconds(now, -30)),
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        # Check with a 30-second idle threshold (at boundary).
        result = is_making_progress(
            session_id,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        assert result["is_making_progress"] is True
        assert result["status"] == "slow"
        assert result["last_activity_seconds_ago"] == 30.0


class TestLivenessEdgeCases:
    """Edge cases and error handling."""

    def test_missing_session_is_unknown(self, harness: tuple[SessionsDal, str]) -> None:
        """A session that doesn't exist returns status='unknown'."""
        dal, db = harness
        now = _now_utc()
        fake_session_id = "ses_nonexistent"

        result = is_making_progress(
            fake_session_id,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        assert result["is_making_progress"] is False
        assert result["status"] == "unknown"
        assert result["last_activity_seconds_ago"] is None
        assert "not found" in result["details"].lower()

    def test_null_activity_timestamp_is_unknown(self, harness: tuple[SessionsDal, str]) -> None:
        """A session with NULL last_activity_at returns status='unknown'."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        # Create a session with NULL last_activity_at.
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test",
                "last_activity_at": None,  # Explicitly NULL
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        result = is_making_progress(
            session_id,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        assert result["is_making_progress"] is False
        assert result["status"] == "unknown"
        assert result["last_activity_seconds_ago"] is None
        assert "no recorded activity" in result["details"].lower()

    def test_malformed_timestamp_is_unknown(self, harness: tuple[SessionsDal, str]) -> None:
        """A session with malformed last_activity_at returns status='unknown'."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        # Create a session with an invalid timestamp.
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test",
                "last_activity_at": "not-a-valid-iso-timestamp",
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        result = is_making_progress(
            session_id,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        assert result["is_making_progress"] is False
        assert result["status"] == "unknown"
        assert result["last_activity_seconds_ago"] is None

    def test_negative_idle_threshold_raises(self, harness: tuple[SessionsDal, str]) -> None:
        """A negative idle threshold is rejected."""
        dal, db = harness
        session_id = new_id("ses")

        with pytest.raises(ValueError, match="idle_threshold_seconds must be positive"):
            is_making_progress(
                session_id,
                idle_threshold_seconds=-5,
                dal=dal,
            )

    def test_zero_idle_threshold_raises(self, harness: tuple[SessionsDal, str]) -> None:
        """A zero idle threshold is rejected."""
        dal, db = harness
        session_id = new_id("ses")

        with pytest.raises(ValueError, match="idle_threshold_seconds must be positive"):
            is_making_progress(
                session_id,
                idle_threshold_seconds=0,
                dal=dal,
            )

    def test_very_small_idle_threshold_works(self, harness: tuple[SessionsDal, str]) -> None:
        """Very small idle thresholds are accepted (e.g., 0.001 seconds)."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test",
                "last_activity_at": _iso_ts(now),  # Just now
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        result = is_making_progress(
            session_id,
            idle_threshold_seconds=0.001,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        # Still within threshold because activity is recent.
        assert result["is_making_progress"] is True
        assert result["status"] == "slow"

    def test_clock_skew_backwards_treated_as_recent(self, harness: tuple[SessionsDal, str]) -> None:
        """If last_activity_at is in the future, treat as recent (clock skew)."""
        dal, db = harness
        now = _now_utc()
        future = _add_seconds(now, 10)  # 10 seconds in the future
        session_id = new_id("ses")

        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test",
                "last_activity_at": _iso_ts(future),
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        result = is_making_progress(
            session_id,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        # Treat negative elapsed time as 0 (clock skew, recent activity).
        assert result["is_making_progress"] is True
        assert result["status"] == "slow"
        assert result["last_activity_seconds_ago"] == 0.0


class TestLivenessMultiProviders:
    """Test liveness across different providers."""

    @pytest.mark.parametrize("provider", ["codex", "grok", "gemini", "kimi", "qwen"])
    def test_liveness_works_for_all_providers(
        self, harness: tuple[SessionsDal, str], provider: str
    ) -> None:
        """is_making_progress works for all supported providers."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": provider,
                "project_dir": "/tmp",
                "state": "running",
                "title": f"test-{provider}",
                "last_activity_at": _iso_ts(_add_seconds(now, -10)),
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        result = is_making_progress(
            session_id,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        assert result["is_making_progress"] is True
        assert result["status"] == "slow"
        assert result["session_id"] == session_id


class TestLivenessReturnStructure:
    """Verify return value structure and completeness."""

    def test_return_dict_has_all_required_keys(self, harness: tuple[SessionsDal, str]) -> None:
        """Return dict always has all expected keys."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test",
                "last_activity_at": _iso_ts(now),
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        result = is_making_progress(
            session_id,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        required_keys = {
            "session_id",
            "is_making_progress",
            "status",
            "last_activity_seconds_ago",
            "idle_threshold_seconds",
            "details",
        }
        assert set(result.keys()) == required_keys

    def test_status_values_are_well_defined(self, harness: tuple[SessionsDal, str]) -> None:
        """Status field always has a valid, well-defined value."""
        dal, db = harness
        now = _now_utc()

        # Test slow status.
        session_slow = new_id("ses")
        dal.create_session(
            {
                "id": session_slow,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test-slow",
                "last_activity_at": _iso_ts(_add_seconds(now, -5)),
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        result_slow = is_making_progress(
            session_slow,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )
        assert result_slow["status"] in ("slow", "stalled", "unknown")
        assert result_slow["status"] == "slow"

        # Test stalled status.
        session_stalled = new_id("ses")
        dal.create_session(
            {
                "id": session_stalled,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test-stalled",
                "last_activity_at": _iso_ts(_add_seconds(now, -60)),
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        result_stalled = is_making_progress(
            session_stalled,
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )
        assert result_stalled["status"] == "stalled"

        # Test unknown status.
        result_unknown = is_making_progress(
            "ses_fake",
            idle_threshold_seconds=30,
            dal=dal,
            now_iso=_iso_ts(now),
        )
        assert result_unknown["status"] == "unknown"


class TestLivenessConsistency:
    """Test consistency between is_making_progress and last_activity_at."""

    def test_is_making_progress_matches_stream_events_signal(
        self, harness: tuple[SessionsDal, str]
    ) -> None:
        """When last_activity_at is recently updated (stream event), is_making_progress=True."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        # Create session.
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test",
                "last_activity_at": _iso_ts(_add_seconds(now, -2)),
                "created_at": _iso_ts(now),
                "updated_at": _iso_ts(now),
            }
        )

        # Check immediately: session is still making progress.
        result1 = is_making_progress(
            session_id,
            idle_threshold_seconds=5,
            dal=dal,
            now_iso=_iso_ts(now),
        )
        assert result1["is_making_progress"] is True
        assert result1["status"] == "slow"

        # Simulate passage of time to threshold.
        later = _add_seconds(now, 5.1)
        result2 = is_making_progress(
            session_id,
            idle_threshold_seconds=5,
            dal=dal,
            now_iso=_iso_ts(later),
        )
        # Should now be stalled.
        assert result2["is_making_progress"] is False
        assert result2["status"] == "stalled"

        # Touch activity (stream event emitted).
        dal.touch_activity(session_id, _iso_ts(later))
        result3 = is_making_progress(
            session_id,
            idle_threshold_seconds=5,
            dal=dal,
            now_iso=_iso_ts(later),
        )
        # Should return to making progress.
        assert result3["is_making_progress"] is True
        assert result3["status"] == "slow"


class TestLivenessCoordinatorScenarios:
    """Scenarios for how the coordinator should consume the liveness signal."""

    def test_coordinator_scenario_slow_job(self, harness: tuple[SessionsDal, str]) -> None:
        """Coordinator scenario: slow but healthy job (long compilation, analysis)."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        # Create a session that started 5 minutes ago but is still emitting.
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "grok",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test-slow-but-healthy",
                "last_activity_at": _iso_ts(_add_seconds(now, -30)),  # Active 30s ago
                "created_at": _iso_ts(_add_seconds(now, -300)),  # Started 5 min ago
                "updated_at": _iso_ts(now),
            }
        )

        # Coordinator checks with a 60-second idle threshold.
        result = is_making_progress(
            session_id,
            idle_threshold_seconds=60,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        # Decision: do NOT kill, it's still making progress (slow but alive).
        assert result["status"] == "slow"
        assert result["is_making_progress"] is True
        # Coordinator logs: "Slow but healthy: 30s since last activity, threshold is 60s"

    def test_coordinator_scenario_stalled_job(self, harness: tuple[SessionsDal, str]) -> None:
        """Coordinator scenario: genuinely stalled/hung job."""
        dal, db = harness
        now = _now_utc()
        session_id = new_id("ses")

        # Create a session that has had no activity for 2 minutes.
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "provider": "codex",
                "project_dir": "/tmp",
                "state": "running",
                "title": "test-stalled",
                "last_activity_at": _iso_ts(_add_seconds(now, -120)),  # No activity for 2 min
                "created_at": _iso_ts(_add_seconds(now, -300)),  # Started 5 min ago
                "updated_at": _iso_ts(now),
            }
        )

        # Coordinator checks with a 60-second idle threshold.
        result = is_making_progress(
            session_id,
            idle_threshold_seconds=60,
            dal=dal,
            now_iso=_iso_ts(now),
        )

        # Decision: KILL, it's stalled. Escalate to costlier tier for retry.
        assert result["status"] == "stalled"
        assert result["is_making_progress"] is False
        # Coordinator logs: "Stalled: 120s since last activity, threshold is 60s. Killing and escalating."
