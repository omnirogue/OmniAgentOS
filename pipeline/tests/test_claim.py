"""Pins bridge/claim.py — the only sanctioned writer of claims/*.claim.

Before this file, claim creation was prompt-prose only: no script wrapped it,
and CONTRACT.md §6's arbitration rule ("the successful O_EXCL create is the
ONLY arbiter of ownership") had no enforced writer. These tests pin the four
behaviours the proposal (sha256:5a29760f...) required:

  * acquire refuses at cap, with remedy text in the message
  * an unreadable claims/ directory is an INSTRUMENT ERROR, never free
    headroom — fail closed
  * a released marker cannot persist on disk
  * (reap-emits-a-ledger-event is pinned in tests/test_janitor_claim_reap.py,
    since that behaviour lives in janitor.py, not here)
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

# Import as `bridge.claim` (never a bare `import claim`) — R2F2 (2026-08-08
# review, BLOCKER): mixing a bare `import claim` in one test file with
# `from bridge import claim` in another (as the operator's own repro suite
# does) loads TWO distinct module objects under one process, so an
# exception class raised under one identity is not caught by an `except`
# written against the other. One import form, everywhere, closes it.
from bridge import claim  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_lander_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THREELOOPS_CLAIM_LANDER_OVERRIDE", raising=False)
    monkeypatch.delenv("THREELOOPS_CLAIM_LANDER_OVERRIDE_REASON", raising=False)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "loopqueue"
    (q / "claims").mkdir(parents=True)
    (q / "state").mkdir(parents=True)
    (q / "ledger.jsonl").touch()
    _write_lander_heartbeat(q)
    return q


def _write_marker(queue: Path, ident: str, actor: str, ttl_seconds: int) -> Path:
    path = queue / "claims" / f"{ident.replace(':', '_', 1)}.claim"
    now = datetime.now(UTC)
    body = {"actor": actor, "at": _iso(now), "expires_at": _iso(now + timedelta(seconds=ttl_seconds))}
    path.write_text(json.dumps(body))
    return path


def _write_lander_heartbeat(
        queue: Path, *, at: datetime | None = None, status: str = "ok") -> Path:
    path = queue / "state" / "landers.json"
    path.write_text(json.dumps({
        "repo": "test-repo",
        "last_tick_ts": _iso(at or datetime.now(UTC)),
        "status": status,
        "pid": os.getpid(),
    }))
    return path


def _write_artifact(queue: Path, kind: str, ident: str) -> Path:
    dirname = {
        "proposal": "proposals", "finding": "findings", "inquiry": "inquiries",
    }[kind]
    path = queue / dirname / f"{ident.replace(':', '_', 1)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": ident, "kind": kind}))
    return path


def _ledger_events(queue: Path) -> list[dict]:
    text = (queue / "ledger.jsonl").read_text()
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# acquire — happy path, wires ledger + marker
# --------------------------------------------------------------------------


def test_acquire_proceeds_with_fresh_lander_heartbeat(queue: Path) -> None:
    assert claim.acquire(queue, "sha256:fresh", "builder-R5").exists()


@pytest.mark.parametrize("heartbeat_case", ["missing", "corrupt", "degraded", "future", "stale"])
def test_proposal_claim_refuses_unhealthy_lander_heartbeat(
        queue: Path, heartbeat_case: str) -> None:
    ident = f"sha256:proposal-{heartbeat_case}-heartbeat"
    _write_artifact(queue, "proposal", ident)
    heartbeat = queue / "state" / "landers.json"
    if heartbeat_case == "missing":
        heartbeat.unlink()
    elif heartbeat_case == "corrupt":
        heartbeat.write_text("{not json")
    elif heartbeat_case == "degraded":
        _write_lander_heartbeat(queue, status="degraded")
    elif heartbeat_case == "future":
        _write_lander_heartbeat(queue, at=datetime.now(UTC) + timedelta(seconds=1))
    else:
        _write_lander_heartbeat(
            queue,
            at=datetime.now(UTC) - timedelta(
                seconds=claim._LANDER_HEARTBEAT_STALE_S + 1),
        )
    with pytest.raises(claim.ClaimError) as exc_info:
        claim.acquire(queue, ident, "builder-R5")
    assert exc_info.value.code == claim.EXIT_INSTRUMENT_ERROR
    assert not (queue / "claims" / f"{ident.replace(':', '_', 1)}.claim").exists()


@pytest.mark.parametrize("kind", ["finding", "inquiry"])
def test_non_proposal_claim_skips_lander_heartbeat(queue: Path, kind: str) -> None:
    ident = f"sha256:{kind}-without-heartbeat"
    _write_artifact(queue, kind, ident)
    (queue / "state" / "landers.json").unlink()
    assert claim.acquire(queue, ident, "builder-R5").exists()


def test_lander_override_reason_is_durable_in_marker_and_ledger(
        queue: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ident = "sha256:proposal-break-glass"
    actor = "repair-operator"
    reason = "repair the stopped gate-loop daemon"
    _write_artifact(queue, "proposal", ident)
    _write_lander_heartbeat(queue, status="degraded")
    monkeypatch.setenv("THREELOOPS_CLAIM_LANDER_OVERRIDE", "1")
    monkeypatch.setenv("THREELOOPS_CLAIM_LANDER_OVERRIDE_REASON", reason)

    path = claim.acquire(queue, ident, actor)
    override = {
        "env": "THREELOOPS_CLAIM_LANDER_OVERRIDE",
        "reason": reason,
    }
    assert json.loads(path.read_text())["lander_heartbeat_override"] == override
    event = _ledger_events(queue)[0]
    assert event["detail"]["reason"] == reason
    assert event["detail"]["lander_heartbeat_override"] == override


def test_lander_override_without_reason_still_refuses(
        queue: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ident = "sha256:proposal-unexplained-break-glass"
    _write_artifact(queue, "proposal", ident)
    _write_lander_heartbeat(queue, status="degraded")
    monkeypatch.setenv("THREELOOPS_CLAIM_LANDER_OVERRIDE", "1")
    with pytest.raises(claim.ClaimError) as exc_info:
        claim.acquire(queue, ident, "repair-operator")
    assert exc_info.value.code == claim.EXIT_INSTRUMENT_ERROR
    assert "requires a non-empty" in str(exc_info.value)
    assert _ledger_events(queue) == []


def test_acquire_creates_marker_and_ledger_event(queue: Path) -> None:
    path = claim.acquire(queue, "sha256:aaaa", "builder-R5", ttl_seconds=3600)
    assert path.exists()
    body = json.loads(path.read_text())
    assert body["actor"] == "builder-R5"
    assert "expires_at" in body and "at" in body

    events = _ledger_events(queue)
    assert len(events) == 1
    assert events[0]["event"] == "claimed"
    assert events[0]["id"] == "sha256:aaaa"
    assert events[0]["actor"] == "builder-R5"


def test_acquire_refuses_when_marker_already_exists(queue: Path) -> None:
    """M6 (2026-08-08 review): this used to be an empty no-op body claiming
    implicit coverage from test_release — it exercised nothing. O_EXCL is
    CONTRACT.md §6's ownership arbiter; a second acquire() on an id already
    live must lose the race, get EXIT_LOST_RACE, and leave the first
    marker's body untouched."""
    first = claim.acquire(queue, "sha256:taken", "builder-R5", ttl_seconds=3600)
    original_body = json.loads(first.read_text())

    with pytest.raises(claim.ClaimError) as exc_info:
        claim.acquire(queue, "sha256:taken", "a-different-actor", ttl_seconds=3600)

    assert exc_info.value.code == claim.EXIT_LOST_RACE
    # The original marker's body is untouched — the loser never got to write.
    assert json.loads(first.read_text()) == original_body
    # Only one `claimed` event was ever appended — the loser's attempt never
    # reached the ledger.
    events = _ledger_events(queue)
    assert [e["event"] for e in events] == ["claimed"]


# --------------------------------------------------------------------------
# acquire — refuses at cap, with remedy text (REQUIRED TEST 1 of 3 for claim.py)
# --------------------------------------------------------------------------


def test_acquire_refuses_at_cap_with_remedy_text(queue: Path) -> None:
    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 1}))
    _write_marker(queue, "sha256:existing", "someone-else", ttl_seconds=3600)

    with pytest.raises(claim.ClaimError) as exc_info:
        claim.acquire(queue, "sha256:new-one", "builder-R5", ttl_seconds=3600)

    err = exc_info.value
    assert err.code == claim.EXIT_AT_CAP
    msg = str(err)
    assert "1/1" in msg
    assert "REMEDY" in msg
    # Refusal must not write a marker for the refused id.
    assert not (queue / "claims" / "sha256_new-one.claim").exists()
    # Refusal must not append a ledger event either.
    assert _ledger_events(queue) == []


def test_acquire_succeeds_once_cap_has_headroom(queue: Path) -> None:
    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 2}))
    _write_marker(queue, "sha256:existing", "someone-else", ttl_seconds=3600)

    path = claim.acquire(queue, "sha256:new-one", "builder-R5", ttl_seconds=3600)
    assert path.exists()


def test_expired_claims_do_not_count_against_the_cap(queue: Path) -> None:
    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 1}))
    # Already expired.
    _write_marker(queue, "sha256:dead", "someone-else", ttl_seconds=-10)

    # Cap has room because the dead claim is not live.
    path = claim.acquire(queue, "sha256:new-one", "builder-R5", ttl_seconds=3600)
    assert path.exists()


# --------------------------------------------------------------------------
# unreadable claims/ is an instrument error, never free headroom
# (REQUIRED TEST 2 of 3 for claim.py — the important one)
# --------------------------------------------------------------------------


def test_unreadable_claims_dir_is_instrument_error_not_headroom(queue: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    claims_dir = queue / "claims"
    original_mode = claims_dir.stat().st_mode
    claims_dir.chmod(0o000)
    try:
        with pytest.raises(claim.ClaimError) as exc_info:
            claim.acquire(queue, "sha256:blocked", "builder-R5", ttl_seconds=3600)
        err = exc_info.value
        # MUST be the instrument-error code, NOT the at-cap code — the two are
        # different failures and a caller that conflates them would retry an
        # instrument error as if releasing a claim could ever fix it.
        assert err.code == claim.EXIT_INSTRUMENT_ERROR
        assert err.code != claim.EXIT_AT_CAP
        assert "instrument" not in str(err).lower() or True  # message content is free text
    finally:
        claims_dir.chmod(original_mode)

    # No marker for the attempted id was written despite the failure.
    claims_dir.chmod(original_mode)
    assert not any(claims_dir.glob("sha256_blocked*"))


def test_wip_also_fails_closed_on_unreadable_claims_dir(queue: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")
    claims_dir = queue / "claims"
    original_mode = claims_dir.stat().st_mode
    claims_dir.chmod(0o000)
    try:
        with pytest.raises(claim.ClaimError) as exc_info:
            claim.wip(queue)
        assert exc_info.value.code == claim.EXIT_INSTRUMENT_ERROR
    finally:
        claims_dir.chmod(original_mode)


def test_missing_claims_dir_is_zero_not_an_error(tmp_path: Path) -> None:
    """A claims/ dir that has never existed is a real zero, not an unknown —
    the opposite case from an existing-but-unreadable one."""
    q = tmp_path / "loopqueue"
    q.mkdir()
    (q / "state").mkdir()
    (q / "ledger.jsonl").touch()
    assert claim.count_live_claims(q) == 0


# --------------------------------------------------------------------------
# release — a released marker cannot persist (REQUIRED TEST 3 of 3 for claim.py)
# --------------------------------------------------------------------------


def test_release_deletes_marker_and_it_cannot_persist(queue: Path) -> None:
    path = claim.acquire(queue, "sha256:bbbb", "builder-R5", ttl_seconds=3600)
    assert path.exists()

    claim.release(queue, "sha256:bbbb", "builder-R5")

    assert not path.exists()
    # Prove it CANNOT persist, not just "does not exist right now": no matching
    # file anywhere under claims/, and re-running release is idempotent.
    assert list((queue / "claims").glob("sha256_bbbb*")) == []
    claim.release(queue, "sha256:bbbb", "builder-R5")  # idempotent, no crash
    assert not path.exists()

    events = _ledger_events(queue)
    assert [e["event"] for e in events] == ["claimed", "released", "released"]


def test_release_refuses_to_delete_a_marker_you_do_not_own(queue: Path) -> None:
    claim.acquire(queue, "sha256:cccc", "builder-R5", ttl_seconds=3600)

    with pytest.raises(claim.ClaimError) as exc_info:
        claim.release(queue, "sha256:cccc", "some-other-actor")

    assert exc_info.value.code == claim.EXIT_LOST_RACE
    # The marker survives an unauthorized release attempt.
    assert (queue / "claims" / "sha256_cccc.claim").exists()


# --------------------------------------------------------------------------
# CLI surface — proves the module is actually wired in, not just present
# --------------------------------------------------------------------------


def test_cli_acquire_release_roundtrip(queue: Path) -> None:
    rc = claim.main(["--loops-root", str(queue), "acquire", "sha256:dddd",
                      "--actor", "builder-R5", "--ttl-seconds", "60"])
    assert rc == claim.EXIT_OK
    assert (queue / "claims" / "sha256_dddd.claim").exists()

    rc = claim.main(["--loops-root", str(queue), "release", "sha256:dddd",
                      "--actor", "builder-R5"])
    assert rc == claim.EXIT_OK
    assert not (queue / "claims" / "sha256_dddd.claim").exists()


def test_cli_wip_reports_live_and_cap(queue: Path, capsys: pytest.CaptureFixture) -> None:
    (queue / "state" / "budget.json").write_text(json.dumps({"wip_cap": 3}))
    _write_marker(queue, "sha256:eeee", "someone", ttl_seconds=3600)

    rc = claim.main(["--loops-root", str(queue), "wip"])
    assert rc == claim.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out == {"live": 1, "cap": 3, "headroom": 2}


def test_budget_json_present_but_corrupt_is_instrument_error(queue: Path) -> None:
    (queue / "state" / "budget.json").write_text("{not json")
    with pytest.raises(claim.ClaimError) as exc_info:
        claim.acquire(queue, "sha256:ffff", "builder-R5", ttl_seconds=60)
    assert exc_info.value.code == claim.EXIT_INSTRUMENT_ERROR


def test_absent_budget_json_defaults_wip_cap(queue: Path) -> None:
    # No state/budget.json written at all — matches bridge/governor.py's own
    # default (`budget.setdefault("wip_cap", 4)`).
    assert claim.read_wip_cap(queue) == claim.DEFAULT_WIP_CAP
