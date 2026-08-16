"""Tests for the gate health watchdog.

Every detector is exercised as a PURE function over synthetic inputs and a fake
clock — no live launchd, no live daemon, no network. The pingers are
monkeypatched wherever a wake path is exercised.

The two properties that matter most here, and that are each pinned by their own
test, are:

  * an unreadable input WAKES — it never crashes the sweep and never passes
    silently (a watchdog that fails quiet also removes the operator's suspicion);
  * a finding's content id is stable across sweeps — volatile data (counts,
    ages, timestamps) must stay OUT of the dedup payload, or a 12-hour stall
    files a finding every 120 seconds.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gate-watch" / "gate_watch.py"
NOW = 1_786_400_000.0  # a fixed epoch; every test's clock


def _load() -> Any:
    name = "gate_watch_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gw = _load()
TH = gw.Thresholds()


def _iso(offset_seconds: float) -> str:
    return gw.iso(NOW + offset_seconds)


# ---------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------

def test_thresholds_come_from_env_and_reject_nonsense() -> None:
    th = gw.thresholds_from_env({
        "GATE_WATCH_LOCK_STALE_MIN": "3",
        "GATE_WATCH_TICK_STALE_MIN": "not-a-number",
        "GATE_WATCH_SKIP_STORM": "0",
        "GATE_WATCH_INSTR_CLUSTER": "7",
    })
    assert th.lock_stale_min == 3
    assert th.instr_cluster == 7
    # A garbage or non-positive override falls back to the default rather than
    # arming a zero threshold that would fire on everything.
    assert th.tick_stale_min == gw.Thresholds().tick_stale_min
    assert th.skip_storm == gw.Thresholds().skip_storm


# ---------------------------------------------------------------------------
# log parsing — a line belongs to the tick whose "at" block FOLLOWS it
# ---------------------------------------------------------------------------

def _tick(at: str, body: str = "") -> str:
    """One tick as gate_loop actually writes it: the JSON block FIRST, then the
    buffered free-text lines (gate_loop.main, `print(json.dumps(...))` followed
    by `for ln in loop.lines`)."""
    return f'{{\n  "at": "{at}",\n  "outcomes": []\n}}\n{body}'


# NOTE ON EXACTNESS (round-2 review finding, and the reason these assertions do
# not use pytest.approx): at a 2026 epoch, approx's DEFAULT relative tolerance is
# 1e-6 * 1.786e9 = +-1786 SECONDS. A whole one-tick stamping inversion is ~64s
# and sails straight through it, so the original stamping tests could not have
# failed on the very bug they were written for. Timestamps here are exact
# strptime outputs; compare them exactly.
def test_parse_log_stamps_lines_with_the_PRECEDING_tick() -> None:
    text = (_tick(_iso(-600), "  skip aaaaaaaaaaaa: reason\n")
            + _tick(_iso(-60), "  dispatched gate for train/gl-1 @ abc\n"))
    lines, newest = gw.parse_log(text)
    assert newest == NOW - 60
    skip = next(ln for ln in lines if "skip" in ln.text)
    dispatch = next(ln for ln in lines if "dispatched gate" in ln.text)
    assert skip.ts == NOW - 600
    assert dispatch.ts == NOW - 60


def test_parse_log_drops_lines_before_the_first_tick_rather_than_guessing() -> None:
    """The byte-window can cut a tick in half. Those orphan lines have no known
    time, and inventing one (``now``, or the next tick) is how a stale dispatch
    gets read as current."""
    text = "  dispatched gate for train/gl-orphan @ old\n" + _tick(_iso(-60))
    lines, _ = gw.parse_log(text)
    assert not [ln for ln in lines if "gl-orphan" in ln.text]


# A VERBATIM slice of var/log/gate-loop.log, captured 2026-08-10T19:13Z. The
# synthetic fixtures above all encoded the same assumption as the parser, so an
# inverted reading of the tick/line order passed 50 of them; only real bytes
# could catch that. Do not "tidy" this string.
_REAL_LOG = (
    '{\n  "at": "2026-08-10T19:11:10Z",\n  "outcomes": [\n    {\n'
    '      "train": "train/gl-85fe9011bfe9",\n      "action": "waiting",\n'
    '      "detail": ""\n    }\n  ]\n}\n'
    "  skip 3f1bdd8fe67f: risky diff requires a genuine cross-lineage build verdict: "
    "pipeline/prompts/PROMPT-planning-loop.md\n"
    "  skip 1dc3d5de715d: risky diff requires a genuine cross-lineage build verdict: "
    "pipeline/bridge/review_policy.py\n"
    "  train/gl-85fe9011bfe9 @ 33aff05f5ad9 still gating (waiting)\n"
    '{\n  "at": "2026-08-10T19:12:14Z",\n  "outcomes": [\n    {\n'
    '      "train": "train/gl-85fe9011bfe9",\n      "action": "waiting",\n'
    '      "detail": ""\n    }\n  ]\n}\n'
    "  skip 3f1bdd8fe67f: risky diff requires a genuine cross-lineage build verdict: "
    "pipeline/prompts/PROMPT-planning-loop.md\n"
)


def test_parse_log_against_a_real_captured_gate_loop_slice() -> None:
    lines, newest = gw.parse_log(_REAL_LOG)
    assert newest == gw.parse_iso("2026-08-10T19:12:14Z")
    skips = [ln for ln in lines if "skip 3f1bdd8fe67f" in ln.text]
    assert len(skips) == 2
    # The first skip block sits BELOW the 19:11:10 header and so belongs to it.
    assert skips[0].ts == gw.parse_iso("2026-08-10T19:11:10Z")
    assert skips[1].ts == gw.parse_iso("2026-08-10T19:12:14Z")
    # Every line is accounted for and none is stamped in the future.
    assert lines and all(ln.ts <= newest for ln in lines)


# ---------------------------------------------------------------------------
# D1 — orphaned builder lock
# ---------------------------------------------------------------------------

def _d1(**over: Any) -> Any:
    kwargs: dict[str, Any] = dict(
        lock_exists=True, lock_text="initializing", lock_mtime=NOW - 3600,
        builder_dir="/repo/var/loopqueue/state/gate-loop-build", probe=gw.IDLE,
        now=NOW, th=TH)
    kwargs.update(over)
    return gw.detect_builder_lock(**kwargs)


def test_d1_fires_and_offers_the_auto_remedy_on_an_orphaned_lock() -> None:
    result = _d1()
    assert result.fired
    (det,) = result.detections
    assert det.auto_remediable is True
    assert "initializing" in det.symptom


def test_d1_silent_when_no_lock_or_when_the_lock_is_fresh() -> None:
    assert not _d1(lock_exists=False).fired
    assert not _d1(lock_mtime=NOW - 60).fired


def test_d1_silent_when_the_reason_is_not_initializing() -> None:
    assert not _d1(lock_text="operator hold").fired


def test_d1_wakes_without_remedying_when_lsof_cannot_prove_the_worktree_is_idle() -> None:
    result = _d1(probe=gw.UNKNOWN)
    (det,) = result.detections
    assert det.auto_remediable is False, "an unprovable lock must never be auto-unlocked"


def test_d1_does_not_fire_while_a_live_process_holds_the_worktree() -> None:
    assert not _d1(probe=gw.BUSY).fired


def test_d1_unreadable_lock_is_a_wake_not_a_pass() -> None:
    result = _d1(lock_text=None)
    (det,) = result.detections
    assert "instrument-unreadable" in det.symptom
    assert det.auto_remediable is False


# ---------------------------------------------------------------------------
# D2 — daemon dead
# ---------------------------------------------------------------------------

def test_d2_fires_when_the_newest_tick_is_older_than_the_threshold() -> None:
    result = gw.detect_daemon_dead(log_text="x", newest_tick=NOW - 20 * 60, now=NOW, th=TH)
    assert result.fired
    assert "tick" in result.detections[0].symptom


def test_d2_silent_on_a_fresh_tick() -> None:
    assert not gw.detect_daemon_dead(log_text="x", newest_tick=NOW - 30, now=NOW, th=TH).fired


def test_d2_wakes_when_the_log_has_no_parseable_tick_at_all() -> None:
    result = gw.detect_daemon_dead(log_text="garbage", newest_tick=None, now=NOW, th=TH)
    assert "instrument-unreadable" in result.detections[0].symptom


def test_d2_wakes_when_the_log_is_unreadable() -> None:
    result = gw.detect_daemon_dead(log_text=None, newest_tick=None, now=NOW, th=TH)
    assert "instrument-unreadable" in result.detections[0].symptom


# ---------------------------------------------------------------------------
# D3 — assembly silence
# ---------------------------------------------------------------------------

def _lines(*pairs: tuple[float, str]) -> list[Any]:
    return [gw.LogLine(NOW + off, text) for off, text in pairs]


def test_d3_fires_when_candidates_wait_and_nothing_is_dispatched() -> None:
    result = gw.detect_assembly_silence(
        lines=_lines((-6 * 3600, "  dispatched gate for train/gl-1 @ abc"),
                     (-60, "  no gate-eligible candidates to land this tick")),
        pending_candidates=["sha256:aa", "sha256:bb"], now=NOW, th=TH)
    assert result.fired
    assert result.detections[0].evidence["pending_count"] == 2


def test_d3_silent_when_a_dispatch_is_recent() -> None:
    result = gw.detect_assembly_silence(
        lines=_lines((-120, "  dispatched gate for train/gl-1 @ abc")),
        pending_candidates=["sha256:aa"], now=NOW, th=TH)
    assert not result.fired


def test_d3_silent_when_the_queue_is_genuinely_empty() -> None:
    # "no gate-eligible candidates" is CORRECT when nothing is waiting; firing
    # on it would train the operator to ignore this job.
    result = gw.detect_assembly_silence(
        lines=_lines((-6 * 3600, "  no gate-eligible candidates to land this tick")),
        pending_candidates=[], now=NOW, th=TH)
    assert not result.fired


def test_d3_counts_a_land_as_progress() -> None:
    for progress in ('  LANDED train/gl-1 @ deadbeef — 2 members',
                     '      "action": "landed"',
                     '  one train landed this tick; remaining trains re-assemble next tick'):
        result = gw.detect_assembly_silence(
            lines=_lines((-300, progress)),
            pending_candidates=["sha256:aa"], now=NOW, th=TH)
        assert not result.fired, progress


def test_d3_does_not_read_the_loops_negative_prose_as_progress() -> None:
    """The trap a bare "landed" substring walks into.

    The gate loop logs "the shell half of the closure contract has NOT landed in
    this pinned workspace" on dispatch paths, and "the candidate landed, the
    bound test did not pass" on a warning path. Matching either would suppress
    D3 permanently — a favourable-absence bug inside the favourable-absence
    detector, and invisible, because the failure mode is silence.
    """
    for prose in ("  bound-test flags WITHHELD for train/gl-1: the shell half of the "
                  "closure contract has not landed in this pinned workspace",
                  "  the bound test did not pass on the merged tree; the candidate landed, "
                  "the finding is not closed"):
        result = gw.detect_assembly_silence(
            lines=_lines((-300, prose)),
            pending_candidates=["sha256:aa"], now=NOW, th=TH)
        assert result.fired, prose


# ---------------------------------------------------------------------------
# D4 — skip storm
# ---------------------------------------------------------------------------

_VERDICT = "risky diff requires a genuine cross-lineage build verdict: pipeline/x.py"
_FAULT = "missing full immutable head_sha"


def test_d4_fires_once_per_storming_fault_class_candidate() -> None:
    lines = _lines(*[(-i * 60, f"  skip 3f1bdd8fe67f: {_FAULT}") for i in range(40)])
    lines += _lines(*[(-i * 60, "  skip 28bd7e8efccd: real diff unreadable (instrument)")
                      for i in range(5)])
    result = gw.detect_skip_storm(lines=lines, now=NOW, th=TH)
    assert len(result.detections) == 1
    det = result.detections[0]
    assert det.detector == "D4"
    assert det.evidence["candidate"] == "3f1bdd8fe67f"
    assert det.evidence["skips_in_window"] >= TH.skip_storm


def test_d4_does_not_fire_on_the_sanctioned_awaiting_verdict_skip() -> None:
    """A risky diff is skipped every tick while its cross-lineage review seat is
    queued — routinely for well over an hour. That is the review policy WORKING.
    Reporting it as a storm is a false alarm on healthy blocked-on-human work,
    and false alarms are how a real one stops being read."""
    lines = _lines(*[(-i * 60, f"  skip 3f1bdd8fe67f: {_VERDICT}") for i in range(40)])
    result = gw.detect_skip_storm(lines=lines, now=NOW, th=TH)
    assert not result.fired
    assert "awaiting-verdict" in result.note


def test_d4b_fires_when_the_review_seat_itself_is_stuck() -> None:
    # Skipped continuously for 7h: the wait is legitimate, the seat is not.
    lines = _lines(*[(-i * 60, f"  skip 3f1bdd8fe67f: {_VERDICT}") for i in range(7 * 60)])
    result = gw.detect_skip_storm(lines=lines, now=NOW, th=TH)
    assert len(result.detections) == 1
    det = result.detections[0]
    assert det.detector == "D4b"
    assert det.evidence["waiting_hours"] >= 6
    assert "reviewer" in det.remedy


def test_d4b_does_not_fire_on_a_candidate_that_stopped_being_skipped() -> None:
    # Waited 7h, then moved on 3h ago: log history must not resurrect it.
    lines = _lines(*[(-(3 * 3600 + i * 60), f"  skip 3f1bdd8fe67f: {_VERDICT}")
                     for i in range(7 * 60)])
    assert not gw.detect_skip_storm(lines=lines, now=NOW, th=TH).fired


def test_d4_classes_are_judged_independently() -> None:
    lines = _lines(*[(-i * 60, f"  skip aaaaaaaaaaaa: {_FAULT}") for i in range(40)])
    lines += _lines(*[(-i * 60, f"  skip bbbbbbbbbbbb: {_VERDICT}") for i in range(7 * 60)])
    result = gw.detect_skip_storm(lines=lines, now=NOW, th=TH)
    assert {d.detector for d in result.detections} == {"D4", "D4b"}


def test_a_candidate_with_both_skip_classes_still_reports_its_fault_storm() -> None:
    """Skip class is tracked per (candidate, class), not per candidate.

    A single candidate legitimately produces both kinds of line as its state
    changes. With one class field per candidate, ONE late awaiting-verdict line
    reclassifies a candidate that stormed with a fault all hour and the storm
    disappears from the report — last-write-wins, failing silent.
    """
    lines = _lines(*[(-i * 60 - 120, f"  skip 3f1bdd8fe67f: {_FAULT}") for i in range(40)])
    lines += _lines((-30, f"  skip 3f1bdd8fe67f: {_VERDICT}"))  # newest line, other class
    result = gw.detect_skip_storm(lines=lines, now=NOW, th=TH)
    faults = [d for d in result.detections if d.detector == "D4"]
    assert len(faults) == 1
    assert faults[0].evidence["candidate"] == "3f1bdd8fe67f"
    # …and the fault count is not polluted by the verdict-class line.
    assert faults[0].evidence["skips_in_window"] == 40
    assert _FAULT in faults[0].evidence["last_skip_line"]


def test_d4b_span_is_order_independent() -> None:
    """min/max, not first/last encountered. A log is normally chronological, but
    a span that silently depends on that collapses to zero the first time it is
    not — and a zero span never fires, so the failure is invisible."""
    offsets = [-i * 60 for i in range(7 * 60)]
    forward = _lines(*[(o, f"  skip 3f1bdd8fe67f: {_VERDICT}") for o in sorted(offsets)])
    shuffled = _lines(*[(o, f"  skip 3f1bdd8fe67f: {_VERDICT}")
                        for o in sorted(offsets, key=lambda x: (x * 7919) % 1013)])
    a = gw.detect_skip_storm(lines=forward, now=NOW, th=TH).detections
    b = gw.detect_skip_storm(lines=shuffled, now=NOW, th=TH).detections
    assert len(a) == len(b) == 1
    assert a[0].evidence["waiting_hours"] == b[0].evidence["waiting_hours"] >= 6


def test_classify_skip_reason_maps_the_real_reason_strings() -> None:
    assert gw.classify_skip_reason(_VERDICT) == gw.SKIP_AWAITING_VERDICT
    for fault in ("missing full immutable head_sha",
                  "approved head_sha 3567e4a8d0c2 is unresolvable in omniagentos",
                  "branch lane/x moved away from approved head_sha c8d601fe",
                  "candidate head/base unresolvable in omniagentos",
                  "real diff unreadable (instrument): boom"):
        assert gw.classify_skip_reason(fault) == gw.SKIP_FAULT


def test_d4_ignores_skips_outside_the_window() -> None:
    old = _lines(*[(-(3600 + i * 60), f"  skip 3f1bdd8fe67f: {_FAULT}") for i in range(50)])
    assert not gw.detect_skip_storm(lines=old, now=NOW, th=TH).fired


def test_d4_does_not_fire_on_many_different_candidates_skipped_once() -> None:
    lines = _lines(*[(-i, f"  skip {i:012x}: {_FAULT}") for i in range(60)])
    assert not gw.detect_skip_storm(lines=lines, now=NOW, th=TH).fired


# ---------------------------------------------------------------------------
# D5 — instrument-error cluster
# ---------------------------------------------------------------------------

def _instr(offset: float) -> dict[str, Any]:
    return {"ts": _iso(offset), "role": "implementer", "event": "instrument_error",
            "detail": {"reason": "twin rc=255"}}


def test_d5_fires_on_a_cluster_inside_the_window() -> None:
    events = [_instr(-60), _instr(-300), _instr(-900), {"ts": _iso(-30), "event": "merged"}]
    result = gw.detect_instrument_cluster(events=events, now=NOW, th=TH)
    assert result.fired
    assert result.detections[0].evidence["count"] == 3


def test_d5_silent_below_the_threshold_and_outside_the_window() -> None:
    events = [_instr(-60), _instr(-2 * 3600), _instr(-3 * 3600)]
    assert not gw.detect_instrument_cluster(events=events, now=NOW, th=TH).fired


def test_d5_tolerates_torn_lines_and_non_dict_events() -> None:
    events = [_instr(-60), _instr(-70), _instr(-80)]
    result = gw.detect_instrument_cluster(events=[*events, {"event": "instrument_error"}],
                                          now=NOW, th=TH)
    assert result.detections[0].evidence["count"] == 3  # the ts-less event is not counted


# ---------------------------------------------------------------------------
# D6 — stuck gate
# ---------------------------------------------------------------------------

def test_d6_fires_on_a_running_gate_past_its_deadline_plus_grace() -> None:
    states = [("train__gl-a@abc.json", {"state": "running", "deadline": NOW - 3600}),
              ("train__gl-b@def.json", {"state": "running", "deadline": NOW + 600}),
              ("receipt-train__gl-c@ghi.json", {"schema": "merge-gate-run.v1"})]
    result = gw.detect_stuck_gates(gate_states=states, now=NOW, th=TH)
    assert len(result.detections) == 1
    assert result.detections[0].evidence["gate"] == "train__gl-a@abc.json"


def test_d6_stays_quiet_inside_the_grace_window() -> None:
    states = [("g.json", {"state": "running", "deadline": NOW - 300})]
    assert not gw.detect_stuck_gates(gate_states=states, now=NOW, th=TH).fired


def test_d6_treats_an_unreadable_or_deadline_less_running_gate_as_a_wake() -> None:
    states = [("torn.json", None), ("nodeadline.json", {"state": "running"})]
    result = gw.detect_stuck_gates(gate_states=states, now=NOW, th=TH)
    assert len(result.detections) == 2
    assert all("instrument-unreadable" in d.symptom for d in result.detections)


# ---------------------------------------------------------------------------
# D7 — capacity (utilization series, under-use, over-commit)
# ---------------------------------------------------------------------------

def _running(deadline: float | None = None, *, mode: str = "remote",
             twin: str = "mw0001-owner", members: list[str] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {"state": "running", "mode": mode, "twin": twin,
                             "members": members if members is not None else []}
    if deadline is not None:
        state["deadline"] = deadline
    return state


def test_utilization_row_shape_and_arithmetic() -> None:
    row = gw.compute_utilization(running_gates=1, max_slots=3, eligible_candidates=7,
                                 candidates_in_flight=2, now=NOW)
    assert set(row) == {"ts", "running_gates", "max_slots", "eligible_candidates",
                        "candidates_in_flight", "idle_slots", "utilization_pct"}
    assert row["idle_slots"] == 2
    assert row["utilization_pct"] == pytest.approx(33.3)
    assert row["ts"] == gw.iso(NOW)
    full = gw.compute_utilization(running_gates=3, max_slots=3, eligible_candidates=0,
                                  candidates_in_flight=0, now=NOW)
    assert full["idle_slots"] == 0 and full["utilization_pct"] == 100.0


def test_utilization_row_is_still_written_when_the_ceiling_is_unknown() -> None:
    """A gap in the series is indistinguishable from a sweep that never ran, and
    this file exists to be counted later."""
    row = gw.compute_utilization(running_gates=1, max_slots=None, eligible_candidates=4,
                                 candidates_in_flight=0, now=NOW, degraded="no gate_loop")
    assert row["max_slots"] is None and row["idle_slots"] is None
    assert row["utilization_pct"] is None and row["degraded"] == "no gate_loop"
    assert row["running_gates"] == 1 and row["eligible_candidates"] == 4


def test_is_running_gate_matches_the_loops_own_slot_accounting() -> None:
    assert gw.is_running_gate(_running(NOW + 600), now=NOW)
    assert not gw.is_running_gate(_running(NOW - 600), now=NOW)
    assert not gw.is_running_gate({"state": "done", "deadline": NOW + 600}, now=NOW)
    # No deadline reads as STILL RUNNING, exactly as gate_loop does: inventing a
    # free slot the scheduler does not believe in would fake spare capacity.
    assert gw.is_running_gate({"state": "running"}, now=NOW)
    assert gw.is_running_gate({"state": "running", "deadline": "soon"}, now=NOW), \
        "a non-numeric deadline reads as running, as in the loop"
    # bool IS an int to isinstance, and the loop's check admits it: `True` is 1,
    # i.e. long expired. Parity beats tidiness in a mirror.
    assert not gw.is_running_gate({"state": "running", "deadline": True}, now=NOW)


def test_underutilised_needs_BOTH_idle_slots_and_surplus_work() -> None:
    def row(idle: int, eligible: int, in_flight: int) -> dict[str, Any]:
        return {"idle_slots": idle, "eligible_candidates": eligible,
                "candidates_in_flight": in_flight}
    assert gw.underutilised(row(1, 3, 1))          # idle + 2 waiting
    assert not gw.underutilised(row(0, 9, 0)), "no idle slots is a BUSY pipeline"
    assert not gw.underutilised(row(3, 0, 0)), "idle slots + empty queue is REST"
    assert not gw.underutilised(row(2, 3, 2)), "only one surplus candidate"
    assert not gw.underutilised({"idle_slots": None, "eligible_candidates": 9,
                                 "candidates_in_flight": 0})


def test_underutil_streak_needs_CONSECUTIVE_sweeps() -> None:
    streak = 0
    for _ in range(4):
        streak = gw.next_streak(streak, True)
    assert streak == 4
    assert gw.next_streak(streak, False) == 0, "one healthy sweep resets the streak"
    assert gw.next_streak(gw.next_streak(0, True), True) == 2
    assert gw.next_streak(-3, True) == 1        # corrupt state cannot fake a streak


def _util_row(**over: Any) -> dict[str, Any]:
    row = gw.compute_utilization(running_gates=1, max_slots=3, eligible_candidates=5,
                                 candidates_in_flight=1, now=NOW)
    row.update(over)
    return row


def test_d7_fires_only_after_the_configured_number_of_sweeps() -> None:
    row = _util_row()
    assert gw.underutilised(row)
    for streak in range(TH.underutil_sweeps):
        assert not gw.detect_underutilisation(row=row, streak=streak,
                                              cause="assembly-unknown", th=TH).fired
    fired = gw.detect_underutilisation(row=row, streak=TH.underutil_sweeps,
                                       cause="assembly-unknown", th=TH)
    assert fired.fired
    det = fired.detections[0]
    assert det.detector == "D7"
    assert det.evidence["streak"] == TH.underutil_sweeps
    assert det.evidence["idle_slots"] == 2
    assert not det.auto_remediable


def test_d7_cause_classes() -> None:
    a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    # 1. shares a file with an in-flight train -> the conflict graph is working.
    assert gw.classify_underutil_cause(
        surplus_ids=[a], in_flight_paths={"pipeline/bridge/gate_loop.py"},
        candidate_paths={a: ["pipeline/bridge/gate_loop.py", "x.py"]},
        awaiting_verdict_ids=set()) == "conflict-serialized"
    # 2. risky diff waiting on a review seat (matched on the loop's short id).
    assert gw.classify_underutil_cause(
        surplus_ids=[a], in_flight_paths=set(), candidate_paths={a: ["x.py"]},
        awaiting_verdict_ids={"a" * 12}) == "awaiting-verdict"
    # 3. neither explanation holds: genuinely unaccounted-for idleness.
    assert gw.classify_underutil_cause(
        surplus_ids=[b], in_flight_paths={"z.py"}, candidate_paths={b: ["x.py"]},
        awaiting_verdict_ids=set()) == "assembly-unknown"
    # The benign explanation wins when both apply, so a serialised stretch is
    # not reported as a stuck reviewer.
    assert gw.classify_underutil_cause(
        surplus_ids=[a], in_flight_paths={"x.py"}, candidate_paths={a: ["x.py"]},
        awaiting_verdict_ids={"a" * 12}) == "conflict-serialized"


def test_real_paths_mirrors_the_assemblers_diff_and_its_none_contract() -> None:
    """None means "could not read", never "touches nothing" — the same contract
    train_assembler.real_paths carries, because the caller's whole conflict
    answer changes on that distinction."""
    seen: list[list[str]] = []

    def ok(cmd: list[str], **_: Any) -> Any:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "a.py\n b.py \n\n", "")

    assert gw.real_paths(Path("/r"), "base", "ref", runner=ok) == {"a.py", "b.py"}
    assert seen[0] == ["git", "-C", "/r", "diff", "--name-only", "base..ref"], \
        "must be the two-dot tree comparison the assembler uses"

    def bad(cmd: list[str], **_: Any) -> Any:
        return subprocess.CompletedProcess(cmd, 128, "", "unknown revision")

    assert gw.real_paths(Path("/r"), "base", "gone", runner=bad) is None

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("no git")

    assert gw.real_paths(Path("/r"), "b", "r", runner=boom) is None


def test_cause_uses_real_paths_and_flags_a_degraded_diagnosis() -> None:
    """For a watchdog the diagnosis IS the product. An unreadable diff must not
    quietly become "touches nothing" and send an operator hunting a scheduler
    bug that does not exist."""
    a = "sha256:" + "a" * 64
    # Real diffs both readable: an honest, unqualified conflict answer.
    assert gw.classify_underutil_cause(
        surplus_ids=[a], in_flight_paths={"src/x.py"},
        candidate_paths={a: {"src/x.py"}}, awaiting_verdict_ids=set()
    ) == "conflict-serialized"
    # Candidate diff unreadable -> same class, but qualified.
    assert gw.classify_underutil_cause(
        surplus_ids=[a], in_flight_paths={"src/x.py"},
        candidate_paths={a: None}, awaiting_verdict_ids=set()
    ) == "assembly-unknown(declared-only)"
    # In-flight diff unreadable -> qualified too, never silently "unknown".
    assert gw.classify_underutil_cause(
        surplus_ids=[a], in_flight_paths=None,
        candidate_paths={a: {"src/x.py"}}, awaiting_verdict_ids=set()
    ) == "assembly-unknown(declared-only)"
    # The remedy carries the qualification so it cannot be acted on blindly.
    assert "distrusts" in gw.underutil_remedy("conflict-serialized(declared-only)")
    assert "distrusts" not in gw.underutil_remedy("conflict-serialized")


def test_cause_is_not_computed_until_the_streak_is_about_to_fire(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One `git diff` per surplus candidate is affordable at the moment of
    firing and not on all 720 sweeps a day."""
    repo, queue = _fixture_repo(tmp_path)
    for n in range(4):
        (queue / "candidates" / f"c{n}.json").write_text(json.dumps(
            {"kind": "candidate", "id": f"sha256:{str(n) * 64}", "branch": f"lane/{n}",
             "base_sha": "a" * 40, "head_sha": "b" * 40}))
    diffs: list[Any] = []
    monkeypatch.setattr(gw, "real_paths",
                        lambda *a, **k: diffs.append(a) or {"src/x.py"})
    state_path = repo / "var" / "gate-watch" / "state.json"
    common = dict(repo=repo, queue=queue,
                  log_path=repo / "var" / "log" / "gate-watch.log",
                  gate_log=repo / "var" / "log" / "gate-loop.log",
                  builder_dir=queue / "state" / "gate-loop-build",
                  state_path=state_path,
                  util_path=repo / "var" / "gate-watch" / "utilization.jsonl",
                  th=TH, dry_run=False)
    for i in range(TH.underutil_sweeps - 2):        # streak climbs to 3 of 5
        gw.run_sweep(now=NOW + i * 120, **common)
    assert not diffs, "no diff may be spent while the streak is far from firing"
    gw.run_sweep(now=NOW + (TH.underutil_sweeps - 2) * 120, **common)   # streak 4 of 5
    assert diffs, "the cause must be resolved before the sweep that fires"


def test_d7_cause_is_part_of_the_dedup_identity() -> None:
    row = _util_row()
    ids = {c: gw.finding_id(_REPO_ROOT,
                            gw.detect_underutilisation(row=row, streak=9, cause=c,
                                                       th=TH).detections[0])
           for c in ("conflict-serialized", "awaiting-verdict", "assembly-unknown")}
    assert len(set(ids.values())) == 3, "each cause must be its own finding"
    # …and the same cause with different measurements is ONE finding.
    again = gw.finding_id(_REPO_ROOT,
                          gw.detect_underutilisation(row=_util_row(running_gates=0),
                                                     streak=41, cause="awaiting-verdict",
                                                     th=TH).detections[0])
    assert again == ids["awaiting-verdict"]


def test_d7_wakes_when_the_slot_ceiling_cannot_be_read() -> None:
    row = gw.compute_utilization(running_gates=0, max_slots=None, eligible_candidates=9,
                                 candidates_in_flight=0, now=NOW,
                                 degraded="pipeline gate_loop module unavailable")
    result = gw.detect_underutilisation(row=row, streak=0, cause="assembly-unknown", th=TH)
    assert result.fired, "an unknown ceiling must WAKE, never read as fully utilised"
    assert "instrument-unreadable" in result.detections[0].symptom


def test_d7_over_fires_on_two_gates_booked_on_one_box() -> None:
    """A favourable-absence pin: the scheduler makes this impossible, so nothing
    downstream looks for it, and if it happens the second gate is graded under
    contention it did not cause."""
    states = [("train__a@1.json", _running(NOW + 600, twin="mw0002")),
              ("train__b@2.json", _running(NOW + 600, twin="mw0002"))]
    result = gw.detect_overcommit(running_states=states, max_slots=3,
                                  twin_host="mw0001-owner", now=NOW)
    assert result.fired
    det = result.detections[0]
    assert det.detector == "D7-over"
    assert det.evidence["box"] == "mw0002"
    assert det.evidence["gates"] == ["train__a@1.json", "train__b@2.json"]
    assert not det.auto_remediable


def test_d7_over_is_silent_when_each_gate_has_its_own_box() -> None:
    states = [("a.json", _running(NOW + 600, twin="mw0002")),
              ("b.json", _running(NOW + 600, twin="mw0001-owner")),
              ("c.json", _running(NOW + 600, mode="direct"))]
    assert not gw.detect_overcommit(running_states=states, max_slots=3,
                                    twin_host="mw0001-owner", now=NOW).fired


def test_d7_over_treats_two_local_gates_as_double_booking() -> None:
    states = [("a.json", _running(NOW + 600, mode="direct")),
              ("b.json", _running(NOW + 600, mode="offload"))]
    result = gw.detect_overcommit(running_states=states, max_slots=3,
                                  twin_host="mw0001-owner", now=NOW)
    assert result.fired and result.detections[0].evidence["box"] == "local"


def test_d7_over_fires_when_running_exceeds_the_ceiling() -> None:
    states = [(f"{h}.json", _running(NOW + 600, twin=h))
              for h in ("h1", "h2", "h3", "h4")]
    result = gw.detect_overcommit(running_states=states, max_slots=3,
                                  twin_host="mw0001-owner", now=NOW)
    assert result.fired
    assert any("ceiling" in d.symptom or "over-booked" in d.symptom
               for d in result.detections)


def test_a_remote_gate_with_no_twin_named_defaults_to_the_primary() -> None:
    # gate_loop resolves a missing twin to TWIN_HOST; disagreeing here would
    # hide a real double-booking of the primary box.
    assert gw.gate_host_key({"mode": "remote", "twin": ""}, twin_host="mw0001-owner") \
        == "mw0001-owner"
    assert gw.gate_host_key({"mode": "direct"}, twin_host="mw0001-owner") == "local"


class _FakeGateLoop:
    def __init__(self, ceiling: Any, twin: str = "fake-host") -> None:
        self.MAX_CONCURRENT_GATES = ceiling
        self.TWIN_HOST = twin


def test_slot_ceiling_is_READ_from_the_pipeline_not_assumed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the DERIVATION, not today's value.

    The real ceiling on this estate is 3, so a hardcoded `3` passes every test
    that only checks the number — which is exactly the drift the pipeline warns
    about at MAX_CONCURRENT_GATES: a cap of 2 against a two-twin pool wasted a
    paid box for 728 deferrals, and a cap of 3 against one twin double-books it.
    A fake pool of 7 is the only thing that can tell reading from assuming.
    """
    monkeypatch.setattr(gw, "import_pipeline", lambda _r, _m: _FakeGateLoop(7, "twin-x"))
    ceiling, twin, why = gw.slot_ceiling(Path("/nowhere"))
    assert ceiling == 7 and twin == "twin-x" and why is None


def test_slot_ceiling_refuses_a_nonsense_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("three", 0, None, True):
        monkeypatch.setattr(gw, "import_pipeline", lambda _r, _m, b=bad: _FakeGateLoop(b))
        ceiling, _twin, why = gw.slot_ceiling(Path("/nowhere"))
        assert ceiling is None and why, f"{bad!r} must not be trusted as a ceiling"
    monkeypatch.setattr(gw, "import_pipeline", lambda _r, _m: None)
    ceiling, _twin, why = gw.slot_ceiling(Path("/nowhere"))
    assert ceiling is None and "unavailable" in (why or "")


def test_slot_ceiling_matches_the_real_pipelines_1_plus_active_twins() -> None:
    """Against the REAL checkout: whatever the pool is, the watchdog agrees with
    it. Computed independently from gate_host so the two would diverge if either
    side changed alone."""
    # gate_host resolves its own siblings off sys.path, which is exactly what
    # import_pipeline sets up; the independence that matters here is that the
    # ceiling comes from gate_loop's constant while the pool comes from
    # gate_host's TWIN_SPECS, so the two drift apart if either changes alone.
    host = gw.import_pipeline(_REPO_ROOT, "gate_host")
    assert host is not None

    ceiling, twin, why = gw.slot_ceiling(_REPO_ROOT)
    assert why is None
    assert ceiling == 1 + len(host.TWIN_SPECS)
    assert twin == host.TWIN_HOST


def test_the_module_cache_is_keyed_by_repo_not_just_module_name(tmp_path: Path) -> None:
    """A cache that ignores half its identity is a cache that lies.

    Keyed by module name alone, one process serving two repos returned the
    FIRST repo's modules for the second — which is how a fixture repo with no
    pipeline/ silently read the real checkout's slot ceiling and looked healthy,
    leaving the degraded path never actually exercised (cross-lineage review).
    """
    real = gw.import_pipeline(_REPO_ROOT, "gate_loop")
    assert real is not None and isinstance(real.MAX_CONCURRENT_GATES, int)

    other = tmp_path / "other-repo"
    (other / "pipeline" / "bridge").mkdir(parents=True)
    (other / "pipeline" / "bridge" / "gate_loop.py").write_text(
        'MAX_CONCURRENT_GATES = 11\nTWIN_HOST = "other-twin"\n')
    assert gw.import_pipeline(other, "gate_loop").MAX_CONCURRENT_GATES == 11
    assert gw.slot_ceiling(other) == (11, "other-twin", None)
    # …and the first repo is unchanged by the second's presence.
    assert gw.import_pipeline(_REPO_ROOT, "gate_loop") is real

    # A repo with NO pipeline stays degraded even after both of the above.
    bare = tmp_path / "bare"
    bare.mkdir()
    ceiling, _twin, why = gw.slot_ceiling(bare)
    assert ceiling is None and why, "a bare repo must not inherit another's ceiling"


def test_the_healthy_fixture_test_passes_STANDALONE(tmp_path: Path) -> None:
    """Isolation proof, run as a subprocess so the class cannot regress.

    The bug this pins was invisible in the suite: the target test passed only
    because an earlier test had already imported the real gate_loop. Order
    dependence hides exactly this kind of defect, so the guard has to run the
    node by itself.
    """
    node = (f"{Path(__file__).resolve()}"
            "::test_a_healthy_fixture_produces_a_clean_sweep")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "-p", "no:randomly",
         "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=tmp_path, timeout=300)
    assert proc.returncode == 0, f"standalone run failed:\n{proc.stdout[-3000:]}"


def test_candidate_loadability_mirrors_the_loaders_cheap_checks() -> None:
    sha = "a" * 40
    ok = {"kind": "candidate", "id": "sha256:x", "branch": "lane/x", "base_sha": sha,
          "head_sha": "b" * 40}
    assert gw.candidate_is_loadable(ok, content_id_fn=None)
    for broken in ({**ok, "kind": "proposal"}, {**ok, "id": ""}, {**ok, "branch": ""},
                   {**ok, "base_sha": "short"}, {**ok, "head_sha": "nope"}):
        assert not gw.candidate_is_loadable(broken, content_id_fn=None), broken
    # base_sha parity with load_candidates is `len == 40` and nothing else: no
    # strip, no hex class. A mirror that is STRICTER than the thing it mirrors
    # under-counts eligible work, which is the direction that hides waste.
    assert gw.candidate_is_loadable({**ok, "base_sha": "z" * 40}, content_id_fn=None), \
        "the loader accepts any 40 chars here; so must the mirror"
    assert gw.candidate_is_loadable({**ok, "base_sha": " " + "a" * 39}, content_id_fn=None), \
        "40 chars is 40 chars: the loader does not strip, and neither may the mirror"
    assert not gw.candidate_is_loadable({**ok, "base_sha": sha + " "}, content_id_fn=None), \
        "a padded 41-char base is rejected by the loader, so also by the mirror"
    # top-level and payload heads that disagree: identity is ambiguous.
    assert not gw.candidate_is_loadable(
        {**ok, "payload": {"head_sha": "c" * 40}}, content_id_fn=None)
    # a payload head is only trusted when the payload actually hashes to the id.
    hoisted = {"kind": "candidate", "id": "sha256:x", "branch": "lane/x",
               "base_sha": sha, "payload": {"head_sha": "b" * 40}}
    assert not gw.candidate_is_loadable(hoisted, content_id_fn=lambda _p: "sha256:other")
    assert gw.candidate_is_loadable(hoisted, content_id_fn=lambda _p: "sha256:x")
    assert not gw.candidate_is_loadable(hoisted, content_id_fn=None), \
        "an unverifiable payload head must not be assumed good"


def test_sweep_writes_one_utilization_row_per_sweep(tmp_path: Path) -> None:
    repo, queue = _fixture_repo(tmp_path)
    util = repo / "var" / "gate-watch" / "utilization.jsonl"
    for i in range(3):
        gw.run_sweep(repo=repo, queue=queue,
                     log_path=repo / "var" / "log" / "gate-watch.log",
                     gate_log=repo / "var" / "log" / "gate-loop.log",
                     builder_dir=queue / "state" / "gate-loop-build",
                     state_path=repo / "var" / "gate-watch" / "state.json",
                     util_path=util, now=NOW + i * 120, th=TH, dry_run=False)
    rows = [json.loads(ln) for ln in util.read_text().splitlines() if ln.strip()]
    assert len(rows) == 3, "the series must have a row per sweep, firing or not"
    assert [r["ts"] for r in rows] == [gw.iso(NOW + i * 120) for i in range(3)]
    assert all(r["running_gates"] == 0 for r in rows)


def test_the_utilization_series_is_bounded(tmp_path: Path) -> None:
    """~720 rows/day unbounded is ~50 MB/year — the same line volume this file's
    own ntfy floor exists to prevent."""
    path = tmp_path / "utilization.jsonl"
    # Production-shaped rows (~120 bytes), so the cheap size trigger in
    # append_utilization is exercised rather than bypassed.
    for i in range(60):
        gw.append_utilization(path, gw.compute_utilization(
            running_gates=i % 4, max_slots=3, eligible_candidates=i,
            candidates_in_flight=0, now=NOW + i), max_rows=10)
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    assert len(rows) <= 10, "the appender must bound the series on its own"
    assert rows[-1]["ts"] == gw.iso(NOW + 59), "the NEWEST rows are the ones kept"
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows), "order preserved"

    # The row cap itself is exact when the prune is asked directly.
    exact = tmp_path / "exact.jsonl"
    exact.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(25)))
    assert gw.prune_utilization(exact, max_rows=10) is True
    kept = [json.loads(ln) for ln in exact.read_text().splitlines() if ln.strip()]
    assert [r["n"] for r in kept] == list(range(15, 25))
    # Below the cap nothing is rewritten, and the prune says so.
    assert gw.prune_utilization(exact, max_rows=10) is False
    assert gw.prune_utilization(tmp_path / "absent.jsonl", max_rows=10) is False


def test_dry_run_writes_no_utilization_row(tmp_path: Path) -> None:
    repo, queue = _fixture_repo(tmp_path)
    util = repo / "var" / "gate-watch" / "utilization.jsonl"
    results = gw.run_sweep(repo=repo, queue=queue,
                           log_path=repo / "var" / "log" / "gate-watch.log",
                           gate_log=repo / "var" / "log" / "gate-loop.log",
                           builder_dir=queue / "state" / "gate-loop-build",
                           state_path=repo / "var" / "gate-watch" / "state.json",
                           util_path=util, now=NOW, th=TH, dry_run=True)
    assert not util.exists(), "--dry-run is print-only, on this path too"
    # …but the row is still COMPUTED and surfaced, so an operator can read the
    # live capacity without a second measurement.
    carried = [r.data for r in results if r.data is not None]
    assert len(carried) == 1 and "utilization_pct" in carried[0]


def test_sweep_counts_in_flight_members_and_surplus(tmp_path: Path) -> None:
    repo, queue = _fixture_repo(tmp_path)
    sha = "a" * 40
    members = []
    for n in range(4):
        ident = f"sha256:{str(n) * 64}"
        (queue / "candidates" / f"c{n}.json").write_text(json.dumps(
            {"kind": "candidate", "id": ident, "branch": f"lane/{n}",
             "base_sha": sha, "head_sha": "b" * 40, "paths": [f"src/{n}.py"]}))
        if n < 2:
            members.append(ident)
    (queue / "state" / "gates" / "train__x@y.json").write_text(json.dumps(
        _running(NOW + 600, members=members)))
    results = gw.run_sweep(repo=repo, queue=queue,
                           log_path=repo / "var" / "log" / "gate-watch.log",
                           gate_log=repo / "var" / "log" / "gate-loop.log",
                           builder_dir=queue / "state" / "gate-loop-build",
                           state_path=repo / "var" / "gate-watch" / "state.json",
                           util_path=repo / "var" / "gate-watch" / "utilization.jsonl",
                           now=NOW, th=TH, dry_run=True)
    row = next(r.data for r in results if r.data is not None)
    assert row["eligible_candidates"] == 4
    assert row["candidates_in_flight"] == 2
    assert row["running_gates"] == 1


# ---------------------------------------------------------------------------
# remedy cooldown
# ---------------------------------------------------------------------------

def test_remedy_cooldown_allows_then_blocks_then_allows_again() -> None:
    state: dict[str, Any] = {}
    assert gw.remedy_allowed(state, "builder_unlock", now=NOW, cooldown_min=30)
    state = gw.record_remedy(state, "builder_unlock", now=NOW, result="rc=0")
    assert not gw.remedy_allowed(state, "builder_unlock", now=NOW + 600, cooldown_min=30)
    assert gw.remedy_allowed(state, "builder_unlock", now=NOW + 1801, cooldown_min=30)
    assert state["builder_unlock"]["count"] == 1


def test_remedy_cooldown_fails_open_on_a_corrupt_state_record() -> None:
    # The cooldown bounds repetition; it is not a safety interlock. A corrupt
    # record must not permanently disable the one repair this job can make.
    assert gw.remedy_allowed({"builder_unlock": "garbage"}, "builder_unlock",
                             now=NOW, cooldown_min=30)
    assert gw.remedy_allowed({"builder_unlock": {"at": "not-a-number"}}, "builder_unlock",
                             now=NOW, cooldown_min=30)


def test_run_sweep_remedies_at_most_once_per_cooldown(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    repo, queue = _fixture_repo(tmp_path)
    builder = queue / "state" / "gate-loop-build"
    _orphan_lock(repo, builder)
    monkeypatch.setattr(gw, "probe_dir_in_use", lambda *a, **k: gw.IDLE)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> Any:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(gw.subprocess, "run", fake_run)
    state_path = repo / "var" / "gate-watch" / "state.json"
    common = dict(repo=repo, queue=queue, log_path=repo / "var" / "log" / "gate-watch.log",
                  gate_log=repo / "var" / "log" / "gate-loop.log", builder_dir=builder,
                  state_path=state_path,
                  util_path=repo / "var" / "gate-watch" / "utilization.jsonl",
                  th=TH, dry_run=False)

    gw.run_sweep(now=NOW, **common)
    gw.run_sweep(now=NOW + 300, **common)
    unlocks = [c for c in calls if "unlock" in c]
    assert len(unlocks) == 1, "the cooldown must bound the remedy to one attempt"

    gw.run_sweep(now=NOW + TH.remedy_cooldown_min * 60 + 1, **common)
    assert len([c for c in calls if "unlock" in c]) == 2


def test_remedy_stands_down_if_the_worktree_is_taken_between_probe_and_unlock(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TOCTOU. The probe behind the detection is seconds and several filesystem
    reads old by the time we would act; a builder that started in that gap holds
    the worktree we are about to unlock. The remedy's entire safety argument is
    "nothing is using it", so the observation backing it must be fresh."""
    repo, queue = _fixture_repo(tmp_path)
    builder = queue / "state" / "gate-loop-build"
    _orphan_lock(repo, builder)
    probes = iter([gw.IDLE, gw.BUSY])  # clean at gather time, taken by act time
    monkeypatch.setattr(gw, "probe_dir_in_use", lambda *a, **k: next(probes))
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> Any:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(gw.subprocess, "run", fake_run)
    results = gw.run_sweep(repo=repo, queue=queue,
                           log_path=repo / "var" / "log" / "gate-watch.log",
                           gate_log=repo / "var" / "log" / "gate-loop.log",
                           builder_dir=builder,
                           state_path=repo / "var" / "gate-watch" / "state.json",
                           util_path=repo / "var" / "gate-watch" / "utilization.jsonl",
                           now=NOW, th=TH, dry_run=False)
    assert any(d.auto_remediable for r in results for d in r.detections)
    assert not any("unlock" in c for c in calls), "a stale IDLE must not authorise an unlock"
    assert "stood down" in (repo / "var" / "log" / "gate-watch.log").read_text()


def test_a_raced_finding_does_not_kill_the_remaining_wake_legs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hand-run sweep can publish the same content id between our exists()
    check and our link(). Dedup has WON at that point — but an escaping
    FileExistsError would take the ntfy push and the medic down with it, so the
    filing race would silence the alert it was meant to accompany."""
    repo, queue = _fixture_repo(tmp_path, now=time.time())
    (queue / "candidates" / "c.json").write_text(json.dumps({"id": "sha256:" + "c" * 64}))
    (repo / "var" / "log" / "gate-loop.log").write_text(_tick(gw.iso(time.time() - 30)))
    monkeypatch.delenv("GATE_WATCH_MEDIC", raising=False)
    # The fixture repo is bare, so point the content-id machinery at the real
    # pipeline/bridge — this test is about the filing RACE, not about a missing
    # canonicaliser short-circuiting the leg before it ever writes.
    monkeypatch.setattr(gw, "_pipeline_dir", lambda _repo: _REPO_ROOT / "pipeline" / "bridge")
    real_link = os.link

    def racing_link(src: str, dst: str) -> None:
        real_link(src, dst)          # the other writer published first…
        raise FileExistsError(dst)   # …so our link loses

    monkeypatch.setattr(gw.os, "link", racing_link)
    pushes: list[str] = []
    monkeypatch.setattr(gw, "push_ntfy",
                        lambda *a, **k: (pushes.append(a[2]), (True, "ntfy pushed"))[1])

    assert gw.main(["--repo", str(repo), "--queue", str(queue)]) == 0
    log = (repo / "var" / "log" / "gate-watch.log").read_text()
    assert "filed concurrently" in log
    assert pushes, "leg (iii) must still run after a filing race"
    assert "medic disabled" in log, "leg (iv) must still run after a filing race"


def test_an_unexpected_filing_error_does_not_kill_the_remaining_wake_legs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The never-raising wrapper around leg (ii), pinned.

    FileExistsError is the race we anticipated; this is the one we did not. A
    disk-full, a permission change, a torn ledger — any of them escaping would
    take the ntfy push and the medic down with the filing, so the leg that
    merely RECORDS the alert would silence the legs that DELIVER it.
    """
    repo, queue = _fixture_repo(tmp_path, now=time.time())
    (queue / "candidates" / "c.json").write_text(json.dumps({"id": "sha256:" + "c" * 64}))
    (repo / "var" / "log" / "gate-loop.log").write_text(_tick(gw.iso(time.time() - 30)))
    monkeypatch.delenv("GATE_WATCH_MEDIC", raising=False)

    def boom(*_a: Any, **_k: Any) -> str:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(gw, "_file_finding", boom)
    pushes: list[str] = []
    monkeypatch.setattr(gw, "push_ntfy",
                        lambda *a, **k: (pushes.append(a[2]), (True, "ntfy pushed"))[1])

    assert gw.main(["--repo", str(repo), "--queue", str(queue)]) == 0
    log = (repo / "var" / "log" / "gate-watch.log").read_text()
    assert "finding NOT filed" in log and "wake continues" in log
    assert pushes, "leg (iii) must survive an unexpected filing error"
    assert "medic disabled" in log, "leg (iv) must survive an unexpected filing error"


def test_dry_run_sweep_never_remedies(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    repo, queue = _fixture_repo(tmp_path)
    builder = queue / "state" / "gate-loop-build"
    _orphan_lock(repo, builder)
    monkeypatch.setattr(gw, "probe_dir_in_use", lambda *a, **k: gw.IDLE)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> Any:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(gw.subprocess, "run", fake_run)
    results = gw.run_sweep(repo=repo, queue=queue,
                           log_path=repo / "var" / "log" / "gate-watch.log",
                           gate_log=repo / "var" / "log" / "gate-loop.log",
                           builder_dir=builder,
                           state_path=repo / "var" / "gate-watch" / "state.json",
                           util_path=repo / "var" / "gate-watch" / "utilization.jsonl",
                           now=NOW, th=TH, dry_run=True)
    assert any(d.auto_remediable for r in results for d in r.detections)
    assert not any("unlock" in c for c in calls)


# ---------------------------------------------------------------------------
# lsof probe
# ---------------------------------------------------------------------------

def test_push_throttle_speaks_once_per_incident(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 12h stall at a 120s interval is ~360 sweeps. The phone must hear one
    notification, not 360 — alarm fatigue is the failure mode that makes the
    NEXT real alert invisible."""
    del monkeypatch
    state: dict[str, Any] = {}
    assert gw.should_push(state, "D1", now=NOW, floor_min=60)
    state = gw.record_push(state, "D1", now=NOW)
    # Same incident, later sweeps: silent, forever.
    assert not gw.should_push(state, "D1", now=NOW + 300, floor_min=60)
    assert not gw.should_push(state, "D1", now=NOW + 12 * 3600, floor_min=60)


def test_push_throttle_speaks_again_when_the_incident_changes_shape() -> None:
    state = gw.record_push({}, "D1", now=NOW)
    # A changed set still waits out the floor — a flapping detector must not be
    # able to buy unlimited pushes.
    assert not gw.should_push(state, "D1,D5", now=NOW + 600, floor_min=60)
    assert gw.should_push(state, "D1,D5", now=NOW + 3601, floor_min=60)


def test_push_throttle_compares_against_the_last_PUSHED_set() -> None:
    # A change suppressed by the floor must still push once the floor expires,
    # which only works if the stored key is the last pushed one, not last seen.
    state = gw.record_push({}, "D1", now=NOW)
    assert not gw.should_push(state, "D1,D5", now=NOW + 60, floor_min=60)
    assert gw.should_push(state, "D1,D5", now=NOW + 3601, floor_min=60)


def test_a_clean_sweep_ends_the_incident_so_a_recurrence_is_announced() -> None:
    state = gw.record_push({}, "D1", now=NOW)
    cleared = gw.clear_push_key(state)
    assert cleared is not None
    assert gw.should_push(cleared, "D1", now=NOW + 3601, floor_min=60)
    # Nothing to clear -> no state write, so a clean sweep does not rewrite the
    # file every 120 seconds.
    assert gw.clear_push_key(cleared) is None
    assert gw.clear_push_key({}) is None


def test_a_failed_push_does_not_advance_the_throttle() -> None:
    """The wedge: push_ntfy is fail-soft, so a transient ntfy outage at the
    MOMENT an incident starts would stamp the key as pushed and mute that
    incident shape for as long as it lasts. Only a confirmed delivery counts."""
    state: dict[str, Any] = {}
    key = "D1,D3"
    assert gw.should_push(state, key, now=NOW, floor_min=60)
    # Attempt fails: record the ATTEMPT only.
    state = gw.record_push_attempt(state, key, now=NOW, delivered=False, note="ntfy FAILED")
    assert gw.should_push(state, key, now=NOW + 120, floor_min=60), \
        "a failed push must retry next sweep"
    assert state["ntfy_attempt"]["consecutive_failures"] == 1
    assert "ntfy" not in state, "an attempt must never look like a delivery"
    # Second attempt fails too: still eligible, failures accumulate.
    state = gw.record_push_attempt(state, key, now=NOW + 120, delivered=False, note="ntfy FAILED")
    assert state["ntfy_attempt"]["consecutive_failures"] == 2
    assert gw.should_push(state, key, now=NOW + 240, floor_min=60)
    # Third attempt lands: NOW the floor applies.
    state = gw.record_push_attempt(state, key, now=NOW + 240, delivered=True, note="ntfy pushed")
    state = gw.record_push(state, key, now=NOW + 240)
    assert state["ntfy_attempt"]["consecutive_failures"] == 0
    assert not gw.should_push(state, key, now=NOW + 300, floor_min=60)


def test_a_dead_push_channel_backs_off_and_a_delivery_restores_it() -> None:
    """Retrying every sweep is right for a transient outage and wrong for a dead
    channel: notify.push_alert notes each failure in ALERTS.md, so unbounded
    retry would write ~720 lines/day into the file the other loops read —
    trading a silent failure for a noisy one. After 5 consecutive failures,
    attempts drop to the hourly floor; any delivery restores full
    responsiveness immediately."""
    state: dict[str, Any] = {}
    key = "D1,D3"
    for i in range(5):
        at = NOW + i * 120
        assert gw.should_push(state, key, now=at, floor_min=60, retry_floor_after=5), i
        state = gw.record_push_attempt(state, key, now=at, delivered=False, note="ntfy FAILED")
    assert state["ntfy_attempt"]["consecutive_failures"] == 5
    last_try = NOW + 4 * 120

    # The 6th sweep, two minutes later, does NOT attempt…
    assert not gw.should_push(state, key, now=last_try + 120, floor_min=60, retry_floor_after=5)
    # …nor anywhere else inside the hour…
    assert not gw.should_push(state, key, now=last_try + 3599, floor_min=60, retry_floor_after=5)
    # …but one attempt is allowed once the floor expires.
    assert gw.should_push(state, key, now=last_try + 3601, floor_min=60, retry_floor_after=5)

    # That attempt delivers: the counter resets and the ordinary
    # novelty/floor throttle takes over again.
    landed = last_try + 3601
    state = gw.record_push_attempt(state, key, now=landed, delivered=True, note="ntfy pushed")
    state = gw.record_push(state, key, now=landed)
    assert state["ntfy_attempt"]["consecutive_failures"] == 0
    assert not gw.should_push(state, key, now=landed + 60, floor_min=60, retry_floor_after=5)
    # A NEW incident shape after the floor is announced normally — the backoff
    # is gone, not merely paused.
    assert gw.should_push(state, "D1,D3,D5", now=landed + 3601, floor_min=60,
                          retry_floor_after=5)


def test_backoff_does_not_engage_below_the_failure_threshold() -> None:
    state: dict[str, Any] = {}
    for i in range(4):
        state = gw.record_push_attempt(state, "D1", now=NOW + i * 120,
                                       delivered=False, note="ntfy FAILED")
    assert state["ntfy_attempt"]["consecutive_failures"] == 4
    assert gw.should_push(state, "D1", now=NOW + 4 * 120, floor_min=60, retry_floor_after=5)


def test_state_distinguishes_delivered_from_attempted() -> None:
    state = gw.record_push_attempt({}, "D1", now=NOW, delivered=False, note="boom")
    assert state["ntfy_attempt"]["delivered"] is False
    assert state.get("ntfy") is None
    delivered = gw.record_push(
        gw.record_push_attempt({}, "D1", now=NOW, delivered=True, note="ok"), "D1", now=NOW)
    assert delivered["ntfy"]["at"] == NOW and delivered["ntfy_attempt"]["delivered"] is True


def test_main_retries_the_push_after_a_failed_delivery(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, queue = _fixture_repo(tmp_path, now=time.time())
    (queue / "candidates" / "c.json").write_text(json.dumps({"id": "sha256:" + "c" * 64}))
    (repo / "var" / "log" / "gate-loop.log").write_text(_tick(gw.iso(time.time() - 30)))
    monkeypatch.delenv("GATE_WATCH_HEALTHCHECK_URL", raising=False)
    monkeypatch.delenv("GATE_WATCH_MEDIC", raising=False)
    monkeypatch.setattr(gw, "file_finding", lambda *a, **k: "filed (stub)")
    attempts: list[bool] = []
    outcomes = iter([False, False, True])  # two transient failures, then delivery

    def flaky(*_a: Any, **_k: Any) -> tuple[bool, str]:
        ok = next(outcomes, True)
        attempts.append(ok)
        return ok, "ntfy pushed" if ok else "ntfy FAILED (TimeoutError)"

    monkeypatch.setattr(gw, "push_ntfy", flaky)
    for _ in range(5):
        assert gw.main(["--repo", str(repo), "--queue", str(queue)]) == 0
    # Retried through both failures, then went quiet once delivered.
    assert attempts == [False, False, True], attempts


def test_fired_key_is_the_detector_set_not_the_finding_list() -> None:
    a = gw.Detection(detector="D4", symptom="x", remedy="y")
    b = gw.Detection(detector="D4", symptom="z", remedy="y")
    c = gw.Detection(detector="D1", symptom="q", remedy="y")
    assert gw.fired_key([a, b]) == "D4"
    assert gw.fired_key([a, c]) == "D1,D4"


def test_main_pushes_once_across_repeated_identical_stalls(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, queue = _fixture_repo(tmp_path, now=time.time())
    (queue / "candidates" / "c.json").write_text(json.dumps({"id": "sha256:" + "c" * 64}))
    (repo / "var" / "log" / "gate-loop.log").write_text(_tick(gw.iso(time.time() - 30)))
    monkeypatch.delenv("GATE_WATCH_HEALTHCHECK_URL", raising=False)
    monkeypatch.delenv("GATE_WATCH_MEDIC", raising=False)
    pushes: list[str] = []
    monkeypatch.setattr(gw, "push_ntfy",
                        lambda *a, **k: (pushes.append(a[2]), (True, "ntfy pushed"))[1])
    monkeypatch.setattr(gw, "file_finding", lambda *a, **k: "filed (stub)")

    for _ in range(5):
        assert gw.main(["--repo", str(repo), "--queue", str(queue)]) == 0
    assert len(pushes) == 1, f"expected one push for one ongoing incident, got {pushes}"


def test_probe_reads_lsof_conservatively(tmp_path: Path) -> None:
    live = tmp_path / "wt"
    live.mkdir()

    def runner(rc: int, out: str = "", err: str = "") -> Any:
        return lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], rc, out, err)

    assert gw.probe_dir_in_use(live, runner=runner(0, "COMMAND PID\ngit 1")) == gw.BUSY
    assert gw.probe_dir_in_use(live, runner=runner(1)) == gw.IDLE
    # macOS emits WARNING lines constantly; treating them as errors would pin
    # the probe at UNKNOWN forever and disable the remedy.
    assert gw.probe_dir_in_use(live, runner=runner(1, "", "lsof: WARNING: can't stat()")) == gw.IDLE
    assert gw.probe_dir_in_use(live, runner=runner(1, "", "lsof: status error")) == gw.UNKNOWN
    assert gw.probe_dir_in_use(live, runner=runner(9)) == gw.UNKNOWN
    # A missing worktree cannot be held by anything — that is the orphan case.
    assert gw.probe_dir_in_use(tmp_path / "gone") == gw.IDLE


def test_probe_reports_unknown_when_lsof_is_absent_or_hangs(tmp_path: Path) -> None:
    live = tmp_path / "wt"
    live.mkdir()

    def boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("lsof")

    def hang(*_a: Any, **_k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="lsof", timeout=20)

    assert gw.probe_dir_in_use(live, runner=boom) == gw.UNKNOWN
    assert gw.probe_dir_in_use(live, runner=hang) == gw.UNKNOWN


# ---------------------------------------------------------------------------
# dedup determinism + filing
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _detection(**over: Any) -> Any:
    kwargs: dict[str, Any] = dict(
        detector="D4", symptom="candidate abc is skipped every tick",
        remedy="repair or withdraw the candidate", evidence={"skips_in_window": 41})
    kwargs.update(over)
    return gw.Detection(**kwargs)


def test_payload_is_exactly_the_three_dedup_keys() -> None:
    assert set(_detection().payload()) == {"symptom", "detector", "remedy"}


def test_content_id_is_stable_across_sweeps_with_different_evidence() -> None:
    # THE dedup property: a stall that persists for hours must keep minting the
    # same id, so it files once. Volatile data lives in evidence, never in the
    # payload.
    first = gw.finding_id(_REPO_ROOT, _detection(evidence={"skips_in_window": 41}))
    second = gw.finding_id(_REPO_ROOT, _detection(evidence={"skips_in_window": 987,
                                                            "observed_at": "later"}))
    assert first is not None and first == second
    assert first.startswith("sha256:")


def test_content_id_differs_per_detector_and_per_subject() -> None:
    base = gw.finding_id(_REPO_ROOT, _detection())
    assert base != gw.finding_id(_REPO_ROOT, _detection(detector="D3"))
    assert base != gw.finding_id(_REPO_ROOT, _detection(symptom="candidate def is skipped"))


def test_detectors_never_put_volatile_data_in_the_payload() -> None:
    lines = _lines(*[(-i * 60, "  skip 3f1bdd8fe67f: missing head_sha") for i in range(40)])
    a = gw.detect_skip_storm(lines=lines, now=NOW, th=TH).detections[0]
    b = gw.detect_skip_storm(lines=lines + _lines((-30, "  skip 3f1bdd8fe67f: missing head_sha")),
                             now=NOW + 900, th=TH).detections[0]
    assert a.payload() == b.payload()
    assert a.evidence["skips_in_window"] != b.evidence["skips_in_window"]


def test_file_finding_writes_once_and_dedups_after(tmp_path: Path) -> None:
    repo, queue = _fixture_repo(tmp_path)
    det = _detection()
    first = gw.file_finding(_REPO_ROOT, queue, det, now=NOW)
    assert first.startswith("filed finding")
    ident = gw.finding_id(_REPO_ROOT, det)
    assert ident is not None
    written = json.loads((queue / "findings" / f"{gw.stem_of(ident)}.json").read_text())
    assert written["kind"] == "finding"
    assert written["producer"]["actor"] == "gate-watch"
    assert written["payload"]["symptom"] == det.symptom

    found = _ledger(queue, "found")
    assert len(found) == 1
    assert found[0]["id"] == ident
    assert found[0]["actor"] == "gate-watch"
    # The schema's role enum — an out-of-vocabulary role makes the event
    # unreadable to every status reader.
    assert found[0]["role"] in ("planner", "reviewer", "implementer", "external")

    assert "already filed" in gw.file_finding(_REPO_ROOT, queue, det, now=NOW + 120)
    assert len(list((queue / "findings").glob("*.json"))) == 1
    assert len(_ledger(queue, "found")) == 1
    del repo


def test_file_finding_heals_a_missing_found_event(tmp_path: Path) -> None:
    # The artifact write and the ledger append are two steps; a crash between
    # them leaves a finding on disk that no ledger reader can see.
    _, queue = _fixture_repo(tmp_path)
    det = _detection()
    ident = gw.finding_id(_REPO_ROOT, det)
    assert ident is not None
    gw.write_json_atomic(queue / "findings" / f"{gw.stem_of(ident)}.json",
                         {"id": ident, "kind": "finding"})
    assert "healed" in gw.file_finding(_REPO_ROOT, queue, det, now=NOW)
    found = _ledger(queue, "found")
    assert len(found) == 1
    assert found[0]["detail"]["healed"] is True


def test_file_finding_never_overrides_a_disposition(tmp_path: Path) -> None:
    _, queue = _fixture_repo(tmp_path)
    det = _detection()
    ident = gw.finding_id(_REPO_ROOT, det)
    assert ident is not None
    (queue / "rejected" / f"{gw.stem_of(ident)}.json").write_text('{"remedy": "drop"}')
    assert "suppressed" in gw.file_finding(_REPO_ROOT, queue, det, now=NOW)
    assert not list((queue / "findings").glob("*.json"))


# ---------------------------------------------------------------------------
# wake legs (no network: every pinger is injected or monkeypatched)
# ---------------------------------------------------------------------------

def test_healthcheck_is_skipped_when_unset_and_fail_soft_when_broken() -> None:
    assert "skipped" in gw.ping_healthcheck("")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise TimeoutError("no route")

    assert "FAILED" in gw.ping_healthcheck("https://example.invalid/ping", opener=boom)


def test_healthcheck_pings_when_armed() -> None:
    seen: list[Any] = []

    class _Resp:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

    def opener(url: str, timeout: float | None = None) -> Any:
        seen.append((url, timeout))
        return _Resp()

    assert gw.ping_healthcheck("https://hc/uuid", opener=opener) == "healthcheck pinged"
    assert seen == [("https://hc/uuid", 5)]


def test_medic_needs_both_switches_and_is_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATE_WATCH_MEDIC", raising=False)
    monkeypatch.setenv("GATE_WATCH_MEDIC_CMD", "/bin/true")
    assert "disabled" in gw.spawn_medic("summary")

    monkeypatch.setenv("GATE_WATCH_MEDIC", "1")
    monkeypatch.delenv("GATE_WATCH_MEDIC_CMD", raising=False)
    assert "disabled" in gw.spawn_medic("summary")

    monkeypatch.setenv("GATE_WATCH_MEDIC_CMD", "/bin/echo wake")
    seen: dict[str, Any] = {}

    def spawner(argv: list[str], **kwargs: Any) -> Any:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return object()

    assert "spawned" in gw.spawn_medic("D1 fired", spawner=spawner)
    assert seen["argv"] == ["/bin/echo", "wake", "D1 fired"]
    assert seen["kwargs"]["start_new_session"] is True


def test_medic_spawn_failure_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATE_WATCH_MEDIC", "1")
    monkeypatch.setenv("GATE_WATCH_MEDIC_CMD", "/nonexistent/medic")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("/nonexistent/medic")

    assert "FAILED" in gw.spawn_medic("D1 fired", spawner=boom)


def test_ntfy_is_skipped_when_the_url_is_unset(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    monkeypatch.delenv("OMNI_NTFY_URL", raising=False)
    _, queue = _fixture_repo(tmp_path)
    delivered, note = gw.push_ntfy(_REPO_ROOT, queue, "title")
    assert delivered is False, "an unset URL is not a delivery — arming it later must push"
    assert "skipped" in note


# ---------------------------------------------------------------------------
# THE unreadable-input property, end to end
# ---------------------------------------------------------------------------

def test_an_unreadable_ledger_wakes_rather_than_crashing_or_passing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The single most important behaviour here: absence is never good news.

    A ledger that cannot be read says NOTHING about the pipeline's health, so it
    must not produce a clean sweep — and it must not take the watchdog down
    either, because launchd would simply re-run the crash every 120 seconds.
    """
    repo, queue = _fixture_repo(tmp_path)
    (queue / "ledger.jsonl").unlink()
    (queue / "ledger.jsonl").mkdir()  # a directory where a file belongs: unreadable

    results = gw.run_sweep(repo=repo, queue=queue,
                           log_path=repo / "var" / "log" / "gate-watch.log",
                           gate_log=repo / "var" / "log" / "gate-loop.log",
                           builder_dir=queue / "state" / "gate-loop-build",
                           state_path=repo / "var" / "gate-watch" / "state.json",
                           util_path=repo / "var" / "gate-watch" / "utilization.jsonl",
                           now=NOW, th=TH, dry_run=True)

    detections = [d for r in results for d in r.detections]
    assert detections, "an unreadable ledger must WAKE, never read as a clean sweep"
    assert any("instrument-unreadable" in d.symptom for d in detections)
    assert {r.detector for r in results if r.fired} >= {"D3", "D5"}
    assert not any(d.auto_remediable for d in detections)
    del monkeypatch


def test_an_unreadable_gate_log_wakes_d2_and_d4(tmp_path: Path) -> None:
    repo, queue = _fixture_repo(tmp_path)
    (repo / "var" / "log" / "gate-loop.log").unlink()
    (repo / "var" / "log" / "gate-loop.log").mkdir()
    results = gw.run_sweep(repo=repo, queue=queue,
                           log_path=repo / "var" / "log" / "gate-watch.log",
                           gate_log=repo / "var" / "log" / "gate-loop.log",
                           builder_dir=queue / "state" / "gate-loop-build",
                           state_path=repo / "var" / "gate-watch" / "state.json",
                           util_path=repo / "var" / "gate-watch" / "utilization.jsonl",
                           now=NOW, th=TH, dry_run=True)
    fired = {r.detector for r in results if r.fired}
    assert {"D2", "D4"} <= fired


def test_a_healthy_fixture_produces_a_clean_sweep(tmp_path: Path) -> None:
    repo, queue = _fixture_repo(tmp_path)
    results = gw.run_sweep(repo=repo, queue=queue,
                           log_path=repo / "var" / "log" / "gate-watch.log",
                           gate_log=repo / "var" / "log" / "gate-loop.log",
                           builder_dir=queue / "state" / "gate-loop-build",
                           state_path=repo / "var" / "gate-watch" / "state.json",
                           util_path=repo / "var" / "gate-watch" / "utilization.jsonl",
                           now=NOW, th=TH, dry_run=True)
    assert not [d for r in results for d in r.detections], \
        [r.note for r in results if r.fired]
    assert [r.detector for r in results] == ["D1", "D2", "D3", "D4", "D5", "D6",
                                             "D7", "D7-over"]
    # The ceiling came from THIS repo's pipeline (4), never from the estate's
    # real one (3) via a stale module cache.
    row = next(r.data for r in results if r.data is not None)
    assert row["max_slots"] == 4, "the sweep must read its OWN repo's ceiling"


def test_main_pings_the_dead_man_only_on_a_clean_sweep(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dead-man is the only thing that detects a broken WATCHDOG, so it must
    be wired to the env var — and it must stay silent while detectors are
    firing, or a stalled pipeline would keep the operator's check green."""
    repo, queue = _fixture_repo(tmp_path, now=time.time())
    monkeypatch.setenv("GATE_WATCH_HEALTHCHECK_URL", "https://hc/uuid")
    monkeypatch.delenv("OMNI_NTFY_URL", raising=False)
    monkeypatch.delenv("GATE_WATCH_MEDIC", raising=False)
    pings: list[str] = []
    monkeypatch.setattr(gw, "ping_healthcheck", lambda url, **_: pings.append(url) or "pinged")
    filed: list[Any] = []
    monkeypatch.setattr(gw, "file_finding",
                        lambda *a, **k: filed.append(a) or "filed (stub)")

    assert gw.main(["--repo", str(repo), "--queue", str(queue)]) == 0
    assert pings == ["https://hc/uuid"]
    assert not filed

    # Now break something: the dead-man must go quiet and the wake legs run.
    (queue / "candidates" / "c.json").write_text(json.dumps({"id": "sha256:" + "c" * 64}))
    (repo / "var" / "log" / "gate-loop.log").write_text(_tick(gw.iso(time.time() - 30)))
    assert gw.main(["--repo", str(repo), "--queue", str(queue)]) == 0
    assert pings == ["https://hc/uuid"], "a firing sweep must not keep the check green"
    assert filed, "a firing sweep must file its finding"


def test_main_dry_run_exits_zero_and_prints_a_summary(tmp_path: Path,
                                                      capsys: Any) -> None:
    repo, queue = _fixture_repo(tmp_path, now=time.time())
    rc = gw.main(["--repo", str(repo), "--queue", str(queue), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sweep" in out and "(dry-run)" in out
    for detector in ("D1", "D2", "D3", "D4", "D5", "D6"):
        assert detector in out


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _fixture_repo(tmp_path: Path, *, now: float = NOW) -> tuple[Path, Path]:
    """A minimal, HEALTHY repo+queue: fresh tick, a landed train, no candidates,
    no locks. Each test breaks exactly the one thing it is about.

    ``now`` is explicit because ``main()`` reads the real clock while the
    detector tests freeze it at ``NOW`` — a fixture built against the frozen
    clock and judged against the real one is a test that starts failing on a
    date rather than on a defect.
    """
    repo = tmp_path / "repo"
    queue = repo / "var" / "loopqueue"
    for sub in ("findings", "rejected", "parked", "candidates", "state/gates"):
        (queue / sub).mkdir(parents=True, exist_ok=True)
    (repo / "var" / "log").mkdir(parents=True, exist_ok=True)
    # A minimal pipeline so the fixture has a READABLE slot ceiling of its own.
    # The ceiling is 4, deliberately NOT the estate's real 3: any test that sees
    # 3 here is reading the wrong repo's module, which is exactly the cache-key
    # defect this fixture now guards against.
    bridge = repo / "pipeline" / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "gate_loop.py").write_text(
        'MAX_CONCURRENT_GATES = 4\nTWIN_HOST = "fixture-twin"\n')
    (queue / "ledger.jsonl").write_text(
        json.dumps({"ts": gw.iso(now - 120), "role": "implementer", "event": "merged",
                    "id": "sha256:" + "a" * 64}) + "\n")
    log = repo / "var" / "log" / "gate-loop.log"
    log.write_text(_tick(gw.iso(now - 60), "  dispatched gate for train/gl-1 @ abc123\n"))
    return repo, queue


def _ledger(queue: Path, event: str) -> list[dict[str, Any]]:
    return [e for e in (json.loads(ln) for ln in
                        (queue / "ledger.jsonl").read_text().splitlines() if ln.strip())
            if e.get("event") == event]


def _orphan_lock(repo: Path, builder: Path) -> None:
    """A git worktree admin dir carrying a stale `initializing` lock, with the
    worktree directory itself already gone — the shape seen on 2026-08-10."""
    admin = repo / ".git" / "worktrees" / builder.name
    admin.mkdir(parents=True, exist_ok=True)
    lock = admin / "locked"
    lock.write_text("initializing\n")
    os.utime(lock, (NOW - 3600, NOW - 3600))
