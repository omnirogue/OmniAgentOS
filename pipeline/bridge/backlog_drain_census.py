"""Admitted-to-terminal age census, and the board's urgent-open rot report.

NSC-C32-04 (C-32 queue-correctness/priority-realization family, severity
high) was permanently NOT_EVALUABLE: its manifest binding named a sentinel
target with no health-sentinel check behind it, and its ``requires`` named a
writer token (``writer:backlog-drain-census``) with no writer. This module IS
that writer. Behind the mask sat a real missing capability, not paperwork:
nothing on the estate reconciled admitted work against its terminal events as
an AGE distribution, and nothing reported the board's urgent-open rot —
exactly the observability C32-O03 ("approved work does not rot",
``devtasks/northstar-cert/tests/S3-opus.md:260``) exists to demand.

Pure, offline, read-only analytics. No launchd, no network, no writes except
an OPTIONAL runtime snapshot the caller may choose to persist itself (this
module never writes one) — see ``evaluate_drain_bounds``.

**Terminal-set correction (2026-08-11, cross-lineage review, GPT-5.6-Sol,
candidate sha256:461364b6, MAJOR).** The first cut of this module copied the
proposal's literal S3-O03 set verbatim — ``merged``/``rejected``/``parked``/
``released``/``claim_expired``/``completed`` — without checking it against the
real loopqueue lifecycle. ``parked`` and ``claim_expired`` are SUSPENSIONS,
not terminals: the documented lifecycle is ``admitted -> parked -> unparked ->
gated -> merged``, and a claim expiry returns an id to the open pool, it does
not retire it. Treating either as terminal made active, still-rotting work
read as drained — measured: both ``admitted -> parked -> unparked`` and
``claim_expired -> admitted`` (a later admission for the same immutable id,
which must get its own fresh clock) reported ZERO open ids under the first
cut, the opposite of the observability this module exists to provide.
``BROAD_TERMINAL_EVENTS`` is now ``merged``/``rejected``/``released``/
``completed`` only. This is still deliberately broader than ``LedgerView``'s
narrow WIP set (``pipeline/bridge/integration.py:510-516``, which is
``merged``/``completed``/``rejected``/``closed`` and is the right set for "can
this id be claimed again", the wrong set for "has approved work drained") —
an id whose only terminal event is ``released`` still counts as CLOSED here,
and ``pipeline/tests/test_backlog_drain_census.py`` pins that AND the
``claim_expired``-does-not-close case, so a future edit that quietly widens
the set back toward suspensions, or narrows it back toward ``LedgerView``,
fails loudly either direction.

**Corrupt/malformed evidence correction (same review, MAJOR).** The first cut
decoded through ``ledger_read.iter_events`` (no status channel — deliberately,
for readers that only want the good events) and silently dropped anything it
could not parse: an admitted id's clock was locked on its FIRST occurrence
even when that occurrence's timestamp failed to parse, so a later valid
timestamp for the same id could never repair it; a terminal event recorded
chronologically before its own admission produced a clamped-to-zero lag
instead of a loud anomaly; and ``evaluate_drain_bounds`` defaulted every
missing field to an empty/zero-observation shape and returned ``PASS`` — an
entirely malformed ``{}`` report passed, indistinguishable from a genuinely
healthy empty ledger. This module now decodes through
``ledger_read.parse_events`` (per-line, WITH a status channel) and counts
lines it could not fully decode; ids whose only clock reading(s) never parsed
are named in ``malformed_ids`` rather than silently excluded; a terminal event
before its own admission is treated as anomalous evidence (excluded from
``closed_lag_hours`` and added to ``malformed_ids``, never clamped to a
fabricated zero); and ``evaluate_drain_bounds`` refuses to return ``PASS`` on
a malformed report shape, a nonzero ``corrupt_line_count``, or a nonempty
``malformed_ids`` — corrupt evidence is now a LOUD, named failure, never a
favourable absence.

**Tail-bound correction (same review, MAJOR).** ``evaluate_drain_bounds``
originally bounded only the aggregate p95 and the open-count trend, so one
indefinitely-stuck item was invisible behind 20 fresh ones (p95 stayed low,
the count stayed flat). It now also bounds ``open_age_hours["max"]`` — by
default against the same ``p95_open_hours_max`` ceiling, so a single stuck
item cannot hide behind a healthy p95 without a caller explicitly choosing a
looser ``max_open_hours_max``.

Decoding funnels through ``pipeline.bridge.ledger_read`` only — never a
private decoder for one JSONL line (see that module's docstring for why: a
private copy is how a torn-tail or spliced-line repair misses a caller).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bridge import ledger_read as LR

#: The corrected terminal set (see the module docstring's 2026-08-11
#: correction note): only events that genuinely retire an admitted id from
#: the "approved and pending" state. ``parked``/``claim_expired`` are
#: suspensions and are deliberately NOT here — they are simply ignored
#: events, like ``observed`` or ``gated``, so an id under one is neither
#: opened nor closed by it; only a real terminal (or the absence of one)
#: decides that.
BROAD_TERMINAL_EVENTS = frozenset({"merged", "rejected", "released", "completed"})

#: Only the first occurrence of each event KIND that also has a parseable
#: timestamp is authoritative for that id's clock — a re-admission after
#: unpark, or a second reject attempt, must not move an already-resolved
#: clock; a FIRST occurrence that fails to parse must not permanently block
#: a later, parseable occurrence from resolving it (the malformed-clock
#: correction above).
_ADMITTED_EVENT = "admitted"

# Required top-level shape of a genuine `compute_loopqueue_drain` report.
# `evaluate_drain_bounds` refuses anything missing these rather than
# defaulting to an empty/healthy shape and returning PASS on it.
_REQUIRED_REPORT_FIELDS = ("open_without_terminal", "open_age_hours")


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 ledger/board timestamp, tolerating a trailing ``Z``.

    Mirrors the idiom at ``omniagentos/projects/portfolio.py:_parse_ts`` —
    same tolerance, same failure mode (``None``, never a raise): a census is
    read-only reporting, and one malformed timestamp must not abort the
    whole run. The offending id is simply excluded from the age stats it
    could not be dated for; it still appears in identity-only fields
    (``open_without_terminal`` and/or ``malformed_ids``) so it is never
    silently dropped, only silently un-aged.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _stats(values: list[float]) -> dict[str, float | int | None]:
    """p50/p95/max over a list of non-negative hour deltas.

    ``n=0`` reports ``None`` for every percentile rather than 0 or raising —
    zero observations is not the same claim as "the age was zero hours", and
    a caller (``evaluate_drain_bounds``) must be able to tell the two apart.

    ``p95`` uses nearest-rank on the sorted list (index
    ``ceil(0.95 * n) - 1``, clamped into range) rather than an interpolated
    percentile: nearest-rank always names an OBSERVED value, which matters
    for a bound check — the reported p95 is a real age that was actually
    measured, not a number synthesised between two measurements.
    """
    if not values:
        return {"p50": None, "p95": None, "max": None, "n": 0}
    ordered = sorted(values)
    n = len(ordered)

    def _rank(fraction: float) -> float:
        import math

        index = max(0, min(n - 1, math.ceil(fraction * n) - 1))
        return ordered[index]

    return {
        "p50": _rank(0.50),
        "p95": _rank(0.95),
        "max": ordered[-1],
        "n": n,
    }


def compute_loopqueue_drain(
    ledger_path: str | Path, *, now: datetime | None = None
) -> dict[str, Any]:
    """Reconcile admitted loopqueue ids against the corrected terminal set.

    Reads ``ledger_path`` (a ``ledger.jsonl``-shaped file) exactly once,
    line by line, through ``ledger_read.parse_events`` — the shared decoder,
    never a private one, and the ONE variant of it that carries a per-line
    status. A census is exactly the kind of read-only consumer that must not
    silently swallow corruption as a favourable absence (see the module
    docstring's 2026-08-11 correction note), so every line whose status is
    ``UNDECODABLE`` or ``NOT_AN_OBJECT`` increments ``corrupt_line_count``
    rather than vanishing; ``BLANK``, ``OK`` and ``RECOVERED`` all still
    contribute whatever whole events they carry.

    For each id: the FIRST ``admitted`` occurrence THAT PARSES resolves its
    admission clock (an earlier, unparseable occurrence does not permanently
    block a later, parseable one — the malformed-clock correction above);
    the FIRST BROAD-terminal occurrence resolves its terminal clock, by
    ``dict.setdefault`` scanning forward, so a later duplicate of either kind
    can never move an already-resolved clock.

    An id whose only admitted record(s) never parsed, or whose terminal
    event is chronologically BEFORE its own admission (corrupt or reordered
    evidence — never a valid same-instant close), is named in
    ``malformed_ids`` and excluded from both age-stat lists rather than
    clamped into a fabricated zero-lag observation.

    An id with a terminal event but NO admitted event in this ledger window
    (e.g. the admitted record predates the window, or was on a line this
    file could not recover) is excluded from ``open_without_terminal``,
    ``closed_lag_hours`` AND ``malformed_ids`` — there is no admission clock
    to measure a lag or an age FROM, and inventing one would manufacture an
    observation that was never measured.

    Returns::

        {
          "admitted_count": int,
          "open_without_terminal": [id, ...],   # sorted, stable
          "open_age_hours": {"p50", "p95", "max", "n"},
          "closed_lag_hours": {"p50", "p95", "max", "n"},
          "terminal_set": [sorted BROAD_TERMINAL_EVENTS],
          "malformed_ids": [id, ...],           # sorted, stable
          "corrupt_line_count": int,
        }
    """
    if now is None:
        now = datetime.now(UTC)

    admitted_ts: dict[str, datetime] = {}
    admitted_ever: set[str] = set()
    terminal_ts: dict[str, datetime] = {}
    corrupt_line_count = 0

    text = Path(ledger_path).read_text(encoding="utf-8")
    for line in text.splitlines():
        events, status = LR.parse_events(line)
        if status in (LR.UNDECODABLE, LR.NOT_AN_OBJECT):
            corrupt_line_count += 1
        for event in events:
            ident = event.get("id")
            kind = event.get("event")
            if not isinstance(ident, str) or not isinstance(kind, str):
                continue
            if kind == _ADMITTED_EVENT:
                admitted_ever.add(ident)
                if ident not in admitted_ts:
                    ts = _parse_ts(event.get("ts"))
                    if ts is not None:
                        admitted_ts[ident] = ts
            elif kind in BROAD_TERMINAL_EVENTS and ident not in terminal_ts:
                ts = _parse_ts(event.get("ts"))
                if ts is not None:
                    terminal_ts[ident] = ts

    open_ages: list[float] = []
    open_ids: list[str] = []
    closed_lags: list[float] = []
    malformed_ids: set[str] = set()
    for ident in admitted_ever:
        a_ts = admitted_ts.get(ident)
        t_ts = terminal_ts.get(ident)
        if ident in terminal_ts:
            if a_ts is None or t_ts is None:
                # Identity and a terminal are both known, but one of the two
                # clocks never parsed — no honest lag can be measured.
                malformed_ids.add(ident)
                continue
            lag_hours = (t_ts - a_ts).total_seconds() / 3600.0
            if lag_hours < 0:
                # A terminal recorded chronologically BEFORE its own
                # admission is corrupt or reordered evidence — surfaced
                # loudly via malformed_ids, never clamped into a
                # fabricated zero-lag "close".
                malformed_ids.add(ident)
                continue
            closed_lags.append(lag_hours)
        else:
            if a_ts is None:
                # Admitted, still open, but no admission clock ever parsed —
                # cannot be aged and cannot be honestly counted as "open"
                # (there is no clock backing that claim); surfaced only via
                # the loud-failure channel, never silently dropped.
                malformed_ids.add(ident)
            else:
                open_ids.append(ident)
                open_ages.append(max(0.0, (now - a_ts).total_seconds() / 3600.0))

    return {
        "admitted_count": len(admitted_ever),
        "open_without_terminal": sorted(open_ids),
        "open_age_hours": _stats(open_ages),
        "closed_lag_hours": _stats(closed_lags),
        "terminal_set": sorted(BROAD_TERMINAL_EVENTS),
        "malformed_ids": sorted(malformed_ids),
        "corrupt_line_count": corrupt_line_count,
    }


def compute_board_urgent_open(
    db_path: str | Path, *, now: datetime | None = None
) -> dict[str, Any]:
    """Age distribution of the board's rotting urgent-open rows.

    Follows the parameterized, read-only SELECT idiom at
    ``omniagentos/projects/portfolio.py:57-75`` — one bare query, no ORM, no
    migration side-effect. Deliberately NOT opened with a ``mode=ro`` URI
    for the same reason ``omniagentos/connectors/secret_catalog.py`` gives
    for its own state read: a read-only connection to a WAL database needs
    the ``-shm``/``-wal`` siblings to already exist, and this module has no
    business asserting anything about the production database's journal
    mode. This connection issues nothing but one SELECT, so a plain
    connection is exactly as safe and does not add that failure mode.
    """
    if now is None:
        now = datetime.now(UTC)

    connection = sqlite3.connect(str(db_path))
    try:
        rows = connection.execute(
            "SELECT created_at FROM board_tasks "
            "WHERE status = 'open' AND priority = 'urgent' AND archived_at IS NULL"
        ).fetchall()
    finally:
        connection.close()

    ages: list[float] = []
    for (created_at,) in rows:
        ts = _parse_ts(created_at)
        if ts is not None:
            ages.append(max(0.0, (now - ts).total_seconds() / 3600.0))

    return {
        "count": len(rows),
        "age_hours": _stats(ages),
    }


def select_drainable_board_tasks(
    db_path: str | Path, *, limit: int, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Up to *limit* open ``board_tasks`` rows, OLDEST-first, for a bounded consumer.

    The read-model ``scripts/reliability/backlog-consumer.py`` builds its
    selection stage on — same read-only-SELECT idiom as
    ``compute_board_urgent_open`` above (one parameterized query, no ORM,
    no migration side-effect, no ``mode=ro`` URI for the same WAL-sibling
    reason). ``status = 'open' AND archived_at IS NULL`` only, so a consumer
    built on this read-model never re-selects a row the detox quarantine
    (``scripts/reliability/collapse-alert-backlog.py --apply``) already
    retired to ``cancelled``, and never selects an archived card. Ordered
    oldest-created-first (ties broken by id) so a bounded consumer that can
    only take a handful per run drains the longest-rotting work first,
    rather than the queue's most recently filed noise.
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if now is None:
        now = datetime.now(UTC)

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, title, discipline, priority, created_at FROM board_tasks "
            "WHERE status = 'open' AND archived_at IS NULL "
            "ORDER BY created_at ASC, id ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    selected: list[dict[str, Any]] = []
    for row in rows:
        ts = _parse_ts(row["created_at"])
        age_hours = max(0.0, (now - ts).total_seconds() / 3600.0) if ts is not None else None
        selected.append(
            {
                "id": str(row["id"]),
                "title": str(row["title"]),
                "discipline": row["discipline"],
                "priority": row["priority"],
                "created_at": row["created_at"],
                "age_hours": age_hours,
            }
        )
    return selected


def evaluate_drain_bounds(
    report: dict[str, Any],
    *,
    p95_open_hours_max: float,
    prior_open_count: int | None = None,
    max_open_hours_max: float | None = None,
) -> tuple[str, list[str]]:
    """The S3-O03 PASS clause: p95 drain lag bounded, tail bounded, open
    count flat-or-down, and the evidence itself must be intact.

    ``report`` is expected to be a ``compute_loopqueue_drain`` result (or
    anything shaped like one). Returns ``(verdict, reasons)`` where
    ``verdict`` is ``"PASS"`` or ``"FAIL"`` and ``reasons`` names every
    clause that failed (empty on PASS).

    **Evidence-integrity clauses (checked FIRST, and each is independently
    sufficient to FAIL — 2026-08-11 correction, see module docstring):**

    * ``report`` missing either of ``open_without_terminal``/
      ``open_age_hours`` is not a valid census result — an entirely empty
      ``{}`` (or any other malformed shape) FAILS with a named reason rather
      than defaulting to a healthy empty-observation PASS.
    * ``corrupt_line_count`` (when present) greater than zero FAILS — a
      ledger this module could not fully decode is not evidence the estate
      is healthy, it is evidence the reading is incomplete.
    * ``malformed_ids`` (when present) nonempty FAILS — an id whose clock(s)
      never parsed, or whose terminal preceded its own admission, is
      unresolved evidence, not silently-passing evidence.

    **Bound clauses (only reached once the evidence is intact):**

    * ``open_age_hours["p95"] > p95_open_hours_max`` FAILS.
    * ``open_age_hours["max"] > max_open_hours_max`` FAILS. When
      ``max_open_hours_max`` is not supplied it defaults to
      ``p95_open_hours_max`` — a single indefinitely-stuck item must not be
      invisible behind an otherwise-healthy p95 just because the caller did
      not think to name a separate tail bound (this is the tail-bound
      correction in the module docstring: 20 fresh items plus one
      thousand-hour item used to read p95=1h and PASS).
    * ``prior_open_count`` given and the current open count exceeds it
      FAILS. ``prior_open_count=None`` means "no prior run to compare
      against" and this clause is SKIPPED, not defaulted to PASS-by-assumption
      or FAIL-by-caution.

    Zero open ids trivially satisfies both bound clauses (``open_age_hours["n"]
    == 0`` means ``p95``/``max`` are ``None`` — there is nothing rotting to
    bound, which is the actual PASS condition, not an absence this function
    should refuse to score).
    """
    reasons: list[str] = []

    missing = [field for field in _REQUIRED_REPORT_FIELDS if field not in report]
    if missing:
        reasons.append(
            "malformed report: missing required field(s) "
            + ", ".join(sorted(missing))
        )
        return "FAIL", reasons

    corrupt_line_count = report.get("corrupt_line_count", 0) or 0
    if corrupt_line_count:
        reasons.append(
            f"ledger corruption: {corrupt_line_count} line(s) could not be decoded"
        )

    malformed_ids = report.get("malformed_ids") or []
    if malformed_ids:
        reasons.append(
            f"{len(malformed_ids)} id(s) have unresolved evidence "
            f"(malformed_ids={sorted(malformed_ids)!r})"
        )

    open_ids = report.get("open_without_terminal", [])
    open_age = report.get("open_age_hours", {}) or {}
    p95 = open_age.get("p95")
    if p95 is not None and p95 > p95_open_hours_max:
        reasons.append(
            f"open_age_p95 {p95:.1f}h exceeds bound {p95_open_hours_max:.1f}h"
        )

    tail_bound = (
        p95_open_hours_max if max_open_hours_max is None else max_open_hours_max
    )
    max_age = open_age.get("max")
    if max_age is not None and max_age > tail_bound:
        reasons.append(
            f"open_age_max {max_age:.1f}h exceeds tail bound {tail_bound:.1f}h"
        )

    open_count = len(open_ids)
    if prior_open_count is not None and open_count > prior_open_count:
        reasons.append(
            f"open_count {open_count} rose above prior {prior_open_count}"
        )

    verdict = "FAIL" if reasons else "PASS"
    return verdict, reasons
