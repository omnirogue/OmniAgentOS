"""Pure math + JSONL bookkeeping for the golden-suite sentinel's history.

No I/O beyond reading/appending the one JSONL file this module owns
(`var/golden/history.jsonl` in production, an injectable path in tests) and
no LLM calls -- see `run_golden.py`'s module docstring for the sentinel's
own "no LLM calls in the driver itself" invariant, which this module shares.

One JSONL row per (date, name): ``{"date": "2026-07-24", "name": "trivial",
"seconds": 12.3, "dnf_reason": null, "run_ref": "..."}`` -- ``seconds`` is
``None`` (json ``null``) on a DNF, and ``dnf_reason`` is a short string
whenever ``seconds`` is ``None`` (and ``None`` otherwise). Consumers should
treat any other field as informational.

Kept import-light (stdlib only: no PyYAML, no httpx) so these functions are
trivially unit-testable without a live API or a real database.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

__all__ = [
    "HistoryEntry",
    "append_history",
    "check_regression",
    "is_regression_night",
    "percentile",
    "read_history",
    "rolling_baseline_median",
    "rolling_percentiles",
]

HistoryEntry = dict[str, Any]


# --------------------------------------------------------------------------
# JSONL read/append
# --------------------------------------------------------------------------


def read_history(path: Path) -> list[HistoryEntry]:
    """Every well-formed JSON object line in ``path``; malformed lines are
    skipped (never raises -- a hand-edited or truncated file must never
    crash the sentinel). Missing file -> empty list."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[HistoryEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def append_history(path: Path, entry: HistoryEntry) -> bool:
    """Append ``entry`` as one JSON line, UNLESS a row with the same
    ``(date, name)`` already exists in the file -- idempotent per (date,
    name) so a re-run on the same UTC day (a retry, a manual re-invocation)
    never double-records a benchmark it already has a line for.

    Returns True if a line was appended, False if a matching (date, name)
    row already existed (a no-op). Creates the parent directory and file if
    needed.
    """
    existing = read_history(path)
    key = (entry.get("date"), entry.get("name"))
    for row in existing:
        if (row.get("date"), row.get("name")) == key:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True))
        fh.write("\n")
    return True


# --------------------------------------------------------------------------
# Percentiles
# --------------------------------------------------------------------------


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile (the common "numpy default" method).

    ``pct`` is 0-100. Returns ``None`` for an empty input.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (pct / 100.0)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[int(rank)])
    lo_val = ordered[int(lo)] * (hi - rank)
    hi_val = ordered[int(hi)] * (rank - lo)
    return float(lo_val + hi_val)


def rolling_percentiles(
    history: list[HistoryEntry], name: str, window: int | None = None
) -> dict[str, Any]:
    """{p50, p90, n} over this benchmark's successful (non-DNF) ``seconds``
    values, most-recent-``window`` entries (by ``date``, ascending) or the
    full history when ``window`` is ``None``/0. This is the REPORTING
    rollup (the "north-star number") -- see :func:`check_regression` for the
    separate regression-detection comparison, which always uses its own
    fixed rolling window regardless of what is passed here.
    """
    entries = sorted(
        (e for e in history if e.get("name") == name),
        key=lambda e: str(e.get("date") or ""),
    )
    if window:
        entries = entries[-window:]
    values = [float(e["seconds"]) for e in entries if e.get("seconds") is not None]
    return {
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "n": len(values),
    }


# --------------------------------------------------------------------------
# Regression detection
# --------------------------------------------------------------------------


def rolling_baseline_median(
    entries: list[HistoryEntry], index: int, window: int = 7
) -> float | None:
    """Median of ``seconds`` over up to ``window`` entries STRICTLY BEFORE
    ``entries[index]`` (``entries`` must already be one benchmark's rows,
    sorted ascending by date). ``None`` when there is no prior successful
    run to compare against."""
    prior = [
        float(e["seconds"])
        for e in entries[max(0, index - window) : index]
        if e.get("seconds") is not None
    ]
    if not prior:
        return None
    return statistics.median(prior)


def is_regression_night(
    entries: list[HistoryEntry],
    index: int,
    threshold_pct: float = 25.0,
    window: int = 7,
) -> bool:
    """True if ``entries[index]`` is a "regression night" for its benchmark:
    a DNF (``seconds`` is ``None``) always counts; otherwise its ``seconds``
    must exceed ``threshold_pct`` percent over the rolling median of the
    prior ``window`` entries. A night with no prior baseline to compare
    against (cold start) is never a regression night."""
    baseline = rolling_baseline_median(entries, index, window)
    if baseline is None or baseline <= 0:
        return False
    seconds = entries[index].get("seconds")
    if seconds is None:
        return True
    return float(seconds) > baseline * (1.0 + threshold_pct / 100.0)


def check_regression(
    history: list[HistoryEntry],
    name: str,
    threshold_pct: float = 25.0,
    consecutive_nights: int = 2,
    window: int = 7,
) -> bool:
    """True iff the LAST ``consecutive_nights`` entries for ``name`` (sorted
    by date) are ALL regression nights (:func:`is_regression_night`) against
    their own rolling baseline at the time. Fewer than ``consecutive_nights``
    entries for this benchmark -> never a regression (nothing to confirm
    against)."""
    entries = sorted(
        (e for e in history if e.get("name") == name),
        key=lambda e: str(e.get("date") or ""),
    )
    if len(entries) < consecutive_nights or consecutive_nights <= 0:
        return False
    n = len(entries)
    for i in range(n - consecutive_nights, n):
        if not is_regression_night(entries, i, threshold_pct, window):
            return False
    return True
