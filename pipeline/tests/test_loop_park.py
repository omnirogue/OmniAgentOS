"""Park-marker liveness contract for run-loop.sh / loop-watchdog.sh.

The defect this guards against (2026-08-10): run-loop.sh exits 2 ("every
seat failed — PARKING, alerting once") on total account exhaustion, a
terminal could-not-run per Ruling #4 — and the watchdog used to test
liveness with nothing but `tmux has-session`, so it restarted the parked
role on its very next tick, converting "do not retry this input" into
"retry forever". These tests exercise bridge/loop_park.py — the shared
marker logic both scripts now use — entirely against a temp directory.
Nothing here spawns tmux, `claude -p`, or touches the live
var/loopqueue/ALERTS.md.
"""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge import loop_park as lp

# ---------------------------------------------------------------------------
# parse_reset_time — item (f): real provider message, including timezone.
# ---------------------------------------------------------------------------

def test_parse_reset_time_real_message_with_timezone():
    now = datetime(2026, 8, 10, 2, 14, 37, tzinfo=UTC)
    got = lp.parse_reset_time(
        "You've hit your weekly limit · resets Aug 14 at 4am (America/New_York)",
        now,
    )
    # Aug 14 04:00 America/New_York is EDT (UTC-4) -> 08:00 UTC.
    assert got == datetime(2026, 8, 14, 8, 0, 0, tzinfo=UTC)


def test_parse_reset_time_pm_and_different_date():
    now = datetime(2026, 8, 10, 2, 14, 37, tzinfo=UTC)
    got = lp.parse_reset_time(
        "You've hit your weekly limit · resets Aug 13 at 2pm (America/New_York)",
        now,
    )
    assert got == datetime(2026, 8, 13, 18, 0, 0, tzinfo=UTC)


def test_parse_reset_time_rolls_year_forward_when_date_already_passed():
    # "resets" dates are always in the future in the real message; if the
    # same-year candidate is already behind `now`, it must mean next year.
    now = datetime(2026, 12, 31, 23, 0, 0, tzinfo=UTC)
    got = lp.parse_reset_time(
        "resets Jan 2 at 9am (America/New_York)", now,
    )
    assert got is not None
    assert got.year == 2027


def test_parse_reset_time_absent_or_unparseable_returns_none():
    assert lp.parse_reset_time("", None) is None
    assert lp.parse_reset_time("some unrelated error text", None) is None
    assert lp.parse_reset_time("resets Aug 14 at 4am (Nowhere/Fake)", None) is None


# ---------------------------------------------------------------------------
# write_park / check_park core contract
# ---------------------------------------------------------------------------

@pytest.fixture
def root(tmp_path):
    r = tmp_path / "var" / "loopqueue"
    (r / "state").mkdir(parents=True)
    return r


@pytest.fixture
def alerts(root):
    return root / "ALERTS.md"


def _read_marker(root, role):
    return json.loads(lp.marker_path(root, role).read_text())


def test_write_park_creates_marker_with_required_fields(root, alerts):
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    lp.write_park(
        root, "reviewer", "every seat failed", ["claude6", "claude4"],
        alerts_file=alerts, now=now,
    )
    m = _read_marker(root, "reviewer")
    for key in ("role", "parked_at", "until", "reason", "seats"):
        assert key in m
    assert m["role"] == "reviewer"
    assert m["seats"] == ["claude6", "claude4"]
    assert m["parked_at"] == "2026-08-10T02:00:00Z"


def test_check_park_unexpired_marker_refuses_restart(root, alerts):
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    lp.write_park(root, "reviewer", "every seat failed", ["claude6"], alerts_file=alerts, now=now)
    later = now + timedelta(minutes=5)
    result = lp.check_park(root, "reviewer", now=later)
    assert result["restart_allowed"] is False
    assert "parked" in result["message"].lower()


def test_check_park_expired_marker_allows_restart(root, alerts):
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    lp.write_park(root, "reviewer", "every seat failed", ["claude6"], alerts_file=alerts, now=now)
    m = _read_marker(root, "reviewer")
    until = lp._parse_ts(m["until"])
    after = until + timedelta(seconds=1)
    result = lp.check_park(root, "reviewer", now=after)
    assert result["restart_allowed"] is True
    assert "expired" in result["message"].lower()
    # check_park() only decides; the marker itself is only actually cleared
    # by a *successful* run-loop.sh iteration (clear_park). Confirm that
    # contract directly:
    assert lp.clear_park(root, "reviewer") is True
    assert not lp.marker_path(root, "reviewer").exists()


def test_clear_park_on_no_marker_is_a_safe_noop(root):
    assert lp.clear_park(root, "reviewer") is False


# ---------------------------------------------------------------------------
# item (d) — corrupt/unreadable marker fails TOWARD restarting, and logs it.
# ---------------------------------------------------------------------------

def test_check_park_corrupt_marker_restarts_and_logs_and_ratelimits(root, alerts):
    marker_path = lp.marker_path(root, "reviewer")
    marker_path.write_text("{not json at all")
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)

    result = lp.check_park(root, "reviewer", alerts_file=alerts, now=now)

    # fail TOWARD liveness
    assert result["restart_allowed"] is True
    assert result["corrupt"] is True
    # logs the parse failure loudly
    assert result["message"]
    assert "corrupt" in result["message"].lower() or "unreadable" in result["message"].lower()

    # rate-limited: the corrupt marker was replaced with a fresh, valid,
    # UNEXPIRED fallback park, so an immediate re-check (same instant, as a
    # tight restart-storm would produce) is refused rather than allowed
    # again — a corrupt marker cannot reproduce the every-tick storm.
    m = _read_marker(root, "reviewer")
    until = lp._parse_ts(m["until"])
    assert until > now
    second = lp.check_park(root, "reviewer", now=now)
    assert second["restart_allowed"] is False


def test_check_park_marker_missing_until_key_is_treated_as_corrupt(root, alerts):
    marker_path = lp.marker_path(root, "reviewer")
    marker_path.write_text(json.dumps({"role": "reviewer"}))  # valid JSON, no "until"
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    result = lp.check_park(root, "reviewer", alerts_file=alerts, now=now)
    assert result["restart_allowed"] is True
    assert result["corrupt"] is True


def test_check_park_no_marker_at_all_allows_restart_silently(root):
    result = lp.check_park(root, "reviewer")
    assert result["restart_allowed"] is True
    assert result["message"] == ""
    assert result["corrupt"] is False


# ---------------------------------------------------------------------------
# item (e) — ALERTS.md line written once per park, not once per tick.
# ---------------------------------------------------------------------------

def test_write_park_called_repeatedly_while_unexpired_alerts_once(root, alerts):
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    for tick in range(5):
        lp.write_park(
            root, "reviewer", "every seat failed", ["claude6", "claude4"],
            alerts_file=alerts, now=now + timedelta(seconds=tick),
        )
    lines = [line for line in alerts.read_text().splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one ALERTS.md line, got {lines}"


def test_write_park_after_expiry_writes_a_new_alert_line(root, alerts):
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    lp.write_park(root, "reviewer", "every seat failed", ["claude6"], alerts_file=alerts, now=now)
    m = _read_marker(root, "reviewer")
    until = lp._parse_ts(m["until"])
    later = until + timedelta(seconds=1)
    lp.write_park(root, "reviewer", "every seat failed", ["claude6"], alerts_file=alerts, now=later)
    lines = [line for line in alerts.read_text().splitlines() if line.strip()]
    assert len(lines) == 2


def test_watchdog_refusal_logged_once_per_park_not_once_per_tick(root, alerts):
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    lp.write_park(root, "reviewer", "every seat failed", ["claude6"], alerts_file=alerts, now=now)
    first = lp.check_park(root, "reviewer", now=now + timedelta(minutes=1))
    second = lp.check_park(root, "reviewer", now=now + timedelta(minutes=2))
    third = lp.check_park(root, "reviewer", now=now + timedelta(minutes=3))
    assert first["restart_allowed"] is False and first["message"] != ""
    assert second["restart_allowed"] is False and second["message"] == ""
    assert third["restart_allowed"] is False and third["message"] == ""


# ---------------------------------------------------------------------------
# `until` derivation: provider-reset vs capped fallback backoff.
# ---------------------------------------------------------------------------

def test_write_park_derives_until_from_provider_reset_message(root, alerts, tmp_path):
    now = datetime(2026, 8, 10, 2, 14, 37, tzinfo=UTC)
    log = tmp_path / "reviewer-loop.log"
    log.write_text(
        "───── 2026-08-10T02:14:36Z reviewer iteration start\n"
        "You've hit your weekly limit · resets Aug 14 at 4am (America/New_York)\n"
    )
    result = lp.write_park(
        root, "reviewer", "every seat failed", ["claude6", "claude4"],
        alerts_file=alerts, log_tail_file=log, now=now,
    )
    m = result["marker"]
    assert m["until_source"] == "provider-reset"
    assert m["until"] == "2026-08-14T08:00:00Z"


def test_write_park_falls_back_to_capped_backoff_when_unparseable(root, alerts):
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    result = lp.write_park(
        root, "reviewer", "every seat failed: unrecognized error text", [],
        alerts_file=alerts, now=now,
    )
    m = result["marker"]
    assert m["until_source"] == "fallback-backoff"
    until = lp._parse_ts(m["until"])
    assert now < until <= now + timedelta(seconds=lp.FALLBACK_CAP_SECONDS)


def test_fallback_backoff_grows_but_stays_capped_across_repeated_unparseable_parks(root, alerts):
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    prev_backoff = 0
    t = now
    for _ in range(8):
        result = lp.write_park(root, "reviewer", "still unparseable", [], alerts_file=alerts, now=t)
        m = result["marker"]
        until = lp._parse_ts(m["until"])
        backoff = (until - t).total_seconds()
        assert backoff <= lp.FALLBACK_CAP_SECONDS
        assert backoff >= prev_backoff
        prev_backoff = backoff
        t = until + timedelta(seconds=1)  # simulate: park expired, watchdog restarted, failed again
    assert prev_backoff == lp.FALLBACK_CAP_SECONDS  # exponential growth actually hit the ceiling


def test_a_parsed_reset_far_in_the_future_is_clamped_to_max_park_seconds(root, alerts):
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    result = lp.write_park(
        root, "reviewer",
        "resets Aug 09 at 1am (America/New_York)",  # parses to ~364 days out
        [], alerts_file=alerts, now=now,
    )
    m = result["marker"]
    until = lp._parse_ts(m["until"])
    assert (until - now).total_seconds() <= lp.MAX_PARK_SECONDS


# ---------------------------------------------------------------------------
# cumulative cap: a chain of renewed parks must not run forever unalerted.
# ---------------------------------------------------------------------------

def test_repeated_park_chain_past_cumulative_cap_alerts_exactly_once(root, alerts, monkeypatch):
    # Shrink the cumulative cap so a handful of exponentially-growing
    # fallback renewals crosses it, rather than needing a real multi-day
    # loop of real 15min/30min/1h/... parks.
    monkeypatch.setattr(lp, "MAX_PARK_SECONDS", 3600)
    now = datetime(2026, 8, 10, 2, 0, 0, tzinfo=UTC)
    t = now
    cap_alert_lines = []
    for _ in range(4):
        result = lp.write_park(root, "reviewer", "still unparseable", [], alerts_file=alerts, now=t)
        cap_alert_lines += [line for line in result["alert_lines"] if "cap" in line.lower()]
        m = result["marker"]
        until = lp._parse_ts(m["until"])
        t = until + timedelta(seconds=1)
    assert len(cap_alert_lines) == 1, f"expected exactly one cap-breach alert, got {cap_alert_lines}"

