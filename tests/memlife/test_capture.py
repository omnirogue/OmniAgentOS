"""L1 acceptance: episodic capture from swarm_attempts ⋈ sessions.

Decisive: a non-empty fixture of N attempts emits exactly N events, and a
tokens-only session never becomes cost_usd=0.0.

Counterfeit: NULL end_reason must map to EventResult.UNKNOWN, never SUCCESS.

Revert-check (run manually by dropping the join in capture.py): reading
swarm_attempts alone must make the tokens-only NULL-cost assertion fail —
usage lives on the session, and without the join a legacy attempt cost of
0.0 is no longer overridden by usage_source='cli-report-tokens-only'.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.memlife.capture import capture_events
from omniagentos.memlife.contracts import EventResult
from omniagentos.memlife.salience import salience_score

# ---------------------------------------------------------------------------
# Fixture DB — minimal schema matching production columns we actually read.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE sessions (
    id                 TEXT PRIMARY KEY,
    source             TEXT NOT NULL DEFAULT 'bridge',
    project_dir        TEXT NOT NULL DEFAULT '/',
    provider           TEXT NOT NULL DEFAULT 'claude',
    state              TEXT NOT NULL DEFAULT 'completed',
    model              TEXT,
    cost_usd           REAL,
    usage_source       TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE swarm_attempts (
    id            TEXT PRIMARY KEY,
    swarm_run_id  TEXT NOT NULL,
    board_task_id TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    session_id    TEXT,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    tier          TEXT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    end_reason    TEXT,
    detail        TEXT NOT NULL DEFAULT '',
    cost_usd      REAL,
    usage_source  TEXT
);
"""

# One row per end_reason class we must map, plus the tokens-only cost trap.
# Attempt a_tokens carries a *legacy* cost_usd=0.0 on the attempt row while the
# session says tokens-only / NULL cost. With the join, usage_source wins and cost
# is None. Without the join, 0.0 leaks through and the decisive assertion fails.
_ATTEMPTS: list[dict[str, object]] = [
    {
        # Lowest rung of the tier ladder — exercises importance=simple.
        "id": "swa_simple",
        "swarm_run_id": "swr_0",
        "board_task_id": "btk_0",
        "seq": 0,
        "session_id": "ses_simple",
        "provider": "gemini",
        "model": "gemini-3.6-flash",
        "tier": "simple",
        "started_at": "2026-07-28T09:00:00Z",
        "ended_at": "2026-07-28T09:02:00Z",
        "end_reason": "completed",
        "detail": "trivial edit",
        "cost_usd": None,
        "usage_source": None,
        "session": {
            "id": "ses_simple",
            "model": "gemini-3.6-flash",
            "cost_usd": 0.01,
            "usage_source": "cli-report",
        },
    },
    {
        "id": "swa_ok",
        "swarm_run_id": "swr_1",
        "board_task_id": "btk_1",
        "seq": 0,
        "session_id": "ses_ok",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "tier": "complex",
        "started_at": "2026-07-28T10:00:00Z",
        "ended_at": "2026-07-28T10:05:00Z",
        "end_reason": "completed",
        "detail": "landed",
        "cost_usd": None,
        "usage_source": None,
        "session": {
            "id": "ses_ok",
            "model": "gpt-5.6-sol",
            "cost_usd": 1.25,
            "usage_source": "cli-report",
        },
    },
    {
        "id": "swa_denied",
        "swarm_run_id": "swr_1",
        "board_task_id": "btk_2",
        "seq": 0,
        "session_id": "ses_denied",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "tier": "complex",
        "started_at": "2026-07-28T11:00:00Z",
        "ended_at": "2026-07-28T11:10:00Z",
        "end_reason": "review_denied",
        "detail": "review rejected",
        "cost_usd": None,
        "usage_source": None,
        "session": {
            "id": "ses_denied",
            "model": "gpt-5.6-sol",
            "cost_usd": 0.5,
            "usage_source": "cli-report",
        },
    },
    {
        "id": "swa_crash",
        "swarm_run_id": "swr_1",
        "board_task_id": "btk_3",
        "seq": 0,
        "session_id": "ses_crash",
        "provider": "claude",
        "model": "claude-opus",
        "tier": "standard",
        "started_at": "2026-07-28T12:00:00Z",
        "ended_at": "2026-07-28T12:01:00Z",
        "end_reason": "crashed",
        "detail": "segfault",
        "cost_usd": None,
        "usage_source": None,
        "session": {
            "id": "ses_crash",
            "model": "claude-opus",
            "cost_usd": 0.1,
            "usage_source": "cli-report",
        },
    },
    {
        "id": "swa_timeout",
        "swarm_run_id": "swr_2",
        "board_task_id": "btk_4",
        "seq": 0,
        "session_id": "ses_timeout",
        "provider": "grok",
        "model": "grok-4.5",
        "tier": None,
        "started_at": "2026-07-28T13:00:00Z",
        "ended_at": "2026-07-28T13:30:00Z",
        "end_reason": "timeout",
        "detail": "wall clock",
        "cost_usd": None,
        "usage_source": None,
        "session": {
            "id": "ses_timeout",
            "model": "grok-4.5",
            "cost_usd": 2.0,
            "usage_source": "cli-report",
        },
    },
    {
        "id": "swa_killed",
        "swarm_run_id": "swr_2",
        "board_task_id": "btk_5",
        "seq": 0,
        "session_id": "ses_killed",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "tier": "complex",
        "started_at": "2026-07-28T14:00:00Z",
        "ended_at": "2026-07-28T14:02:00Z",
        "end_reason": "killed",
        "detail": "operator",
        "cost_usd": None,
        "usage_source": None,
        "session": {
            "id": "ses_killed",
            "model": "gpt-5.6-sol",
            "cost_usd": 0.0,
            "usage_source": "cli-report",
        },
    },
    {
        # Counterfeit seed: NULL end_reason must stay UNKNOWN, never SUCCESS.
        "id": "swa_null_reason",
        "swarm_run_id": "swr_2",
        "board_task_id": "btk_6",
        "seq": 0,
        "session_id": "ses_null",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "tier": "complex",
        "started_at": "2026-07-28T15:00:00Z",
        "ended_at": "2026-07-28T15:05:00Z",
        "end_reason": None,
        "detail": "session exited cleanly but attempt never closed",
        "cost_usd": None,
        "usage_source": None,
        "session": {
            "id": "ses_null",
            "model": "gpt-5.6-sol",
            "cost_usd": 0.3,
            "usage_source": "cli-report",
        },
    },
    {
        # Decisive cost trap: legacy attempt cost 0.0 + tokens-only session.
        "id": "swa_tokens",
        "swarm_run_id": "swr_3",
        "board_task_id": "btk_7",
        "seq": 0,
        "session_id": "ses_tokens",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "tier": "complex",
        "started_at": "2026-07-28T16:00:00Z",
        "ended_at": "2026-07-28T16:08:00Z",
        "end_reason": "completed",
        "detail": "tokens only, no dollar figure",
        "cost_usd": 0.0,  # legacy seed — NOT a measured zero
        "usage_source": None,
        "session": {
            "id": "ses_tokens",
            "model": "gpt-5.6-sol",
            "cost_usd": None,
            "usage_source": "cli-report-tokens-only",
        },
    },
]

N_ATTEMPTS = len(_ATTEMPTS)
assert N_ATTEMPTS > 0, "fixture definition itself must be non-empty"


def _build_fixture_db(path: Path) -> int:
    """Create the fixture DB. Returns attempt count (asserted non-empty by callers)."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        now = "2026-07-28T12:00:00Z"
        for row in _ATTEMPTS:
            session = row["session"]
            assert isinstance(session, dict)
            conn.execute(
                "INSERT INTO sessions "
                "(id, source, project_dir, provider, state, model, cost_usd, "
                "usage_source, created_at, updated_at) "
                "VALUES (?, 'bridge', '/', 'codex', 'completed', ?, ?, ?, ?, ?)",
                (
                    session["id"],
                    session["model"],
                    session["cost_usd"],
                    session["usage_source"],
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO swarm_attempts "
                "(id, swarm_run_id, board_task_id, seq, session_id, provider, model, "
                "tier, started_at, ended_at, end_reason, detail, cost_usd, usage_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["swarm_run_id"],
                    row["board_task_id"],
                    row["seq"],
                    row["session_id"],
                    row["provider"],
                    row["model"],
                    row["tier"],
                    row["started_at"],
                    row["ended_at"],
                    row["end_reason"],
                    row["detail"],
                    row["cost_usd"],
                    row["usage_source"],
                ),
            )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM swarm_attempts").fetchone()[0]
    finally:
        conn.close()
    return int(n)


@pytest.fixture()
def fixture_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "capture_fixture.sqlite3"
    n = _build_fixture_db(db_path)
    # Precondition: an empty fixture makes every containment check vacuously true.
    assert n > 0, "fixture DB must contain attempts before any other assertion"
    assert n == N_ATTEMPTS
    return db_path


class TestDecisive:
    """Decisive L1 acceptance."""

    def test_fixture_nonempty_and_emits_exactly_n_events(self, fixture_db: Path) -> None:
        events = capture_events(fixture_db)

        # Assert non-empty BEFORE any other check (vacuous-truth trap).
        assert events, "capture_events returned empty list — fixture or query is broken"
        assert len(events) == N_ATTEMPTS

    def test_tokens_only_session_carries_cost_none(self, fixture_db: Path) -> None:
        events = capture_events(fixture_db)
        assert events, "fixture must be non-empty before cost checks"

        by_id = {e.id: e for e in events}
        tokens = by_id["swa_tokens"]
        assert tokens.cost_usd is None, (
            f"tokens-only must be cost_usd=None, never 0.0; got {tokens.cost_usd!r}"
        )
        # Joined usage must not collapse unknown price into a free run.
        assert tokens.cost_usd != 0.0

    def test_known_cost_is_pulled_from_session_join(self, fixture_db: Path) -> None:
        events = capture_events(fixture_db)
        assert events, "fixture must be non-empty"
        ok = {e.id: e for e in events}["swa_ok"]
        assert ok.cost_usd == pytest.approx(1.25)


class TestCounterfeit:
    """NULL end_reason must never be flattered into SUCCESS."""

    def test_null_end_reason_is_unknown_not_success(self, fixture_db: Path) -> None:
        events = capture_events(fixture_db)
        assert events, "fixture must be non-empty before result checks"

        by_id = {e.id: e for e in events}
        null_row = by_id["swa_null_reason"]
        assert null_row.result is EventResult.UNKNOWN
        assert null_row.result is not EventResult.SUCCESS

    def test_end_reason_mapping_table(self, fixture_db: Path) -> None:
        events = capture_events(fixture_db)
        assert events, "fixture must be non-empty"
        by_id = {e.id: e for e in events}
        assert by_id["swa_ok"].result is EventResult.SUCCESS
        assert by_id["swa_denied"].result is EventResult.DENIED
        assert by_id["swa_crash"].result is EventResult.FAILURE
        assert by_id["swa_timeout"].result is EventResult.FAILURE
        assert by_id["swa_killed"].result is EventResult.FAILURE


class TestProvenance:
    def test_provenance_links_run_attempt_session(self, fixture_db: Path) -> None:
        events = capture_events(fixture_db)
        assert events, "fixture must be non-empty"
        ok = {e.id: e for e in events}["swa_ok"]
        assert ok.provenance.run_id == "swr_1"
        assert ok.provenance.attempt_id == "swa_ok"
        assert ok.provenance.session_id == "ses_ok"
        assert ok.provenance.source == "swarm_attempt"


class TestPainAndImportanceAreDerived:
    """W2.2: the producer must populate the two factors it never wrote.

    ``capture.py`` contained no reference to ``pain`` or ``importance`` at all,
    so every captured event inherited the contract default and every downstream
    salience was annihilated to 0.0 by the product.

    Both are DERIVED, and both claim ordering rather than precision:
    pain from the attempt outcome, importance from the complexity tier.
    """

    def test_pain_is_ordered_by_outcome(self, fixture_db: Path) -> None:
        by_id = {e.id: e for e in capture_events(fixture_db)}
        success = by_id["swa_ok"].pain
        denied = by_id["swa_denied"].pain
        failure = by_id["swa_crash"].pain
        assert success is not None and denied is not None and failure is not None
        assert 0.0 < success < denied < failure, (
            f"expected 0 < success({success}) < denied({denied}) < failure({failure})"
        )

    def test_successful_attempt_has_nonzero_pain(self, fixture_db: Path) -> None:
        """A landed attempt still cost tokens and wall-clock.

        It must also not be 0.0: salience is a product, so pain 0.0 would
        annihilate recency and recurrence and make every success unrankable —
        the exact defect this lane exists to remove.
        """
        ok = {e.id: e for e in capture_events(fixture_db)}["swa_ok"]
        assert ok.pain is not None
        assert ok.pain > 0.0

    def test_unknown_outcome_has_no_pain(self, fixture_db: Path) -> None:
        """NULL end_reason → we do not know whether it failed → unknown cost."""
        null_row = {e.id: e for e in capture_events(fixture_db)}["swa_null_reason"]
        assert null_row.result is EventResult.UNKNOWN
        assert null_row.pain is None, (
            f"unknown outcome must not be assigned a pain number; got {null_row.pain!r}"
        )
        assert null_row.pain != 0.0

    def test_importance_is_ordered_by_tier(self, fixture_db: Path) -> None:
        by_id = {e.id: e for e in capture_events(fixture_db)}
        simple = by_id["swa_simple"].importance
        standard = by_id["swa_crash"].importance  # tier = standard
        complex_ = by_id["swa_ok"].importance  # tier = complex
        assert simple is not None and standard is not None and complex_ is not None
        assert 0.0 < simple < standard < complex_, (
            f"expected 0 < simple({simple}) < standard({standard}) < complex({complex_})"
        )

    def test_absent_tier_has_no_importance(self, fixture_db: Path) -> None:
        """``swarm_attempts.tier`` is nullable; NULL is no signal, not zero."""
        timeout = {e.id: e for e in capture_events(fixture_db)}["swa_timeout"]
        assert timeout.importance is None
        assert timeout.importance != 0.0

    def test_captured_events_are_actually_scorable(self, fixture_db: Path) -> None:
        """The decisive end of W2.2: capture must produce rankable salience.

        Before the fix every captured event scored exactly 0.0. Measured on the
        live corpus 2026-08-06T18:36Z: 904 of 904 distinct archived events (910
        archive lines, 6 of them duplicate ids) and 210 of 210 candidate rows.
        The 211th file in ``candidates/`` is ``queue.json``, not a candidate; an
        earlier denominator counted it and wrongly implied one candidate had
        escaped the collapse. None did.
        """
        now = datetime(2026, 7, 28, 18, 0, 0, tzinfo=UTC)
        events = capture_events(fixture_db)
        assert events
        scores = {e.id: salience_score(e, now) for e in events}

        known = {eid: s for eid, s in scores.items() if s is not None}
        assert known, "no captured event produced a score at all"
        assert any(s > 0.0 for s in known.values()), (
            f"every captured event still scores 0.0 — the defect is not fixed: {scores}"
        )
        assert len(set(known.values())) > 1, (
            f"captured events are not separable by salience: {known}"
        )

        # Unknown stays unknown, in both directions.
        assert scores["swa_null_reason"] is None  # unknown outcome → unknown pain
        assert scores["swa_timeout"] is None  # NULL tier → unknown importance

        # Pain dominates a marginal recency edge: swa_killed (FAILURE, complex,
        # 14:02Z) must outrank swa_tokens (SUCCESS, complex, and *more* recent
        # at 16:08Z) — both sit far inside the 90-day decay window.
        assert known["swa_killed"] > known["swa_tokens"]


class TestInFlightAttemptsAreNotEvents:
    """An attempt that has not finished has not happened yet.

    Capturing it produced an ``UNKNOWN`` event with ``pain=None`` that staged a
    candidate with ``salience=NULL``. The terminal row shares the attempt id, so
    it derives the same event and cluster id, and ``stage_candidate``'s
    ``ON CONFLICT(id) DO NOTHING`` then refused the row that finally had a real
    score — freezing unknown permanently. This is the round-trip check: a later
    correct value must be able to repair the wrong one.
    """

    @staticmethod
    def _db(path: Path, *, ended_at: str | None, end_reason: str | None) -> Path:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO sessions (id, source, project_dir, provider, state, "
                "model, cost_usd, usage_source, created_at, updated_at) "
                "VALUES ('ses_l', 'bridge', '/', 'codex', 'running', 'm', 1.0, "
                "'cli-report', '2026-07-28T10:00:00Z', '2026-07-28T10:00:00Z')"
            )
            conn.execute(
                "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, "
                "session_id, provider, model, tier, started_at, ended_at, end_reason, "
                "detail, cost_usd, usage_source) VALUES "
                "('swa_live','swr','btk',0,'ses_l','codex','m','complex',"
                "'2026-07-28T10:00:00Z',?,?,'oauth refresh failed',NULL,NULL)",
                (ended_at, end_reason),
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_in_flight_attempt_is_not_captured(self, tmp_path: Path) -> None:
        db = self._db(tmp_path / "live.sqlite3", ended_at=None, end_reason=None)
        assert capture_events(db) == [], (
            "an unfinished attempt must not become an UNKNOWN episodic event"
        )

    def test_same_attempt_is_captured_once_it_lands(self, tmp_path: Path) -> None:
        db = self._db(
            tmp_path / "done.sqlite3",
            ended_at="2026-07-28T11:00:00Z",
            end_reason="crashed",
        )
        events = capture_events(db)
        assert len(events) == 1
        ev = events[0]
        assert ev.result is EventResult.FAILURE
        assert ev.pain is not None and ev.pain > 0.0
        score = salience_score(ev, datetime(2026, 7, 28, 12, 0, tzinfo=UTC))
        assert score is not None and score > 0.0, (
            "the terminal form of the attempt must carry a real score"
        )


class TestEndReasonVocabularyIsFullyClassified:
    """A KNOWN disposition must never be filed as UNKNOWN.

    ``swarm_attempts.end_reason`` is a closed vocabulary — migration 044's CHECK
    enforces it and ``swarm.contracts.ATTEMPT_END_REASONS`` mirrors it. Two of
    its members, ``split`` and ``rerouted``, used to reach capture's UNKNOWN
    fallthrough, which cost them their pain, which cost their whole cluster a
    score. Measured on the live runtime DB (2026-08-06T18:36Z): all 30 ``split``
    attempts fell into one 69-member cluster (39 ``timeout`` + 30 ``split``) and
    it was the ONLY NULL among 210 active candidates — ranked below every
    one-off, because SQLite sorts NULL last under ``ORDER BY salience DESC``.

    This is the ratchet, not a spot-check: it iterates the swarm contract, so
    adding an end_reason there without classifying it here fails here.
    """

    @staticmethod
    def _db(path: Path, end_reason: str) -> Path:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO sessions (id, created_at, updated_at) "
                "VALUES ('ses_v', '2026-07-28T10:00:00Z', '2026-07-28T10:00:00Z')"
            )
            conn.execute(
                "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, "
                "session_id, provider, model, tier, started_at, ended_at, end_reason, "
                "detail) VALUES ('swa_v', 'swr', 'btk', 0, 'ses_v', 'codex', 'm', "
                "'complex', '2026-07-28T10:00:00Z', '2026-07-28T11:00:00Z', ?, 'd')",
                (end_reason,),
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_every_contract_end_reason_is_classified_and_scorable(
        self, tmp_path: Path
    ) -> None:
        from omniagentos.swarm.contracts import ATTEMPT_END_REASONS

        assert ATTEMPT_END_REASONS, "the vocabulary itself must be non-empty"
        now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        unclassified: list[str] = []
        unscorable: list[str] = []
        for reason in ATTEMPT_END_REASONS:
            db = self._db(tmp_path / f"v_{reason}.sqlite3", reason)
            (event,) = capture_events(db)
            if event.result is EventResult.UNKNOWN:
                unclassified.append(reason)
                continue
            if event.pain is None or salience_score(event, now) is None:
                unscorable.append(reason)
        assert not unclassified, (
            "these end_reasons are recorded by the scheduler but capture files them "
            f"as UNKNOWN, which nulls every cluster containing one: {unclassified}"
        )
        assert not unscorable, f"classified but still unscorable: {unscorable}"

    def test_split_is_a_known_disposition_not_an_unknown_outcome(
        self, tmp_path: Path
    ) -> None:
        """The live case: 30 second-timeout splits, one 69-member cluster."""
        db = self._db(tmp_path / "split.sqlite3", "split")
        (event,) = capture_events(db)
        assert event.result is EventResult.MOVED
        assert event.pain is not None and event.pain > 0.0

    def test_moved_sits_between_success_and_failure(self, tmp_path: Path) -> None:
        """Cost spent and nothing landed, but nothing broke and no reviewer time."""
        pains = {}
        for reason in ("completed", "split", "crashed"):
            db = self._db(tmp_path / f"band_{reason}.sqlite3", reason)
            (event,) = capture_events(db)
            pains[reason] = event.pain
        assert None not in pains.values()
        assert pains["completed"] < pains["split"] < pains["crashed"], pains

    def test_a_value_outside_the_vocabulary_is_still_unknown(
        self, tmp_path: Path
    ) -> None:
        """Classifying the known set must not turn the fallthrough into a guess."""
        db = self._db(tmp_path / "alien.sqlite3", "teleported")
        (event,) = capture_events(db)
        assert event.result is EventResult.UNKNOWN
        assert event.pain is None


class TestSinceFilter:
    def test_since_is_an_inclusive_lower_bound(self, fixture_db: Path) -> None:
        """Inclusive on purpose: attempt timestamps are second-resolution and
        50 of 904 live terminal attempts share a second with another attempt
        across 20 colliding seconds (re-measured 2026-08-06T18:36Z), so an
        exclusive watermark would silently drop the boundary row's co-timed
        siblings. Duplicates are suppressed by id in the dream cycle instead."""
        boundary = datetime(2026, 7, 28, 14, 2, 0, tzinfo=UTC)  # swa_killed ended_at
        ids = {e.id for e in capture_events(fixture_db, since=boundary)}
        assert "swa_killed" in ids, (
            "an event exactly at the bound must be returned, not skipped"
        )

    def test_since_excludes_earlier_attempts(self, fixture_db: Path) -> None:
        since = datetime(2026, 7, 28, 14, 0, 0, tzinfo=UTC)
        events = capture_events(fixture_db, since=since)
        # Non-empty precondition for the filtered set we expect.
        assert events, "expected some attempts at/after 14:00Z"
        ids = {e.id for e in events}
        assert "swa_killed" in ids
        assert "swa_null_reason" in ids
        assert "swa_tokens" in ids
        assert "swa_ok" not in ids
        assert "swa_denied" not in ids
