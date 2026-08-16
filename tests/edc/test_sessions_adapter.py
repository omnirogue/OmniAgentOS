"""Session source adapter tests — classification, dedupe, expiry, flag-off no-op.

Drives the real ``DecisionStore``/``SqliteStore`` composition (the conftest
``store`` fixture) exactly like ``test_pipeline.py`` does for email. Session rows
are supplied through an injected fake DAL for the columns a PARALLEL package is
still adding (``attention_state``/``attention_reason``/``attention_since``/
``company``/``agent_name``), and through the REAL ``SessionsDal`` for the
conditions today's schema already supports (idle, failed) so the DAL composition
itself is covered rather than mocked away.

The load-bearing safety assertions, and why each exists:

* **no LLM** — triage runs with a client that raises on any call.
* **never URGENT** — an urgent verdict is what DMs the operator; a quiet agent session
  must never do that.
* **no executor** — an open session decision offers snooze/dismiss/note only, so
  reply/delegate/defer (the paths with side effects) are unreachable.
* **flag OFF is a true no-op** — the adapter is not even constructed.
"""

from __future__ import annotations

import shlex
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omniagentos.edc.adapters.sessions import (
    FAILED_CONDITION,
    IDLE_CONDITION,
    NEEDS_INPUT_CONDITION,
    SESSION_SOURCE,
    SESSIONS_FLAG_ENV,
    SessionAdapter,
    session_conditions,
    sessions_source_enabled,
)
from omniagentos.edc.main import default_adapters, run_session_sweep, run_triage
from omniagentos.edc.store import DecisionStore, available_actions_for
from omniagentos.sessions.dal import TERMINAL_SESSION_STATES, SessionsDal, SessionState
from omniagentos.steward.config import EdcAccountCfg, EdcConfig, StewardConfig

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
_CFG = StewardConfig(
    edc=EdcConfig(
        accounts={
            SESSION_SOURCE: EdcAccountCfg(
                owner_employee_id="emp_owner", company_slug="", source_account="session"
            )
        }
    )
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeDal:
    """A sessions DAL stand-in: live rows + failed rows, no schema coupling."""

    def __init__(self, live: list[dict[str, Any]], failed: list[dict[str, Any]] | None = None):
        self.live = live
        self.failed = failed or []
        self.closed = False

    def list_live_sessions(self, limit: int = 500) -> list[dict[str, Any]]:
        return self.live[:limit]

    def list_sessions(self, state: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        return self.failed[:limit] if state == FAILED_CONDITION else []

    def close(self) -> None:  # pragma: no cover - injected dals are caller-owned
        self.closed = True


class _ExplodingLlm:
    """Any LLM call at all is a test failure for this source."""

    used = 0
    limit = 0

    def complete_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the session source must never call an LLM")


def _waiting_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "ses_wait1",
        "state": "awaiting_approval",
        "source": "bridge",
        "provider": "claude",
        "project_dir": "/Users/youruser/OmniAgentOS",
        "session_ref": "abc-123",
        "title": "gate loop",
        "agent_name": "Bob",
        "agent_status": "waiting",
        "company": "globex",
        "attention_state": NEEDS_INPUT_CONDITION,
        "attention_reason": "needs approval to push",
        "attention_since": _iso(NOW - timedelta(minutes=5)),
        "last_activity_at": _iso(NOW - timedelta(minutes=5)),
        "updated_at": _iso(NOW - timedelta(minutes=5)),
    }
    row.update(overrides)
    return row


def _adapter(dal: _FakeDal, now: datetime = NOW) -> SessionAdapter:
    """An adapter with the tick's clock PINNED, so dwell arithmetic is exact."""
    return SessionAdapter(dal=dal, config=_CFG, now=now)


def _triage(store: Any, adapter: SessionAdapter, now: datetime = NOW) -> dict[str, int]:
    return run_triage(store, cfg=_CFG, now=now, adapters=[adapter], llm_client=_ExplodingLlm())


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def test_needs_input_past_dwell_files_one_decision(store, decisions, employees) -> None:
    stats = _triage(store, _adapter(_FakeDal([_waiting_row()])))

    assert stats["created"] == 1
    assert stats["llm_calls"] == 0
    assert stats["dm_sent"] == 0
    row = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    assert row["source"] == SESSION_SOURCE
    assert row["classification"] == "needs_owner"
    assert row["classifier"] == "deterministic"
    assert row["title"] == "Session Bob (globex) is waiting: needs approval to push"
    assert row["recommended"]["human_line"] == "Open the Sessions panel and approve/deny"
    assert row["recommended"]["params"]["deep_link"] == "/sessions"
    assert row["recommended"]["params"]["session_id"] == "ses_wait1"
    assert row["source_ref"].startswith("ses_wait1|needs_input|")


def test_needs_input_inside_the_dwell_is_not_yet_a_decision(store, decisions, employees) -> None:
    row = _waiting_row(
        attention_since=_iso(NOW - timedelta(seconds=30)),
        last_activity_at=_iso(NOW - timedelta(seconds=30)),
    )
    stats = _triage(store, _adapter(_FakeDal([row])))

    assert stats["created"] == 0
    assert decisions.list_decisions(owner_employee_id="emp_owner") == []


def test_needs_input_without_a_provable_dwell_fails_closed() -> None:
    """No attention_since and no last_activity_at ⇒ the dwell cannot be proven."""
    row = _waiting_row(attention_since=None, last_activity_at=None)
    assert session_conditions(row, now=NOW) == []


def test_idle_live_session_recommends_close_with_the_resume_note(
    store, decisions, employees
) -> None:
    row = {
        "id": "ses_idle1",
        "state": "running",
        "provider": "claude",
        "project_dir": "/Users/youruser/Work/thing",
        "session_ref": "res-9",
        "title": "long refactor",
        "last_activity_at": _iso(NOW - timedelta(hours=9)),
        "updated_at": _iso(NOW - timedelta(hours=9)),
    }
    _triage(store, _adapter(_FakeDal([row])))

    decision = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    assert decision["source_ref"].startswith("ses_idle1|idle|")
    assert "idle" in decision["title"]
    assert "no activity for 9h" in decision["title"]
    human_line = decision["recommended"]["human_line"]
    assert human_line.startswith("Close or offload this idle session")
    assert "cd /Users/youruser/Work/thing && claude --resume res-9" in human_line


def test_idle_below_the_threshold_and_terminal_sessions_are_quiet() -> None:
    fresh = {"id": "ses_a", "state": "running", "last_activity_at": _iso(NOW - timedelta(hours=7))}
    done = {
        "id": "ses_b",
        "state": "completed",
        "last_activity_at": _iso(NOW - timedelta(hours=40)),
        "updated_at": _iso(NOW - timedelta(hours=40)),
    }
    assert session_conditions(fresh, now=NOW) == []
    assert session_conditions(done, now=NOW) == []


def test_failed_session_files_a_triage_decision_inside_the_window(
    store, decisions, employees
) -> None:
    failed = {
        "id": "ses_bad1",
        "state": "failed",
        "title": "publisher",
        "error": "provider returned 401",
        "updated_at": _iso(NOW - timedelta(hours=2)),
    }
    stale = {
        "id": "ses_bad2",
        "state": "failed",
        "title": "ancient",
        "error": "who knows",
        "updated_at": _iso(NOW - timedelta(hours=40)),
    }
    _triage(store, _adapter(_FakeDal([], [failed, stale])))

    rows = decisions.list_decisions(owner_employee_id="emp_owner")
    assert [row["source_ref"].split("|")[0] for row in rows] == ["ses_bad1"]
    assert rows[0]["title"].endswith("failed: provider returned 401")
    assert rows[0]["recommended"]["human_line"] == "Open the Sessions panel and triage the failure"


def test_one_session_can_carry_two_independent_conditions() -> None:
    """Waiting AND idle are distinct conditions — one decision each, no collision."""
    row = _waiting_row(
        state="running",
        attention_since=_iso(NOW - timedelta(hours=10)),
        last_activity_at=_iso(NOW - timedelta(hours=10)),
    )
    found = session_conditions(row, now=NOW)
    assert sorted(item.condition for item in found) == [IDLE_CONDITION, NEEDS_INPUT_CONDITION]


def test_no_session_verdict_is_ever_urgent() -> None:
    adapter = _adapter(_FakeDal([]))
    rows = [
        _waiting_row(),
        {"id": "ses_i", "state": "running", "last_activity_at": _iso(NOW - timedelta(hours=9))},
        {"id": "ses_f", "state": "failed", "updated_at": _iso(NOW - timedelta(hours=1))},
    ]
    for row in rows:
        for found in session_conditions(row, now=NOW):
            from omniagentos.edc.adapters.sessions import session_event

            event = session_event(row, found, owner_employee_id="emp_owner")
            verdict = adapter.classify_event(event, now=NOW)
            assert verdict["classification"] == "needs_owner"
            assert verdict["status"] == "open"
            assert verdict["surfaced"] == 0


# --------------------------------------------------------------------------
# dedupe + expiry
# --------------------------------------------------------------------------


def test_repeated_ticks_dedupe_to_one_open_decision(store, decisions, employees) -> None:
    adapter = _adapter(_FakeDal([_waiting_row()]))
    first = _triage(store, adapter)
    second = _triage(store, adapter, now=NOW + timedelta(minutes=6))

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["duplicate"] == 1
    assert len(decisions.list_decisions(owner_employee_id="emp_owner")) == 1


def test_a_new_waiting_episode_after_the_first_cleared_is_a_new_decision(
    store, decisions, employees
) -> None:
    dal = _FakeDal([_waiting_row()])
    adapter = _adapter(dal)
    _triage(store, adapter)

    # The session answers: the condition clears and the suggestion is retired.
    dal.live = [_waiting_row(attention_state=None, attention_since=None)]
    assert run_session_sweep(store, adapter=adapter, now=NOW)["session_expired"] == 1

    # Hours later it raises its hand again — a genuinely new episode.
    later = NOW + timedelta(hours=3)
    dal.live = [
        _waiting_row(
            attention_since=_iso(later - timedelta(minutes=2)),
            last_activity_at=_iso(later - timedelta(minutes=2)),
        )
    ]
    stats = _triage(store, _adapter(dal, now=later), now=later)

    assert stats["created"] == 1
    rows = decisions.list_decisions(owner_employee_id="emp_owner")
    assert sorted(row["status"] for row in rows) == ["expired", "open"]


def test_sweep_expires_a_cleared_condition_without_faking_an_owner_decision(
    store, decisions, employees
) -> None:
    dal = _FakeDal([_waiting_row()])
    adapter = _adapter(dal)
    _triage(store, adapter)
    dal.live = []  # the session is gone entirely

    assert run_session_sweep(store, adapter=adapter, now=NOW)["session_expired"] == 1

    decision = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    assert decision["status"] == "expired"
    # NEVER a resolution: the operator did not decide this, so the learner must not read it.
    assert not decision["resolution"]
    assert (
        decisions.resolved_for_learning(owner_employee_id="emp_owner", since_iso="2000-01-01") == []
    )
    events = [
        event["event"]
        for event in decisions.list_events(decision["id"], owner_employee_id="emp_owner")
    ]
    assert events == ["create", "expire"]


def test_sweep_keeps_a_decision_whose_condition_still_holds(store, decisions, employees) -> None:
    adapter = _adapter(_FakeDal([_waiting_row()]))
    _triage(store, adapter)

    assert run_session_sweep(store, adapter=adapter, now=NOW)["session_expired"] == 0
    assert decisions.list_decisions(owner_employee_id="emp_owner")[0]["status"] == "open"


# --------------------------------------------------------------------------
# suggestions-only guarantees
# --------------------------------------------------------------------------


def test_session_decision_offers_no_executor_affordance(store, decisions, employees) -> None:
    _triage(store, _adapter(_FakeDal([_waiting_row()])))
    decision = decisions.list_decisions(owner_employee_id="emp_owner")[0]

    assert decision["available_actions"] == ["snooze", "dismiss", "note"]
    # The SERVER-recomputed matrix is the authority, and it agrees.
    assert available_actions_for(decision) == ["snooze", "dismiss", "note"]
    for forbidden in ("reply", "delegate", "defer", "execute", "approve"):
        assert forbidden not in available_actions_for(decision)


def test_resolve_refuses_an_executor_resolution_on_a_session_decision(
    store, decisions, employees
) -> None:
    _triage(store, _adapter(_FakeDal([_waiting_row()])))
    decision = decisions.list_decisions(owner_employee_id="emp_owner")[0]

    with pytest.raises(ValueError):
        decisions.resolve(decision["id"], actor="emp_owner", resolution="delegate")
    # Dismissing it — the owner saying "not now" — still works.
    dismissed = decisions.resolve(decision["id"], actor="emp_owner", resolution="dismiss")
    assert dismissed["status"] == "dismissed"


def test_store_source_literal_matches_the_adapter_name() -> None:
    """``available_actions_for`` keys off the literal ``'session'`` (no import cycle)."""
    assert SESSION_SOURCE == "session"
    assert available_actions_for({"source": "session", "status": "open"}) == [
        "snooze",
        "dismiss",
        "note",
    ]


def test_live_state_constants_do_not_drift_from_the_sessions_dal() -> None:
    from omniagentos.edc.adapters.sessions import LIVE_SESSION_STATES

    expected = {state.value for state in SessionState if state not in TERMINAL_SESSION_STATES}
    assert set(LIVE_SESSION_STATES) == expected


# --------------------------------------------------------------------------
# the flag
# --------------------------------------------------------------------------


def test_flag_defaults_off_and_the_adapter_is_not_constructed(monkeypatch) -> None:
    monkeypatch.delenv(SESSIONS_FLAG_ENV, raising=False)
    assert sessions_source_enabled() is False
    assert [adapter.name for adapter in default_adapters(_CFG)] == ["email"]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_flag_accepts_the_estate_truthy_spellings(value: str, monkeypatch) -> None:
    monkeypatch.setenv(SESSIONS_FLAG_ENV, value)
    assert sessions_source_enabled() is True
    assert [adapter.name for adapter in default_adapters(_CFG)] == ["email", "session"]


@pytest.mark.parametrize("value", ["0", "false", "off", "", "  "])
def test_flag_rejects_everything_else(value: str, monkeypatch) -> None:
    monkeypatch.setenv(SESSIONS_FLAG_ENV, value)
    assert sessions_source_enabled() is False


def test_flag_off_makes_the_sweep_a_noop(store, decisions, employees, monkeypatch) -> None:
    monkeypatch.delenv(SESSIONS_FLAG_ENV, raising=False)
    assert run_session_sweep(store, cfg=_CFG)["session_expired"] == 0


# --------------------------------------------------------------------------
# real DAL composition (today's schema: idle + failed)
# --------------------------------------------------------------------------


def test_reads_real_session_rows_through_the_sessions_dal(store, decisions, employees) -> None:
    dal = SessionsDal(store._db_path)
    try:
        dal.create_session(
            {
                "id": "ses_real1",
                "source": "bridge",
                "project_dir": "/Users/youruser/OmniAgentOS",
                "provider": "claude",
                "session_ref": "real-ref",
                "state": "running",
                "title": "overnight drain",
                "last_activity_at": _iso(NOW - timedelta(hours=12)),
            }
        )
    finally:
        dal.close()

    adapter = SessionAdapter(config=_CFG, now=NOW)  # builds its own DAL from the store path
    stats = _triage(store, adapter)

    assert stats["created"] == 1
    decision = DecisionStore(store).list_decisions(owner_employee_id="emp_owner")[0]
    assert decision["source_ref"].startswith("ses_real1|idle|")
    assert decision["counterparty"] == "session:ses_real1"


# --------------------------------------------------------------------------
# Adversarial-review regressions (crit-20260815 F001-F009). Each test names the
# lead it pins and the property that must hold, not just the symptom.
# --------------------------------------------------------------------------


def _idle_row(session_id: str, hours: float = 9, **overrides: Any) -> dict[str, Any]:
    row = {
        "id": session_id,
        "state": "running",
        "provider": "claude",
        "project_dir": "/Users/youruser/Work/thing",
        "session_ref": f"ref-{session_id}",
        "last_activity_at": _iso(NOW - timedelta(hours=hours)),
    }
    row.update(overrides)
    return row


def test_f001_a_truncated_read_never_authorizes_expiry(store, decisions, employees) -> None:
    """An incomplete observation is not evidence that a condition cleared."""
    rows = [_idle_row(f"ses_{index:04d}") for index in range(600)]
    full = SessionAdapter(dal=_FakeDal(rows), config=_CFG, now=NOW, batch_limit=500)
    assert full.live_snapshot(store).complete is False

    # File a decision for a session that a later capped read cannot see.
    single = SessionAdapter(dal=_FakeDal([rows[-1]]), config=_CFG, now=NOW)
    _triage(store, single)
    assert decisions.list_decisions(owner_employee_id="emp_owner")[0]["status"] == "open"

    stats = run_session_sweep(store, adapter=full, now=NOW)
    assert stats["session_expired"] == 0
    assert stats["session_expiry_skipped"] >= 1
    assert decisions.list_decisions(owner_employee_id="emp_owner")[0]["status"] == "open"


def test_f002_an_unreadable_timestamp_is_indeterminate_not_cleared(
    store, decisions, employees
) -> None:
    """ "Could not tell" must never be spelled "definitely gone"."""
    dal = _FakeDal([_idle_row("ses_corrupt")])
    adapter = _adapter(dal)
    _triage(store, adapter)

    dal.live = [_idle_row("ses_corrupt", last_activity_at="not-an-iso-timestamp")]
    snapshot = adapter.live_snapshot(store, now=NOW)
    assert snapshot.events == []
    assert "ses_corrupt|idle" in snapshot.indeterminate

    stats = run_session_sweep(store, adapter=adapter, now=NOW)
    assert stats["session_expired"] == 0
    assert stats["session_expiry_skipped"] == 1
    assert decisions.list_decisions(owner_employee_id="emp_owner")[0]["status"] == "open"


def test_f002_a_system_expiry_is_reversible_when_the_condition_returns(
    store, decisions, employees
) -> None:
    """A false expiry must be able to repair itself; UNIQUE dedupe cannot reopen."""
    dal = _FakeDal([_idle_row("ses_flap")])
    adapter = _adapter(dal)
    _triage(store, adapter)

    dal.live = []  # the condition genuinely disappears from the read
    assert run_session_sweep(store, adapter=adapter, now=NOW)["session_expired"] == 1
    assert decisions.list_decisions(owner_employee_id="emp_owner")[0]["status"] == "expired"

    dal.live = [_idle_row("ses_flap")]  # ... and comes back under the same identity
    stats = run_session_sweep(store, adapter=adapter, now=NOW)
    assert stats["session_revived"] == 1
    revived = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    assert revived["status"] == "open"
    assert revived["available_actions"] == ["snooze", "dismiss", "note"]


def test_f002_an_owner_dismissal_is_never_revived(store, decisions, employees) -> None:
    dal = _FakeDal([_idle_row("ses_done")])
    adapter = _adapter(dal)
    _triage(store, adapter)
    decision = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    decisions.resolve(decision["id"], actor="emp_owner", resolution="dismiss")

    stats = run_session_sweep(store, adapter=adapter, now=NOW)
    assert stats["session_revived"] == 0
    assert decisions.list_decisions(owner_employee_id="emp_owner")[0]["status"] == "dismissed"


def test_f003_a_persistent_occurrence_keeps_one_stable_identity(
    store, decisions, employees
) -> None:
    """Without attention_since the episode must NOT ride the moving activity stamp."""
    refs: set[str] = set()
    for minutes in range(6):
        row = _waiting_row(
            attention_since=None,
            last_activity_at=_iso(NOW - timedelta(minutes=10 - minutes)),
        )
        found = session_conditions(row, now=NOW)
        assert [item.condition for item in found] == [NEEDS_INPUT_CONDITION]
        refs.add(f"{row['id']}|{found[0].condition}|{found[0].episode}")
        _triage(store, _adapter(_FakeDal([row])))

    assert len(refs) == 1
    assert len(decisions.list_decisions(owner_employee_id="emp_owner")) == 1


def test_f004_a_snoozed_card_retires_when_its_condition_clears(store, decisions, employees) -> None:
    """A snooze defers when the operator looks; it does not promise the item still exists."""
    from omniagentos.edc.snooze import resolve_snooze

    dal = _FakeDal([_idle_row("ses_snoozed")])
    adapter = _adapter(dal)
    _triage(store, adapter)
    decision = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    resolve_snooze(
        decisions,
        decision,
        actor="emp_owner",
        until=_iso(NOW + timedelta(days=3)),
        now=NOW,
    )
    assert decisions.list_decisions(owner_employee_id="emp_owner")[0]["status"] == "snoozed"

    dal.live = []
    assert run_session_sweep(store, adapter=adapter, now=NOW)["session_expired"] == 1
    retired = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    assert retired["status"] == "expired"
    assert retired["snooze_until"] is None
    # The audit fact that the operator snoozed it survives the retirement.
    assert retired["resolution"] == "snooze"
    events = [
        event["event"]
        for event in decisions.list_events(retired["id"], owner_employee_id="emp_owner")
    ]
    assert events == ["create", "snooze", "expire"]


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("claude", "claude --resume ref-1"),
        ("codex", "codex resume ref-1"),
        ("gemini", "gemini --resume ref-1"),
    ],
)
def test_f005_resume_syntax_is_looked_up_per_provider(provider: str, expected: str) -> None:
    from omniagentos.edc.adapters.sessions import _resume_note

    note = _resume_note({"project_dir": "/tmp/plain", "session_ref": "ref-1", "provider": provider})
    assert note == f"cd /tmp/plain && {expected}"


def test_f005_unknown_provider_gets_no_fabricated_command() -> None:
    from omniagentos.edc.adapters.sessions import _resume_note

    note = _resume_note({"project_dir": "/tmp/plain", "session_ref": "ref-1", "provider": ""})
    assert "--resume" not in note and "resume ref-1" not in note
    assert note == "reattach from /tmp/plain via the Sessions panel"


def test_f005_shell_metacharacters_in_a_project_dir_are_quoted() -> None:
    from omniagentos.edc.adapters.sessions import _resume_note

    hostile = "/tmp/acme project; touch /tmp/should-not-run"
    note = _resume_note({"project_dir": hostile, "session_ref": "ref-1", "provider": "claude"})
    assert f"cd {shlex.quote(hostile)} &&" in note
    assert "; touch /tmp/should-not-run &&" not in note


def test_f005_late_metadata_refreshes_the_open_suggestion(store, decisions, employees) -> None:
    """The owner must act on what the source says now, not at mint time."""
    dal = _FakeDal([_idle_row("ses_late", session_ref="", provider="")])
    adapter = _adapter(dal)
    _triage(store, adapter)
    assert (
        "via the Sessions panel"
        in (decisions.list_decisions(owner_employee_id="emp_owner")[0]["recommended"]["human_line"])
    )

    dal.live = [_idle_row("ses_late", session_ref="ref-late", provider="codex")]
    stats = run_session_sweep(store, adapter=adapter, now=NOW)
    assert stats["session_refreshed"] == 1
    refreshed = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    assert "codex resume ref-late" in refreshed["recommended"]["human_line"]
    assert refreshed["status"] == "open"
    # A second sweep with nothing new must not churn the row.
    assert run_session_sweep(store, adapter=adapter, now=NOW)["session_refreshed"] == 0


@pytest.mark.parametrize(
    "updated_at",
    [
        _iso(NOW + timedelta(days=30)),  # clock skew / a bad writer
        _iso(NOW - timedelta(hours=24)),  # the exclusive upper bound
        _iso(NOW - timedelta(hours=40)),  # plain history
    ],
)
def test_f006_failed_window_is_zero_to_twentyfour_hours(updated_at: str) -> None:
    row = {"id": "ses_bounds", "state": "failed", "updated_at": updated_at}
    assert session_conditions(row, now=NOW) == []


def test_f006_a_failure_just_inside_the_window_still_files() -> None:
    row = {
        "id": "ses_bounds",
        "state": "failed",
        "updated_at": _iso(NOW - timedelta(hours=23, minutes=59)),
    }
    assert [item.condition for item in session_conditions(row, now=NOW)] == [FAILED_CONDITION]


def test_f007_an_owner_resolution_wins_the_expiry_race(store, decisions, employees) -> None:
    """The sweep's read is stale by construction; the CAS is what makes it safe."""
    dal = _FakeDal([_idle_row("ses_race")])
    adapter = _adapter(dal)
    _triage(store, adapter)
    decision = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    decisions.resolve(decision["id"], actor="emp_owner", resolution="dismiss")

    # A sweep still holding the pre-dismissal snapshot must not overwrite it.
    assert (
        decisions.expire_decision(
            decision["id"],
            owner_employee_id="emp_owner",
            from_status="open",
            note="session condition cleared",
        )
        is None
    )
    assert decisions.get_decision(decision["id"], owner_employee_id="emp_owner")["status"] == (
        "dismissed"
    )


def test_f007_expiry_and_its_receipt_are_one_transaction(store, decisions, employees) -> None:
    dal = _FakeDal([_idle_row("ses_crash")])
    adapter = _adapter(dal)
    _triage(store, adapter)
    decision = decisions.list_decisions(owner_employee_id="emp_owner")[0]

    original = decisions._insert_event

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("crash between the status write and its audit receipt")

    decisions._insert_event = _boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            decisions.expire_decision(
                decision["id"], owner_employee_id="emp_owner", from_status="open"
            )
    finally:
        decisions._insert_event = original  # type: ignore[method-assign]

    # The status mutation rolled back with its missing receipt.
    assert decisions.get_decision(decision["id"], owner_employee_id="emp_owner")["status"] == "open"


def test_f008_unrelated_decision_volume_cannot_hide_a_stale_session(
    store, decisions, employees
) -> None:
    """list_decisions is a paged UI read; a source sweep must not depend on it."""
    dal = _FakeDal([_idle_row("ses_buried")])
    adapter = _adapter(dal)
    _triage(store, adapter)
    for index in range(220):
        decisions.create_decision(
            {
                "owner_employee_id": "emp_owner",
                "source": "email",
                "source_ref": f"msg-{index:04d}",
                "title": "noise",
                "classification": "needs_owner",
                "recommended": {"kind": "reply", "human_line": "reply"},
            }
        )

    dal.live = []
    assert run_session_sweep(store, adapter=adapter, now=NOW)["session_expired"] == 1
    buried = decisions.list_source_decisions(
        owner_employee_id="emp_owner", source=SESSION_SOURCE, statuses=("expired",)
    )
    assert len(buried) == 1


def test_f009_a_naive_clock_is_read_as_utc_not_host_local() -> None:
    """Naive row stamps mean UTC; a naive `now` must mean the same thing."""
    row = {"id": "ses_naive", "state": "running", "last_activity_at": "2026-08-15T04:30:00"}
    naive_now = datetime(2026, 8, 15, 12, 0, 0)
    assert session_conditions(row, now=naive_now) == []  # 7.5h — under the threshold
    assert session_conditions(row, now=naive_now.replace(tzinfo=UTC)) == []
    later = datetime(2026, 8, 15, 13, 0, 0)
    assert [item.condition for item in session_conditions(row, now=later)] == [IDLE_CONDITION]


def test_n2_session_decisions_are_excluded_from_the_learner_by_default(
    store, decisions, employees
) -> None:
    """Defense in depth: the exclusion is explicit, not incidental via counterparty."""
    from omniagentos.edc.store import INTERNAL_DECISION_SOURCES

    assert SESSION_SOURCE in INTERNAL_DECISION_SOURCES
    _triage(store, _adapter(_FakeDal([_waiting_row()])))
    decision = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    decisions.resolve(decision["id"], actor="emp_owner", resolution="dismiss")

    assert (
        decisions.resolved_for_learning(owner_employee_id="emp_owner", since_iso="2000-01-01") == []
    )
