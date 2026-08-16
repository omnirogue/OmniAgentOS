"""Regression tests for the 2026-08-08 round-4 cross-lineage review of R5's
third candidate (rejected id
sha256:14716104700e26e5662ac4f3de524e18e593a8d535da6982e2d073caff728efa).

One blocker, one major, both about the SAME shared mechanism the coordinator
required in place of a fourth patch-in-isolation:

  R3F1 (BLOCKER) janitor.py — the R2F5 fix made the leak observable via
                              `self._alert()`, but that state was IN-MEMORY
                              (`self.alerts`). launchd execs a fresh Python
                              process every 300s (StartInterval), so an
                              unreadable marker wrote a new ALERTS.md line
                              every 5 minutes forever (288/day).
  R3F2 (major)   claim.py   — `_alert_once`'s dedup was `key in existing_text`,
                              a substring check: a marker named `sha256_test`
                              was silently swallowed if `sha256_test_1` had
                              already been alerted (false negative), and the
                              reverse direction (false suppression of an
                              unrelated marker) was equally possible.

Fix: ONE shared function, `bridge.claim.alert_once(loops_root, key, msg,
source=...)`, persisting an exact-match keyset to `state/alerted.json`
(small, separate from ALERTS.md, so a lookup never re-reads the whole log).
Both `claim.py`'s own callers and `bridge/janitor.py`'s `_alert_dedup()`
wrapper call this ONE function — there is no sibling copy left to miss.

R3F1's test below spawns TWO REAL, SEPARATE OS PROCESSES (subprocess.run,
not two Python objects in one interpreter) — a same-process test would pass
against a broken in-memory version too (two fresh `Janitor()` instances in
one process share nothing either, but neither would a same-process test
prove the fix survives what launchd actually does: a brand-new interpreter
with an empty heap). This also closes the coverage gap the reviewer raised
as a side note: the standalone `python bridge/janitor.py` invocation branch
(the plist's ACTUAL invocation form) was previously untested by CI.

Third-caller audit (coordinator: "assume three until you've looked"):
`bridge/github_bridge.py` (`_alert`), `bridge/integration.py`
(`Loop._alert`), and `bridge/integrity.py` (`Rule.alert`) each write their
own lines to ALERTS.md, independently of both `claim.py` and `janitor.py`.
NONE of them alert about claim markers or call into the claims-reaper path
at all (grepped for "claim"/"marker"/"unreadable" co-occurring with
"alert" across all three — no hits beyond an unrelated `probe.claim`
healthcheck fixture in integrity.py). So there is no undiscovered THIRD
caller of the specific alert this fix covers — but all three ARE separate,
pre-existing, un-deduplicated alert mechanisms in their own right, out of
this build's ownership (bridge/janitor.py, bridge/claim.py, and tests
only), and are named here rather than silently left unmentioned.
"""

from __future__ import annotations

import json
import os
import subprocess
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
    return q


def _alert_lines(queue: Path, needle: str) -> list[str]:
    """Lines in ALERTS.md containing ``needle`` — a content substring, not
    an implementation-detail source-prefix, so this helper works whether the
    line was written by a pre-fix or post-fix version of the code (the
    message BODY is the same either way; only the dedup behaviour differs)."""
    path = queue / "ALERTS.md"
    if not path.exists():
        return []
    return [ln for ln in path.read_text().splitlines() if needle in ln]


# --------------------------------------------------------------------------
# R3F1 — persistent, cross-PROCESS dedup, proved with real subprocesses
# --------------------------------------------------------------------------


def test_r3f1_alert_dedup_survives_a_real_process_restart(queue: Path) -> None:
    """Adapted from .fusion/repro/R3F1.py, strengthened: the repro itself
    only proves cross-OBJECT dedup within one interpreter (two `Janitor()`
    instances in the same process, which already share the same Python
    heap and would trivially "pass" against an in-memory fix that merely
    moved from `self` to a module-level cache). This spawns two REAL,
    independent `python bridge/janitor.py` subprocesses — no Python object,
    module, or interpreter state survives between them, only the
    filesystem — which is what actually has to hold for launchd's
    StartInterval=300 respawn to not flood ALERTS.md. This is also the
    first CI coverage of the standalone script invocation branch (the
    plist's actual invocation form), which the round-3 reviewer flagged as
    untested."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    marker = queue / "claims" / "sha256_flood.claim"
    now = datetime.now(UTC)
    marker.write_text(json.dumps({"actor": "test", "expires_at": _iso(now - timedelta(hours=1))}))
    marker.chmod(0o000)

    try:
        janitor_py = PKG / "bridge" / "janitor.py"
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, str(janitor_py), "--loops-root", str(queue),
                 "--claims-only", "--apply"],
                capture_output=True, text=True, timeout=120,  # load-robust under concurrent gate suite
            )
            assert result.returncode == 0, f"janitor.py subprocess failed: {result.stderr}"

        alert_lines = _alert_lines(queue, "sha256:flood is unreadable")
        assert len(alert_lines) == 1, (
            f"expected exactly 1 alert line across 2 SEPARATE process invocations, "
            f"got {len(alert_lines)}: {alert_lines}"
        )
    finally:
        marker.chmod(0o644)


def test_r3f1_a_third_process_still_does_not_add_another_alert(queue: Path) -> None:
    """Belt-and-suspenders: three separate invocations, still one line —
    guards against an off-by-one where the SECOND call happens to dedup
    (state written by the first) but a THIRD, later call re-reads stale or
    partially-written state."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    marker = queue / "claims" / "sha256_flood3.claim"
    now = datetime.now(UTC)
    marker.write_text(json.dumps({"actor": "test", "expires_at": _iso(now - timedelta(hours=1))}))
    marker.chmod(0o000)

    try:
        janitor_py = PKG / "bridge" / "janitor.py"
        for _ in range(3):
            result = subprocess.run(
                [sys.executable, str(janitor_py), "--loops-root", str(queue),
                 "--claims-only", "--apply"],
                capture_output=True, text=True, timeout=120,  # load-robust under concurrent gate suite
            )
            assert result.returncode == 0

        assert len(_alert_lines(queue, "sha256:flood3 is unreadable")) == 1
    finally:
        marker.chmod(0o644)


def test_r3f1_dedup_state_persists_on_disk_between_invocations(queue: Path) -> None:
    """Direct proof the mechanism is disk-backed, not incidental: after one
    real subprocess run, state/alerted.json exists and names the marker."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission bits")

    marker = queue / "claims" / "sha256_persisted.claim"
    now = datetime.now(UTC)
    marker.write_text(json.dumps({"actor": "test", "expires_at": _iso(now - timedelta(hours=1))}))
    marker.chmod(0o000)

    try:
        janitor_py = PKG / "bridge" / "janitor.py"
        result = subprocess.run(
            [sys.executable, str(janitor_py), "--loops-root", str(queue),
             "--claims-only", "--apply"],
            capture_output=True, text=True, timeout=120,  # load-robust under concurrent gate suite
        )
        assert result.returncode == 0

        state_path = queue / "state" / "alerted.json"
        assert state_path.exists()
        keys = json.loads(state_path.read_text())
        assert "sha256_persisted.claim" in keys
    finally:
        marker.chmod(0o644)


# --------------------------------------------------------------------------
# R3F2 — exact key match, not substring
# --------------------------------------------------------------------------


def test_r3f2_no_substring_collision_false_negative(queue: Path) -> None:
    """Adapted from .fusion/repro/R3F2.py: a marker whose name is a
    substring of an EARLIER, unrelated, already-alerted key must still get
    its own alert."""
    claim.alert_once(queue, "sha256_test_1.claim", "sha256_test_1.claim is unreadable")
    claim.alert_once(queue, "sha256_test.claim", "sha256_test.claim is unreadable")

    lines = [ln for ln in (queue / "ALERTS.md").read_text().splitlines() if ln.strip()]
    assert len(lines) == 2, "a substring collision suppressed a distinct, valid alert"


def test_r3f2_no_substring_collision_false_positive(queue: Path) -> None:
    """The mirror direction the raw repro doesn't check: a key that is a
    SUPERSTRING of an existing one must ALSO still get its own alert (not
    suppressed by "contains" logic running the other way)."""
    claim.alert_once(queue, "sha256_test.claim", "sha256_test.claim is unreadable")
    claim.alert_once(queue, "sha256_test_extended.claim", "sha256_test_extended.claim is unreadable")

    lines = [ln for ln in (queue / "ALERTS.md").read_text().splitlines() if ln.strip()]
    assert len(lines) == 2


def test_r3f2_exact_repeat_key_still_dedups(queue: Path) -> None:
    """The property the mechanism exists to preserve: the SAME key, called
    twice, still only alerts once."""
    claim.alert_once(queue, "sha256_same.claim", "first message")
    claim.alert_once(queue, "sha256_same.claim", "second message, different text, same key")

    lines = [ln for ln in (queue / "ALERTS.md").read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "first message" in lines[0]


def test_r3f2_dedup_state_is_a_small_separate_file_not_alerts_md_itself(queue: Path) -> None:
    """'Don't read the whole alerts file per call' — the dedup mechanism
    must not be implemented as a re-scan of ALERTS.md; it has its own,
    much smaller state file."""
    claim.alert_once(queue, "sha256_probe.claim", "probe message")
    state_path = queue / "state" / "alerted.json"
    assert state_path.exists()
    assert state_path.name != "ALERTS.md"
    keys = json.loads(state_path.read_text())
    assert keys == ["sha256_probe.claim"]


def test_r3f2_corrupt_dedup_state_fails_toward_alerting_again_not_silence(queue: Path) -> None:
    """A corrupt state/alerted.json must never permanently mute future
    alerts (the opposite failure direction from the bug this fix closes) —
    it should be treated as 'nothing alerted yet', re-alerting once more
    rather than staying silent forever."""
    state_path = queue / "state"
    state_path.mkdir(exist_ok=True)
    (state_path / "alerted.json").write_text("{not valid json")

    claim.alert_once(queue, "sha256_after_corruption.claim", "should still alert")

    lines = [ln for ln in (queue / "ALERTS.md").read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


# --------------------------------------------------------------------------
# one shared implementation — both callers actually go through it
# --------------------------------------------------------------------------


def test_janitor_alert_dedup_calls_the_shared_claim_function(
    queue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves 'one implementation, both callers' by execution, not just by
    reading the source: janitor._alert_dedup must actually invoke
    claim.alert_once, not a private re-implementation."""
    calls = []
    original = claim.alert_once

    def spy(loops_root, key, msg, **kwargs):
        calls.append((key, msg))
        return original(loops_root, key, msg, **kwargs)

    monkeypatch.setattr(claim, "alert_once", spy)

    marker = queue / "claims" / "sha256_wired.claim"
    now = datetime.now(UTC)
    marker.write_text(json.dumps({"actor": "x", "expires_at": _iso(now - timedelta(hours=1))}))
    marker.chmod(0o000)
    try:
        j = janitor.Janitor(queue, apply=True)
        j.sweep(claims_only=True)
    finally:
        marker.chmod(0o644)

    assert calls, "janitor._alert_dedup did not call claim.alert_once"
    assert calls[0][0] == "sha256_wired.claim"
