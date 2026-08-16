"""Read-only readers for the four test-observability categories.

Every reader returns the same envelope::

    {"available": bool, "reason": str | None, "overview": {...},
     "series": [...], "weakspots": [...], "skipped_rows": int}

The router slices that envelope into the three wire shapes. Honesty rules the
whole module:

* absent, locked, corrupt OR present-but-unusable sources all report
  ``available: false`` with a reason — a window holding only aborted runs, or a
  cert run with nothing scored, is NOT a green zero;
* one bad row never kills a category: it is skipped and counted in
  ``skipped_rows`` (including rows whose counts are negative or internally
  inconsistent, which are corruption, not data);
* rates are ``None`` when the denominator is zero, never 0.0 or 100.0;
* reasons name no filesystem path and no exception class — these are gated
  reads, but a reason string is still the wrong place to describe local layout.

All timestamps are parsed (never string-sliced), normalised to UTC, and bucketed
by UTC date; window cutoffs are datetime comparisons, so an offset-bearing
timestamp cannot be mis-windowed or land in the wrong day.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from omniagentos import runtime_paths

CATEGORIES: tuple[str, ...] = ("northstar", "memory", "diagnostics", "suite")

# North Star pulse metrics, in display order, with their axis labels.
NSC_METRICS: dict[str, tuple[str, str]] = {
    "nsc.distance": ("Distance", "%"),
    "nsc.gate_pass_rate": ("Gate pass rate", "%"),
    "nsc.detection_rate": ("Detection rate", "%"),
    "nsc.mechanics_refusal_ratio": ("Mechanics refusal ratio", "%"),
    "nsc.void_ratio": ("Void ratio", "%"),
}

# Window the overview + weakspots endpoints scan: wide enough that the pinned
# 7-day suite stats and the 3-day staleness rule are both fully covered.
OVERVIEW_DAYS = 14

# Sidecars written beside a merge-gate receipt that are NOT receipts.
_SKIP_MARKERS: tuple[str, ...] = (".junit-", ".counterfeit-", ".ladder-")
_LANE_REPORTS: tuple[tuple[str, str], ...] = (
    ("fast-lane", "fast-lane-latest.xml"),
    ("test-pr", "test-pr-latest.xml"),
    ("test-nightly", "test-nightly-latest.xml"),
)
_LANE_FEATURE = "__lane__"
_STALE_AFTER_DAYS = 3
_RECENT_DAYS = 7
# A cell that stopped running must stay visible as STALE long after its last
# record falls out of the caller's window, so last-seen is resolved over a fixed
# horizon rather than over ``days``.
_STALE_HORIZON_DAYS = 90
_LEDGER_SHARD = re.compile(r"^ledger-(\d{4})(\d{2})\.jsonl$")
# Clock-skew allowance on the upper time bound. Anything stamped beyond it is a
# record of a run that has not happened.
_FUTURE_SKEW = timedelta(hours=1)
# Bounded confirmation probe: how many out-of-window receipts may be parsed to
# answer "does this store have ANY history?" when the window came back empty.
_HISTORY_PROBE_FILES = 50
_REASON_PREFIX = "__nsc_reason__:"
_DETAIL_CHARS = 300
_CACHE_TTL_S = 60.0
_CACHE_MAXSIZE = 32

_cache: OrderedDict[tuple[str, str, int], tuple[float, dict[str, Any]]] = OrderedDict()
_cache_lock = threading.Lock()


def _repo_root() -> Path:
    """Repo root = parent of the omniagentos package (same anchor as contracts)."""
    return Path(__file__).resolve().parent.parent.parent


def testobs_var_root() -> Path:
    """The var/ tree the readers read. ``OMNIAGENTOS_TESTOBS_VAR`` wins (tests)."""
    return runtime_paths.resolve_var_root(("OMNIAGENTOS_TESTOBS_VAR",))


def _absent(reason: str, skipped: int = 0) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "overview": {"available": False, "reason": reason},
        "series": [],
        "weakspots": [],
        "skipped_rows": skipped,
    }


def _ok(
    overview: dict[str, Any], series: list[Any], weak: list[Any], skipped: int
) -> dict[str, Any]:
    return {
        "available": True,
        "reason": None,
        "overview": {"available": True, **overview},
        "series": series,
        "weakspots": weak,
        "skipped_rows": skipped,
    }


def _parse_ts(value: Any) -> datetime | None:
    """Any recorded timestamp as an aware UTC datetime, or None if unparseable.

    Naive stamps are read as UTC (every writer in this repo stamps UTC), and a
    trailing ``Z`` is normalised because ``fromisoformat`` predates accepting it.
    Never string-slice a timestamp: ``2026-08-14T01:00:00-05:00`` is the 14th
    locally and the 14th at 06:00 UTC, and only one of those buckets is right.
    """
    if not isinstance(value, str) or len(value) < 10:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_date(value: Any) -> date | None:
    """A recorded calendar date (``pulse_series.date``), or None if unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _day(moment: datetime) -> str:
    return moment.astimezone(UTC).date().isoformat()


def _cutoff(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _counts(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, int] | None:
    """Non-negative integer counts, or None if ANY field is absent or ill-shaped.

    A MISSING count is not a zero — a record that never recorded how many tests
    passed did not record a clean run, and defaulting it to 0 promotes an
    incomplete row to a completed one. Negative, non-integral, boolean and
    non-finite values are corruption for the same reason. Either way the row is
    skipped whole rather than partially believed.
    """
    out: dict[str, int] = {}
    for key in keys:
        if key not in source:
            return None
        raw = source[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        if isinstance(raw, float) and not math.isfinite(raw):
            return None  # JSON admits NaN/Infinity; int() would raise on both
        value = int(raw)
        if value != raw or value < 0:
            return None
        out[key] = value
    return out


def _optional_count(source: dict[str, Any], key: str) -> int:
    """An optional non-negative count (absent/ill-shaped reads as 0).

    Only for fields whose absence is normal and means "none of these", like
    ``expected_failures``. Never for a field that carries the measurement.
    """
    counts = _counts(source, (key,))
    return counts[key] if counts else 0


def _rate(passed: int, failed: int) -> float | None:
    """Pass percentage in [0, 100], or None when nothing ran — never a coerced 0."""
    total = passed + failed
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, passed * 100.0 / total)), 1)


def _series(metric: str, label: str, unit: str, by_day: dict[str, float]) -> dict[str, Any]:
    points = [{"date": day, "value": by_day[day]} for day in sorted(by_day)]
    return {"metric": metric, "label": label, "unit": unit, "points": points}


# --------------------------------------------------------------------------- northstar


def _gate_check_ids() -> frozenset[str]:
    """Check ids flagged ``gate: true`` in the cert manifest; empty if unreadable.

    The manifest is config, not var — it is never fixtured, so an unreadable
    manifest yields ``gate: null`` on weakspots rather than a false ``false``.
    """
    try:
        import yaml

        path = _repo_root() / "configs" / "northstar-cert" / "manifest.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return frozenset(
            str(c["id"]) for c in (data.get("checks") or []) if isinstance(c, dict) and c.get("gate")
        )
    except Exception:  # noqa: BLE001 — a missing/odd manifest must not break the read
        return frozenset()


def _decode_reason(encoded: str) -> str | None:
    """Decode a ``__nsc_reason__:`` payload.

    The recorder writes ``base64.urlsafe_b64encode``
    (scripts/northstar_cert/record_results.py), so urlsafe is the primary
    decoder; standard base64 is tried as a fallback for any older payload.
    """
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(encoded).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            continue
    return None


def _nsc_verdict(metrics: dict[str, Any]) -> str | None:
    scored = _counts(metrics, ("pass", "fail", "void", "not_evaluable"))
    if scored is None or sum(scored.values()) != 1:
        return None  # unscored or multi-scored: corruption, skip the row
    for key, label in (("fail", "FAIL"), ("pass", "PASS"), ("void", "VOID"),
                       ("not_evaluable", "NOT_EVALUABLE")):
        if scored[key]:
            return label
    return None


def _nsc_case(per_case: dict[str, Any]) -> tuple[str | None, str | None]:
    """(check id, decoded reason) out of one ``per_case_json`` payload."""
    check_id: str | None = None
    reason: str | None = None
    for key in per_case:
        if key.startswith(_REASON_PREFIX):
            reason = _decode_reason(key[len(_REASON_PREFIX) :])
        elif check_id is None:
            check_id = key
    return check_id, reason


def _nsc_latest_run(conn: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Per-check verdicts of the newest cert run: (overview slice, weakspots, skipped)."""
    # Every (run, stamp) pair UNAGGREGATED, then per-run max and the overall max
    # both taken on PARSED values. SQL MAX is lexical, and a lexical max inside
    # the GROUP BY is just as wrong as one outside it: an offset-bearing stamp
    # ("2026-08-13T20:00:00-05:00") sorts BEFORE the UTC stamp it postdates, so
    # aggregating first can hand back the wrong run's verdicts. Bounded: one row
    # per check per run (hundreds).
    try:
        pairs = conn.execute("SELECT experiment_id, created_at FROM eval_results").fetchall()
    except sqlite3.Error:
        pairs = []
    newest: dict[str, datetime] = {}
    for run, raw in pairs:
        if run is None:
            continue
        moment = _parse_ts(raw)
        if moment is None:
            continue
        prior = newest.get(str(run))
        if prior is None or moment > prior:
            newest[str(run)] = moment
    if not newest:
        return {}, [], 0

    # Ties broken by run id so the choice is deterministic, never insertion order.
    run_id, verdict_at = max(newest.items(), key=lambda item: (item[1], item[0]))
    gates = _gate_check_ids()
    counts: dict[str, int] = defaultdict(int)
    weak: list[dict[str, Any]] = []
    skipped = 0
    for metrics_json, per_case_json in conn.execute(
        "SELECT metrics_json, per_case_json FROM eval_results WHERE experiment_id = ?", (run_id,)
    ):
        try:
            metrics, per_case = json.loads(metrics_json), json.loads(per_case_json)
        except (TypeError, ValueError):
            skipped += 1
            continue
        verdict = _nsc_verdict(metrics) if isinstance(metrics, dict) else None
        check_id, reason = _nsc_case(per_case) if isinstance(per_case, dict) else (None, None)
        if verdict is None or check_id is None:
            skipped += 1
            continue
        counts[verdict] += 1
        if verdict != "PASS":
            weak.append({
                "category": "northstar", "id": check_id, "label": check_id, "status": verdict,
                "detail": (reason or "")[:_DETAIL_CHARS] or None,
                "gate": (check_id in gates) if gates else None,
                "since": _day(verdict_at),
            })
    if not sum(counts.values()):
        return {}, [], skipped  # a run that scored nothing is not a measurement

    overview = {
        "run_id": run_id,
        "verdict_ts": verdict_at.isoformat(),
        "checks_pass": counts["PASS"], "checks_fail": counts["FAIL"],
        "checks_not_evaluable": counts["NOT_EVALUABLE"], "checks_void": counts["VOID"],
    }
    return overview, weak, skipped


def _nsc_metric_days(conn: sqlite3.Connection, days: int) -> tuple[dict[str, dict[str, float]], int]:
    by_metric: dict[str, dict[str, float]] = defaultdict(dict)
    skipped = 0
    cutoff = _cutoff(days).date()
    try:
        # The date bound is a deliberately loose PREFILTER (one day early) that
        # only bounds the scan; membership is decided on the parsed date below.
        rows = conn.execute(
            "SELECT metric, date, value FROM pulse_series "
            f"WHERE metric IN ({','.join('?' * len(NSC_METRICS))}) AND date >= ?",
            (*NSC_METRICS, (cutoff - timedelta(days=1)).isoformat()),
        ).fetchall()
    except sqlite3.Error:
        return by_metric, 0
    for metric, raw_date, value in rows:
        day = _parse_date(raw_date)
        if day is None or not isinstance(value, (int, float)) or isinstance(value, bool):
            skipped += 1
            continue
        if day >= cutoff:
            by_metric[str(metric)][day.isoformat()] = float(value)
    return by_metric, skipped


def read_northstar(days: int) -> dict[str, Any]:
    path = testobs_var_root() / "northstar-cert" / "results.sqlite3"
    if not path.exists():
        return _absent("no northstar-cert results recorded yet")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return _absent("northstar-cert results are not readable right now")

    try:
        by_metric, skipped = _nsc_metric_days(conn, days)
        run_overview, weak, run_skipped = _nsc_latest_run(conn)
    except sqlite3.Error:
        return _absent("northstar-cert results are not readable right now")
    finally:
        conn.close()
    skipped += run_skipped

    if not run_overview:
        return _absent("no scored checks in the latest northstar-cert run", skipped)

    series = [
        _series(metric, label, unit, by_metric[metric])
        for metric, (label, unit) in NSC_METRICS.items()
        if by_metric.get(metric)
    ]
    distance = [by_metric["nsc.distance"][d] for d in sorted(by_metric.get("nsc.distance", {}))]
    gate_rate = by_metric.get("nsc.gate_pass_rate", {})
    overview = {
        "distance": distance[-1] if distance else None,
        "delta_distance": round(distance[-1] - distance[-2], 3) if len(distance) >= 2 else None,
        "gate_pass_rate": gate_rate[max(gate_rate)] if gate_rate else None,
        **run_overview,
    }
    return _ok(overview, series, weak, skipped)


# ------------------------------------------------------------------------------ memory


def _junit_summary(path: Path) -> dict[str, Any] | None:
    """Root ``testsuite`` attrs of a JUnit file, or None when unusable.

    ``mtime`` is carried alongside the timestamp so same-day runs can be ordered
    chronologically — directory-name order is not run order.
    """
    try:
        root = ElementTree.parse(path).getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        if suite is None:
            return None
        total = suite.get("tests")
        if total is None:
            return None  # no total: the file records no measurement to report
        attrs: dict[str, str | int] = {"tests": total}
        # These three are absent-means-none in several JUnit writers, unlike the
        # total, whose absence means the file never said what it ran.
        for key in ("failures", "errors", "skipped"):
            attrs[key] = suite.get(key) or 0
        counts = _counts(
            {key: int(value) for key, value in attrs.items()},
            ("tests", "failures", "errors", "skipped"),
        )
        mtime = path.stat().st_mtime
    except (ElementTree.ParseError, OSError, TypeError, ValueError):
        return None
    if counts is None or counts["skipped"] > counts["tests"]:
        return None
    if counts["failures"] + counts["errors"] > counts["tests"] - counts["skipped"]:
        return None  # more bad outcomes than tests that ran: inconsistent
    moment = datetime.fromtimestamp(mtime, UTC)
    return {**counts, "mtime": mtime, "ts": moment.isoformat()}


def _junit_rate(summary: dict[str, Any]) -> float | None:
    """Pass rate over the tests that actually RAN (skips excluded from both sides)."""
    ran = summary["tests"] - summary["skipped"]
    bad = summary["failures"] + summary["errors"]
    return _rate(ran - bad, bad) if ran > 0 else None


def read_memory(days: int) -> dict[str, Any]:
    root = testobs_var_root() / "memcert"
    runs = root / "runs"
    candidates = [root / "t1-junit.xml"]
    if runs.is_dir():
        candidates += sorted(p / "junit.xml" for p in runs.iterdir() if p.is_dir())
    candidates = [p for p in candidates if p.exists()]
    if not candidates:
        return _absent("no durable memcert runs recorded yet")

    cutoff, skipped = _cutoff(days).timestamp(), 0
    summaries: list[dict[str, Any]] = []
    for path in candidates:
        summary = _junit_summary(path)
        if summary is None:
            skipped += 1
        elif summary["mtime"] >= cutoff:
            summaries.append(summary)
    if not summaries:
        return _absent("no usable memcert runs in the requested window", skipped)

    # Chronological by mtime: two runs recorded on the same day must resolve by
    # WHEN they ran, not by which directory name sorts last.
    summaries.sort(key=lambda item: item["mtime"])
    by_day: dict[str, float] = {}
    for summary in summaries:
        rate = _junit_rate(summary)
        if rate is not None:
            by_day[_day(datetime.fromtimestamp(summary["mtime"], UTC))] = rate
    latest = summaries[-1]

    overview = {"runs": len(summaries), "pass_rate": _junit_rate(latest),
                "last_run_ts": latest["ts"],
                **{k: latest[k] for k in ("tests", "failures", "errors", "skipped")}}
    series = [_series("memcert.pass_rate", "Pass rate", "%", by_day)] if by_day else []
    return _ok(overview, series, [], skipped)


# ------------------------------------------------------------------------- diagnostics

# Severity ladder mirrored from scripts/feature_health/fh.py (VERDICT_RANK), so
# this reader cannot disagree with the grid, LATEST.md or SYSTEM-LEDGER.md about
# which cell is the bad one. Aborted rows never reach here — they are not
# measurements — so ABORT/ABSENT have no entry.
# Severity ladder mirrored from scripts/feature_health/fh.py (VERDICT_RANK), so
# this reader cannot disagree with the grid, LATEST.md or SYSTEM-LEDGER.md about
# which cell is the bad one. ABORT ties ERR at 4 there, and does here.
_VERDICT_RANK = {"PASS": 1, "EMPTY": 2, "MISS": 3, "ERR": 4, "ABORT": 4, "FAIL": 5}
# Verdicts that mean "this cell is currently a weak spot". PASS is the only
# verdict that is not: EMPTY (nothing ran), MISS (declared paths absent) and
# ABORT (the run never happened) are all unproven, and reporting them as green
# is the favourable-absence class this module exists to refuse.
_WEAK_VERDICTS = ("FAIL", "ERR", "ABORT", "MISS", "EMPTY")
_FAILING = frozenset({"ERR", "FAIL"})


def _ledger_files(root: Path, days: int) -> tuple[list[Path], int]:
    """Every ``ledger-YYYYMM.jsonl`` shard whose month overlaps the window.

    Bounded at BOTH ends. Reading only the current and previous month truncated
    any window longer than ~60 days; an open upper bound admits a future-dated
    shard, whose rows would fabricate freshness the estate never measured. A
    regex-valid but calendar-invalid name (``ledger-202613.jsonl``) is counted
    and skipped — a typo in a filename must not poison the category.
    """
    first_month = _cutoff(days).date().replace(day=1)
    last_month = datetime.now(UTC).date().replace(day=1)
    shards: list[Path] = []
    skipped = 0
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return [], 0
    for entry in entries:
        match = _LEDGER_SHARD.match(entry.name)
        if not match:
            continue
        try:
            month = date(int(match.group(1)), int(match.group(2)), 1)
        except ValueError:
            skipped += 1
            continue
        if first_month <= month <= last_month:
            shards.append(Path(entry.path))
    return shards, skipped


def _ledger_rows(
    files: list[Path], cutoff: datetime, horizon: datetime
) -> tuple[list[tuple[dict, datetime]], int]:
    """Records inside ``[cutoff, horizon]``; anything else is skipped and counted.

    ``horizon`` is now plus a small clock-skew allowance: a future-dated record
    is not evidence of a run that has not happened, and admitting one moves
    ``last_run_ts`` into the future and makes every stale cell look fresh.
    """
    rows: list[tuple[dict[str, Any], datetime]] = []
    skipped = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            skipped += 1
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            moment = _parse_ts(row.get("ts")) if isinstance(row, dict) else None
            if moment is None:
                skipped += 1
                continue
            if moment > horizon:
                skipped += 1
                continue
            if moment >= cutoff:
                rows.append((row, moment))
    return rows, skipped


def _stream_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """(feature, tier, env, stream) — the identity of one independent sub-run.

    Transcribed from ``fh.stream_key``: the stream is the report basename with
    its run timestamp stripped. One cell can have several sub-runs (an isolated
    suite, live probes, Playwright), and any reader that collapses them to
    "newest record for the cell" lets a clean sibling erase a standing failure —
    the masking bug fh.py explicitly warns three previous readers grew.
    """
    report = os.path.basename(str(row.get("report_path") or ""))
    stream = report.split("-", 1)[1] if "-" in report else report
    return (
        str(row.get("feature") or "unknown"),
        str(row.get("tier") or "unknown"),
        str(row.get("env") or ""),
        stream,
    )


def _verdict_class(row: dict[str, Any], counts: dict[str, int]) -> str:
    """Severity class of one completed record (``fh.verdict_class``, sans ABORT)."""
    if row.get("status") == "error":
        return "ERR"
    bad = counts["failed"] + counts["errors"] - _optional_count(row, "expected_failures")
    if bad > 0:
        return "FAIL"
    if row.get("missing_paths"):
        return "MISS"
    return "PASS" if counts["passed"] else "EMPTY"


def _cells_from_streams(
    streams: dict[tuple[str, str, str, str], dict[str, Any]],
    last_completed: dict[tuple[str, str], datetime],
) -> dict[tuple[str, str], dict[str, Any]]:
    """{(feature, tier): the WORST of that cell's newest-per-stream records}.

    "Worst", never "newest" (``fh.worst_per_cell``): a cell is only as healthy
    as its unhealthiest sub-run, and the newest append is simply whichever
    sub-run finished last. Ties on severity go to the newest of the tied records.

    One departure from a plain worst-of: an ABORT is superseded by any LATER
    completed record in the same cell. An abort is a statement about the cell
    ("this did not run"), not a verdict on one sub-run, so a run that completed
    afterwards closes the gap. Without that rule a single load-guard abort would
    outrank every subsequent pass forever, which is the mirror image of the
    latched-red bug and just as untrue.
    """
    worst: dict[tuple[str, str], dict[str, Any]] = {}
    for (feature, tier, _env, _stream), record in streams.items():
        cell = (feature, tier)
        if record["verdict"] == "ABORT" and record["moment"] < last_completed.get(
            cell, record["moment"]
        ):
            continue
        prior = worst.get(cell)
        rank = (_VERDICT_RANK[record["verdict"]], record["moment"])
        if prior is None or rank > (_VERDICT_RANK[prior["verdict"]], prior["moment"]):
            worst[cell] = record
    return worst


def _failing_since(history: list[tuple[datetime, str]]) -> datetime:
    """Start of the current unbroken failing streak of one stream."""
    since = history[-1][0]
    for moment, verdict in reversed(history):
        if verdict not in _FAILING:
            break
        since = moment
    return since


def _weak_detail(record: dict[str, Any], seen: datetime) -> str:
    """Operator-facing one-liner for a weak cell. Never names a filesystem path.

    Failing cells carry test node ids (the point of the panel); MISS reports how
    many declared paths were absent WITHOUT listing them, and ABORT reports the
    abort reason only if it is a short token like ``load-guard``.
    """
    verdict = record["verdict"]
    row = record["row"]
    if verdict == "ERR" and row.get("did_not_run"):
        return "instrument error: the run observed no tests"
    if verdict in _FAILING:
        counts = record["counts"]
        nodes = [
            str(failure["nodeid"])
            for failure in row.get("failures") or []
            if isinstance(failure, dict) and failure.get("nodeid")
        ]
        bad = counts["failed"] + counts["errors"]
        listed = ", ".join(nodes[:3]) or "no node ids recorded"
        return f"{bad} failing: {listed}" if verdict == "FAIL" else f"errored: {listed}"
    if verdict == "ABORT":
        reason = str(row.get("abort_reason") or "").strip()
        clean = reason if reason and "/" not in reason and len(reason) <= 40 else ""
        return f"did not run ({clean})" if clean else "did not run"
    if verdict == "MISS":
        missing = row.get("missing_paths")
        count = len(missing) if isinstance(missing, list) else 1
        return f"incomplete run: {count} declared path(s) not on disk"
    if verdict == "EMPTY":
        return "ran, but nothing was measured"
    return f"last run {_day(seen)}"


def read_diagnostics(days: int) -> dict[str, Any]:
    root = testobs_var_root() / "feature-health"
    # Last-seen is resolved over a fixed horizon so a cell that STOPPED running
    # stays flagged stale instead of vanishing once its final record ages out of
    # the caller's window.
    horizon = max(days, _STALE_HORIZON_DAYS)
    files, skipped = _ledger_files(root, horizon)
    if not files:
        return _absent("no feature-health ledger for the requested window", skipped)
    rows, row_skipped = _ledger_rows(files, _cutoff(horizon), datetime.now(UTC) + _FUTURE_SKEW)
    skipped += row_skipped
    if not rows:
        return _absent("no feature-health rows in the requested window", skipped)

    window_cutoff = _cutoff(days)
    recent_cutoff = _cutoff(_RECENT_DAYS)
    stale_cutoff = _cutoff(_STALE_AFTER_DAYS)
    tier_days: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    streams: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    history: dict[tuple[str, str, str, str], list[tuple[datetime, str]]] = defaultdict(list)
    last_seen: dict[tuple[str, str], datetime] = {}
    last_completed: dict[tuple[str, str], datetime] = {}
    aborted_recent = 0
    measured_in_window = 0  # runs that produced evidence: completions AND instrument errors
    last_run_at: datetime | None = None

    for row, moment in sorted(rows, key=lambda item: item[1]):
        # ``aborted`` and ``did_not_run``-only are DIFFERENT things, and fh.py
        # grades them differently (fh.py:399 checks ``aborted`` first, then
        # status == "error"). An abort is the load guard refusing to start; a
        # did_not_run-only record is stamped by fh.py:660 when a run that DID
        # start observed zero testcases — collection failed, or the selection
        # deselected everything. That is an instrument error (ERR), not a gap in
        # coverage, so it must NOT take the supersedable ABORT path where a
        # sibling stream's pass would quietly clear it.
        aborted = bool(row.get("aborted"))
        no_observations = aborted or bool(row.get("did_not_run"))
        if no_observations:
            aborted_recent += 1 if moment >= recent_cutoff else 0
            counts = {"passed": 0, "failed": 0, "errors": 0}
            if not aborted and moment >= window_cutoff:
                # A did_not_run-only record is a run that STARTED and errored,
                # so the window IS measured — degraded, but measured. Gating its
                # cell behind an ordinary completion elsewhere made an instrument
                # error visible only when some UNRELATED feature happened to pass.
                # A true abort is different: nothing ran, so it proves nothing.
                measured_in_window += 1
                last_run_at = moment if last_run_at is None else max(last_run_at, moment)
        else:
            # A completed record must have recorded what it counted.
            maybe = _counts(row, ("passed", "failed", "errors"))
            if maybe is None:
                skipped += 1
                continue
            counts = maybe
            if moment >= window_cutoff:
                # did_not_run/aborted rows never enter a pass rate — a load-guard
                # abort is not a failure and must not be averaged in as one.
                measured_in_window += 1
                last_run_at = moment if last_run_at is None else max(last_run_at, moment)
                tier = str(row.get("tier") or "unknown")
                bucket = tier_days[(tier, _day(moment))]
                bucket[0] += counts["passed"]
                bucket[1] += counts["failed"] + counts["errors"]
        if str(row.get("feature") or "") == _LANE_FEATURE:
            continue  # lane markers count as runs, not as a grid cell
        key = _stream_key(row)
        if aborted:
            verdict = "ABORT"
        elif no_observations:
            # fh.py:660 stamps these status "error" too, but the verdict is
            # pinned on the flag rather than on the status field so a writer
            # that omits one cannot downgrade an instrument error to EMPTY.
            verdict = "ERR"
        else:
            verdict = _verdict_class(row, counts)
        history[key].append((moment, verdict))
        # Rows are walked oldest-first, so the last write per stream is its newest.
        streams[key] = {"moment": moment, "verdict": verdict, "counts": counts, "row": row}
        cell = (key[0], key[1])
        last_seen[cell] = max(last_seen.get(cell, moment), moment)
        if not aborted:
            last_completed[cell] = max(last_completed.get(cell, moment), moment)

    if not measured_in_window:
        return _absent("no completed feature-health runs in the requested window", skipped)

    series = []
    for tier in sorted({tier for tier, _ in tier_days}):
        by_day = {}
        for (row_tier, day), (passed, bad) in tier_days.items():
            rate = _rate(passed, bad) if row_tier == tier else None
            if rate is not None:
                by_day[day] = rate
        if by_day:
            series.append(_series(f"fh.{tier}.pass_rate", f"{tier} pass rate", "%", by_day))

    cells = _cells_from_streams(streams, last_completed)
    stale_cells = {cell for cell, seen in last_seen.items() if seen < stale_cutoff}
    weak = []
    for cell in sorted(cells):
        feature, tier = cell
        record = cells[cell]
        verdict = record["verdict"]
        weak_verdict = verdict in _WEAK_VERDICTS
        if not weak_verdict and cell not in stale_cells:
            continue
        status = verdict if weak_verdict else "STALE"
        since = (
            _failing_since(history[_stream_key(record["row"])])
            if verdict in _FAILING
            else (record["moment"] if weak_verdict else last_seen[cell])
        )
        weak.append({
            "category": "diagnostics", "id": f"{feature}/{tier}", "label": f"{feature} · {tier}",
            "status": status,
            "detail": _weak_detail(record, last_seen[cell])[:_DETAIL_CHARS],
            "gate": None,
            "since": _day(since),
        })

    failing_cells = {cell for cell, record in cells.items() if record["verdict"] in _FAILING}
    overview = {
        "features_total": len({feature for feature, _ in cells}),
        "features_failing": len({feature for feature, _ in failing_cells}),
        "features_stale": len({feature for feature, _ in stale_cells}),
        "cells_stale": len(stale_cells),
        "last_run_ts": last_run_at.isoformat() if last_run_at else None,
        "aborted_recent": aborted_recent,
    }
    return _ok(overview, series, weak, skipped)


# ------------------------------------------------------------------------------- suite


def _latest_lane(root: Path) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for name, filename in _LANE_REPORTS:
        path = root / "test-reports" / filename
        summary = _junit_summary(path) if path.exists() else None
        if summary is not None and (best is None or summary["mtime"] > best["mtime"]):
            best = {"name": name, **summary}
    if best is not None:
        best.pop("mtime", None)
    return best


def _is_receipt_name(name: str) -> bool:
    return name.endswith(".json") and not any(marker in name for marker in _SKIP_MARKERS)


def _read_receipt(path: str) -> dict[str, Any] | None | bool:
    """Parsed check-bearing receipt, ``False`` for "valid but not one", else None.

    ``False`` marks a run-envelope receipt (``.run-*.json``): a different, valid
    schema that carries no check counts — nothing to aggregate, and NOT a
    malformed row. ``None`` is corruption.
    """
    try:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict):
        return None
    return receipt if "checks_collected" in receipt else False


def _valid_receipt(payload: object) -> dict[str, int] | None:
    """Validated check counts of a receipt, or None if it is not a usable one.

    The SAME validation on both paths — the windowed scan and the out-of-window
    history probe. A receipt that only declares ``checks_collected`` is
    incomplete, and letting the probe accept it fabricated store history, which
    turned an honest "unmeasured" into a green-looking zero.
    """
    if not isinstance(payload, dict):
        return None
    counts = _counts(payload, ("checks_collected", "checks_passed", "checks_failed"))
    if counts is None:
        return None
    if counts["checks_passed"] + counts["checks_failed"] > counts["checks_collected"]:
        return None  # more outcomes than checks collected: inconsistent receipt
    return counts


def _receipt_days(records: Path, days: int) -> tuple[dict[str, list[int]], int, bool] | None:
    """(per-UTC-day [runs, passed, failed], skipped, store-has-any-history).

    The directory holds ~1500 files; ``st_mtime`` is checked from the scandir
    entry BEFORE any file is opened, so a 7-day view never parses a year of JSON.
    But mtime only PREFILTERS: a receipt copied or re-signed today can carry a
    two-month-old ``started_at``, and the receipt's own timestamp is what decides
    membership and which day it lands in. Only a receipt with no parseable
    timestamp at all falls back to mtime.

    ``history`` answers a different question from membership — does this store
    hold ANY usable receipt, in the window or out — so a valid receipt whose own
    timestamp excluded it from the window still proves the store has history.
    """
    cutoff = _cutoff(days)
    cutoff_ts = cutoff.timestamp()
    by_day: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    skipped = 0
    history = False
    older: list[tuple[float, str]] = []
    try:
        entries = list(os.scandir(records))
    except OSError:
        return None
    for entry in entries:
        if not _is_receipt_name(entry.name):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            skipped += 1
            continue
        if mtime < cutoff_ts:
            older.append((mtime, entry.path))
            continue
        receipt = _read_receipt(entry.path)
        if receipt is None:
            skipped += 1
            continue
        if not isinstance(receipt, dict):
            continue  # run-envelope receipt: valid, just not check-bearing
        counts = _valid_receipt(receipt)
        if counts is None:
            skipped += 1
            continue
        history = True  # a usable receipt exists, whichever window it lands in
        moment = _parse_ts(receipt.get("started_at") or receipt.get("finished_at"))
        if moment is None:
            moment = datetime.fromtimestamp(mtime, UTC)
        if moment < cutoff:
            continue  # the file is new; the run it records is not
        bucket = by_day[_day(moment)]
        bucket[0] += 1
        bucket[1] += counts["checks_passed"]
        bucket[2] += counts["checks_failed"]
    return by_day, skipped, history or _has_receipt_history(older)


def _has_receipt_history(older: list[tuple[float, str]]) -> bool:
    """Does this store hold ANY usable receipt outside the window?

    Bounded: the newest ``_HISTORY_PROBE_FILES`` out-of-window files only. It
    answers one question — is an empty window a real zero, or no measurement at
    all — and the whole point of the mtime prefilter is not to parse the archive.
    """
    for _mtime, path in sorted(older, reverse=True)[:_HISTORY_PROBE_FILES]:
        if _valid_receipt(_read_receipt(path)) is not None:
            return True
    return False


def read_suite(days: int) -> dict[str, Any]:
    root = testobs_var_root()
    records = root / "gate-evidence" / "records" / "merge-gate"
    if not records.is_dir():
        return _absent("no merge-gate receipts recorded yet")
    scanned = _receipt_days(records, days)
    if scanned is None:
        return _absent("merge-gate receipts are not readable right now")
    by_day, skipped, history = scanned

    latest_lane = _latest_lane(root)
    if not by_day and latest_lane is None:
        return _absent("no usable merge-gate receipts in the requested window", skipped)

    recent_day = _day(_cutoff(_RECENT_DAYS))
    recent = [bucket for day, bucket in by_day.items() if day >= recent_day]
    # Sub-source honesty: a lane-JUnit-only suite has measured NO gate runs, and
    # reporting 0 there claims a measurement that was never taken. A store that
    # HAS receipts but none in the window is a real zero and keeps it.
    overview = {
        "runs_7d": sum(b[0] for b in recent) if history else None,
        "pass_rate_7d": _rate(sum(b[1] for b in recent), sum(b[2] for b in recent)),
        "latest_lane": latest_lane,
    }
    rates = {day: rate for day, (_, p, f) in by_day.items() if (rate := _rate(p, f)) is not None}
    series = (
        [
            _series("gate.pass_rate", "Gate pass rate", "%", rates),
            _series("gate.runs", "Gate runs", "runs", {d: float(b[0]) for d, b in by_day.items()}),
        ]
        if by_day
        else []
    )
    return _ok(overview, series, [], skipped)


# ------------------------------------------------------------------------------- glue

READERS = {
    "northstar": read_northstar,
    "memory": read_memory,
    "diagnostics": read_diagnostics,
    "suite": read_suite,
}

# Display order. Two rules are composed here:
#   * the design contract — gate FAILs, other FAILs, diagnostics failing cells,
#     northstar mechanics (VOID/NOT_EVALUABLE), then unproven/stale cells;
#   * fh.py's severity ladder WITHIN diagnostics — FAIL/ERR, then ABORT, then
#     MISS/EMPTY, then STALE.
# Anything unlisted sorts last rather than silently ahead of a real failure.
_RANK = {
    ("northstar", "FAIL", True): 0,
    ("northstar", "FAIL", False): 1,
    ("diagnostics", "FAIL", False): 2,
    ("diagnostics", "ERR", False): 3,
    ("northstar", "VOID", False): 4,
    ("northstar", "NOT_EVALUABLE", False): 5,
    ("diagnostics", "ABORT", False): 6,
    ("diagnostics", "MISS", False): 7,
    ("diagnostics", "EMPTY", False): 8,
    ("diagnostics", "STALE", False): 9,
}
_RANK_LAST = 10

#: Every status the weakspots endpoint can emit (the UI's string union).
WEAKSPOT_STATUSES: tuple[str, ...] = (
    "FAIL", "ERR", "ABORT", "MISS", "EMPTY", "STALE", "VOID", "NOT_EVALUABLE",
)


def weakspot_rank(item: dict[str, Any]) -> tuple[int, str]:
    key = (str(item.get("category")), str(item.get("status")), bool(item.get("gate")))
    rank = _RANK.get(key, _RANK.get((key[0], key[1], False), _RANK_LAST))
    return rank, str(item.get("id") or "")


def snapshot(category: str, days: int, *, refresh: bool = False) -> dict[str, Any]:
    """TTL-cached (60s) category snapshot. Keyed on var root so tests stay isolated.

    Only SUCCESSES are cached, and the cache is LRU-bounded: caching an
    unavailable result would pin a transient failure (a locked db, a half-written
    ledger) for a minute, and an unbounded key space grows with every distinct
    ``days`` a caller asks for. Re-reading a failed source costs one bounded scan.

    A reader that raises at all reports unavailable — one broken source must
    never 500 the whole dashboard.

    Every touch of the ``OrderedDict`` is under ``_cache_lock``; only the read
    itself runs unlocked (two threads may duplicate one bounded scan, which is
    cheaper than serialising them). Unlocked ``get`` + ``move_to_end`` was a
    KeyError waiting to happen: a concurrent eviction between the two calls
    turns a cache hit into a 500.
    """
    key = (str(testobs_var_root()), category, days)
    started = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and not refresh and started - cached[0] < _CACHE_TTL_S:
            _cache.move_to_end(key)
            return cached[1]
        _cache.pop(key, None)
    try:
        result = READERS[category](days)
    except Exception:  # noqa: BLE001 — degrade, never 500 on a bad source
        result = _absent(f"{category} evidence could not be read")
    if result["available"]:
        with _cache_lock:
            existing = _cache.get(key)
            # Recheck: a slow scan that STARTED before the cached entry was
            # stored is older evidence, whatever order the two threads finish
            # in. Overwriting would serve a stale read out of a fresh cache.
            if existing is None or existing[0] <= started:
                _cache[key] = (time.monotonic(), result)
                _cache.move_to_end(key)
                while len(_cache) > _CACHE_MAXSIZE:
                    _cache.popitem(last=False)
    return result


def _clear_cache() -> None:
    """Test support (mirrors prompts.registry._clear_cache) — not production API."""
    with _cache_lock:
        _cache.clear()


__all__ = [
    "CATEGORIES",
    "NSC_METRICS",
    "OVERVIEW_DAYS",
    "READERS",
    "WEAKSPOT_STATUSES",
    "read_diagnostics",
    "read_memory",
    "read_northstar",
    "read_suite",
    "snapshot",
    "testobs_var_root",
    "weakspot_rank",
]
