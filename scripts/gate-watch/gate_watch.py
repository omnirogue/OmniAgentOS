#!/usr/bin/env python3
"""Gate health watchdog — ONE sweep, then exit (launchd StartInterval re-runs it).

WHY THIS EXISTS (operator, 2026-08-10). The merge-gate pipeline stalled three
times in one day and nothing woke anybody:

  a. **candidate starvation** — every tick logged "no gate-eligible candidates"
     or a per-candidate `skip <id>:` line, for 11.6 hours;
  b. **twin false-rejections** — `instrument_error` / rc-255 clusters;
  c. **an orphaned builder-worktree lock** — `.git/worktrees/gate-loop-build/locked`
     with reason "initializing" halted ALL train assembly for 30 minutes behind a
     single "ALERT: refusing to disturb" line in a log nobody drains.

Each of those is *visible* in a file on disk and *invisible* to a human. This
job is the detector, the one safe remedy, and the wake path.

DESIGN RULES, in the order they matter:

1. **Absence is never good news.** An input this watchdog cannot read is a WAKE
   with reason `instrument-unreadable` — never a crash (launchd would just re-run
   it) and never a silent pass (that is the exact failure mode being fixed). A
   watchdog that fails quiet is worse than no watchdog, because it also removes
   the operator's suspicion.

2. **Detectors are pure.** Every ``detect_*`` takes injected strings/paths/times
   and returns a :class:`DetectorResult`. No clock, no filesystem, no network
   inside them — that is what makes the six stall shapes testable without a live
   daemon. ``main()`` is the only place that touches the world.

3. **Exactly one auto-remedy** (D1's ``git worktree unlock``), cooldown-bounded
   through a state file. Everything else wakes a human. A watchdog that repairs
   things it does not fully understand manufactures new failure modes.

4. **Findings dedup by content, so the payload carries NO volatile data.** The
   id is sha256 over the RFC-8785 canonical form of ``{symptom, detector, remedy}``
   only; counts, timestamps and ages ride in ``evidence``, which is outside the
   id. Put a count in the symptom and a 12-hour stall files 300 findings.

Exit code is 0 for any completed sweep — detections are reported, not raised.
Non-zero means the sweep itself could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_DEFAULT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DEFAULT / "pipeline"))
from bridge.ledger_write import append_event  # noqa: E402

ACTOR = "gate-watch"
ROLE = "external"  # the ledger-event schema enum; a watchdog is outside the loops

# Terminal ledger events. `closed` IS terminal since 157c2fb9c (the finding-side
# terminal event) — omitting it would make closed findings look like live work.
TERMINAL_EVENTS = ("merged", "completed", "rejected", "closed")

# `lsof` verdicts. UNKNOWN is a first-class answer, not an error: it is the
# difference between "nothing holds the worktree" and "we could not tell", and
# only the first one may be remedied.
BUSY = "busy"
IDLE = "idle"
UNKNOWN = "unknown"

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
# The gate loop stamps a tick by printing a JSON block ending the tick, whose
# "at" field is the tick time; the free-text lines PRECEDE it (see gate_loop.py
# run_once / the `--once` printer).
_AT_RE = re.compile(r'"at"\s*:\s*"(' + _ISO_RE.pattern + r')')
# `skip <shortid>: <reason>` — keyed on the CANDIDATE, not the whole line, so a
# reason whose text varies (a moving branch tip) still counts as one storm.
_SKIP_RE = re.compile(r"\bskip ([0-9a-f]{6,64})\s*:\s*(.*)")
# The ONE skip reason that is a sanctioned wait rather than a fault: the loop
# refuses to mechanically land a risky diff until a cross-lineage review seat
# returns a verdict, and it says so every tick while that seat is queued. Waiting
# hours for a human/AI reviewer is the system WORKING; counting it as a storm
# reports healthy blocked-on-human work as a stall and trains the operator to
# ignore the alert. It gets its own detector and its own much longer threshold
# instead, so "waiting" and "waiting far too long" stay distinguishable.
_AWAITING_VERDICT = "risky diff requires a genuine cross-lineage build verdict"
SKIP_AWAITING_VERDICT = "awaiting-verdict"
SKIP_FAULT = "fault"


def classify_skip_reason(reason: str) -> str:
    """Which skip class a `skip <id>: <reason>` line belongs to.

    Everything that is not the sanctioned verdict wait — a missing or
    unresolvable ``head_sha``, a branch that moved away from its approved sha, an
    unreadable diff — is a FAULT: the candidate can never land until someone
    repairs it, and no amount of waiting helps.
    """
    return SKIP_AWAITING_VERDICT if _AWAITING_VERDICT in reason else SKIP_FAULT
# What counts as the pipeline MAKING PROGRESS. Deliberately not a bare "landed"
# substring: the loop also logs "the shell half of the closure contract has NOT
# landed in this pinned workspace" on every dispatch, and "the candidate landed,
# the bound test did not pass" on a warning path. Either would have matched and
# suppressed D3 forever — a favourable-absence bug in the detector whose whole
# job is to catch favourable absence. The four alternatives below are the loop's
# actual progress emissions: the dispatch line (gate_loop `dispatched gate for
# <branch>`), the machine-readable per-tick outcome action, the uppercase LANDED
# line, and the single-writer serialisation note.
_PROGRESS_RE = re.compile(
    r'dispatched gate|"action"\s*:\s*"(?:landed|dispatched)"|\bLANDED\b'
    r'|one train landed this tick')


# ---------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Thresholds:
    """Every number this watchdog judges by, in one place and env-overridable.

    Defaults are the operator's: a stall shorter than these is ordinary work.
    """

    lock_stale_min: int = 10
    remedy_cooldown_min: int = 30
    tick_stale_min: int = 5
    dispatch_stale_min: int = 45
    skip_storm: int = 30
    instr_cluster: int = 3
    # A candidate legitimately waits hours for a review seat; only a wait past
    # this says the SEAT is stuck rather than busy.
    verdict_stale_min: int = 360
    # Floor between two phone pushes. At a 120s StartInterval an unthrottled
    # push leg turns a 12-hour stall into ~360 notifications, which is the
    # alarm fatigue that makes the next real alert invisible.
    push_floor_min: int = 60
    # Consecutive failed DELIVERIES after which retries back off to one per
    # push_floor_min. Retrying every sweep is right for a transient outage and
    # wrong for a dead channel: notify.push_alert records each failure in
    # ALERTS.md, so an unbounded retry writes ~720 lines/day into the file the
    # other loops read — trading one silent failure for a noisy one.
    ntfy_retry_floor_after: int = 5
    # Consecutive sweeps of idle-capacity-with-waiting-work before D7 wakes.
    # 5 sweeps at a 120s interval is ~10 minutes: longer than any single
    # assembly gap, shorter than a wasted gate run.
    underutil_sweeps: int = 5
    # Not operator-tunable: these are properties of the thing being watched,
    # not of the operator's patience.
    instr_window_min: int = 30
    skip_window_min: int = 60
    gate_grace_min: int = 10


def _int_env(env: dict[str, str], name: str, default: int) -> int:
    """An unparseable or non-positive override falls back to the default and is
    NOT silently honoured as 0 — a zero threshold would fire on everything and
    train the operator to ignore this job."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def thresholds_from_env(env: dict[str, str] | None = None) -> Thresholds:
    e = dict(os.environ if env is None else env)
    return Thresholds(
        lock_stale_min=_int_env(e, "GATE_WATCH_LOCK_STALE_MIN", 10),
        remedy_cooldown_min=_int_env(e, "GATE_WATCH_REMEDY_COOLDOWN_MIN", 30),
        tick_stale_min=_int_env(e, "GATE_WATCH_TICK_STALE_MIN", 5),
        dispatch_stale_min=_int_env(e, "GATE_WATCH_DISPATCH_STALE_MIN", 45),
        skip_storm=_int_env(e, "GATE_WATCH_SKIP_STORM", 30),
        instr_cluster=_int_env(e, "GATE_WATCH_INSTR_CLUSTER", 3),
        verdict_stale_min=_int_env(e, "GATE_WATCH_VERDICT_STALE_MIN", 360),
        push_floor_min=_int_env(e, "GATE_WATCH_PUSH_FLOOR_MIN", 60),
        ntfy_retry_floor_after=_int_env(e, "GATE_WATCH_NTFY_RETRY_FLOOR_AFTER", 5),
        underutil_sweeps=_int_env(e, "GATE_WATCH_UNDERUTIL_SWEEPS", 5),
    )


# ---------------------------------------------------------------------------
# detections
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Detection:
    """One thing wrong, in the shape the wake path needs.

    ``symptom``/``remedy`` are STABLE prose — they and ``detector`` are the whole
    dedup key, so they must never carry a count, an age, a timestamp or a sha.
    ``evidence`` is where all of that goes; it never reaches the content id.
    """

    detector: str
    symptom: str
    remedy: str
    evidence: dict[str, Any] = field(default_factory=dict)
    auto_remediable: bool = False

    def payload(self) -> dict[str, str]:
        """The deterministic dedup payload. Exactly three keys, by contract."""
        return {"symptom": self.symptom, "detector": self.detector, "remedy": self.remedy}

    def title(self) -> str:
        return f"gate-watch {self.detector}: {self.symptom}"[:200]


@dataclass(frozen=True)
class DetectorResult:
    """What one detector saw. ``note`` is the human one-liner for --dry-run and
    the log, and is written even when nothing fired — "D2 ok (tick 41s ago)" is
    the line that tells an operator the watchdog is actually looking."""

    detector: str
    note: str
    detections: tuple[Detection, ...] = ()
    #: Optional structured payload a detector wants surfaced even when it does
    #: NOT fire — D7 carries its utilization row here so `--dry-run` can show
    #: the operator the live capacity reading without a second measurement.
    data: dict[str, Any] | None = None

    @property
    def fired(self) -> bool:
        return bool(self.detections)


def instrument_unreadable(detector: str, source: str, error: str) -> Detection:
    """The universal 'we could not tell' detection.

    ``source`` is a stable identifier (a role name or a file stem), NEVER the
    error text with its varying errno/pid — otherwise every sweep mints a new
    finding for the same broken file.
    """
    return Detection(
        detector=detector,
        symptom=(f"instrument-unreadable: gate-watch could not read {source}, so "
                 f"{detector} cannot say whether the gate pipeline is healthy"),
        remedy=("repair or remove the unreadable input, then confirm the next "
                "gate-watch sweep reports the detector clean"),
        evidence={"error": error},
    )


# ---------------------------------------------------------------------------
# time / log parsing helpers (pure)
# ---------------------------------------------------------------------------

def parse_iso(stamp: str | None) -> float | None:
    """RFC3339-UTC -> epoch seconds, or None. Tolerant of fractional seconds and
    a `+00:00` offset; returns None rather than raising on anything else."""
    if not isinstance(stamp, str):
        return None
    text = stamp.strip()
    if not text:
        return None
    text = text.replace("Z", "").replace("z", "")
    if "+" in text:
        text = text.split("+", 1)[0]
    text = text.split(".", 1)[0]
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def iso(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class LogLine:
    ts: float
    text: str


def parse_log(text: str) -> tuple[list[LogLine], float | None]:
    """Stamp every gate-loop log line with the tick it belongs to.

    THE DIRECTION IS LOAD-BEARING and it is the opposite of what the raw log
    looks like by eye. ``gate_loop.main`` prints the tick JSON FIRST and the
    buffered free-text lines AFTER it::

        print(json.dumps({"at": _iso(_now()), "outcomes": [...]}, indent=2))
        for ln in loop.lines:  print(f"  {ln}")
        for a in loop.alerts:  print(f"  ALERT: {a}")

    so a line belongs to the ``"at"`` block ABOVE it. Reading it the other way
    (this file's first version did) stamps every line with the NEXT tick and so
    reports everything as ~one tick fresher than it is — an error that always
    points the favourable way, toward "recently active".

    Lines that appear BEFORE the first ``"at"`` in the slice are the tail of a
    tick whose header the byte-window truncated away: their time is genuinely
    unknown, so they are DROPPED rather than given an invented timestamp. That
    costs at most one tick out of the window and it is the safe direction for
    both readers — D3 sees less evidence of progress, D4 counts fewer skips.

    Returns (stamped lines, newest tick timestamp or None).
    """
    lines: list[LogLine] = []
    current: float | None = None
    newest_tick: float | None = None
    for raw in text.splitlines():
        match = _AT_RE.search(raw)
        if match:
            ts = parse_iso(match.group(1))
            if ts is not None:
                current = ts
                newest_tick = ts if newest_tick is None else max(newest_tick, ts)
        if current is None:
            continue  # pre-first-tick remnant: unknown time, never guessed
        lines.append(LogLine(current, raw))
    return lines, newest_tick


def _age(now: float, then: float) -> str:
    delta = max(0.0, now - then)
    if delta < 90:
        return f"{delta:.0f}s"
    if delta < 5400:
        return f"{delta / 60:.0f}m"
    return f"{delta / 3600:.1f}h"


# ---------------------------------------------------------------------------
# D1 — orphaned builder-worktree lock
# ---------------------------------------------------------------------------

def detect_builder_lock(
    *,
    lock_exists: bool,
    lock_text: str | None,
    lock_mtime: float | None,
    builder_dir: str,
    probe: str,
    now: float,
    th: Thresholds,
) -> DetectorResult:
    """The 30-minute assembly halt (c).

    Fires only on the shape that was actually seen: a lock whose reason contains
    "initializing", older than ``lock_stale_min``, with nothing holding the
    worktree. ``probe == UNKNOWN`` wakes but never remedies — unlocking a
    worktree a live builder is mid-write in would corrupt the very assembly this
    job exists to protect.
    """
    if not lock_exists:
        return DetectorResult("D1", "ok (no builder lock)")
    if lock_text is None or lock_mtime is None:
        return DetectorResult(
            "D1", "WAKE (builder lock unreadable)",
            (instrument_unreadable("D1", "the builder worktree lock file",
                                   "lock present but its contents/mtime are unreadable"),))
    if "initializing" not in lock_text:
        return DetectorResult("D1", f"ok (lock held, reason not 'initializing': "
                                    f"{lock_text.strip()[:60]!r})")
    age_min = (now - lock_mtime) / 60.0
    if age_min < th.lock_stale_min:
        return DetectorResult("D1", f"ok (initializing lock is fresh, {_age(now, lock_mtime)})")
    if probe == BUSY:
        return DetectorResult("D1", f"ok (lock is {_age(now, lock_mtime)} old but a live "
                                    f"process holds the worktree)")

    evidence = {
        "builder_dir": builder_dir,
        "lock_reason": lock_text.strip()[:200],
        "lock_age_minutes": round(age_min, 1),
        "lsof_probe": probe,
        "threshold_minutes": th.lock_stale_min,
    }
    if probe == UNKNOWN:
        return DetectorResult(
            "D1", "WAKE (stale initializing lock; lsof could not prove it is idle)",
            (Detection(
                detector="D1",
                symptom=("a gate builder-worktree lock reading 'initializing' is older than "
                         "the stale threshold and gate-watch could not determine whether any "
                         "process still holds the worktree; train assembly is halted"),
                remedy=("check by hand whether a builder is alive, then "
                        "`git -C <repo> worktree unlock <builder-dir>` if it is not — "
                        "gate-watch will not unlock a worktree it cannot prove is idle"),
                evidence=evidence,
                auto_remediable=False),))

    return DetectorResult(
        "D1", "WAKE (orphaned builder lock — auto-remedy eligible)",
        (Detection(
            detector="D1",
            symptom=("a gate builder-worktree lock reading 'initializing' is older than the "
                     "stale threshold and no live process holds the worktree; every train "
                     "assembly is refused until it is unlocked"),
            remedy="git worktree unlock the orphaned builder worktree (gate-watch auto-remedy)",
            evidence=evidence,
            auto_remediable=True),))


# ---------------------------------------------------------------------------
# D2 — daemon dead
# ---------------------------------------------------------------------------

def detect_daemon_dead(
    *, log_text: str | None, newest_tick: float | None, now: float, th: Thresholds,
) -> DetectorResult:
    """No tick in ``tick_stale_min``. The loop is supposed to tick every ~60s;
    a log with no parseable tick at all is unreadable, not healthy."""
    if log_text is None:
        return DetectorResult(
            "D2", "WAKE (gate-loop log unreadable)",
            (instrument_unreadable("D2", "the gate-loop log", "log file could not be read"),))
    if newest_tick is None:
        return DetectorResult(
            "D2", "WAKE (no tick timestamp in the gate-loop log)",
            (instrument_unreadable("D2", "any tick timestamp in the gate-loop log",
                                   "no parseable tick timestamp found in the log tail"),))
    age_min = (now - newest_tick) / 60.0
    if age_min < th.tick_stale_min:
        return DetectorResult("D2", f"ok (newest tick {_age(now, newest_tick)} ago)")
    return DetectorResult(
        "D2", f"WAKE (newest tick {_age(now, newest_tick)} ago)",
        (Detection(
            detector="D2",
            symptom=("the gate loop has not written a tick to its log within the tick-stale "
                     "threshold; the daemon is dead or wedged and nothing is being gated"),
            remedy=("check the gate-loop launchd job / process and restart it, then confirm "
                    "a fresh tick timestamp appears in the log"),
            evidence={"newest_tick": iso(newest_tick),
                      "tick_age_minutes": round(age_min, 1),
                      "threshold_minutes": th.tick_stale_min}),))


# ---------------------------------------------------------------------------
# D3 — assembly silence
# ---------------------------------------------------------------------------

def detect_assembly_silence(
    *, lines: list[LogLine], pending_candidates: list[str], now: float, th: Thresholds,
) -> DetectorResult:
    """The 11.6-hour starvation (a), stated as an OUTCOME rather than a symptom:
    work is waiting and nothing has been dispatched or landed.

    Deliberately not keyed on the "no gate-eligible candidates" line — that line
    is *correct* when the queue is genuinely empty. The stall is only a stall
    when a non-terminal candidate is sitting there while the loop dispatches
    nothing, which is why ``pending_candidates`` is required to fire.
    """
    if not pending_candidates:
        return DetectorResult("D3", "ok (no non-terminal candidates waiting)")
    window_start = now - th.dispatch_stale_min * 60
    last_seen: float | None = None
    for line in lines:
        if _PROGRESS_RE.search(line.text):
            last_seen = line.ts if last_seen is None else max(last_seen, line.ts)
    if last_seen is not None and last_seen >= window_start:
        return DetectorResult("D3", f"ok (last dispatch/land {_age(now, last_seen)} ago, "
                                    f"{len(pending_candidates)} candidate(s) pending)")
    seen = "never" if last_seen is None else f"{_age(now, last_seen)} ago"
    return DetectorResult(
        "D3", f"WAKE (last dispatch/land {seen}, {len(pending_candidates)} candidate(s) pending)",
        (Detection(
            detector="D3",
            symptom=("non-terminal candidates are waiting in candidates/ but the gate loop has "
                     "neither dispatched a gate nor landed a train within the dispatch-silence "
                     "threshold; the pipeline is running and starving at the same time"),
            remedy=("read the tick's per-candidate skip lines to find why every candidate is "
                    "ineligible, and fix the candidates or the eligibility rule"),
            evidence={"pending_count": len(pending_candidates),
                      "pending_sample": sorted(pending_candidates)[:5],
                      "last_dispatch_or_land": None if last_seen is None else iso(last_seen),
                      "threshold_minutes": th.dispatch_stale_min}),))


# ---------------------------------------------------------------------------
# D4 — skip storm
# ---------------------------------------------------------------------------

def detect_skip_storm(
    *, lines: list[LogLine], now: float, th: Thresholds,
) -> DetectorResult:
    """One candidate skipped over and over is the *cause* D3 only sees the shape of.

    Split by skip CLASS, because the two classes mean opposite things:

      **D4 (fault)** — a candidate the loop can never land as it stands (missing
      head_sha, moved branch, unreadable diff). Counted per candidate over the
      last hour: >= ``skip_storm`` skips is a storm, because no amount of time
      fixes it and every tick spent on it is wasted.

      **D4b (awaiting-verdict)** — a risky diff queued behind a cross-lineage
      review seat. That is the system working, and it routinely runs past an
      hour, so counting it would fire on healthy work. It is judged by ELAPSED
      SPAN instead: still being skipped now, and first skipped more than
      ``verdict_stale_min`` (6h) ago, means the review seat itself is stuck.
    """
    window_start = now - th.skip_window_min * 60
    # Keyed by (candidate, CLASS), never by candidate alone. A single candidate
    # can legitimately produce BOTH kinds of skip as its state changes, and a
    # per-candidate class field would be last-write-wins: one late
    # awaiting-verdict line would reclassify a candidate that had been storming
    # with a fault all hour, and the storm would vanish from the report.
    counts: dict[tuple[str, str], int] = {}
    reasons: dict[tuple[str, str], str] = {}
    first_seen: dict[tuple[str, str], float] = {}
    last_seen: dict[tuple[str, str], float] = {}
    for line in lines:
        match = _SKIP_RE.search(line.text)
        if not match:
            continue
        ident, reason = match.group(1), match.group(2)
        slot = (ident, classify_skip_reason(reason))
        # Span tracking covers the WHOLE parsed slice, not just the hour window
        # — that is what makes the 6h D4b judgement possible at all. min/max
        # rather than first/last ENCOUNTERED: a log is normally chronological,
        # but a span that silently depends on that would collapse to zero the
        # first time it is not, and a zero span never fires.
        first_seen[slot] = min(first_seen.get(slot, line.ts), line.ts)
        last_seen[slot] = max(last_seen.get(slot, line.ts), line.ts)
        reasons[slot] = line.text.strip()[:200]
        if line.ts >= window_start:
            counts[slot] = counts.get(slot, 0) + 1

    faults = sorted((i for (i, cls), n in counts.items()
                     if cls == SKIP_FAULT and n >= th.skip_storm),
                    key=lambda i: (-counts[(i, SKIP_FAULT)], i))
    # "Still live" = skipped within the ordinary storm window; a candidate that
    # stopped being skipped has moved on and must not fire from log history.
    waiting = sorted((i for (i, cls) in first_seen
                      if cls == SKIP_AWAITING_VERDICT
                      and last_seen[(i, cls)] >= window_start
                      and (now - first_seen[(i, cls)]) >= th.verdict_stale_min * 60),
                     key=lambda i: (first_seen[(i, SKIP_AWAITING_VERDICT)], i))

    detections = tuple([
        Detection(
            detector="D4",
            # The short id is STABLE per candidate, so one storming candidate
            # files exactly one finding no matter how long it storms.
            symptom=(f"gate-loop candidate {ident} is being skipped every tick for a fault "
                     f"it cannot recover from and can never land; the skip is only visible "
                     f"in a log nobody drains"),
            remedy=("repair or withdraw the candidate named in the skip reason — an "
                    "unfixable candidate must be rejected, not skipped forever"),
            evidence={"candidate": ident,
                      "skips_in_window": counts[(ident, SKIP_FAULT)],
                      "window_minutes": th.skip_window_min,
                      "threshold": th.skip_storm,
                      "last_skip_line": reasons[(ident, SKIP_FAULT)]})
        for ident in faults
    ] + [
        Detection(
            detector="D4b",
            symptom=(f"gate-loop candidate {ident} has been waiting on its cross-lineage "
                     f"build verdict for longer than the review-seat threshold; the wait is "
                     f"legitimate but the seat is not returning"),
            remedy=("find out why the review seat never returned a verdict for this "
                    "candidate and dispatch or reassign it — this is a stuck reviewer, "
                    "NOT a broken candidate"),
            evidence={"candidate": ident,
                      "waiting_hours": round(
                          (now - first_seen[(ident, SKIP_AWAITING_VERDICT)]) / 3600.0, 1),
                      "threshold_hours": round(th.verdict_stale_min / 60.0, 1),
                      "last_skip_line": reasons[(ident, SKIP_AWAITING_VERDICT)]})
        for ident in waiting
    ])

    live_waiting = [i for (i, cls), seen in last_seen.items()
                    if cls == SKIP_AWAITING_VERDICT and seen >= window_start]
    fault_counts = [n for (_i, cls), n in counts.items() if cls == SKIP_FAULT]
    context = (f"{len(live_waiting)} awaiting-verdict candidate(s) "
               f"(threshold {th.verdict_stale_min / 60.0:.0f}h), busiest fault-class "
               f"candidate skipped {max(fault_counts, default=0)}x in the last "
               f"{th.skip_window_min}m (threshold {th.skip_storm})")
    if not detections:
        return DetectorResult("D4", f"ok ({context})")
    return DetectorResult(
        "D4", f"WAKE ({len(faults)} fault-class storm(s), {len(waiting)} stuck "
              f"verdict wait(s)) — {context}", detections)


# ---------------------------------------------------------------------------
# D5 — instrument-error cluster
# ---------------------------------------------------------------------------

def detect_instrument_cluster(
    *, events: list[dict[str, Any]] | None, now: float, th: Thresholds,
) -> DetectorResult:
    """The twin false-rejection clusters (b). One instrument error is weather;
    three in half an hour is a broken instrument, and the gate's verdicts in
    that window mean nothing."""
    if events is None:
        return DetectorResult(
            "D5", "WAKE (ledger unreadable)",
            (instrument_unreadable("D5", "the loopqueue ledger",
                                   "ledger.jsonl could not be read"),))
    window_start = now - th.instr_window_min * 60
    hits: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "instrument_error":
            continue
        ts = parse_iso(event.get("ts"))
        if ts is None or ts < window_start:
            continue
        hits.append(event)
    if len(hits) < th.instr_cluster:
        return DetectorResult("D5", f"ok ({len(hits)} instrument_error(s) in the last "
                                    f"{th.instr_window_min}m, threshold {th.instr_cluster})")
    details = [str(h.get("detail"))[:120] for h in hits[-5:]]
    return DetectorResult(
        "D5", f"WAKE ({len(hits)} instrument_error(s) in the last {th.instr_window_min}m)",
        (Detection(
            detector="D5",
            symptom=("instrument_error events are clustering in the loopqueue ledger; gate "
                     "verdicts produced in this window are instrument noise, not judgements "
                     "about the candidates"),
            remedy=("diagnose the gate host/twin (connectivity, rc-255, workspace pin) before "
                    "re-gating anything — re-running an unchanged input against a broken "
                    "instrument buys the same answer twice"),
            evidence={"count": len(hits),
                      "window_minutes": th.instr_window_min,
                      "threshold": th.instr_cluster,
                      "recent_details": details}),))


# ---------------------------------------------------------------------------
# D6 — stuck gate
# ---------------------------------------------------------------------------

def detect_stuck_gates(
    *, gate_states: list[tuple[str, dict[str, Any] | None]], now: float, th: Thresholds,
) -> DetectorResult:
    """A gate-state file still saying "running" long past its own deadline.

    A ``running`` state with no numeric deadline is treated as unreadable, not as
    healthy: "running forever, no deadline" is precisely the invisible stall, and
    a missing field must never read as "nothing wrong".
    """
    grace = th.gate_grace_min * 60
    detections: list[Detection] = []
    stuck: list[str] = []
    running = 0
    for name, state in gate_states:
        if state is None:
            detections.append(instrument_unreadable("D6", f"gate-state file {name}",
                                                    "file is missing, torn, or not a JSON object"))
            continue
        if state.get("state") != "running":
            continue
        running += 1
        deadline = state.get("deadline")
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            detections.append(instrument_unreadable(
                "D6", f"the deadline of running gate-state file {name}",
                "state is 'running' but the deadline is missing or not a number"))
            continue
        if now <= deadline + grace:
            continue
        stuck.append(name)
        detections.append(Detection(
            detector="D6",
            symptom=(f"gate-state file {name} still reads 'running' well past its own "
                     f"deadline; the gate never reported and the train behind it cannot "
                     f"progress"),
            remedy=("check the gate host for the run, then let the loop re-gate or clear the "
                    "state file — a gate that never reported is an instrument fact, never a pass"),
            evidence={"gate": name,
                      "deadline": iso(float(deadline)),
                      "overdue_minutes": round((now - deadline) / 60.0, 1),
                      "grace_minutes": th.gate_grace_min}))
    if not detections:
        return DetectorResult("D6", f"ok ({running} gate(s) running, none past deadline)")
    note = (f"WAKE ({len(stuck)} stuck gate(s), "
            f"{len(detections) - len(stuck)} unreadable gate-state file(s))")
    return DetectorResult("D6", note, tuple(detections))


# ---------------------------------------------------------------------------
# D7 — gate capacity: are we maxing the boxes out, and not over-booking them?
# ---------------------------------------------------------------------------
#
# Operator ask (the operator, 2026-08-10): "make sure we are always maxing out gate
# capacity (yet not going too over)." Both halves are real and they fail in
# opposite directions:
#
#   UNDER — idle slots while work waits. Measured on this estate the same day:
#           728 "gate slots full" deferrals across 12 trains while MW0002 sat at
#           zero agents, because the concurrency cap was hard-coded 2 against a
#           two-twin pool. A wasted paid box is invisible unless something counts.
#   OVER  — two gates on one machine. The second is then graded under contention
#           it did not cause, which turns a capacity fault into a false
#           candidate-defect verdict. That is worse than idling.
#
# The counts here are also written to a JSONL time series every sweep, because
# "are we maxed out?" is an empirical question and nobody should have to answer
# it from anecdote.

def is_running_gate(state: dict[str, Any], *, now: float) -> bool:
    """Whether the LOOP would treat this gate as occupying a slot.

    Mirrors ``gate_loop._running_gate_count`` deliberately, including its
    treatment of a MISSING deadline as still-running: the question D7 asks is
    "does the scheduler think this slot is taken", not "is this gate healthy".
    A gate with no deadline is D6's problem, and counting it as free here would
    manufacture an idle slot that the loop does not believe in.
    """
    if state.get("state") != "running":
        return False
    deadline = state.get("deadline")
    # `isinstance(x, (int, float))` admits True/False, and gate_loop's check is
    # exactly this. Excluding bool here would count a `"deadline": true` gate as
    # running while the loop counts it expired — parity beats tidiness in a
    # mirror, including on the pathological edges.
    if isinstance(deadline, (int, float)):
        return now <= deadline
    return True


def gate_host_key(state: dict[str, Any], *, twin_host: str) -> str:
    """Which physical box a running gate occupies.

    A remote gate occupies its twin (``gate_loop`` defaults a missing twin to
    TWIN_HOST, and so does this); every other mode occupies the local Mac.
    """
    if state.get("mode") == "remote":
        return str(state.get("twin") or twin_host)
    return "local"


def compute_utilization(*, running_gates: int, max_slots: int | None,
                        eligible_candidates: int, candidates_in_flight: int,
                        now: float, degraded: str | None = None) -> dict[str, Any]:
    """One row of the capacity time series.

    ``max_slots`` may be None when the pipeline's own constant could not be
    read. The row is still written, with the fields that ARE known and a
    ``degraded`` reason — a gap in the series would be indistinguishable from a
    sweep that did not run, and this file exists precisely to be counted later.
    """
    idle = None if max_slots is None else max(0, max_slots - running_gates)
    pct = None
    if max_slots:
        pct = round(100.0 * running_gates / max_slots, 1)
    row: dict[str, Any] = {
        "ts": iso(now),
        "running_gates": running_gates,
        "max_slots": max_slots,
        "eligible_candidates": eligible_candidates,
        "candidates_in_flight": candidates_in_flight,
        "idle_slots": idle,
        "utilization_pct": pct,
    }
    if degraded:
        row["degraded"] = degraded
    return row


def underutilised(row: dict[str, Any]) -> bool:
    """Idle capacity AND real surplus work. BOTH are required.

    Idle slots alone are the normal resting state of a drained queue, and a
    backlog alone is what a busy pipeline looks like. Only the two together mean
    capacity is being wasted, which is why either one on its own must stay
    silent — a detector that fires on an empty queue is one the operator learns
    to ignore before it ever catches the real thing.
    """
    idle = row.get("idle_slots")
    if not isinstance(idle, int) or idle < 1:
        return False
    surplus = row["eligible_candidates"] - row["candidates_in_flight"]
    return surplus >= 2


def next_streak(previous: int, condition: bool) -> int:
    """Consecutive-sweep counter. Resets to zero the moment the condition
    lapses, so a detector that needs N sweeps needs N CONSECUTIVE sweeps —
    an accumulating counter would eventually fire on nothing but noise."""
    if not condition:
        return 0
    if not isinstance(previous, int) or isinstance(previous, bool) or previous < 0:
        previous = 0
    return previous + 1


def real_paths(repo: Path, base: str, ref: str, *,
               runner: Callable[..., Any] | None = None) -> set[str] | None:
    """Files ``base..ref`` actually changes, from the REAL tree diff.

    A byte-for-byte mirror of ``train_assembler.real_paths``, including its
    two-dot form and its None-on-failure contract: None means the diff could not
    be read, an INSTRUMENT fact that must never be mistaken for "touches
    nothing". The declared ``paths`` field is NOT a substitute — the pipeline's
    own doctrine distrusts it ("an understated paths claim cannot downgrade its
    own review tier; read the real tree diff here"), and the conflict graph that
    actually decides what serialises runs on this, not on the envelope.
    """
    run = runner or subprocess.run
    try:
        proc = run(["git", "-C", str(repo), "diff", "--name-only", f"{base}..{ref}"],
                   capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return {ln.strip() for ln in (getattr(proc, "stdout", "") or "").splitlines()
            if ln.strip()}


def classify_underutil_cause(*, surplus_ids: list[str], in_flight_paths: set[str] | None,
                             candidate_paths: dict[str, set[str] | None],
                             awaiting_verdict_ids: set[str]) -> str:
    """WHY the slots are idle.

    Ordered so the benign explanation is offered first: work that shares files
    with an in-flight train CANNOT be gated concurrently — the serialisation is
    the conflict graph doing its job, and reporting it as waste would be a false
    alarm. Only when neither the conflict graph nor a pending verdict explains
    the idleness is it genuinely unaccounted for.

    A None path-set means its diff was unreadable. That is NOT treated as
    "touches nothing" (which would silently manufacture `assembly-unknown` and
    send an operator hunting a scheduler bug that does not exist); the cause is
    suffixed ``(declared-only)`` so the diagnosis carries its own confidence.
    For a watchdog the diagnosis IS the product, and an unqualified wrong
    diagnosis costs more than a qualified uncertain one.
    """
    degraded = in_flight_paths is None or any(
        candidate_paths.get(i) is None for i in surplus_ids)
    held = in_flight_paths or set()
    cause = "assembly-unknown"
    for ident in surplus_ids:
        if held.intersection(candidate_paths.get(ident) or set()):
            cause = "conflict-serialized"
            break
    else:
        if any(_short(i) in awaiting_verdict_ids or i in awaiting_verdict_ids
               for i in surplus_ids):
            cause = "awaiting-verdict"
    return f"{cause}(declared-only)" if degraded else cause


def _short(ident: str) -> str:
    """The 12-hex form the gate loop prints in its `skip <id>:` lines."""
    return ident.split(":", 1)[-1][:12]


def underutil_remedy(cause: str) -> str:
    """The remedy text for a cause, with the ``(declared-only)`` qualifier
    handled once. A degraded diagnosis says so in the remedy too — an operator
    acting on it needs to know the conflict answer came from envelope claims
    rather than the tree."""
    base = cause.replace("(declared-only)", "")
    text = _UNDERUTIL_REMEDY.get(base, _UNDERUTIL_REMEDY["assembly-unknown"])
    if base != cause:
        text += (" — NOTE: at least one real tree diff was unreadable, so this "
                 "cause was derived from DECLARED envelope paths, which the "
                 "pipeline deliberately distrusts; confirm before acting")
    return text


_UNDERUTIL_REMEDY = {
    "conflict-serialized": (
        "no action if this is brief: the waiting candidates touch files an "
        "in-flight train already holds, so the conflict graph is serialising "
        "them on purpose. If it persists, split the overlapping lanes."),
    "awaiting-verdict": (
        "the idle slots are waiting on cross-lineage review seats, not on gate "
        "capacity — dispatch or reassign the verdicts (see D4b)"),
    "assembly-unknown": (
        "read the tick's assembly lines: candidates are loadable and slots are "
        "free, so something between loading and dispatch is refusing to build "
        "a train"),
}


def detect_underutilisation(*, row: dict[str, Any], streak: int, cause: str,
                            th: Thresholds) -> DetectorResult:
    """Capacity being wasted, for long enough that it is not a normal gap."""
    if row.get("idle_slots") is None:
        return DetectorResult(
            "D7", "WAKE (slot ceiling unknown)",
            (instrument_unreadable("D7", "the pipeline's gate-slot ceiling",
                                   str(row.get("degraded") or "max_slots unavailable")),))
    surplus = row["eligible_candidates"] - row["candidates_in_flight"]
    if streak < th.underutil_sweeps:
        return DetectorResult(
            "D7", f"ok ({row['running_gates']}/{row['max_slots']} slots busy, "
                  f"{row['utilization_pct']}%, {surplus} candidate(s) waiting, "
                  f"under-util streak {streak}/{th.underutil_sweeps})")
    return DetectorResult(
        "D7",
        f"WAKE (idle capacity for {streak} consecutive sweeps: "
        f"{row['running_gates']}/{row['max_slots']} busy, {surplus} waiting, "
        f"cause={cause})",
        (Detection(
            detector="D7",
            # Cause is IN the symptom (it is a stable class, not a measurement)
            # so a conflict-serialised stretch and an unexplained one are
            # different findings rather than one that flip-flops.
            symptom=(f"gate capacity is idle while candidates wait ({cause}): the gate "
                     f"pool has free slots and loadable candidates are not being "
                     f"dispatched into them"),
            remedy=underutil_remedy(cause),
            evidence={**row, "streak": streak, "cause": cause,
                      "threshold_sweeps": th.underutil_sweeps}),))


def detect_overcommit(*, running_states: list[tuple[str, dict[str, Any]]],
                      max_slots: int | None, twin_host: str,
                      now: float) -> DetectorResult:
    """Two gates on one box, or more gates than slots. Never auto-remedied.

    This is a FAVOURABLE-ABSENCE PIN: the scheduler is built so neither can
    happen, so nothing downstream looks for them, and if one ever does happen it
    is silent — the second gate simply runs slower and reports a candidate
    defect that is really contention. A cheap assertion against an
    impossible-by-design invariant is the only thing that turns that into news.
    """
    del now
    detections: list[Detection] = []
    by_box: dict[str, list[str]] = {}
    for name, state in running_states:
        by_box.setdefault(gate_host_key(state, twin_host=twin_host), []).append(name)

    for box, names in sorted(by_box.items()):
        if len(names) < 2:
            continue
        detections.append(Detection(
            detector="D7-over",
            symptom=(f"two or more gate runs are executing on box {box} at once; the "
                     f"scheduler is designed so this cannot happen, and the second run "
                     f"is being graded under contention it did not cause"),
            remedy=("stop the duplicate run and find out how two trains claimed one "
                    "box — a verdict produced under contention is an instrument "
                    "artefact, not a judgement about the candidate"),
            evidence={"box": box, "gates": sorted(names)}))

    if isinstance(max_slots, int) and len(running_states) > max_slots:
        detections.append(Detection(
            detector="D7-over",
            symptom=("more gate runs are in flight than the pipeline's own concurrency "
                     "ceiling allows; the fleet is over-booked and every running gate "
                     "is competing for cores with the others"),
            remedy=("reconcile the running gate-state files with the ceiling "
                    "(MAX_CONCURRENT_GATES = 1 + active twins) and stop the excess"),
            evidence={"running_gates": len(running_states), "max_slots": max_slots,
                      "gates": sorted(n for n, _ in running_states)}))

    if not detections:
        return DetectorResult("D7-over", f"ok ({len(running_states)} running, "
                                         f"{len(by_box)} box(es), no double-booking)")
    return DetectorResult("D7-over", f"WAKE ({len(detections)} over-capacity condition(s))",
                          tuple(detections))


# ---------------------------------------------------------------------------
# remedy (D1 only) + cooldown
# ---------------------------------------------------------------------------

def remedy_allowed(state: dict[str, Any], key: str, *, now: float, cooldown_min: int) -> bool:
    """Whether the cooldown has expired for this remedy key.

    A malformed/absent record reads as ALLOWED: the cooldown bounds repetition,
    it is not a safety interlock, and a corrupt state file must not permanently
    disable the one repair this job can make."""
    record = state.get(key)
    if not isinstance(record, dict):
        return True
    last = record.get("at")
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        return True
    return (now - float(last)) >= cooldown_min * 60


def fired_key(detections: list[Detection]) -> str:
    """The identity of an ONGOING incident: the sorted set of fired detectors.

    Deliberately coarse. Keying on individual findings would push again every
    time a stall grew a new symptom; keying on the detector set means the phone
    speaks when the SHAPE of the incident changes and stays quiet while it
    merely persists.
    """
    return ",".join(sorted({d.detector for d in detections}))


def should_push(state: dict[str, Any], key: str, *, now: float, floor_min: int,
                retry_floor_after: int = 5) -> bool:
    """Whether this sweep should ATTEMPT a phone push.

    Three independent brakes, all required to pass:

      1. **dead-channel backoff** — after ``retry_floor_after`` consecutive
         failed DELIVERIES, attempts drop to one per ``floor_min``. Retrying
         every sweep is correct for a transient outage and wrong for a dead
         channel, because each failed attempt writes its own note to ALERTS.md;
         unbounded retry would trade a silent failure for a noisy one. Any
         successful delivery resets the counter, so a channel that comes back
         is immediately back to full responsiveness.
      2. **the floor** — never more than one delivered push per ``floor_min``,
         even when the incident changes shape. A flapping detector must not be
         able to buy unlimited notifications.
      3. **novelty** — the same detector set as the last PUSHED one stays
         silent forever. A 12-hour stall is one notification, not 360.

    Brakes 2 and 3 read the last DELIVERY, never the last attempt: a fail-soft
    push that advanced its own throttle would mute the incident it failed to
    report. Comparing against the last *pushed* set (not the last *seen* set) is
    what makes a change suppressed by the floor still push once the floor
    expires.
    """
    attempt = state.get("ntfy_attempt")
    if isinstance(attempt, dict):
        failures = attempt.get("consecutive_failures")
        last_try = attempt.get("at")
        if (isinstance(failures, int) and not isinstance(failures, bool)
                and failures >= retry_floor_after
                and isinstance(last_try, (int, float)) and not isinstance(last_try, bool)
                and (now - float(last_try)) < floor_min * 60):
            return False

    record = state.get("ntfy")
    if not isinstance(record, dict):
        return True
    last_at = record.get("at")
    if not isinstance(last_at, (int, float)) or isinstance(last_at, bool):
        return True
    if (now - float(last_at)) < floor_min * 60:
        return False
    return record.get("key") != key


def record_push(state: dict[str, Any], key: str, *, now: float) -> dict[str, Any]:
    """Record a CONFIRMED delivery. Only this moves the throttle forward."""
    updated = dict(state)
    updated["ntfy"] = {"at": now, "at_iso": iso(now), "key": key}
    return updated


def record_push_attempt(state: dict[str, Any], key: str, *, now: float,
                        delivered: bool, note: str) -> dict[str, Any]:
    """Record the ATTEMPT, separately from the delivery.

    Kept in its own slot so `ntfy.at` never means "we tried" — an operator
    reading this file can tell a channel that is delivering from one that is
    being retried every sweep, and `consecutive_failures` names a dead channel
    without anyone having to correlate log lines.
    """
    previous = state.get("ntfy_attempt")
    failures = previous.get("consecutive_failures", 0) if isinstance(previous, dict) else 0
    if not isinstance(failures, int) or isinstance(failures, bool):
        failures = 0
    updated = dict(state)
    updated["ntfy_attempt"] = {
        "at": now, "at_iso": iso(now), "key": key, "delivered": delivered,
        "result": note[:200],
        "consecutive_failures": 0 if delivered else failures + 1,
    }
    return updated


def clear_push_key(state: dict[str, Any]) -> dict[str, Any] | None:
    """Forget the last pushed set once a sweep comes back CLEAN, so a stall that
    recurs after a quiet period is a NEW incident and pushes again. Returns None
    when there is nothing to clear, so a clean sweep does not rewrite the state
    file every 120 seconds."""
    record = state.get("ntfy")
    if not isinstance(record, dict) or record.get("key") is None:
        return None
    updated = dict(state)
    updated["ntfy"] = {**record, "key": None}
    return updated


def record_remedy(state: dict[str, Any], key: str, *, now: float, result: str) -> dict[str, Any]:
    record = state.get(key)
    count = record.get("count", 0) if isinstance(record, dict) else 0
    if not isinstance(count, int) or isinstance(count, bool):
        count = 0
    updated = dict(state)
    updated[key] = {"at": now, "at_iso": iso(now), "count": count + 1, "result": result}
    return updated


def probe_dir_in_use(directory: Path, *, runner: Callable[..., Any] | None = None) -> str:
    """BUSY / IDLE / UNKNOWN for ``lsof +D <dir>``.

    ``lsof`` returns 1 both for "found nothing" and for "something went wrong",
    so the two are separated by stderr. macOS emits `lsof: WARNING:` lines
    constantly (unstattable mounts); treating those as errors would pin this
    probe at UNKNOWN forever and disable the remedy, so they are benign. ANY
    other stderr, any other return code, a missing binary or a timeout is
    UNKNOWN — which wakes a human and blocks the remedy.
    """
    run = runner or subprocess.run
    if not directory.exists():
        # Nothing can hold a directory that is not there. This is the classic
        # orphan: the worktree was removed and its admin lock outlived it.
        return IDLE
    env = dict(os.environ, LC_ALL="C")
    try:
        proc = run(["lsof", "+D", str(directory)], capture_output=True, text=True,
                   check=False, timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    rc = getattr(proc, "returncode", None)
    stdout = (getattr(proc, "stdout", "") or "").strip()
    stderr_lines = [ln for ln in (getattr(proc, "stderr", "") or "").splitlines()
                    if ln.strip() and not ln.startswith("lsof: WARNING:")]
    if rc == 0:
        return BUSY if stdout else IDLE
    if rc == 1 and not stdout and not stderr_lines:
        return IDLE
    return UNKNOWN


def run_unlock(repo: Path, builder_dir: Path,
               *, runner: Callable[..., Any] | None = None) -> tuple[int, str]:
    """The ONLY thing this watchdog changes: `git worktree unlock`.

    Chosen because it is the exact inverse of the fault, it touches no working
    tree, no ref and no index, and if the diagnosis was wrong the worst case is
    a builder that has to take its own lock again.
    """
    run = runner or subprocess.run
    try:
        proc = run(["git", "-C", str(repo), "worktree", "unlock", str(builder_dir)],
                   capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 255, f"{type(exc).__name__}: {exc}"
    out = ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")).strip()
    return int(getattr(proc, "returncode", 255) or 0), out


# ---------------------------------------------------------------------------
# filing (the dedup idiom shared with gate_loop._note_ineligible)
# ---------------------------------------------------------------------------

def _pipeline_dir(repo: Path) -> Path:
    return repo / "pipeline" / "bridge"


def import_pipeline(repo: Path, module: str) -> Any | None:
    """Load a ``pipeline/bridge`` module, or None if it is unavailable.

    Importing rather than re-implementing is deliberate: a second canonicaliser
    would produce different ids for the same payload and silently defeat dedup,
    and a second ntfy sender would bypass the egress guardrail in notify.py.
    This codebase's measured #1 defect class is exactly this — one fix becoming
    2-13 fixes across clone families — so the failure mode chosen here is "the
    leg is unavailable and says so", never "a private copy of it".

    ``pipeline/bridge`` is put on ``sys.path`` (that is how its own modules
    resolve their siblings) but the module is loaded from an explicit FILE under
    a namespaced key, and the path entry is removed again. A bare
    ``__import__("notify")`` would publish generic names like ``notify`` /
    ``integration`` into a long-lived interpreter's module table and shadow
    whatever else claims them.
    """
    import importlib.util

    # Keyed by (REPO, module), not by module name alone. The name-only key made
    # one process serve a second repo's sweep from the first repo's modules —
    # which in practice meant a test fixture with no pipeline/ silently read the
    # real checkout's slot ceiling and looked healthy, so the degraded-ceiling
    # path was never actually exercised end to end (cross-lineage review,
    # 2026-08-10). A cache that ignores half its identity is a cache that lies.
    # (The repo path goes in verbatim — sys.modules keys are arbitrary strings,
    # and a readable key is worth more here than a short one. Not a hash: a
    # hashed key is unstable across processes and unreadable in a traceback.)
    key = f"_gate_watch_pipeline_{module}@{repo}"
    cached = sys.modules.get(key)
    if cached is not None:
        return cached
    directory = str(_pipeline_dir(repo))
    inserted = False
    if directory not in sys.path:
        sys.path.insert(0, directory)
        inserted = True
    try:
        spec = importlib.util.spec_from_file_location(key, Path(directory) / f"{module}.py")
        if spec is None or spec.loader is None:
            return None
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[key] = loaded
        spec.loader.exec_module(loaded)
        return loaded
    except Exception:  # noqa: BLE001 — an unimportable leg degrades, never crashes the sweep
        sys.modules.pop(key, None)
        return None
    finally:
        if inserted:
            try:
                sys.path.remove(directory)
            except ValueError:
                pass


def finding_id(repo: Path, detection: Detection) -> str | None:
    canonical = import_pipeline(repo, "canonical")
    if canonical is None:
        return None
    return str(canonical.content_id(detection.payload()))


def stem_of(ident: str) -> str:
    return ident.replace(":", "_")


def write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    """mkstemp + link, so two sweeps racing cannot half-publish a finding.
    mkstemp is 0600; findings are world-readable by convention, hence the chmod."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def append_ledger(queue: Path, event: dict[str, Any]) -> None:
    """Append one event through the queue's shared locked transport."""
    append_event(queue, event)


def ledger_has_event(queue: Path, ident: str, event: str) -> bool:
    path = queue / "ledger.jsonl"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("id") == ident \
                and parsed.get("event") == event:
            return True
    return False


def file_finding(repo: Path, queue: Path, detection: Detection, *, now: float) -> str:
    """File ONE deduped finding, NEVER raising. Returns a one-line log outcome.

    Every failure is converted to a returned string because this is leg (ii) of
    four in the wake path: an exception here would take the ntfy push and the
    medic down with it, so a filing problem would silence the very alert it is
    supposed to accompany. The filing is the least important leg — a human
    being told is the most important one — and the ordering of failures must
    reflect that.
    """
    try:
        return _file_finding(repo, queue, detection, now=now)
    except Exception as exc:  # noqa: BLE001 — a broken leg (ii) must not cost legs (iii)/(iv)
        return (f"finding NOT filed for {detection.detector} "
                f"({type(exc).__name__}: {exc}); wake continues")


def _file_finding(repo: Path, queue: Path, detection: Detection, *, now: float) -> str:
    """Dedup is by content id over {symptom, detector, remedy}; a stem already
    present in findings/, rejected/ or parked/ suppresses the re-file, and a
    finding on disk whose `found` event is missing gets the event healed rather
    than a duplicate artifact (the artifact write and the ledger append are two
    steps, and a crash between them leaves an invisible finding).
    """
    ident = finding_id(repo, detection)
    if ident is None:
        return f"finding NOT filed for {detection.detector}: pipeline canonical module unavailable"
    stem = stem_of(ident)
    for area in ("rejected", "parked"):
        if (queue / area / f"{stem}.json").exists():
            return f"finding {stem[:19]}… suppressed: a {area}/ disposition already covers it"
    target = queue / "findings" / f"{stem}.json"
    if target.exists():
        if not ledger_has_event(queue, ident, "found"):
            append_ledger(queue, {"ts": iso(now), "role": ROLE, "event": "found", "id": ident,
                                  "actor": ACTOR,
                                  "detail": {"detector": detection.detector, "healed": True}})
            return f"healed missing found event for {stem[:19]}…"
        return f"finding {stem[:19]}… already filed ({detection.detector})"
    try:
        write_json_atomic(target, {
            "contract": "v1.1",
            "id": ident,
            "kind": "finding",
            "title": detection.title(),
            "created_at": iso(now),
            "producer": {"role": ROLE, "actor": ACTOR},
            "payload": detection.payload(),
            "evidence": [{"claim": f"gate-watch {detection.detector} fired on this sweep",
                          "verified_by": "reading",
                          "result": json.dumps(detection.evidence, sort_keys=True)[:1500]}],
        })
    except FileExistsError:
        # A hand-run sweep raced the scheduled one and published this exact id
        # between our exists() check and our link(). Dedup already won — the
        # artifact is there, filed by the other writer, and this is the
        # SUCCESSFUL outcome of a content-addressed store, not a failure. It
        # must not raise: the ntfy and medic legs still have to run.
        return f"finding {stem[:19]}… filed concurrently by another sweep"
    append_ledger(queue, {"ts": iso(now), "role": ROLE, "event": "found", "id": ident,
                          "actor": ACTOR, "detail": {"detector": detection.detector}})
    return f"filed finding {stem[:19]}… ({detection.detector})"


# ---------------------------------------------------------------------------
# wake legs: ntfy, healthcheck, medic
# ---------------------------------------------------------------------------

def push_ntfy(repo: Path, queue: Path, title: str) -> tuple[bool, str]:
    """Terse title-only push. Returns ``(delivered, log line)``.

    ``delivered`` is True ONLY for a confirmed 2xx. The caller records the
    throttle key on delivery and on nothing else, because the failure this
    boolean exists to prevent is silent and permanent: a transient ntfy outage
    at the MOMENT an incident starts would otherwise stamp the key as "already
    pushed" and mute that incident shape for as long as it lasts. A wake path
    whose fail-soft leg reports success to its own throttle is worse than one
    with no throttle.

    An UNSET url is not a delivery either — arming OMNI_NTFY_URL later must
    still announce an incident that began before it was set.

    notify.push_alert carries the operator-approved egress guardrail (ntfy is a
    public relay) and is already fail-soft with a 5s timeout — reusing it is the
    difference between a bounded operational line and leaking paths off the box.
    """
    url = (os.environ.get("OMNI_NTFY_URL") or "").strip()
    if not url:
        return False, "ntfy skipped (OMNI_NTFY_URL unset)"
    notify = import_pipeline(repo, "notify")
    if notify is None:
        return False, "ntfy FAILED: pipeline notify module unavailable"
    try:
        status = notify.push_alert(queue, "gate-watch", title, source="gate-watch", timeout=5)
    except Exception as exc:  # noqa: BLE001 — a wake leg never raises into the sweep
        return False, f"ntfy FAILED ({type(exc).__name__})"
    if status:
        return True, "ntfy pushed"
    return False, "ntfy push did not confirm (noted in ALERTS.md); retries next sweep"


def ping_healthcheck(url: str, *, opener: Callable[..., Any] | None = None) -> str:
    """The DEAD-MAN leg: a healthchecks.io check that rings when this GET stops
    arriving. Everything above detects a broken pipeline; this is the only thing
    that detects a broken watchdog."""
    if not url:
        return "healthcheck skipped (GATE_WATCH_HEALTHCHECK_URL unset)"
    import urllib.request
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(url, timeout=5):
            pass
    except Exception as exc:  # noqa: BLE001 — fail-soft, logged
        return f"healthcheck FAILED ({type(exc).__name__})"
    return "healthcheck pinged"


def spawn_medic(summary: str, *, spawner: Callable[..., Any] | None = None) -> str:
    """The AI-wake hook, DEFAULT OFF.

    Requires BOTH ``GATE_WATCH_MEDIC=1`` and ``GATE_WATCH_MEDIC_CMD`` — two
    independent switches, so neither an inherited env var nor a stray command
    string alone can start spawning agents. Detached and never waited on: a
    watchdog on a 120s StartInterval must not be held open by a medic that takes
    minutes, and launchd would otherwise stack overlapping sweeps.
    """
    if (os.environ.get("GATE_WATCH_MEDIC") or "").strip() != "1":
        return "medic disabled (GATE_WATCH_MEDIC != 1)"
    cmd = (os.environ.get("GATE_WATCH_MEDIC_CMD") or "").strip()
    if not cmd:
        return "medic disabled (GATE_WATCH_MEDIC_CMD unset)"
    spawn = spawner or subprocess.Popen
    try:
        spawn([*cmd.split(), summary], start_new_session=True,
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001 — fail-soft, logged
        return f"medic spawn FAILED ({type(exc).__name__}: {exc})"
    return "medic spawned (detached)"


# ---------------------------------------------------------------------------
# gathering (the only code that touches the world)
# ---------------------------------------------------------------------------

def read_text(path: Path, *, tail_bytes: int | None = None) -> str | None:
    """Text or None. None means UNREADABLE — which every caller turns into a
    wake, never into an empty string that would read as 'nothing there'."""
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def read_ledger(queue: Path) -> list[dict[str, Any]] | None:
    """Parsed ledger events, or None if the file itself cannot be read. A torn or
    non-object line is skipped (the file is appended to concurrently), but a
    whole-file failure is never allowed to look like an empty ledger."""
    text = read_text(queue / "ledger.jsonl")
    if text is None:
        return None
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def terminal_ids(events: list[dict[str, Any]]) -> set[str]:
    return {str(e["id"]) for e in events
            if e.get("event") in TERMINAL_EVENTS and isinstance(e.get("id"), str)}


def gather_candidates(queue: Path, terminal: set[str]) -> tuple[list[str], list[Detection]]:
    """Ids of candidates with no terminal event, plus a wake for each envelope
    that could not be read — an unreadable candidate is not an absent one."""
    pending: list[str] = []
    problems: list[Detection] = []
    cdir = queue / "candidates"
    if not cdir.is_dir():
        return pending, problems
    try:
        entries = sorted(cdir.glob("*.json"))
    except OSError as exc:
        problems.append(instrument_unreadable("D3", "the candidates/ directory", str(exc)))
        return pending, problems
    for entry in entries:
        try:
            parsed = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            problems.append(instrument_unreadable(
                "D3", f"candidate envelope {entry.name}", f"{type(exc).__name__}"))
            continue
        ident = parsed.get("id") if isinstance(parsed, dict) else None
        if not isinstance(ident, str) or not ident:
            ident = entry.stem.replace("_", ":", 1)
        if ident not in terminal:
            pending.append(ident)
    return pending, problems


def slot_ceiling(repo: Path) -> tuple[int | None, str, str | None]:
    """(max_slots, twin_host, failure reason).

    Read from ``gate_loop.MAX_CONCURRENT_GATES`` — the pipeline's OWN constant,
    which is itself derived as ``1 + len(TWIN_SPECS)`` and honours
    ``THREELOOPS_ACTIVE_TWINS``. Recomputing that formula here would create the
    exact drift the pipeline documents at that constant: a cap of 2 against a
    two-twin pool wasted a paid box for 728 deferrals, and a cap of 3 against
    one twin double-books it. A watchdog that judges capacity against its own
    private idea of capacity is measuring nothing.
    """
    module = import_pipeline(repo, "gate_loop")
    if module is None:
        return None, "", "pipeline gate_loop module unavailable"
    ceiling = getattr(module, "MAX_CONCURRENT_GATES", None)
    twin = str(getattr(module, "TWIN_HOST", "") or "")
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
        return None, twin, f"MAX_CONCURRENT_GATES is not a positive int ({ceiling!r})"
    return ceiling, twin, None


def candidate_is_loadable(art: dict[str, Any], *, content_id_fn: Any) -> bool:
    """The gate loader's CHEAP eligibility checks, and only those.

    Mirrors ``gate_loop.load_candidates`` up to the point where it starts
    touching git: kind, id, branch, a 40-hex base_sha, and a full immutable
    head_sha taken from the top level or hoisted from a payload that actually
    hashes to the envelope id.

    This is deliberately an UPPER BOUND on true eligibility — the loader also
    resolves branches and demands a verdict for risky diffs, neither of which is
    cheap. Over-counting here can only make D7 claim there is more waiting work
    than there is, which is why D7 reports a CAUSE: `awaiting-verdict` is
    exactly the gap between this cheap count and the real one, named rather than
    hidden.
    """
    if art.get("kind") != "candidate":
        return False
    ident, branch, base = art.get("id"), art.get("branch"), art.get("base_sha")
    if not ident or not branch:
        return False
    # Byte-parity with load_candidates' cheap stage: `len(base) != 40` and
    # nothing else — no strip, no hex class. Being STRICTER here under-counts a
    # base the loader would accept, and a mirror that disagrees with the thing
    # it mirrors is a measurement of neither.
    if not isinstance(base, str) or len(base) != 40:
        return False
    top = art.get("head_sha")
    top_tip = top.strip() if isinstance(top, str) and re.fullmatch(
        r"[0-9a-fA-F]{40}", top.strip()) else None
    payload = art.get("payload")
    payload_raw = payload.get("head_sha") if isinstance(payload, dict) else None
    payload_tip = payload_raw.strip() if isinstance(payload_raw, str) and re.fullmatch(
        r"[0-9a-fA-F]{40}", payload_raw.strip()) else None
    if top_tip and payload_tip and top_tip.lower() != payload_tip.lower():
        return False                       # ambiguous identity — the loader refuses
    if top_tip:
        return True
    if payload_tip is None:
        return False
    if content_id_fn is None:
        return False       # cannot verify the payload binds the id: do not assume
    try:
        return bool(content_id_fn(payload) == ident)
    except (TypeError, ValueError):
        return False


def gather_capacity(queue: Path, repo: Path, terminal: set[str],
                    *, now: float) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]],
                                            list[str], dict[str, tuple[str, str]], str]:
    """Everything D7 needs: the utilization row, the running gates, the surplus
    candidate ids, each candidate's (base, branch) REFS, and the twin host name.

    Refs rather than declared paths: the caller resolves the real tree diff
    lazily, and only for the ids that would end up in a finding.
    """
    max_slots, twin_host, why = slot_ceiling(repo)

    running: list[tuple[str, dict[str, Any]]] = []
    unreadable = 0
    for name, state in gather_gate_states(queue):
        if state is None:
            unreadable += 1
            continue
        if is_running_gate(state, now=now):
            running.append((name, state))

    in_flight: set[str] = set()
    for _name, state in running:
        members = state.get("members")
        if isinstance(members, list):
            in_flight.update(str(m) for m in members if isinstance(m, str))

    canonical = import_pipeline(repo, "canonical")
    content_id_fn = getattr(canonical, "content_id", None) if canonical else None
    eligible: list[str] = []
    refs: dict[str, tuple[str, str]] = {}
    cdir = queue / "candidates"
    if cdir.is_dir():
        for entry in sorted(cdir.glob("*.json")):
            try:
                art = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue          # D3 already wakes on an unreadable envelope
            if not isinstance(art, dict):
                continue
            ident = art.get("id")
            if not isinstance(ident, str) or ident in terminal:
                continue
            if not candidate_is_loadable(art, content_id_fn=content_id_fn):
                continue
            eligible.append(ident)
            refs[ident] = (str(art.get("base_sha") or ""), str(art.get("branch") or ""))

    degraded = why
    if unreadable and not degraded:
        degraded = f"{unreadable} unreadable gate-state file(s)"
    row = compute_utilization(running_gates=len(running), max_slots=max_slots,
                              eligible_candidates=len(eligible),
                              candidates_in_flight=len(in_flight),
                              now=now, degraded=degraded)
    surplus = [i for i in eligible if i not in in_flight]
    return row, running, surplus, refs, twin_host


#: Rows kept in the utilization series. At one sweep per 120s that is ~720
#: rows/day, so 20,000 rows is ~28 days of history — long enough to answer "were
#: we maxed out last month", short enough that the file never becomes the thing
#: someone has to clean up. Unbounded, it would reach ~50 MB/year, which is the
#: same failure this file's own ntfy floor exists to prevent.
UTILIZATION_MAX_ROWS = 20_000


def prune_utilization(path: Path, *, max_rows: int = UTILIZATION_MAX_ROWS) -> bool:
    """Keep the newest ``max_rows`` rows. Returns whether it rewrote anything.

    Rewrites via a temp file and one atomic replace, so a reader never sees a
    half-truncated series, and only runs when the file is genuinely over the cap
    (checked cheaply by line count on an already-read file).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= max_rows:
        return False
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines[-max_rows:]) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def append_utilization(path: Path, row: dict[str, Any],
                       *, max_rows: int = UTILIZATION_MAX_ROWS) -> None:
    """One O_APPEND JSONL line per sweep, with the series bounded.

    Best-effort like the log: a lost row must never cost a wake. The prune is
    gated on a cheap ``stat`` so the common sweep does not read the file at all.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(row, separators=(",", ":")) + "\n").encode()
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        return
    try:
        # ~120 bytes/row, so this only starts reading the file once it is
        # plausibly over the cap — never on an ordinary sweep.
        if path.stat().st_size > max_rows * 120:
            prune_utilization(path, max_rows=max_rows)
    except OSError:
        pass


def gather_gate_states(queue: Path) -> list[tuple[str, dict[str, Any] | None]]:
    out: list[tuple[str, dict[str, Any] | None]] = []
    gdir = queue / "state" / "gates"
    if not gdir.is_dir():
        return out
    for entry in sorted(gdir.glob("*.json")):
        try:
            parsed = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            out.append((entry.name, None))
            continue
        out.append((entry.name, parsed if isinstance(parsed, dict) else None))
    return out


def builder_lock_path(repo: Path, builder_dir: Path) -> Path:
    """`.git/worktrees/<name>/locked` — resolved through the worktree's own
    `.git` pointer when it exists, so a relocated admin dir is still found, and
    falling back to the conventional location when the worktree dir is gone
    (which is exactly the orphan case)."""
    dotgit = builder_dir / ".git"
    try:
        if dotgit.is_file():
            text = dotgit.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir: "):
                admin = Path(text.removeprefix("gitdir: "))
                if not admin.is_absolute():
                    admin = (builder_dir / admin).resolve()
                return admin / "locked"
    except OSError:
        pass
    return repo / ".git" / "worktrees" / builder_dir.name / "locked"


def append_log(log_path: Path, message: str, *, now: float) -> None:
    """The always-on leg. Best-effort: if even this fails the sweep continues —
    losing the log line must never cost the ntfy/medic wake."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{iso(now)} gate-watch: {message}\n")
    except OSError:
        pass


def read_state(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------

def run_sweep(
    *, repo: Path, queue: Path, log_path: Path, gate_log: Path, builder_dir: Path,
    state_path: Path, util_path: Path, now: float, th: Thresholds, dry_run: bool,
) -> list[DetectorResult]:
    """Gather the world, run the six pure detectors, return their results."""
    results: list[DetectorResult] = []

    # D1 -------------------------------------------------------------------
    lock_path = builder_lock_path(repo, builder_dir)
    lock_exists = lock_path.exists() or lock_path.is_symlink()
    lock_text: str | None = None
    lock_mtime: float | None = None
    probe = UNKNOWN
    if lock_exists:
        lock_text = read_text(lock_path)
        try:
            lock_mtime = lock_path.stat().st_mtime
        except OSError:
            lock_mtime = None
        if lock_text is not None and "initializing" in lock_text:
            probe = probe_dir_in_use(builder_dir)
    results.append(detect_builder_lock(
        lock_exists=lock_exists, lock_text=lock_text, lock_mtime=lock_mtime,
        builder_dir=str(builder_dir), probe=probe, now=now, th=th))

    # D2 / D3 / D4 ---------------------------------------------------------
    # 4 MiB of tail comfortably covers the one-hour skip window and the
    # dispatch window on the busiest observed day, without reading a log that
    # grows without bound.
    log_text = read_text(gate_log, tail_bytes=4 * 1024 * 1024)
    lines, newest_tick = parse_log(log_text or "")
    results.append(detect_daemon_dead(log_text=log_text, newest_tick=newest_tick,
                                      now=now, th=th))

    events = read_ledger(queue)
    terminal = terminal_ids(events) if events is not None else set()
    pending, candidate_problems = gather_candidates(queue, terminal)
    if events is None:
        # Without the ledger every candidate looks non-terminal, so D3's input is
        # a guess. Say so instead of firing on a fabricated pending set.
        results.append(DetectorResult(
            "D3", "WAKE (ledger unreadable — terminal set unknown)",
            (instrument_unreadable("D3", "the loopqueue ledger",
                                   "ledger.jsonl could not be read"),)))
    else:
        d3 = detect_assembly_silence(lines=lines, pending_candidates=pending, now=now, th=th)
        if candidate_problems:
            d3 = DetectorResult(d3.detector,
                                f"{d3.note}; {len(candidate_problems)} unreadable envelope(s)",
                                (*d3.detections, *candidate_problems))
        results.append(d3)

    if log_text is None:
        results.append(DetectorResult(
            "D4", "WAKE (gate-loop log unreadable)",
            (instrument_unreadable("D4", "the gate-loop log", "log file could not be read"),)))
    else:
        results.append(detect_skip_storm(lines=lines, now=now, th=th))

    # D5 / D6 --------------------------------------------------------------
    results.append(detect_instrument_cluster(events=events, now=now, th=th))
    results.append(detect_stuck_gates(gate_states=gather_gate_states(queue), now=now, th=th))

    # D7 — capacity ---------------------------------------------------------
    row, running, surplus, cand_refs, twin_host = gather_capacity(
        queue, repo, terminal, now=now)
    awaiting = {ident for (ident, cls) in
                {(m.group(1), classify_skip_reason(m.group(2)))
                 for m in (_SKIP_RE.search(ln.text) for ln in lines) if m}
                if cls == SKIP_AWAITING_VERDICT}
    # The streak is the ONLY piece of D7 that needs memory, and it is read
    # before the remedy block below so both share one state read/write cycle.
    state_now = read_state(state_path)
    previous = state_now.get("underutil_streak")
    streak = next_streak(previous if isinstance(previous, int) else 0, underutilised(row))

    # The CAUSE costs one `git diff` per surplus candidate plus one per running
    # train, so it is resolved LAZILY: only within one sweep of firing, and only
    # for the ids that would appear in the finding. Every other sweep pays
    # nothing, which is what keeps a 120s watchdog cheap.
    in_flight_paths: set[str] | None = set()
    cand_paths: dict[str, set[str] | None] = {}
    cause = "not-computed"
    if streak >= th.underutil_sweeps - 1:
        for _n, st in running:
            # From the TRAIN's own base..tip, not from its members' envelopes: a
            # member whose envelope already left candidates/ would otherwise
            # contribute no paths and silently shrink the in-flight set.
            train_base, train_tip = st.get("base"), st.get("tip")
            got = (real_paths(repo, str(train_base), str(train_tip))
                   if train_base and train_tip else None)
            if got is None:
                in_flight_paths = None
                break
            in_flight_paths = (in_flight_paths or set()) | got
        for ident in surplus:
            base, ref = cand_refs.get(ident, (None, None))
            cand_paths[ident] = (real_paths(repo, base, ref) if base and ref else None)
        cause = classify_underutil_cause(surplus_ids=surplus,
                                         in_flight_paths=in_flight_paths,
                                         candidate_paths=cand_paths,
                                         awaiting_verdict_ids=awaiting)
    d7 = detect_underutilisation(row=row, streak=streak, cause=cause, th=th)
    results.append(DetectorResult(d7.detector, d7.note, d7.detections, data=row))
    results.append(detect_overcommit(running_states=running, max_slots=row.get("max_slots"),
                                     twin_host=twin_host, now=now))
    if not dry_run:
        # The series is written EVERY sweep, firing or not — "are we maxed out?"
        # is an empirical question and a row that only appears when something is
        # wrong cannot answer it.
        append_utilization(util_path, row)
        if previous != streak:
            write_state(state_path, {**state_now, "underutil_streak": streak})

    # remedy — D1 only, cooldown-bounded, never in --dry-run ----------------
    remediable = [d for r in results for d in r.detections if d.auto_remediable]
    if remediable and not dry_run:
        state = read_state(state_path)
        if remedy_allowed(state, "builder_unlock", now=now,
                          cooldown_min=th.remedy_cooldown_min):
            # RE-PROBE immediately before acting. The probe behind the detection
            # was taken at the top of the sweep, seconds and several filesystem
            # reads ago, and a builder that started in that gap holds the
            # worktree we are about to unlock. The whole safety argument for
            # this remedy is "nothing is using it", and a stale observation does
            # not support that claim — so the decision is re-taken against a
            # fresh one, and anything but a second clean IDLE stands down.
            confirm = probe_dir_in_use(builder_dir)
            if confirm != IDLE:
                append_log(log_path,
                           f"REMEDY stood down: re-probe of {builder_dir} returned "
                           f"{confirm!r}, not {IDLE!r} — the worktree is in use or "
                           f"unprovable since the sweep began", now=now)
            else:
                rc, out = run_unlock(repo, builder_dir)
                outcome = f"rc={rc} {out[:200]}"
                append_log(log_path,
                           f"REMEDY git worktree unlock {builder_dir}: {outcome}", now=now)
                write_state(state_path,
                            record_remedy(state, "builder_unlock", now=now, result=outcome))
        else:
            append_log(log_path,
                       f"REMEDY suppressed by cooldown ({th.remedy_cooldown_min}m): "
                       f"builder unlock already attempted recently", now=now)
    # --dry-run is print-only: it writes nothing anywhere, so an operator can
    # run it against live state without touching the queue or the log.

    return results


def summarize(results: list[DetectorResult], *, now: float, dry_run: bool) -> str:
    detections = [d for r in results for d in r.detections]
    head = (f"sweep {iso(now)}{' (dry-run)' if dry_run else ''} — "
            f"{len(detections)} detection(s) across "
            f"{sum(1 for r in results if r.fired)} detector(s)")
    body = "\n".join(f"  {r.detector} {r.note}" for r in results)
    return f"{head}\n{body}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One gate-health sweep: detect merge-gate stalls, remedy an "
                    "orphaned builder lock, and wake a human (or an AI).")
    parser.add_argument("--repo", default=str(REPO_DEFAULT))
    parser.add_argument("--queue", default=None,
                        help="loopqueue root (default <repo>/var/loopqueue)")
    parser.add_argument("--gate-log", default=None,
                        help="gate-loop log (default <repo>/var/log/gate-loop.log)")
    parser.add_argument("--builder-dir", default=None,
                        help="builder worktree (default <queue>/state/gate-loop-build)")
    parser.add_argument("--dry-run", action="store_true",
                        help="full sweep, print-only: no remedy, no finding, no pings")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    queue = Path(args.queue).expanduser() if args.queue else repo / "var" / "loopqueue"
    gate_log = Path(args.gate_log).expanduser() if args.gate_log \
        else repo / "var" / "log" / "gate-loop.log"
    builder_dir = Path(args.builder_dir).expanduser() if args.builder_dir \
        else queue / "state" / "gate-loop-build"
    log_path = repo / "var" / "log" / "gate-watch.log"
    state_path = repo / "var" / "gate-watch" / "state.json"
    util_path = repo / "var" / "gate-watch" / "utilization.jsonl"
    th = thresholds_from_env()
    now = time.time()

    try:
        results = run_sweep(repo=repo, queue=queue, log_path=log_path, gate_log=gate_log,
                            builder_dir=builder_dir, state_path=state_path,
                            util_path=util_path, now=now, th=th, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — a crashed sweep must still say so
        append_log(log_path, f"SWEEP FAILED ({type(exc).__name__}: {exc})", now=now)
        print(f"gate-watch: sweep failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1

    summary = summarize(results, now=now, dry_run=args.dry_run)
    detections = [d for r in results for d in r.detections]

    if args.dry_run:
        print(summary)
        for result in results:
            if result.data is not None:
                print(f"  utilization: {json.dumps(result.data, separators=(',', ': '))}")
        for det in detections:
            print(f"  - {det.detector} {det.symptom}")
            print(f"      evidence: {json.dumps(det.evidence, sort_keys=True)[:300]}")
        print("  (dry-run: no remedy, no finding, no pings)")
        return 0

    if detections:
        # (i) the log line, ALWAYS and FIRST — it is the only leg with no
        # dependency on the network, the queue or another module, so it is the
        # one that survives when the others are broken.
        for result in results:
            if result.fired:
                append_log(log_path, f"{result.detector} {result.note}", now=now)
        # (ii) one deduped finding per distinct detection.
        for det in detections:
            append_log(log_path, file_finding(repo, queue, det, now=now), now=now)
        # (iii) terse push — THROTTLED. The log and the finding record every
        # sweep; the phone only hears about a new or changed incident, because
        # an alert that arrives 360 times is one nobody reads the 361st time.
        state = read_state(state_path)
        key = fired_key(detections)
        if should_push(state, key, now=now, floor_min=th.push_floor_min,
                       retry_floor_after=th.ntfy_retry_floor_after):
            delivered, note = push_ntfy(repo, queue,
                                        f"gate pipeline stalled: {key} fired "
                                        f"({len(detections)} detection(s))")
            append_log(log_path, note, now=now)
            # The throttle advances on DELIVERY only. A failed attempt leaves
            # the key eligible, so a transient outage costs a delay rather than
            # the whole incident's notification. (Accepted cost: while a channel
            # is dead, notify.push_alert notes each failed attempt in ALERTS.md —
            # visible-but-noisy is the right side of silent-and-muted.)
            state = record_push_attempt(state, key, now=now, delivered=delivered, note=note)
            if delivered:
                state = record_push(state, key, now=now)
            write_state(state_path, state)
        else:
            attempt = state.get("ntfy_attempt")
            failures = attempt.get("consecutive_failures", 0) if isinstance(attempt, dict) else 0
            if isinstance(failures, int) and not isinstance(failures, bool) \
                    and failures >= th.ntfy_retry_floor_after:
                append_log(log_path,
                           f"ntfy backed off: {failures} consecutive failed deliveries — "
                           f"the push channel looks dead; retrying at most once per "
                           f"{th.push_floor_min}m", now=now)
            else:
                append_log(log_path,
                           f"ntfy suppressed: incident {key} already pushed "
                           f"(floor {th.push_floor_min}m)", now=now)
        # (iv) the AI-wake hook, off unless both switches are set.
        append_log(log_path, spawn_medic(summary), now=now)
        print(summary)
        return 0

    # A clean sweep ends the incident: forget the pushed set so a stall that
    # RECURS after a quiet period is announced again rather than swallowed as
    # "same as last time".
    cleared = clear_push_key(read_state(state_path))
    if cleared is not None:
        write_state(state_path, cleared)

    # DEAD-MAN: only a clean sweep pings, so a watchdog that starts firing and a
    # watchdog that dies both stop the heartbeat and ring the operator's check.
    ping = ping_healthcheck((os.environ.get("GATE_WATCH_HEALTHCHECK_URL") or "").strip())
    if "FAILED" in ping:
        # A clean sweep is otherwise SILENT on disk: at one sweep per 120s, a
        # per-detector "ok" block would add ~5,000 lines a day and bury the
        # firing sweeps in exactly the kind of log nobody drains that this job
        # exists to replace. A broken dead-man is the one thing worth a line.
        append_log(log_path, ping, now=now)
    print(summary.splitlines()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
