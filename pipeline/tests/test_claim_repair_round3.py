"""Regression tests for the 2026-08-08 round-3 cross-lineage review of R5's
second candidate (rejected id
sha256:6c9852047469e42e30ebcb45071aa73ea57235dbcb18fe16e7d8d5b2cebfeeb3).

Two blockers, four majors. Every one of round 2's fixes was "fixed but
relocated" — the right change in one place, with a sibling left behind or a
new failure introduced at the edge. Repros on disk at
.fusion/repro/R2F1.py .. R2F6.py. Findings and this file's coverage:

  R2F1 (BLOCKER) claim.py    — B3's fix (raise on an unreadable marker) made
                               ONE bad file anywhere in claims/ refuse EVERY
                               acquire for EVERY unrelated id: a global DoS
                               traded for a per-file bug. Fixed: count it as
                               LIVE and alert once, never raise.
  R2F2 (BLOCKER) janitor.py  — `sys.path.insert(bridge/); import claim`
                               creates a SECOND "claim" module distinct from
                               "bridge.claim", so `except claim.MarkerUnreadable`
                               silently fails to catch across the boundary.
                               Fixed: `from bridge import claim` (one
                               identity), falling back to the bare sibling
                               import ONLY for standalone script execution,
                               where no other identity exists to alias
                               against. Every test file in this suite was
                               ALSO migrated off the bare-import pattern for
                               the same reason (test_claim.py,
                               test_janitor_claim_reap.py, and this file).
  R2F3 (major)   claim.py    — release() kept its own inline
                               `except (OSError, JSONDecodeError)` instead of
                               migrating to read_claim_marker(): the "one
                               shared helper closes the family" claim was
                               true for 2 of 3 carriers. Fixed: release() now
                               calls read_claim_marker() too.
  R2F5 (major)   janitor.py  — _reap_one_claim silently `return`ed on
                               MarkerUnreadable: no ledger event, no alert.
                               Combined with R2F1's remedy (unreadable counts
                               as live), such a marker would consume a WIP
                               slot forever, unobserved. Fixed: an
                               `instrument_error` ledger event plus an alert.

R2F4 and R2F6 are NOT here by design — the coordinator's own ruling on each
was documentation-only, not a behaviour change (see the round-3 envelope's
payload for the honest wording change on each). Forcing a "flip" on either
would misrepresent what was actually fixed.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import claim, janitor  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "loopqueue"
    (q / "claims").mkdir(parents=True)
    (q / "state").mkdir(parents=True)
    (q / "ledger.jsonl").touch()
    (q / "state" / "landers.json").write_text(json.dumps({
        "repo": "test-repo", "last_tick_ts": _iso(datetime.now(UTC)),
        "status": "ok", "pid": os.getpid(),
    }))
    return q


def _ledger_events(queue: Path) -> list[dict]:
    text = (queue / "ledger.jsonl").read_text()
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _alerts(queue: Path) -> str:
    path = queue / "ALERTS.md"
    return path.read_text() if path.exists() else ""


# --------------------------------------------------------------------------
# R2F1 — one unreadable marker must never DoS every unrelated acquire
# --------------------------------------------------------------------------


def test_r2f1_unreadable_marker_does_not_block_unrelated_acquires(queue: Path) -> None:
    """Adapted from .fusion/repro/R2F1.py (that repro is phrased so PASSING
    means the bug is present — `pytest.raises` wrapping the unrelated
    acquire, with a message describing the DoS). This is the direct,
    positively-phrased version: an unrelated id must succeed."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 5}))
    bad_marker = claim.acquire(queue, "sha256:bad", "actor1", ttl_seconds=3600)
    bad_marker.chmod(0o000)

    try:
        # This must SUCCEED — a bad marker for a DIFFERENT id must never
        # refuse an unrelated acquire.
        path = claim.acquire(queue, "sha256:unrelated", "actor2", ttl_seconds=3600)
        assert path.exists()
    finally:
        bad_marker.chmod(0o644)


def test_r2f1_unreadable_marker_still_counts_against_the_cap(queue: Path) -> None:
    """The DoS fix must not become a new favourable absence in the other
    direction: an unreadable marker still occupies a WIP slot. cap=1, one
    unreadable marker already live: a SECOND, unrelated acquire is refused
    at cap — not with an instrument error, with a normal EXIT_AT_CAP."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 1}))
    bad_marker = claim.acquire(queue, "sha256:bad", "actor1", ttl_seconds=3600)
    bad_marker.chmod(0o000)

    try:
        with pytest.raises(claim.ClaimError) as exc_info:
            claim.acquire(queue, "sha256:unrelated", "actor2", ttl_seconds=3600)
        assert exc_info.value.code == claim.EXIT_AT_CAP
    finally:
        bad_marker.chmod(0o644)


def test_r2f1_unreadable_marker_alerts_once_not_per_call(queue: Path) -> None:
    """count_live_claims() is called on every acquire/wip; an unreadable
    marker that stays unreadable across many calls must alert ONCE, not
    flood ALERTS.md — CONTRACT.md §9's "one alert per parked item, ever"
    doctrine, applied here."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 5}))
    bad_marker = claim.acquire(queue, "sha256:bad", "actor1", ttl_seconds=3600)
    bad_marker.chmod(0o000)

    try:
        for _ in range(5):
            claim.count_live_claims(queue)
        alert_lines = [ln for ln in _alerts(queue).splitlines() if ln.strip()]
        # Exactly one ALERT LINE for this marker — the marker's own path
        # legitimately appears more than once WITHIN that one line (once in
        # the human-readable prefix, again inside the embedded OSError
        # text), so count lines, not raw substring occurrences.
        assert len(alert_lines) == 1, f"expected exactly one alert line, got {len(alert_lines)}"
        assert "sha256_bad.claim" in alert_lines[0]
    finally:
        bad_marker.chmod(0o644)


# --------------------------------------------------------------------------
# R2F2 — janitor.claim and bridge.claim must be ONE module identity
# --------------------------------------------------------------------------


def test_r2f2_janitor_and_claim_share_one_module_identity() -> None:
    """Adapted from .fusion/repro/R2F2.py, but a direct identity check
    instead of the repro's roundabout bare-`import claim` probe — the bare
    import the repro relies on to detect aliasing is EXACTLY the pattern
    the fix removes from the reachable path, so a faithful adaptation checks
    the property that actually matters: janitor's own reference to `claim`
    IS `bridge.claim`, not a second, distinct module object."""
    assert janitor.claim is claim, (
        "bridge.janitor's `claim` reference is not the same module object as "
        "bridge.claim — MarkerUnreadable raised by one will not be caught by "
        "`except claim.MarkerUnreadable` written against the other"
    )
    assert janitor.claim.MarkerUnreadable is claim.MarkerUnreadable


def test_r2f2_marker_unreadable_raised_by_claim_is_caught_by_janitor(queue: Path) -> None:
    """The end-to-end proof: janitor._reap_one_claim() must actually catch
    a MarkerUnreadable that claim.read_claim_marker() raises — not merely
    share a class identity in the abstract, but interoperate for real."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    marker = queue / "claims" / "sha256_unreadable.claim"
    now = datetime.now(UTC)
    marker.write_text(json.dumps({"actor": "x", "expires_at": _iso(now - timedelta(hours=1))}))
    marker.chmod(0o000)

    try:
        j = janitor.Janitor(queue, apply=True)
        # Must not raise ANYTHING uncaught — a real cross-module catch.
        j.sweep(claims_only=True)
        assert marker.exists(), "an unreadable marker must not be reaped"
    finally:
        marker.chmod(0o644)


# --------------------------------------------------------------------------
# R2F3 — release() must use the SAME shared marker reader as every other
# caller; the clone-family fix must cover all three carriers
# --------------------------------------------------------------------------


def test_r2f3_release_uses_the_shared_marker_reader(queue: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adapted from .fusion/repro/R2F3.py: patches bridge.claim.read_claim_marker
    and asserts it WAS called by release() — the inverse of the repro's own
    assertion (which checks it was NOT called, i.e. PASSES while the bug is
    present)."""
    marker = queue / "claims" / "sha256_missing_read.claim"
    marker.write_text(json.dumps({"actor": "my-actor", "expires_at": "2030-01-01T00:00:00Z"}))

    calls = []
    original = claim.read_claim_marker

    def spy(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(claim, "read_claim_marker", spy)

    claim.release(queue, "sha256:missing_read", "my-actor")

    assert calls, "release() did not call the shared read_claim_marker() helper"
    assert not marker.exists()


def test_r2f3_release_raises_marker_unreadable_via_the_shared_path(queue: Path) -> None:
    """The behavioural consequence of using the shared helper: an
    OS-unreadable (not merely corrupt) marker now raises through
    read_claim_marker()'s MarkerUnreadable -> ClaimError(EXIT_LOST_RACE)
    path in release() too, exactly as it already does in count_live_claims()
    and janitor._reap_one_claim()."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    marker = queue / "claims" / "sha256_perm.claim"
    marker.write_text(json.dumps({"actor": "someone", "expires_at": "2030-01-01T00:00:00Z"}))
    marker.chmod(0o000)

    try:
        with pytest.raises(claim.ClaimError) as exc_info:
            claim.release(queue, "sha256:perm", "someone")
        assert exc_info.value.code == claim.EXIT_LOST_RACE
        assert marker.exists()
    finally:
        marker.chmod(0o644)


# --------------------------------------------------------------------------
# R2F5 — an unreadable marker the reaper leaves alone must be OBSERVABLE,
# never a silent, permanent capacity leak
# --------------------------------------------------------------------------


def test_r2f5_reaper_leaves_a_ledger_event_for_an_unreadable_marker(queue: Path) -> None:
    """Adapted from .fusion/repro/R2F5.py. The reaper must not silently skip
    an unreadable marker: an instrument_error ledger event AND an alert are
    both required, so the leak (R2F1's count-as-live) stays observable
    rather than permanent-and-invisible."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    marker = queue / "claims" / "sha256_test.claim"
    now = datetime.now(UTC)
    marker.write_text(json.dumps({"actor": "actor", "expires_at": _iso(now - timedelta(hours=1))}))
    marker.chmod(0o000)

    try:
        j = janitor.Janitor(queue, apply=True)
        j.sweep(claims_only=True)

        events = _ledger_events(queue)
        assert len(events) == 1
        assert events[0]["event"] == "instrument_error"
        assert events[0]["id"] == "sha256:test"

        alerts = _alerts(queue)
        assert "sha256_test.claim" in alerts or "sha256:test" in alerts
    finally:
        marker.chmod(0o644)


def test_r2f5_marker_still_exists_after_the_instrument_error(queue: Path) -> None:
    """The marker itself must survive — R2F5 is about observability of the
    non-reap, not about the reaper being allowed to touch an unreadable file
    at all (that stays refused, per the fail-closed rule from round 2)."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    marker = queue / "claims" / "sha256_survives.claim"
    now = datetime.now(UTC)
    marker.write_text(json.dumps({"actor": "actor", "expires_at": _iso(now - timedelta(hours=1))}))
    marker.chmod(0o000)

    try:
        j = janitor.Janitor(queue, apply=True)
        j.sweep(claims_only=True)
        assert marker.exists()
    finally:
        marker.chmod(0o644)
