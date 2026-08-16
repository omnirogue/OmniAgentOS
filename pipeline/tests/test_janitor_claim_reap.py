"""Pins the claim-reaper half of bridge/janitor.py.

Before this file: the janitor deleted expired/stale-unparseable claim markers
SILENTLY. Evidence attached to proposal sha256:75ba9c5e...: `launchctl print
gui/501/com.threeloops.janitor` showed `runs = 0` and a plist with
`RunAtLoad=false, StartInterval=86400` — the reaper had NEVER fired, so a
4-5h claim TTL was decorative. A silent unlink also made "no claim has ever
been reaped" unfalsifiable even once the reaper does run. These tests pin:

  * a reap emits a `claim_expired` ledger event (REQUIRED TEST)
  * an unparseable marker younger than the 10-minute grace is NEVER reaped —
    it is a claim mid-creation, not an orphan (REQUIRED, by execution)
  * `--claims-only` skips every other sweep
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

# `from bridge import janitor` (never a bare `import janitor`) — see the
# identical note in tests/test_claim.py; R2F2 requires ONE import form for
# every module across the whole suite, not just inside janitor.py itself.
from bridge import janitor  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "loopqueue"
    for sub in ("claims", "inquiries", "findings", "proposals", "candidates",
                "rejected", "parked", "receipts"):
        (q / sub).mkdir(parents=True)
    (q / "ledger.jsonl").touch()
    return q


def _ledger_events(queue: Path) -> list[dict]:
    text = (queue / "ledger.jsonl").read_text()
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _write_expired_marker(queue: Path, ident: str, actor: str) -> Path:
    path = queue / "claims" / f"{ident.replace(':', '_', 1)}.claim"
    now = datetime.now(UTC)
    body = {"actor": actor, "at": _iso(now - timedelta(hours=2)),
            "expires_at": _iso(now - timedelta(minutes=5))}
    path.write_text(json.dumps(body))
    return path


# --------------------------------------------------------------------------
# reap emits a ledger event (REQUIRED TEST)
# --------------------------------------------------------------------------


def test_reap_of_expired_claim_emits_claim_expired_ledger_event(queue: Path) -> None:
    marker = _write_expired_marker(queue, "sha256:dead1", "goal-session@claude-account-7")

    j = janitor.Janitor(queue, apply=True)
    j.sweep(claims_only=True)

    assert not marker.exists()  # actually reaped

    events = _ledger_events(queue)
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "claim_expired"
    assert ev["id"] == "sha256:dead1"
    assert ev["detail"]["dead_actor"] == "goal-session@claude-account-7"
    assert ev["detail"]["age_past_expiry_s"] >= 0
    assert "ts" in ev and "role" in ev


def test_reap_report_mode_does_not_write_but_records_intent(queue: Path) -> None:
    marker = _write_expired_marker(queue, "sha256:dead2", "someone")

    j = janitor.Janitor(queue, apply=False)  # no --apply
    j.sweep(claims_only=True)

    assert marker.exists()  # NOT reaped without --apply
    assert (queue / "ledger.jsonl").read_text() == ""  # NOT written without --apply
    assert any("claim_expired" in a for a in j.actions)  # but intent recorded


def test_live_unexpired_claim_is_untouched(queue: Path) -> None:
    path = queue / "claims" / "sha256_alive.claim"
    now = datetime.now(UTC)
    body = {"actor": "builder-R5", "at": _iso(now),
            "expires_at": _iso(now + timedelta(hours=1))}
    path.write_text(json.dumps(body))

    j = janitor.Janitor(queue, apply=True)
    j.sweep(claims_only=True)

    assert path.exists()
    assert _ledger_events(queue) == []


# --------------------------------------------------------------------------
# the 10-minute young-unparseable exemption, proved by EXECUTION
# --------------------------------------------------------------------------


def test_young_unparseable_marker_is_never_reaped(queue: Path) -> None:
    """O_EXCL creates the marker file EMPTY; the body lands a moment later.
    A marker younger than 10 minutes with no parseable body is a claim
    mid-creation, not an orphan — CONTRACT.md §6's empty-marker rule."""
    marker = queue / "claims" / "sha256_midwrite.claim"
    marker.write_text("")  # empty, as O_EXCL leaves it before the body lands
    # mtime is "now" by construction (just created) — well within the grace.

    j = janitor.Janitor(queue, apply=True)
    j.sweep(claims_only=True)

    assert marker.exists(), "a marker younger than the 10-minute grace must survive a reap"
    assert _ledger_events(queue) == []


def test_old_unparseable_marker_past_grace_is_reaped(queue: Path) -> None:
    """The complement of the test above: once a marker is corrupt/empty AND
    older than the 10-minute grace, it is an orphan and must be reaped, with
    a claim_expired event."""
    import os
    import time

    marker = queue / "claims" / "sha256_stale_midwrite.claim"
    marker.write_text("")
    # Push the mtime back past CLAIM_GRACE_SECONDS (600s) by execution, not
    # by mocking time — the janitor reads the real mtime off disk.
    old = time.time() - (janitor.CLAIM_GRACE_SECONDS + 30)
    os.utime(marker, (old, old))

    j = janitor.Janitor(queue, apply=True)
    j.sweep(claims_only=True)

    assert not marker.exists()
    events = _ledger_events(queue)
    assert len(events) == 1
    assert events[0]["event"] == "claim_expired"
    assert events[0]["id"] == "sha256:stale_midwrite"
    assert events[0]["detail"]["dead_actor"] is None  # never parsed — unknown, not guessed


def test_marker_at_the_grace_boundary_is_not_reaped(queue: Path) -> None:
    """age_s > CLAIM_GRACE_SECONDS is the reap condition (strict >), so a
    marker exactly at the boundary is still protected. Regression guard for
    an off-by-one on the boundary the safety rule depends on."""
    import os
    import time

    marker = queue / "claims" / "sha256_boundary.claim"
    marker.write_text("")
    just_under = time.time() - (janitor.CLAIM_GRACE_SECONDS - 5)
    os.utime(marker, (just_under, just_under))

    j = janitor.Janitor(queue, apply=True)
    j.sweep(claims_only=True)

    assert marker.exists()


# --------------------------------------------------------------------------
# --claims-only skips every other sweep
# --------------------------------------------------------------------------


def test_claims_only_skips_every_other_sweep(queue: Path) -> None:
    # A terminal artifact old enough to be deleted by the FULL sweep.
    art = queue / "proposals" / "sha256_old.json"
    art.write_text(json.dumps({"id": "sha256:old", "kind": "proposal"}))
    import os
    import time
    old = time.time() - (8 * 24 * 3600)
    os.utime(art, (old, old))
    line = json.dumps({"ts": _iso(datetime.now(UTC) - timedelta(days=8)),
                        "role": "implementer", "event": "rejected", "id": "sha256:old",
                        "detail": {}}) + "\n"
    (queue / "ledger.jsonl").write_text(line)

    j = janitor.Janitor(queue, apply=True)
    j.sweep(claims_only=True)

    assert art.exists(), "--claims-only must not touch artifact retention at all"


def test_full_sweep_still_reaps_claims_too(queue: Path) -> None:
    """The full (non --claims-only) sweep must still reap claims — this is a
    regression guard that refactoring the claims logic into a shared method
    did not silently drop it from the default path."""
    marker = _write_expired_marker(queue, "sha256:dead3", "someone")
    # A non-empty ledger: the fixture's zero-byte ledger.jsonl is itself a
    # torn-ledger condition the full sweep correctly refuses (see
    # read_ledger's LedgerTornError) — seed one harmless line so this test
    # exercises the claims-reap path, not that unrelated refusal.
    (queue / "ledger.jsonl").write_text(json.dumps({
        "ts": _iso(datetime.now(UTC)), "role": "external", "event": "found",
        "id": "sha256:unrelated", "detail": {},
    }) + "\n")

    j = janitor.Janitor(queue, apply=True)
    j.sweep()  # no claims_only

    assert not marker.exists()
    events = [e for e in _ledger_events(queue) if e["event"] == "claim_expired"]
    assert len(events) == 1
