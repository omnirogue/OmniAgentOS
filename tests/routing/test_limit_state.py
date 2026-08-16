"""WP2: durable rate-limit / cooldown / inflight authority (routing.limit_state).

Hermetic -- every test runs on a tmp migrated SQLite DB; machine account
detection is disabled; accounts/swarm configs are pointed at tmp files where a
test depends on their values; jitter uses seeded ``random.Random`` instances.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.longhaul.limits import classify_limit_text, classify_terminal
from omniagentos.routing import limit_state
from omniagentos.routing.account_pool import AccountPool, Outcome
from omniagentos.routing.config import Account, AccountPoolConfig, ProviderPool
from tests.support.db_template import migrated_db


@pytest.fixture(autouse=True)
def _no_machine_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never auto-register this machine's real ~/.claude* dirs."""
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])


@pytest.fixture(autouse=True)
def _isolated_accounts_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin configs/accounts.yaml to a known transient base (claude: 100s)."""
    path = tmp_path / "accounts.yaml"
    path.write_text(
        "providers:\n  claude:\n    cooldown_seconds: 100\n    accounts: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIAGENTOS_ACCOUNTS_CONFIG", str(path))


@pytest.fixture
def db(tmp_path: Path) -> str:
    # ``migrated_db(SqliteStore, ...)`` is ``migrate(path)`` with the 86 file
    # applies replaced by a copy of this process's pre-migrated template;
    # SqliteStore's constructor is _connect + migrate_connection, exactly what
    # ``migrate()`` does.
    return migrated_db(SqliteStore, tmp_path / "limit_state.db")


def _conn(db: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    return connection


def _iso_in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_account(
    db: str,
    account_id: str,
    *,
    provider: str = "claude",
    enabled: int = 1,
    status: str = "ok",
    cooldown_until: str | None = None,
    config_dir: str | None = None,
) -> None:
    now = utc_now_iso()
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO claude_accounts "
            "(id, label, auth_type, config_dir, enabled, is_default, status, provider, "
            " cooldown_until, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                account_id,
                "config_dir",
                config_dir,
                enabled,
                0,
                status,
                provider,
                cooldown_until,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _account_row(db: str, account_id: str) -> dict:
    conn = _conn(db)
    try:
        return dict(
            conn.execute("SELECT * FROM claude_accounts WHERE id = ?", (account_id,)).fetchone()
        )
    finally:
        conn.close()


def _seed_session(
    db: str,
    session_id: str,
    *,
    account_id: str | None,
    state: str = "running",
    kill_requested: int = 0,
    last_activity_at: str | None = None,
    age_minutes: float = 0.0,
) -> None:
    stamp = (datetime.now(UTC) - timedelta(minutes=age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO sessions "
            "(id, source, project_dir, provider, state, kill_requested, "
            " last_activity_at, account_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                "bridge",
                "/tmp/project",
                "claude",
                state,
                kill_requested,
                last_activity_at if last_activity_at is not None else stamp,
                account_id,
                stamp,
                stamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_task_session(
    db: str,
    attempt_id: str,
    board_task_id: str,
    *,
    account_id: str | None,
    session_id: str | None,
    ended_at: str | None = None,
) -> None:
    conn = _conn(db)
    try:
        conn.execute(
            "INSERT INTO task_sessions "
            "(id, board_task_id, seq, session_id, harness, model, account_id, "
            " started_at, ended_at, end_reason, detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,NULL,'')",
            (
                attempt_id,
                board_task_id,
                0,
                session_id,
                "cli-claude",
                "opus",
                account_id,
                utc_now_iso(),
                ended_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Four-class outcome table
# ---------------------------------------------------------------------------


class TestReportOutcomeTable:
    def test_transient_rate_limit_jittered_cooldown_within_bounds(self, db: str) -> None:
        """base 100s (pinned accounts.yaml) ±20% -> every sample in [80, 120]s."""
        _seed_account(db, "acct_t")
        samples: set[str] = set()
        for seed in range(12):
            before = datetime.now(UTC)
            result = limit_state.report_outcome(
                "claude",
                "acct_t",
                "transient_rate_limit",
                "429",
                db_path=db,
                rng=Random(seed),
            )
            after = datetime.now(UTC)
            until = _parse_iso(result["cooldown_until"])
            samples.add(result["cooldown_until"])
            low = (until - after).total_seconds()
            high = (until - before).total_seconds()
            # ISO second truncation: allow 1s of slack on the lower bound.
            assert high >= 100 * 0.8 - 1.0
            assert low <= 100 * 1.2
        # It actually jitters: 12 seeds over a 40s window cannot all collide.
        assert len(samples) > 1
        row = _account_row(db, "acct_t")
        assert row["status"] == "rate_limited"
        assert row["cooldown_until"] is not None

    def test_quota_exhausted_cools_until_parsed_reset(self, db: str) -> None:
        _seed_account(db, "acct_q")
        reset_at = _iso_in(6 * 3600)
        result = limit_state.report_outcome(
            "claude",
            "acct_q",
            "quota_exhausted",
            "weekly limit",
            reset_at=reset_at,
            db_path=db,
        )
        assert result["cooldown_until"] == reset_at
        row = _account_row(db, "acct_q")
        assert row["cooldown_until"] == reset_at
        assert row["status"] == "rate_limited"
        # Cooled, NEVER disabled (longhaul invariant: usage limits cool).
        assert row["enabled"] == 1

    def test_quota_exhausted_without_reset_uses_fallback_window(self, db: str) -> None:
        _seed_account(db, "acct_qf")
        before = datetime.now(UTC)
        result = limit_state.report_outcome(
            "claude", "acct_qf", "quota_exhausted", "limit", db_path=db
        )
        until = _parse_iso(result["cooldown_until"])
        seconds = (until - before).total_seconds()
        assert 3600 - 5 <= seconds <= 3600 + 5

    def test_overloaded_short_backoff_and_no_status_change(self, db: str) -> None:
        _seed_account(db, "acct_o", status="ok")
        for seed in range(12):
            before = datetime.now(UTC)
            result = limit_state.report_outcome(
                "claude",
                "acct_o",
                "overloaded",
                "529 overloaded",
                db_path=db,
                rng=Random(seed),
            )
            until = _parse_iso(result["cooldown_until"])
            seconds = (until - before).total_seconds()
            assert 30 - 1.0 <= seconds <= 120 + 1.0
        row = _account_row(db, "acct_o")
        # A backoff, NOT a rate_limited status flip.
        assert row["status"] == "ok"
        assert row["cooldown_until"] is not None
        assert row["enabled"] == 1

    def test_auth_error_stops_the_line(self, db: str) -> None:
        """Disable + operator notification; the account never rotates back in."""
        _seed_account(db, "acct_x")
        result = limit_state.report_outcome(
            "claude", "acct_x", "auth_error", "401 OAuth token revoked", db_path=db
        )
        assert result["disabled"] is True
        row = _account_row(db, "acct_x")
        assert row["enabled"] == 0
        assert row["status"] == "error"
        # Never auto-retried: selection skips it entirely.
        assert limit_state.pick_account("claude", db_path=db) is None
        conn = _conn(db)
        try:
            note = conn.execute(
                "SELECT kind, severity, payload_json FROM notifications WHERE ref_id = ?",
                ("acct_x",),
            ).fetchone()
        finally:
            conn.close()
        assert note is not None
        assert note["kind"] == "escalation"
        assert note["severity"] == "critical"
        assert "account_auth_failure" in note["payload_json"]

    def test_unknown_outcome_raises(self, db: str) -> None:
        _seed_account(db, "acct_u")
        with pytest.raises(ValueError, match="unknown limit outcome"):
            limit_state.report_outcome("claude", "acct_u", "rate_limted", db_path=db)


class TestStructuredFirst:
    def test_ok_clears_cooldown_and_status(self, db: str) -> None:
        """A clean completion overrides whatever an intermediate hint wrote."""
        _seed_account(db, "acct_s")
        limit_state.report_outcome(
            "claude", "acct_s", "transient_rate_limit", "intermediate 429", db_path=db
        )
        assert _account_row(db, "acct_s")["cooldown_until"] is not None
        limit_state.report_outcome("claude", "acct_s", "ok", db_path=db)
        row = _account_row(db, "acct_s")
        assert row["cooldown_until"] is None
        assert row["status"] == "ok"
        assert limit_state.pick_account("claude", db_path=db) is not None

    def test_ok_never_reenables_a_disabled_account(self, db: str) -> None:
        """Auth disables are operator-only reversals -- stop-the-line stays stopped."""
        _seed_account(db, "acct_d")
        limit_state.report_outcome("claude", "acct_d", "auth_error", "401", db_path=db)
        limit_state.report_outcome("claude", "acct_d", "ok", db_path=db)
        row = _account_row(db, "acct_d")
        assert row["enabled"] == 0
        assert row["status"] == "error"

    def test_clean_completion_beats_intermediate_429_for_new_providers(self) -> None:
        events = [
            {
                "type": "system",
                "subtype": "api_retry",
                "error_status": 429,
                "error": "429 too many requests",
            },
            {"type": "result", "subtype": "success", "is_error": False},
        ]
        for provider in ("gemini", "kimi", "grok", "qwen"):
            outcome = classify_terminal(events, 0, 0.0, provider=provider)
            assert outcome["kind"] == "completed"


# ---------------------------------------------------------------------------
# Selection + provider filter
# ---------------------------------------------------------------------------


class TestPickAccount:
    def test_provider_filter(self, db: str) -> None:
        _seed_account(db, "acct_claude", provider="claude")
        _seed_account(db, "acct_grok", provider="grok")
        picked_grok = limit_state.pick_account("grok", db_path=db)
        assert picked_grok is not None and picked_grok.account_id == "acct_grok"
        picked_claude = limit_state.pick_account("claude", db_path=db)
        assert picked_claude is not None and picked_claude.account_id == "acct_claude"
        assert limit_state.pick_account("gemini", db_path=db) is None

    def test_cooling_account_excluded(self, db: str) -> None:
        _seed_account(db, "acct_cool", cooldown_until=_iso_in(3600))
        assert limit_state.pick_account("claude", db_path=db) is None

    def test_list_available_accounts_orders_lru_and_filters(self, db: str) -> None:
        _seed_account(db, "acct_1")
        _seed_account(db, "acct_2")
        _seed_account(db, "acct_gone", cooldown_until=_iso_in(3600))
        _seed_account(db, "acct_other", provider="grok")
        available = limit_state.list_available_accounts("claude", db_path=db)
        assert [row["id"] for row in available] == ["acct_1", "acct_2"]
        # A pick rotates the account to the back of the LRU order.
        limit_state.pick_account("claude", db_path=db)
        available = limit_state.list_available_accounts("claude", db_path=db)
        assert [row["id"] for row in available] == ["acct_2", "acct_1"]


# ---------------------------------------------------------------------------
# Durable inflight
# ---------------------------------------------------------------------------


class TestDurableInflight:
    def test_counts_live_fresh_sessions_only(self, db: str) -> None:
        _seed_account(db, "acct_a")
        _seed_session(db, "ses_run", account_id="acct_a", state="running")
        _seed_session(db, "ses_start", account_id="acct_a", state="starting")
        _seed_session(db, "ses_kill", account_id="acct_a", state="running", kill_requested=1)
        _seed_session(db, "ses_stale", account_id="acct_a", state="running", age_minutes=45)
        _seed_session(db, "ses_done", account_id="acct_a", state="completed")
        _seed_session(db, "ses_other", account_id="acct_b", state="running")
        assert limit_state.inflight_count("acct_a", db_path=db) == 2

    def test_unions_open_longhaul_attempts_without_double_count(self, db: str) -> None:
        _seed_account(db, "acct_a")
        # Longhaul attempt whose session row never got attribution (the
        # historical gap): must count.
        _seed_session(db, "ses_lh", account_id=None, state="running")
        _seed_task_session(db, "tks_1", "btk_1", account_id="acct_a", session_id="ses_lh")
        # Attempt whose session row IS attributed: already counted as a
        # session; the union must not double-count it.
        _seed_session(db, "ses_attr", account_id="acct_a", state="running")
        _seed_task_session(db, "tks_2", "btk_2", account_id="acct_a", session_id="ses_attr")
        # Closed attempt: never counts.
        _seed_task_session(
            db,
            "tks_3",
            "btk_3",
            account_id="acct_a",
            session_id=None,
            ended_at=utc_now_iso(),
        )
        assert limit_state.inflight_count("acct_a", db_path=db) == 2

    def test_reservations_count_as_inflight(self, db: str) -> None:
        _seed_account(db, "acct_a")
        reservation = limit_state.reserve_account("claude", max_inflight=3, db_path=db)
        assert reservation is not None
        assert limit_state.inflight_count("acct_a", db_path=db) == 1


# ---------------------------------------------------------------------------
# Atomic reservations
# ---------------------------------------------------------------------------


class TestReserveAccount:
    def test_two_concurrent_pickers_exactly_one_wins(self, db: str) -> None:
        """limit 1 -> the count+reserve BEGIN IMMEDIATE serializes the race."""
        _seed_account(db, "acct_a")
        barrier = threading.Barrier(2)
        results: list[limit_state.Reservation | None] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            outcome = limit_state.reserve_account("claude", max_inflight=1, db_path=db)
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert winners[0].account.account_id == "acct_a"

    def test_release_frees_the_slot(self, db: str) -> None:
        _seed_account(db, "acct_a")
        first = limit_state.reserve_account("claude", max_inflight=1, db_path=db)
        assert first is not None
        assert limit_state.reserve_account("claude", max_inflight=1, db_path=db) is None
        assert limit_state.release_reservation(first.id, db_path=db) is True
        assert limit_state.reserve_account("claude", max_inflight=1, db_path=db) is not None

    def test_ttl_reclaims_a_crashed_spawn(self, db: str) -> None:
        _seed_account(db, "acct_a")
        expired = limit_state.reserve_account("claude", max_inflight=1, ttl_seconds=0.0, db_path=db)
        assert expired is not None
        # The expired claim is invisible to counts and reaped by the next reserve.
        assert limit_state.inflight_count("acct_a", db_path=db) == 0
        assert limit_state.reserve_account("claude", max_inflight=1, db_path=db) is not None

    def test_convert_hands_the_slot_to_the_sessions_row(self, db: str) -> None:
        _seed_account(db, "acct_a")
        reservation = limit_state.reserve_account("claude", max_inflight=1, db_path=db)
        assert reservation is not None
        # Supervisor persists sessions.account_id at launch, then converts.
        _seed_session(db, "ses_new", account_id="acct_a", state="running")
        assert limit_state.convert_reservation(reservation.id, "ses_new", db_path=db) is True
        # Slot is carried by the session now: exactly 1 inflight, no double count.
        assert limit_state.inflight_count("acct_a", db_path=db) == 1
        # Converting again reports the reservation gone.
        assert limit_state.convert_reservation(reservation.id, "ses_new", db_path=db) is False

    def test_cooling_accounts_are_not_reservable(self, db: str) -> None:
        _seed_account(db, "acct_cool", cooldown_until=_iso_in(3600))
        assert limit_state.reserve_account("claude", max_inflight=5, db_path=db) is None


# ---------------------------------------------------------------------------
# Pressure + all_cooling
# ---------------------------------------------------------------------------


class TestPressure:
    @pytest.fixture(autouse=True)
    def _fixed_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(limit_state, "max_inflight_per_account", lambda provider: 2)

    def test_no_enabled_accounts_is_full_pressure(self, db: str) -> None:
        assert limit_state.provider_pressure("claude", db_path=db) == 1.0

    def test_cooling_fraction(self, db: str) -> None:
        _seed_account(db, "acct_a", cooldown_until=_iso_in(3600))
        _seed_account(db, "acct_b")
        assert limit_state.provider_pressure("claude", db_path=db) == pytest.approx(0.5)

    def test_inflight_saturation_fraction(self, db: str) -> None:
        _seed_account(db, "acct_a")
        _seed_account(db, "acct_b")
        # capacity = 2 accounts x ceiling 2 = 4; 2 live sessions -> 0.5.
        _seed_session(db, "ses_1", account_id="acct_a", state="running")
        _seed_session(db, "ses_2", account_id="acct_a", state="running")
        assert limit_state.provider_pressure("claude", db_path=db) == pytest.approx(0.5)

    def test_combined_pressure_clamps_at_one(self, db: str) -> None:
        _seed_account(db, "acct_a", cooldown_until=_iso_in(3600))
        _seed_account(db, "acct_b")
        _seed_session(db, "ses_1", account_id="acct_b", state="running")
        _seed_session(db, "ses_2", account_id="acct_b", state="running")
        _seed_session(db, "ses_3", account_id="acct_b", state="starting")
        # 0.5 cooling + 3/4 saturation clamps to 1.0.
        assert limit_state.provider_pressure("claude", db_path=db) == 1.0

    def test_all_cooling(self, db: str) -> None:
        _seed_account(db, "acct_a", cooldown_until=_iso_in(3600))
        assert limit_state.all_cooling("claude", db_path=db) is True
        _seed_account(db, "acct_b")
        assert limit_state.all_cooling("claude", db_path=db) is False
        # Providers with no accounts at all report cooling (same fallback
        # signal as AccountPool.all_cooling).
        assert limit_state.all_cooling("gemini", db_path=db) is True


# ---------------------------------------------------------------------------
# Fleet session budget
# ---------------------------------------------------------------------------


class TestFleetAvailable:
    def test_pure_math(self) -> None:
        snapshot = limit_state.fleet_available(120, 20, total_live=30, swarm_live=5)
        assert snapshot.available_global == 90
        # Swarm may only grow the fleet to (120 - 20) = 100 sessions.
        assert snapshot.available_for_swarm == 90

    def test_swarm_ceiling_binds_before_global(self) -> None:
        snapshot = limit_state.fleet_available(120, 20, total_live=40, swarm_live=95)
        assert snapshot.available_global == 80
        assert snapshot.available_for_swarm == 5

    def test_global_exhaustion_floors_at_zero(self) -> None:
        snapshot = limit_state.fleet_available(120, 20, total_live=130, swarm_live=110)
        assert snapshot.available_global == 0
        assert snapshot.available_for_swarm == 0

    def test_reserved_headroom_is_protected(self) -> None:
        # 100 live of which 100 are swarm: small tasks still have their 20.
        snapshot = limit_state.fleet_available(120, 20, total_live=100, swarm_live=100)
        assert snapshot.available_global == 20
        assert snapshot.available_for_swarm == 0

    def test_reads_live_counts_from_db(self, db: str) -> None:
        _seed_account(db, "acct_a")
        _seed_session(db, "ses_1", account_id="acct_a", state="running")
        _seed_session(db, "ses_2", account_id=None, state="starting")
        _seed_session(db, "ses_kill", account_id=None, state="running", kill_requested=1)
        _seed_session(db, "ses_stale", account_id=None, state="running", age_minutes=45)
        snapshot = limit_state.fleet_available(10, 2, db_path=db)
        assert snapshot.total_live == 2
        assert snapshot.available_global == 8


# ---------------------------------------------------------------------------
# Provider output-pattern classification (longhaul/limits extension)
# ---------------------------------------------------------------------------


class TestClassifyLimitText:
    @pytest.mark.parametrize(
        ("provider", "text", "expected"),
        [
            (
                "kimi",
                "Error: rate_limit_reached_error: your account reached max request",
                "transient_rate_limit",
            ),
            (
                "kimi",
                "exceeded_current_quota_error: please check your account balance",
                "quota_exhausted",
            ),
            ("kimi", "engine_overloaded_error: the engine is currently overloaded", "overloaded"),
            ("kimi", "invalid_authentication_error: auth failed", "auth_error"),
            ("gemini", "status 429 RESOURCE_EXHAUSTED", "transient_rate_limit"),
            (
                "gemini",
                "Quota exceeded for quota metric 'Generate requests per day'",
                "quota_exhausted",
            ),
            ("gemini", "API key not valid. Please pass a valid API key.", "auth_error"),
            ("gemini", "The model is overloaded. Please try again later.", "overloaded"),
            ("grok", "429 Too Many Requests", "transient_rate_limit"),
            ("grok", "Your team has run out of credits", "quota_exhausted"),
            ("grok", "Incorrect API key provided", "auth_error"),
            # Unknown provider falls back to the generic table.
            ("codex", "usage limit reached for this account", "quota_exhausted"),
            ("codex", "429 too many requests", "transient_rate_limit"),
        ],
    )
    def test_provider_patterns(self, provider: str, text: str, expected: str) -> None:
        assert classify_limit_text(provider, text) == expected

    @pytest.mark.parametrize(
        ("provider", "text"),
        [
            ("grok", "Segmentation fault (core dumped)"),
            ("gemini", "SyntaxError: unexpected token"),
            ("kimi", "the request was malformed"),
        ],
    )
    def test_plain_errors_do_not_classify(self, provider: str, text: str) -> None:
        """A normal crash is NOT evidence about the account -- must not cool."""
        assert classify_limit_text(provider, text) is None

    @pytest.mark.parametrize(
        ("provider", "text"),
        [
            # Node stack-trace frames: file:line:column positions embedding
            # 429/401/503 must NEVER classify (live gemini crash streams are
            # full of these — a false match cools/disables a healthy account).
            ("gemini", "at generateContent (file:///opt/gemini/dist/geminiChat.js:429:12)"),
            ("gemini", "at Object.run (/usr/lib/node_modules/cli/loader.js:401:5)"),
            ("gemini", "at process (/opt/gemini/dist/core.js:503:22)"),
            ("grok", "panic at runtime/proc.go:429"),
            ("kimi", "at handler (bundle.min.js:1:401)"),
            # Generic table (unknown provider) gets the same protection.
            ("codex", "Error at file:///app/dist/main.js:429:17"),
            # Larger numbers / dotted runs containing the code.
            ("gemini", "downloaded 14290 bytes in 0.429 seconds"),
            ("grok", "request id 88429177"),
            # F3 pin: a stack frame's line:column position (no status code at
            # all here) must never classify.
            ("gemini", "at file:///opt/gemini/dist/geminiChat.js:445:32"),
            # Separator followed by a digit is still a position, not phrasing.
            ("gemini", "at geminiChat.js:429:12"),
            ("gemini", "worker exited at 429.5 seconds"),
        ],
    )
    def test_stack_trace_numbers_never_classify(self, provider: str, text: str) -> None:
        assert classify_limit_text(provider, text) is None

    @pytest.mark.parametrize(
        ("provider", "text", "expected"),
        [
            # Real bare status codes in ordinary error phrasing still classify.
            ("gemini", "HTTP 429 Too Many Requests", "transient_rate_limit"),
            ("gemini", "server returned 401", "auth_error"),
            ("gemini", "upstream error 503", "overloaded"),
            ("grok", "got 429 from api", "transient_rate_limit"),
            ("kimi", "status code: 429", "transient_rate_limit"),
            ("codex", "request failed with 429", "transient_rate_limit"),
            ("codex", "response 401 returned", "auth_error"),
            # F3 pin: a code followed by ':' or '.' and NON-digit text is real
            # error phrasing — the trailing guard only rejects separator-
            # followed-by-digit, so these MUST classify.
            ("gemini", "error 429: x", "transient_rate_limit"),
            ("gemini", "HTTP 429.", "transient_rate_limit"),
            ("gemini", "HTTP Error 401: Unauthorized", "auth_error"),
            ("grok", "error 503: service unavailable", "overloaded"),
        ],
    )
    def test_real_bare_status_codes_still_classify(
        self, provider: str, text: str, expected: str
    ) -> None:
        assert classify_limit_text(provider, text) == expected


class TestClassifyTerminalProviders:
    def test_kimi_rate_limit_terminal_error(self) -> None:
        events = [
            {"type": "result", "is_error": True, "error": "rate_limit_reached_error"},
        ]
        outcome = classify_terminal(events, 1, 0.0, provider="kimi")
        assert outcome["kind"] == "usage_limited"
        assert outcome["detail"].startswith("transient_rate_limit:")

    def test_gemini_auth_terminal_error(self) -> None:
        events = [
            {
                "type": "result",
                "is_error": True,
                "error": "API key not valid. Please pass a valid API key.",
            },
        ]
        outcome = classify_terminal(events, 1, 0.0, provider="gemini")
        assert outcome["kind"] == "auth_failed"

    def test_default_provider_behavior_unchanged(self) -> None:
        events = [
            {"type": "result", "is_error": True, "error": "You've reached your usage limit"},
        ]
        outcome = classify_terminal(events, 1, 0.0)
        assert outcome["kind"] == "usage_limited"
        assert outcome["detail"].startswith("terminal rate limit:")


# ---------------------------------------------------------------------------
# AccountPool as a short-TTL write-through cache
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _pool_for(db: str, tmp_path: Path, clock: FakeClock, ttl: float = 5.0) -> AccountPool:
    config_dir = tmp_path / "cfg-pool"
    config_dir.mkdir(exist_ok=True)
    _seed_account(db, "acct_pool", config_dir=str(config_dir))
    config = AccountPoolConfig(
        providers={
            "claude": ProviderPool(
                cooldown_seconds=60,
                accounts=[Account(id="account-1", config_dir=str(config_dir))],
            )
        }
    )
    return AccountPool(config, now=clock, durable_db_path=db, durable_ttl_seconds=ttl)


class TestAccountPoolWriteThrough:
    def test_rate_limited_report_writes_durable_cooldown(self, db: str, tmp_path: Path) -> None:
        pool = _pool_for(db, tmp_path, FakeClock())
        picked = pool.pick("claude")
        assert picked is not None
        pool.report("account-1", Outcome.RATE_LIMITED)
        row = _account_row(db, "acct_pool")
        assert row["cooldown_until"] is not None
        assert row["status"] == "rate_limited"

    def test_ok_report_clears_durable_cooldown(self, db: str, tmp_path: Path) -> None:
        pool = _pool_for(db, tmp_path, FakeClock())
        pool.pick("claude")
        pool.report("account-1", Outcome.RATE_LIMITED)
        pool.pick("claude")  # no-op pick attempt keeps in_flight sane
        pool.report("account-1", Outcome.OK)
        row = _account_row(db, "acct_pool")
        assert row["cooldown_until"] is None
        assert row["status"] == "ok"

    def test_error_report_is_not_written_through(self, db: str, tmp_path: Path) -> None:
        pool = _pool_for(db, tmp_path, FakeClock())
        pool.pick("claude")
        pool.report("account-1", Outcome.ERROR)
        row = _account_row(db, "acct_pool")
        assert row["cooldown_until"] is None
        assert row["status"] == "ok"

    def test_pick_honors_cooldown_written_by_another_process(self, db: str, tmp_path: Path) -> None:
        clock = FakeClock()
        pool = _pool_for(db, tmp_path, clock)
        # Another process (longhaul engine / sessions daemon) cools the account.
        limit_state.report_outcome(
            "claude", "acct_pool", "quota_exhausted", reset_at=_iso_in(3600), db_path=db
        )
        assert pool.pick("claude") is None

    def test_pick_rechecks_durable_state_after_ttl(self, db: str, tmp_path: Path) -> None:
        clock = FakeClock()
        pool = _pool_for(db, tmp_path, clock, ttl=5.0)
        limit_state.report_outcome(
            "claude", "acct_pool", "transient_rate_limit", db_path=db, rng=Random(1)
        )
        assert pool.pick("claude") is None
        # The other process clears the cooldown (structured-first ok)...
        limit_state.report_outcome("claude", "acct_pool", "ok", db_path=db)
        # ...but the durable cooldown was mapped onto the local clock, so the
        # account stays cooling locally until that expires; advance past it
        # and past the TTL, then the re-check sees the durable clear.
        clock.advance(130.0)
        assert pool.pick("claude") is not None

    def test_without_durable_binding_behavior_is_unchanged(self, tmp_path: Path) -> None:
        config = AccountPoolConfig(
            providers={
                "claude": ProviderPool(
                    cooldown_seconds=60,
                    accounts=[Account(id="account-1", config_dir=str(tmp_path))],
                )
            }
        )
        pool = AccountPool(config, now=FakeClock())
        picked = pool.pick("claude")
        assert picked is not None and picked.id == "account-1"
        pool.report("account-1", Outcome.RATE_LIMITED)  # no DB -> purely local
        assert pool.pick("claude") is None
