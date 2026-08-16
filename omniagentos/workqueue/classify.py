"""Post-hoc log classification — SPEC-shared-queue §4.4b.

**An instrument error must never be reported as a candidate defect.** A refusal
that blames the code for a dirty workspace, an expired token or a full disk
sends the next agent to debug the wrong thing — that is the failure this table
exists to stop. It is the direct fix for Initech repair cycles 68→71: one
blocker, four cycles, root cause an OAuth quota error that was counted as a
defect every time.

The table below is the whole mechanism. It runs AFTER the gate, over the
captured log, and overrides the outcome regardless of the process exit code —
including exit 0. That direction is deliberate and safe: a match can only ever
turn a pass into a re-run, never turn a failure into a pass. The worker still
branches its pass/fail/could-not-run trichotomy on the PROCESS exit code alone
(§4.3 rule 1); this only reclassifies WHICH could-not-run class applies, and
which counter the outcome is charged to.

Known false-positive surface, stated rather than hidden: ``\\b401\\b`` and
``\\b403\\b`` are the spec's own regexes and they match a bare number, so a log
line like "401 passed" reclassifies a pass into a terminal environment fault.
The cost is one park with a named remedy that a human clears in seconds; the
cost of the opposite error (an auth failure counted as three code defects) is
the four-cycle burn this table was written to end. Fail toward the instrument.

Retryability rides in the CLASS, never in the exit code (§4.3 rule 2):
``retryable=1`` means the recorded remedy REQUIRES re-running the same key
(the instrument is not part of the fingerprint, so repairing it never changes
the key). ``retryable=0`` means re-running is prohibited — the store parks it
immediately as ``terminal-instrument``.
"""

from __future__ import annotations

import re
from typing import NamedTuple

__all__ = [
    "SIGNATURES",
    "Signature",
    "classify_log",
    "classify_start_load",
]


class Signature(NamedTuple):
    """``(outcome, retryable, remedy)`` — the tuple every classifier returns."""

    outcome: str
    retryable: int
    remedy: str


#: The §4.4b table, in evaluation order. Order is load-bearing: the terminal
#: auth/suspension row is checked BEFORE the retryable quota row, so a log that
#: carries both (a 429 storm that ended in a 401) parks instead of retrying
#: three times against a dead credential.
#:
#: Every pattern is compiled once at import; the worker scans a whole agent log
#: per attempt and must not pay a re-compile for it.
SIGNATURES: tuple[tuple[re.Pattern[str], Signature], ...] = (
    (
        re.compile(
            r"\b401\b|\b403\b|invalid_api_key|authentication.*fail|account.*suspend",
            re.IGNORECASE,
        ),
        Signature(
            "environment",
            0,
            "credential/authorisation fault on this box — TERMINAL at the first "
            "occurrence: re-run the same key and it fails identically. Repair the "
            "provider credential (~/.config/omni/connections.env, mode 600), then "
            "`wq unpark <unit> --because '<what you fixed>'`.",
        ),
    ),
    (
        re.compile(
            r"quota (exceeded|exhausted)|rate.?limit|\b429\b|insufficient_quota",
            re.IGNORECASE,
        ),
        Signature(
            "environment",
            1,
            "provider quota/rate limit — the box was unfit, the code was never "
            "graded. Retries back off 60/300/900s and park as terminal-instrument "
            "after the third. This never enters the defect count.",
        ),
    ),
    (
        re.compile(r"database is locked|disk I/O error|No space left on device", re.IGNORECASE),
        Signature(
            "instrument-error",
            1,
            "local storage/SQLite instrument fault — free disk or clear the writer "
            "contention on this machine, then re-run the SAME key (the instrument "
            "is not part of the fingerprint, so a repair never changes it).",
        ),
    ),
    (
        re.compile(
            r"ssh: connect to host|Connection refused|Could not resolve host", re.IGNORECASE
        ),
        Signature(
            "environment",
            1,
            "network/transport fault reaching another host — check the tunnel or "
            "tailnet route, then re-run the same key.",
        ),
    ),
    (
        # 'Operation not permitted' UNDER .git/ only: a sandbox denial writing git
        # metadata is an instrument fact (scripts/land-lane.sh:9-19), whereas the
        # same phrase elsewhere in a log is ordinary noise and must not reclassify.
        re.compile(r"Operation not permitted[^\n]*\.git/|\.git/[^\n]*Operation not permitted"),
        Signature(
            "instrument-error",
            1,
            "sandbox denied a write under .git/ — this is the sandbox, not the "
            "code (scripts/land-lane.sh:9-19). Run the unit on a machine whose "
            "profile grants git metadata writes, or widen the profile sandbox.",
        ),
    ),
)

#: loadavg > 4 x perf-cores at start is a contention-flake by construction: the
#: reading, not the code, is what is in doubt (pipeline/bridge/gate_host.py
#: measured that gate verdicts above load ~10 are flakes, not verdicts).
_LOAD_MULTIPLIER = 4

_CONTENTION = Signature(
    "contention-flake",
    1,
    "the box was above 4x its performance-core count when the unit started — the "
    "reading is void, not the code. Re-run ONCE in a quiet window; if it fails "
    "quiet, it is a candidate-defect.",
)


def classify_log(text: str | None) -> Signature | None:
    """Return the §4.4b override for a captured log, or ``None`` for no match.

    ``None`` means "the exit code stands" — this function never invents a
    verdict, it only recognises the instrument speaking.
    """
    if not text:
        return None
    for pattern, signature in SIGNATURES:
        if pattern.search(text):
            return signature
    return None


def classify_start_load(load1: float | None, perf_cores: int | None) -> Signature | None:
    """The one non-log row of §4.4b: load at unit START, sampled by the worker.

    Unknown load or unknown core count returns ``None`` — an unknown box state is
    not a busy box, and must never become a free reclassification (2026-08-06
    favourable-absence rule).
    """
    if load1 is None or not perf_cores or perf_cores <= 0:
        return None
    if load1 > _LOAD_MULTIPLIER * perf_cores:
        return _CONTENTION
    return None
