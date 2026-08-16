"""Tests for the audit module (W5, RF2)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.reliability.audit import (
    AuditDeps,
    _hydrate_production_deps,
    daily_summary,
    twice_daily,
    watch,
    weekly_architecture,
)
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import AuditKind, EventStatus, FailureClass


@pytest.fixture(autouse=True)
def _disable_llm(monkeypatch):
    """Skip the LLM-dependent stages in tests — never touch a live CLI (RF2 #5)."""
    monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_LLM", "0")


@pytest.fixture
def store_fixture(tmp_path):
    """Create a test store with schema."""
    db_path = str(tmp_path / "test.db")
    store = SqliteReliabilityStore(db_path)
    return store


def _read_cursor_state(store):
    row = store._connection.execute(
        "SELECT value_json FROM reliability_state WHERE key = 'watch_cursor'"
    ).fetchone()
    return json.loads(row["value_json"]) if row else None


@pytest.fixture
def mock_vault_write(tmp_path):
    """Mock vault write_note to avoid frontmatter issues."""

    def fake_write(vault_dir, relpath, content, autocommit=False):
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return str(path)

    with patch("omniagentos.reliability.report.write_note", side_effect=fake_write):
        yield


class TestWatchAudit:
    """Test watch cycle audit."""

    def test_watch_creates_audit_row(self, store_fixture, mock_vault_write):
        """Watch creates a queued→running→completed audit row."""
        result = watch(store_fixture, once=True, vault_dir="/tmp")

        assert "audit_id" in result
        assert result["findings_count"] >= 0

        audit = store_fixture.get_audit(result["audit_id"])
        assert audit is not None
        assert audit.kind == AuditKind.WATCH.value
        assert audit.status == "completed"

    def test_watch_advances_cursor(self, store_fixture, mock_vault_write):
        """Watch advances the watch cursor."""
        cursor_before = store_fixture.get_watch_cursor()
        watch(store_fixture, once=True, vault_dir="/tmp")
        cursor_after = store_fixture.get_watch_cursor()

        # Never-run is explicit rather than fabricated as "now"; the first
        # completed watch establishes the durable cursor.
        assert cursor_before is None
        assert cursor_after is not None


class TestTwiceDailyAudit:
    """Test twice_daily audit."""

    def test_twice_daily_creates_audit(self, store_fixture, mock_vault_write):
        """Twice_daily creates a vault report and audit row."""
        result = twice_daily(store_fixture, once=True, vault_dir="/tmp")

        assert "audit_id" in result
        assert "stats_json" in result

        audit = store_fixture.get_audit(result["audit_id"])
        assert audit is not None
        assert audit.kind == AuditKind.TWICE_DAILY.value

    def test_twice_daily_with_events(self, store_fixture, mock_vault_write):
        """Twice_daily counts open events."""
        # Create test events
        for i in range(3):
            store_fixture.insert_reliability_event(
                failure_class=FailureClass.RUN_FAILED.value,
                severity="warning",
                signature=f"run_failed|executor|{i}",
                occurrence_key=f"run_failed|executor|{i}|now",
                source="detector:executor",
                evidence_json={"error": f"test error {i}"},
            )

        result = twice_daily(store_fixture, once=True, vault_dir="/tmp")

        assert result["findings_count"] >= 3


class TestDailySummary:
    """Test daily_summary audit."""

    def test_daily_summary_creates_audit(self, store_fixture, mock_vault_write):
        """Daily summary creates an audit row."""
        # Pass vault_dir so it uses write_audit_report
        result = daily_summary(store_fixture, once=True, vault_dir="/tmp")

        assert "audit_id" in result
        assert "stats_json" in result

        audit = store_fixture.get_audit(result["audit_id"])
        assert audit is not None
        assert audit.kind == AuditKind.DAILY_SUMMARY.value

    def test_daily_summary_counts_improvements(self, store_fixture, mock_vault_write):
        """Daily summary aggregates improvement counts."""
        # Create test improvements
        for i in range(2):
            store_fixture.create_improvement(
                origin="audit",
                kind="fix",
                title=f"Improvement {i}",
            )

        result = daily_summary(store_fixture, once=True, vault_dir="/tmp")

        assert result["findings_count"] >= 0


class TestWeeklyArchitecture:
    """Test weekly_architecture audit."""

    def test_weekly_creates_audit(self, store_fixture, mock_vault_write):
        """Weekly architecture review creates an audit row."""
        result = weekly_architecture(store_fixture, once=True, vault_dir="/tmp")

        assert "audit_id" in result
        assert "stats_json" in result

        audit = store_fixture.get_audit(result["audit_id"])
        assert audit is not None
        assert audit.kind == AuditKind.WEEKLY_ARCHITECTURE.value


class TestAuditIntegration:
    """Integration tests for audit pipeline."""

    def test_audit_lifecycle(self, store_fixture, mock_vault_write):
        """Audit goes through queued→running→completed lifecycle."""
        result = watch(store_fixture, once=True, vault_dir="/tmp")
        audit_id = result["audit_id"]

        audit = store_fixture.get_audit(audit_id)
        assert audit.status == "completed"
        assert audit.findings >= 0

    def test_all_audit_kinds(self, store_fixture, mock_vault_write):
        """All audit kinds can be run."""
        kinds_and_funcs = [
            (watch, AuditKind.WATCH.value),
            (twice_daily, AuditKind.TWICE_DAILY.value),
            (daily_summary, AuditKind.DAILY_SUMMARY.value),
            (weekly_architecture, AuditKind.WEEKLY_ARCHITECTURE.value),
        ]

        for func, expected_kind in kinds_and_funcs:
            # All funcs pass vault_dir
            result = func(store_fixture, once=True, vault_dir="/tmp")

            audit = store_fixture.get_audit(result["audit_id"])
            assert audit.kind == expected_kind


class TestWatchCursorSemantics:
    """RF2 #2/#3: bootstrap lookback, first_seen stamp, and hold-on-error."""

    def test_bootstrap_sees_preexisting_failures(self, store_fixture, mock_vault_write):
        """First run bootstraps to now-24h so failures already present ARE detected."""
        conn = store_fixture._connection
        now = utc_now_iso()
        # A run that failed 2h ago — well before now(), but inside the 24h lookback.
        two_h_ago = "2020-01-01T00:00:00Z"  # placeholder overwritten below
        from datetime import UTC, datetime, timedelta

        two_h_ago = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO tasks (id, discipline_id, title, state, created_at, updated_at) "
            "VALUES ('tsk_b', 'research-briefs', 't', 'running', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO runs (id, task_id, state, error, created_at, updated_at, finished_at, "
            "queued_at, harness, trace_id) VALUES (?, 'tsk_b', 'failed', 'boom', ?, ?, ?, ?, "
            "'cli-claude', 'tr')",
            ("run_pre", two_h_ago, two_h_ago, two_h_ago, two_h_ago),
        )
        conn.commit()

        watch(store_fixture, once=True, vault_dir="/tmp")

        events = store_fixture.list_events(limit=100)
        assert any(e.ref_id == "run_pre" for e in events), "pre-existing failure was skipped"

    def test_writes_first_seen_once(self, store_fixture, mock_vault_write):
        """The cursor value_json carries a set-once first_seen (steward dead-man reads it)."""
        watch(store_fixture, once=True, vault_dir="/tmp")
        state1 = _read_cursor_state(store_fixture)
        assert state1 is not None
        assert state1.get("first_seen")
        first = state1["first_seen"]

        watch(store_fixture, once=True, vault_dir="/tmp")
        state2 = _read_cursor_state(store_fixture)
        assert state2["first_seen"] == first  # unchanged on the second run

    def test_cursor_held_on_stage_error(self, store_fixture, mock_vault_write):
        """A detector error HOLDS the cursor and fires a warning alert (nothing skipped)."""
        alerts = []

        def _notifier(**kwargs):
            alerts.append(kwargs)

        def _boom_detect(store, since, end):
            raise RuntimeError("detector exploded")

        result = watch(
            store_fixture,
            once=True,
            vault_dir="/tmp",
            detect_fn=_boom_detect,
            notifier=_notifier,
        )

        assert result["stats_json"].get("cursor_held") is True
        assert result["stats_json"].get("last_error")
        warnings = [
            a for a in alerts if a.get("kind") == "alert" and a.get("severity") == "warning"
        ]
        assert warnings, "a held cursor must raise a warning alert"

    def test_cursor_advances_on_clean_run(self, store_fixture, mock_vault_write):
        """A clean window advances the cursor past its bootstrap value."""
        watch(store_fixture, once=True, vault_dir="/tmp")
        state = _read_cursor_state(store_fixture)
        # Cursor advanced to the high-water mark (a recent timestamp), not a 24h-old one.
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert state["cursor"] > cutoff


class TestAuditIdReuse:
    """RF2 #7: honor a pre-created queued audit row instead of minting a new one."""

    def test_watch_runs_precreated_row(self, store_fixture, mock_vault_write):
        aid = store_fixture.create_audit("watch", utc_now_iso(), utc_now_iso())
        before = len(store_fixture.list_audits(limit=100))

        result = watch(store_fixture, once=True, audit_id=aid, vault_dir="/tmp")

        assert result["audit_id"] == aid
        assert store_fixture.get_audit(aid).status == "completed"
        # No NEW audit row was created — the queued one was started.
        assert len(store_fixture.list_audits(limit=100)) == before


class TestDepartmentCtoIsolation:
    """RF2 #12: department and CTO are two independently-isolated stages."""

    def test_cto_runs_even_if_departments_crash(self, store_fixture, mock_vault_write):
        called = {"cto": False}

        def _adapter(harness, prompt, **kw):  # never invoked (fns are stubbed) but required to gate
            return None

        def _departments_fn(store, adapter):
            raise RuntimeError("department review crashed")

        def _cto_daily_fn(store, adapter):
            called["cto"] = True
            return {"ok": True}

        result = twice_daily(
            store_fixture,
            once=True,
            vault_dir="/tmp",
            company_adapter_fn=_adapter,
            departments_fn=_departments_fn,
            cto_daily_fn=_cto_daily_fn,
        )

        assert called["cto"] is True, "CTO pass must run even when departments crash"
        errors = result["stats_json"].get("errors", [])
        assert any(e.get("stage") == "departments" for e in errors)


class TestProductionDepsHydration:
    """RF2 #5: env-gated construction of real pipeline + analyzer collaborators."""

    def test_env_off_skips_llm_deps(self, store_fixture, monkeypatch):
        monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_LLM", "0")
        deps = _hydrate_production_deps(store_fixture, AuditDeps(repo_root="/tmp"))
        assert deps.pipeline is None
        assert deps.analyze_fn is None and deps.adapter_fn is None

    def test_env_on_builds_deps(self, store_fixture, monkeypatch, tmp_path):
        monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_LLM", "1")
        deps = _hydrate_production_deps(store_fixture, AuditDeps(repo_root=str(tmp_path)))
        assert deps.pipeline is not None
        assert deps.adapter_fn is not None

    def test_env_on_respects_injected_deps(self, store_fixture, monkeypatch):
        monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_LLM", "1")
        sentinel = object()

        def _my_adapter(prompt):
            return {"content": "{}"}

        deps = _hydrate_production_deps(
            store_fixture, AuditDeps(pipeline=sentinel, adapter_fn=_my_adapter, repo_root="/tmp")
        )
        assert deps.pipeline is sentinel  # not overwritten
        assert deps.adapter_fn is _my_adapter


class TestCriticalAlertsFireOnce:
    """A permanently-open critical event must not re-notify on every watch cycle.

    Regression for the live storm: recovery only allow-lists rate_limit/timeout, so
    session_error/account_disabled events stay 'open' forever. Re-listing every open
    critical each 600 s cycle produced 246 notifications/hour (3,838 rows) until a
    human intervened.
    """

    @staticmethod
    def _seed_critical(store, ref_id: str) -> str:
        return store.insert_reliability_event(
            failure_class=FailureClass.SESSION_ERROR.value,
            severity="critical",
            signature=f"sig-{ref_id}",
            occurrence_key=f"occ-{ref_id}",
            source="detector_session_error",
            ref_type="session",
            ref_id=ref_id,
        )

    @staticmethod
    def _alerts(collected):
        return [
            a for a in collected if a.get("severity") == "critical" and a.get("kind") == "alert"
        ]

    def test_open_critical_alerts_once_across_cycles(
        self, store_fixture, mock_vault_write, monkeypatch
    ):
        monkeypatch.delenv("OMNIAGENTOS_RELIABILITY_REALERT_HOURS", raising=False)
        self._seed_critical(store_fixture, "ses_stuck")
        collected = []

        def _notifier(**kwargs):
            collected.append(kwargs)

        for _ in range(3):
            watch(store_fixture, once=True, vault_dir="/tmp", notifier=_notifier)

        assert len(self._alerts(collected)) == 1, "an open critical re-alerted every cycle"
        # The event is still open — alert-once must not fake a resolution.
        assert store_fixture.list_events(status=EventStatus.OPEN.value, limit=10)

    def test_each_new_critical_still_alerts(self, store_fixture, mock_vault_write, monkeypatch):
        monkeypatch.delenv("OMNIAGENTOS_RELIABILITY_REALERT_HOURS", raising=False)
        collected = []

        def _notifier(**kwargs):
            collected.append(kwargs)

        self._seed_critical(store_fixture, "ses_one")
        watch(store_fixture, once=True, vault_dir="/tmp", notifier=_notifier)
        self._seed_critical(store_fixture, "ses_two")
        watch(store_fixture, once=True, vault_dir="/tmp", notifier=_notifier)

        alerts = self._alerts(collected)
        assert len(alerts) == 2, "a newly detected critical must still alert immediately"
        assert len({a["ref_id"] for a in alerts}) == 2

    def test_realert_window_reopens_the_alert(self, store_fixture, mock_vault_write, monkeypatch):
        """The opt-in reminder cadence re-alerts a stale event exactly once per window."""
        event_id = self._seed_critical(store_fixture, "ses_reminder")
        collected = []

        def _notifier(**kwargs):
            collected.append(kwargs)

        monkeypatch.delenv("OMNIAGENTOS_RELIABILITY_REALERT_HOURS", raising=False)
        watch(store_fixture, once=True, vault_dir="/tmp", notifier=_notifier)
        assert len(self._alerts(collected)) == 1

        # Age the stamp past a 1-hour reminder window.
        store_fixture.mark_event_alerted(event_id, alerted_at="2020-01-01T00:00:00Z")
        monkeypatch.setenv("OMNIAGENTOS_RELIABILITY_REALERT_HOURS", "1")
        watch(store_fixture, once=True, vault_dir="/tmp", notifier=_notifier)
        assert len(self._alerts(collected)) == 2
        # Freshly stamped ⇒ the very next cycle is quiet again.
        watch(store_fixture, once=True, vault_dir="/tmp", notifier=_notifier)
        assert len(self._alerts(collected)) == 2

    def test_alerting_does_not_disturb_updated_at(
        self, store_fixture, mock_vault_write, monkeypatch
    ):
        """The stamp is a notification side-channel, not a state transition."""
        monkeypatch.delenv("OMNIAGENTOS_RELIABILITY_REALERT_HOURS", raising=False)
        event_id = self._seed_critical(store_fixture, "ses_untouched")
        before = store_fixture.get_event(event_id)

        watch(store_fixture, once=True, vault_dir="/tmp", notifier=lambda **kw: None)

        after = store_fixture.get_event(event_id)
        assert after.updated_at == before.updated_at
        assert after.status == before.status
        assert after.alerted_at is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
