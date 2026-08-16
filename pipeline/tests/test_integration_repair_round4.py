"""Regression tests for repair round 4 on bridge/integration.py.

Round 3 put a guard on `Integration.reject()`'s belief that it is the ONLY
writer of a terminal `rejected` event. That belief was disproved:
`_append_ledger` accepts a VARIABLE event object, and nothing stopped a
different — or future — caller from building a `{"event": "rejected",
"detail": {"class": "instrument-error" | "blocked-on-human", ...}}` dict
directly and handing it to `_append_ledger`, bypassing `reject()` entirely.
No grep for a literal call site could ever have found this, which is why the
round-3 enumeration missed it.

  A1 — the write-boundary guard: a DYNAMIC caller (a variable event dict, not
       the literal string "rejected" typed at a call site) must not be able
       to terminalize an instrument-error/blocked-on-human refusal via
       `_append_ledger`, regardless of whether it goes through `reject()`.
  D1 — the round-4 regression `reject()` introduced while closing A1: because
       the guard wrote no non-terminal ledger status, `state/queue.json`
       reported `items: [], wip: 0` for a candidate that was actually
       blocked — a false "maximum headroom" read at the exact moment there
       was none. Fixed: a non-terminal `instrument_error` event, mapped to
       queue status `blocked`, that does not touch `ledger.terminal` or
       `ledger.parked` (so re-admission on repair is unaffected). WIP charging
       depends on entry provenance: blocked-on-human enters WIP at refusal;
       generic instrument errors do not invent occupancy.

Repair round 5 (see .fusion/repro/R4F2_*.py and R4F3_*.py, and
`test_top_level_summary_wip_agrees_with_published_queue_wip` below) closed two
more findings against round 4's own fix:

  R4F2 — the write-boundary guard above (A1's fix) refused a write by
       `return`ing `None` -- success-shaped, identical to what a landed write
       also returns, so a caller could not tell a refused write from one that
       landed. `_append_ledger` now raises `LedgerWriteRefused` instead. The
       A1 tests below were strengthened to assert the raise, not just the
       ledger's after-effects (a caller that ignored the return value would
       have passed the old assertions too).
  R4F3 — D1's own fix (the non-terminal `instrument_error` bookkeeping event)
       reached the canonical `state/queue.json` but not `Integration.iterate()`'s
       own top-level `summary["wip"]`, which was computed from a stale,
       pre-tick ledger view -- so `summary["wip"]` and `queue["wip"]` could
       disagree about the same candidate.

Both repros back these tests live at .fusion/repro/A1_*.py and
.fusion/repro/D1_*.py and are executed directly (not via pytest) as part of
the mechanical pass-list; this file pins the same properties so `pytest` on
its own catches a regression too.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PKG))

from bridge import integration as I  # noqa: E402
from verdict_absence_matrix import envelope, scratch_repo  # noqa: E402

# Probe discipline (verdict_absence_matrix.py's own convention): refuse to
# measure the wrong tree.
assert Path(I.__file__).resolve() == (PKG / "bridge" / "integration.py").resolve()

try:
    import jsonschema  # noqa: F401
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False

pytestmark = pytest.mark.skipif(
    not _HAS_JSONSCHEMA,
    reason="this suite measures the jsonschema-PRESENT admission path; run it "
           "under a conforming interpreter",
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    for name in ("candidates", "rejected", "state"):
        (r / name).mkdir(parents=True, exist_ok=True)
    return r


# --------------------------------------------------------------------------
# A1: the write boundary, not the caller, enforces CONTRACT §1
# --------------------------------------------------------------------------


def test_dynamic_rejected_event_with_instrument_class_is_refused(root: Path) -> None:
    """A caller that builds a "rejected" event directly — NOT via reject() —
    with detail.class="instrument-error" must not terminalize the candidate.

    This is the exact shape round 3 missed: it guarded reject() itself, but
    _append_ledger is a generic method that takes whatever dict it is given.

    Round 5, IN SCOPE 1: the refusal must also be DISTINGUISHABLE to the
    caller — a successful append and a refused one used to both return
    `None`. Assert the guard raises `LedgerWriteRefused` rather than merely
    checking the ledger's after-effects, which a caller silently ignoring
    the return value would also see.
    """
    ident = "sha256:" + "e" * 64
    adapter = I.Integration(root, root, root, PKG / "schema", apply=True)
    with pytest.raises(I.LedgerWriteRefused, match="instrument-error"):
        adapter._append_ledger({
            "ts": "2026-08-08T18:00:00Z", "role": "implementer", "event": "rejected",
            "id": ident, "actor": "dynamic-writer-repro",
            "detail": {"reason": "synthetic instrument outage", "class": "instrument-error",
                       "expires_at": "2026-08-09T18:00:00Z", "remedy": "blocked"},
        })
    ledger = I.LedgerView.build(root)
    assert ident not in ledger.terminal, (
        "a dynamic writer terminalized an instrument-error as rejected — the "
        "write-boundary guard did not fire")
    # The ledger file itself must carry no "rejected" line at all — a refused
    # write means NOTHING was appended, not a repaired variant of the event.
    raw = (root / "ledger.jsonl").read_text() if (root / "ledger.jsonl").exists() else ""
    assert '"event":"rejected"' not in raw.replace(" ", "")


def test_dynamic_rejected_event_with_blocked_on_human_class_is_refused(root: Path) -> None:
    """Same guard, the other guarded class (CONTRACT.md:628 vocabulary).

    Round 5, IN SCOPE 1: same distinguishability requirement as the sibling
    instrument-error test above.
    """
    ident = "sha256:" + "f" * 64
    adapter = I.Integration(root, root, root, PKG / "schema", apply=True)
    with pytest.raises(I.LedgerWriteRefused, match="blocked-on-human"):
        adapter._append_ledger({
            "ts": "2026-08-08T18:00:00Z", "role": "implementer", "event": "rejected",
            "id": ident, "actor": "dynamic-writer-repro",
            "detail": {"reason": "awaiting a human decision", "class": "blocked-on-human",
                       "expires_at": "2026-09-07T18:00:00Z", "remedy": "blocked"},
        })
    ledger = I.LedgerView.build(root)
    assert ident not in ledger.terminal


def test_reject_via_the_normal_route_still_refuses_a_real_candidate_defect(
    root: Path,
) -> None:
    """The guard must not swallow a REAL candidate-defect rejection — only
    instrument-error/blocked-on-human are refused at the write boundary."""
    ident = "sha256:" + "a" * 64
    cand = I.Candidate(ident, root / "candidates" / "x.json", {}, branch="lane/x",
                       base_sha="0" * 40, title="probe")
    adapter = I.Integration(root, root, root, PKG / "schema", apply=True)
    adapter.reject(cand, I.Refusal("a real defect", "candidate-defect", "replan"))
    ledger = I.LedgerView.build(root)
    assert ledger.terminal.get(ident, {}).get("event") == "rejected"
    assert (root / "rejected" / f"{ident.replace(':', '_', 1)}.json").exists()


# --------------------------------------------------------------------------
# D1: the guard must not make queue.json lie about a blocked candidate
# --------------------------------------------------------------------------


def test_instrument_blocked_admission_is_visible_in_queue_state(root: Path) -> None:
    """A candidate refused with class instrument-error at ADMISSION time
    (validate() -> reject()'s non-terminal branch) must still show up in
    LedgerView.status (and therefore state/queue.json), without consuming WIP
    before the candidate has actually been admitted."""
    repo, base, branch = scratch_repo(root.parent)
    (root / "state" / "budget.json").write_text(json.dumps({
        "updated_at": I._iso(I._now()), "wip_cap": 4, "disk_free_gb": 100,
        "disk_free_gb_min": 20, "load_avg_1m": 0, "load_avg_1m_max": 100,
    }))
    art = envelope(base, branch,
                   [{"reviewer": "gpt", "lineage": "openai", "verdict": "approve"}])
    (root / "candidates" / "candidate.json").write_text(json.dumps(art))

    real_probe = I.is_ancestor_of_main
    I.is_ancestor_of_main = lambda _repo, _sha: None  # simulate an unreadable repo
    try:
        adapter = I.Integration(root, repo, root.parent / "missing-gate", PKG / "schema",
                                apply=True)
        adapter.iterate()
    finally:
        I.is_ancestor_of_main = real_probe

    ledger = I.LedgerView.build(root)
    assert art["id"] not in ledger.terminal, "an instrument condition must never terminalize"
    assert ledger.status.get(art["id"]) == "blocked"

    queue = json.loads((root / "state" / "queue.json").read_text())
    ids = [item["id"] for item in queue["items"]]
    assert art["id"] in ids, "blocked candidate is missing from queue.json — false headroom"
    assert queue["wip"] == 0, "an unadmitted instrument error must preserve WIP headroom"


def test_top_level_summary_wip_agrees_with_published_queue_wip(root: Path) -> None:
    """Repair round 5, IN SCOPE 2: `Integration.iterate()`'s top-level
    `summary["wip"]` used to be computed from the ledger read at the TOP of
    the iteration -- before this tick's own refusal/admission events were
    appended. The probe below writes `admitted` during this tick, before
    returning an instrument failure, so the fresh queue is non-zero while the
    pre-tick summary is zero. The reconciliation assignment is therefore
    load-bearing rather than a 0 == 0 tautology.

    Red-first proven in a scratch copy of the pre-round-5 tree: this test
    failed with `summary["wip"]=0` vs `queue["wip"]=1` before the fix."""
    repo, base, branch = scratch_repo(root.parent)
    (root / "state" / "budget.json").write_text(json.dumps({
        "updated_at": I._iso(I._now()), "wip_cap": 4, "disk_free_gb": 100,
        "disk_free_gb_min": 20, "load_avg_1m": 0, "load_avg_1m_max": 100,
    }))
    art = envelope(base, branch,
                   [{"reviewer": "gpt", "lineage": "openai", "verdict": "approve"}])
    (root / "candidates" / "candidate.json").write_text(json.dumps(art))

    adapter = I.Integration(root, repo, root.parent / "missing-gate", PKG / "schema",
                            apply=True)
    admitted_written = False

    def admit_then_fail_probe(_repo: Path, _sha: str) -> None:
        nonlocal admitted_written
        if not admitted_written:
            adapter._append_ledger({
                "ts": I._iso(I._now()), "role": I.ROLE, "event": "admitted",
                "id": art["id"], "actor": "test-same-tick-admission", "detail": {},
            })
            admitted_written = True
        return None

    real_probe = I.is_ancestor_of_main
    I.is_ancestor_of_main = admit_then_fail_probe
    try:
        adapter.iterate()
    finally:
        I.is_ancestor_of_main = real_probe

    queue = json.loads((root / "state" / "queue.json").read_text())
    assert queue["wip"] == 1 and queue["items"][0]["status"] == "blocked"
    assert adapter.summary["wip"] == queue["wip"], (
        f"summary[\"wip\"]={adapter.summary['wip']!r} disagrees with the published "
        f"queue's wip={queue['wip']!r} for the same blocked candidate — two "
        "operator-facing surfaces reporting different backpressure numbers")
    assert adapter.summary["queue"]["wip"] == queue["wip"]


def test_instrument_blocked_candidate_remains_re_admittable(root: Path) -> None:
    """Repairing the instrument (the probe starts returning a real answer)
    must re-admit the SAME candidate automatically — the non-terminal
    bookkeeping event must not regress round 3's re-admission property."""
    repo, base, branch = scratch_repo(root.parent)
    (root / "state" / "budget.json").write_text(json.dumps({
        "updated_at": I._iso(I._now()), "wip_cap": 4, "disk_free_gb": 100,
        "disk_free_gb_min": 20, "load_avg_1m": 0, "load_avg_1m_max": 100,
    }))
    art = envelope(base, branch,
                   [{"reviewer": "gpt", "lineage": "openai", "verdict": "approve"}])
    (root / "candidates" / "candidate.json").write_text(json.dumps(art))

    real_probe = I.is_ancestor_of_main
    I.is_ancestor_of_main = lambda _repo, _sha: None
    try:
        I.Integration(root, repo, root.parent / "missing-gate", PKG / "schema",
                      apply=True).iterate()
    finally:
        I.is_ancestor_of_main = real_probe

    ledger = I.LedgerView.build(root)
    assert ledger.status.get(art["id"]) == "blocked"
    assert art["id"] not in ledger.terminal
    assert art["id"] not in ledger.parked

    # Instrument repaired: the probe now works for real.
    I.Integration(root, repo, root.parent / "missing-gate", PKG / "schema",
                 apply=True).iterate()

    ledger = I.LedgerView.build(root)
    assert ledger.status.get(art["id"]) == "admitted", (
        "the candidate did not re-admit automatically once the instrument was "
        "repaired — the blocked bookkeeping event regressed re-admission")
