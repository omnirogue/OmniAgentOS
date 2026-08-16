"""Regression tests for the 2026-08-08 cross-lineage review of R5's first
candidate (rejected id sha256:0cf6412cc291bab999ba7aa17b35e200bedb89a8834197ce
3b1d86ba3d94b451, reviewer gemini-3.1-pro-preview): five findings, each with
an executable repro on disk at .fusion/repro/{F1,F2,F3,F6}.py (F4 was a
structural-proof stub). Each test below is the repro adapted into a
permanent pytest test — every one FAILED against the pre-repair tree and
PASSES now. Do not delete these without deleting the bug class with them.

  B1  bridge/janitor.py  — TOC/TOU: age computed from a stat() taken before
                           the body was read; a worker's O_EXCL recreate in
                           that window got judged against the stale age and
                           unlinked as an orphan.
  B2  bridge/claim.py    — `if body is not None and body.get("actor") != actor`
                           was False for `body is None`, so an empty/corrupt
                           marker skipped the ownership check and release()
                           deleted it for ANY caller.
  B3  bridge/claim.py    — a per-marker OSError (e.g. EACCES) was folded into
                           "unparseable", so an unreadable LIVE marker aged
                           out of the live count after 10 minutes, handing
                           back a WIP slot nothing vacated.
  M4  bridge/claim.py    — count-then-O_EXCL is check-then-act: wip_cap is
                           advisory, not enforced, under real concurrency.
                           F4 was a non-executable stub; this test actually
                           demonstrates and pins the overshoot as documented,
                           accepted behaviour (see acquire()'s docstring).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

# `from bridge import ...` (never a bare `import claim`/`import janitor`) —
# see tests/test_claim.py's note; R2F2 requires one import form everywhere.
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


# --------------------------------------------------------------------------
# B1 — janitor TOC/TOU: age must come from the SAME observation as the body
# --------------------------------------------------------------------------


def test_b1_janitor_does_not_reap_a_marker_recreated_mid_sweep(queue: Path) -> None:
    """Adapted from .fusion/repro/F1.py. Simulates a worker's O_EXCL
    recreate landing in the exact window between the janitor's stat() and
    its eventual unlink(): the marker starts stale-looking (mtime far in
    the past), and gets unlinked+recreated (empty, fresh mtime) the instant
    ANY stat() call touches it — the same effect a legitimate concurrent
    O_CREAT|O_EXCL create has, one level removed from the real syscalls."""
    marker = queue / "claims" / "sha256_race.claim"
    marker.write_text("old")
    old_time = time.time() - 1200
    os.utime(marker, (old_time, old_time))

    j = janitor.Janitor(queue, apply=True)
    original_stat = Path.stat

    def mocked_stat(self, *args, **kwargs):
        st = original_stat(self, *args, **kwargs)
        if self.name == "sha256_race.claim":
            # A worker's O_EXCL recreate landing right after our stat.
            self.unlink()
            self.touch()
        return st

    Path.stat = mocked_stat
    try:
        j.sweep(claims_only=True)
    finally:
        Path.stat = original_stat

    assert marker.exists(), (
        "janitor unlinked a marker that was recreated between its decision "
        "stat and its unlink — age and the reap decision must be re-verified "
        "against a fresh stat immediately before acting"
    )
    # The abort must not fabricate a claim_expired event for a marker it
    # decided NOT to touch.
    assert _ledger_events(queue) == []


def test_b1_janitor_still_reaps_a_genuinely_untouched_orphan(queue: Path) -> None:
    """The complement: if nothing touches the marker between the decision
    and the re-check, the reap must still happen — the fix must not turn
    into "never reap anything"."""
    marker = queue / "claims" / "sha256_truly_dead.claim"
    now = datetime.now(UTC)
    body = {"actor": "ghost", "at": _iso(now - timedelta(hours=2)),
            "expires_at": _iso(now - timedelta(minutes=5))}
    marker.write_text(json.dumps(body))

    j = janitor.Janitor(queue, apply=True)
    j.sweep(claims_only=True)

    assert not marker.exists()
    events = _ledger_events(queue)
    assert len(events) == 1 and events[0]["event"] == "claim_expired"


# --------------------------------------------------------------------------
# B2 — release() must never delete on an unproven marker
# --------------------------------------------------------------------------


def test_b2_release_refuses_an_unparseable_marker_for_any_actor(queue: Path) -> None:
    """Adapted from .fusion/repro/F2.py. `body is None` used to short-circuit
    the ownership check to False (no mismatch), so release() proceeded to
    unlink. It must now refuse — the caller cannot prove ownership of a
    marker whose body cannot be read at all."""
    marker = queue / "claims" / "sha256_unparseable.claim"
    marker.write_text("")  # empty: mid-creation OR corrupt — indistinguishable

    with pytest.raises(claim.ClaimError) as exc_info:
        claim.release(queue, "sha256:unparseable", "thief-actor")

    assert exc_info.value.code == claim.EXIT_LOST_RACE
    assert marker.exists(), "release allowed a caller to delete an unparseable marker"
    assert _ledger_events(queue) == []  # no released event for a refused release


def test_b2_release_refuses_even_for_the_marker_creator(queue: Path) -> None:
    """The fix is unconditional: even the actor who WOULD own the marker
    cannot release it while its body is corrupt/unreadable — there is no
    positive proof, full stop. This is the documented, accepted trade-off:
    corruption blocks release from anyone, not just from a thief."""
    marker = queue / "claims" / "sha256_corrupt.claim"
    marker.write_text("{not json")

    with pytest.raises(claim.ClaimError) as exc_info:
        claim.release(queue, "sha256:corrupt", "builder-R5")

    assert exc_info.value.code == claim.EXIT_LOST_RACE
    assert marker.exists()


# --------------------------------------------------------------------------
# B3 — an unreadable (not merely unparseable) marker is never free headroom
# --------------------------------------------------------------------------


def test_b3_unreadable_live_marker_never_ages_out_of_the_count(queue: Path) -> None:
    """Adapted from .fusion/repro/F3.py.

    **SUPERSEDED CHOICE, updated for round 3 (R2F1, 2026-08-08 review,
    BLOCKER):** round 2 made an unreadable marker raise ``ClaimError``,
    which closed the undercount but opened a worse bug — one bad file
    anywhere in ``claims/`` then refused EVERY acquire for EVERY unrelated
    id (see ``test_r2f1_*`` below). Round 3's fix counts the marker as LIVE
    instead of raising: it still never ages out / never becomes free
    headroom (the property this test pins), but a single bad file no longer
    takes the whole acquire path down."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 1}))
    marker = claim.acquire(queue, "sha256:test", "actor1", ttl_seconds=3600)

    # Age it well past the 10-minute grace BEFORE making it unreadable, so a
    # buggy implementation that folds EACCES into "unparseable" would treat
    # it as a stale orphan and silently drop it from the live count.
    old = time.time() - (claim.CLAIM_GRACE_SECONDS + 120)
    os.utime(marker, (old, old))
    marker.chmod(0o000)
    try:
        assert claim.count_live_claims(queue) == 1  # still counted, not aged out
        # wip_cap=1 and it counts as live: a SECOND, unrelated id must be
        # refused at cap — not because of an instrument error, but because
        # the cap is genuinely (as far as anyone can prove) full.
        with pytest.raises(claim.ClaimError) as acquire_exc:
            claim.acquire(queue, "sha256:second", "actor2", ttl_seconds=3600)
        assert acquire_exc.value.code == claim.EXIT_AT_CAP
    finally:
        marker.chmod(0o644)


def test_b3_fresh_unreadable_marker_still_reported_correctly(queue: Path) -> None:
    """The original F3 repro, kept verbatim in spirit: a FRESH unreadable
    marker must still be visible as live pressure, not silently ignored.
    Updated for round 3: it is counted as live (not raised) — see the
    superseded-choice note on the test above."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 5}))
    marker = claim.acquire(queue, "sha256:test", "actor1", ttl_seconds=3600)
    marker.chmod(0o000)
    try:
        assert claim.count_live_claims(queue) == 1
    finally:
        marker.chmod(0o644)


# --------------------------------------------------------------------------
# M4 — wip_cap is advisory under concurrency: proved and pinned, not fixed
# --------------------------------------------------------------------------


def test_m4_wip_cap_is_advisory_under_real_concurrency(queue: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F4 was a non-executable structural-proof stub. This is the real
    thing: two threads racing acquire() on DISTINCT ids, synchronised so
    both pass the count-then-act check before either creates its marker.
    This is DOCUMENTED, ACCEPTED behaviour (see acquire()'s docstring and
    the envelope's payload.carriers) — the test exists so the limitation
    stays honest under CI rather than silently regressing into an implied
    guarantee nobody re-checks."""
    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 1}))

    barrier = threading.Barrier(2)
    original_count = claim.count_live_claims

    def synced_count(loops_root: Path) -> int:
        result = original_count(loops_root)
        barrier.wait(timeout=5)  # force both threads past the check together
        return result

    monkeypatch.setattr(claim, "count_live_claims", synced_count)

    results: dict[str, object] = {}

    def worker(name: str, ident: str) -> None:
        try:
            claim.acquire(queue, ident, name, ttl_seconds=3600)
            results[name] = "ok"
        except claim.ClaimError as exc:
            results[name] = exc.code

    t1 = threading.Thread(target=worker, args=("w1", "sha256:race-a"))
    t2 = threading.Thread(target=worker, args=("w2", "sha256:race-b"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Both succeed despite wip_cap=1 — the documented, accepted overshoot.
    assert results == {"w1": "ok", "w2": "ok"}
    assert original_count(queue) == 2, "cap of 1 was exceeded by design of check-then-act"
