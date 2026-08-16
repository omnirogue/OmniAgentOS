"""Throughput registry for LLM loops.

The rule this module exists to enforce
--------------------------------------
For an LLM loop, **liveness is not health**.  A loop whose process is up but
which has processed zero items is broken, and must never resolve ``OK``.  Every
other capability kind in capmap can be graded on "did the process exit 0"; a
loop cannot, because the most common real failure mode for such loops is a
daemon that is happily running and quietly doing nothing (a dispatcher logging
``0 dispatched`` for days on end; a replier sending zero replies while tickets
keep arriving).

So a loop is graded on four dimensions, in this order of authority:

1. **process** — not loaded / not running is ``DOWN``, full stop.
2. **volume** — items actually processed in the trailing window, compared
   against a floor *derived from the loop's own history*, never invented.
3. **errors** — refusal / fallback / error rate over the same window.
4. **burn** — cost or tokens, recorded for audit; never used to fail a loop,
   because a cheap loop is not a broken one.

Floors are derived, not guessed
-------------------------------
``derive_floor`` takes the trailing daily series and returns 30% of its median.
Two deliberate refusals:

* Days the history does not cover are **not** padded with zeros.  The
  loopqueue ledger may only reach back a few days; padding it to 30 days would
  drive every median to 0 and manufacture a floor of 0, which would let a dead
  loop read ``OK`` forever.  Only days the source actually covers are sampled.
* Fewer than ``MIN_FLOOR_SAMPLE_DAYS`` covered days returns ``floor=None`` with
  a reason.  A capability with a null floor is ``UNVERIFIED`` on the volume
  dimension and says so.  A fabricated floor is worse than no floor.

Everything in the grading core is a pure function over supplied data, so the
tests are fixtures rather than live systems.
"""

import datetime
import json
import os
import statistics

CAPABILITY_PREFIX = "loop-"

# Status vocabulary, kept identical to store.py so a loop row can be written
# into the same snapshot as every other capability without translation.
OK = "OK"
DEGRADED = "DEGRADED"
DOWN = "DOWN"
UNVERIFIED = "UNVERIFIED"

# Process dimension.
RUNNING = "RUNNING"
NOT_LOADED = "NOT_LOADED"
STOPPED = "STOPPED"
PROCESS_UNKNOWN = "UNKNOWN"

FLOOR_FRACTION = 0.3
MIN_FLOOR_SAMPLE_DAYS = 7
FLOOR_SAMPLE_WINDOW_DAYS = 30
DEFAULT_MAX_ERROR_RATE = 0.25

# Reason codes.  Stable strings so a dashboard can key on them.
R_NOT_LOADED = "process-not-loaded"
R_STOPPED = "process-stopped"
R_NO_SIGNAL = "no-throughput-signal"
R_ZERO_VOLUME = "zero-volume-while-running"
R_BELOW_FLOOR = "volume-below-derived-floor"
R_NO_FLOOR = "floor-underived-insufficient-history"
R_ERROR_RATE = "error-rate-above-threshold"
R_ABOVE_FLOOR = "volume-at-or-above-derived-floor"
R_PROCESS_UNKNOWN = "process-state-unknown"


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def _utc(value):
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        try:
            parsed = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _now(now=None):
    return _utc(now) or datetime.datetime.now(datetime.timezone.utc)


# --------------------------------------------------------------------------
# floor derivation
# --------------------------------------------------------------------------

def derive_floor(daily_counts, fraction=FLOOR_FRACTION, min_days=MIN_FLOOR_SAMPLE_DAYS,
                 window_days=FLOOR_SAMPLE_WINDOW_DAYS, now=None):
    """Derive an expected-volume floor from a loop's own trailing history.

    *daily_counts* maps ``YYYY-MM-DD`` to that day's volume.  Only days present
    in the mapping and inside the trailing *window_days* (excluding today,
    which is partial) are sampled; absent days are not imputed as zero.

    Returns a dict carrying the floor **and the sample it came from**, so the operator
    can audit and adjust it rather than trusting a bare number.
    """
    if not isinstance(daily_counts, dict):
        daily_counts = {}
    current = _now(now)
    window = [(current - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
              for offset in range(1, int(window_days) + 1)]
    covered = sorted(day for day in window if day in daily_counts)
    sample = [float(daily_counts[day]) for day in covered]
    result = {
        "floor": None,
        "median": None,
        "fraction": fraction,
        "sample_days": len(sample),
        "min_sample_days": min_days,
        "sample_window": None,
        "sample": None,
        "reason": None,
    }
    if len(sample) < min_days:
        result["reason"] = (
            f"insufficient history: {len(sample)} covered day(s) in the trailing "
            f"{int(window_days)}, need {min_days}"
        )
        if covered:
            result["sample_window"] = f"{covered[0]}..{covered[-1]}"
            result["sample"] = dict(zip(covered, [int(v) if v == int(v) else v for v in sample]))
        return result
    median = statistics.median(sample)
    result["median"] = median
    result["floor"] = round(median * fraction, 2)
    result["sample_window"] = f"{covered[0]}..{covered[-1]}"
    result["sample"] = dict(zip(covered, [int(v) if v == int(v) else v for v in sample]))
    return result


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------

def resolve(process_state=PROCESS_UNKNOWN, volume=None, floor=None, error_rate=None,
            max_error_rate=DEFAULT_MAX_ERROR_RATE, has_signal=True):
    """Grade one loop.  Returns ``{"status", "reasons", "dimensions"}``.

    ``volume=None`` means *no count was observed* — that is ``UNVERIFIED``, and
    is the honest answer for a loop whose log carries no timestamps.
    ``volume=0`` means *a count was observed and it was zero* — that is
    ``DEGRADED``, and no floor, missing history or absent error rate can
    upgrade it.  The two are deliberately not the same value.
    """
    reasons = []
    dimensions = {
        "process": process_state,
        "volume": volume,
        "floor": floor,
        "error_rate": error_rate,
        "max_error_rate": max_error_rate,
    }

    if process_state in (NOT_LOADED, STOPPED):
        reasons.append(R_NOT_LOADED if process_state == NOT_LOADED else R_STOPPED)
        return {"status": DOWN, "reasons": reasons, "dimensions": dimensions}

    if not has_signal or volume is None:
        reasons.append(R_NO_SIGNAL)
        if process_state == PROCESS_UNKNOWN:
            reasons.append(R_PROCESS_UNKNOWN)
        return {"status": UNVERIFIED, "reasons": reasons, "dimensions": dimensions}

    # A zero count from a live loop is the whole point of this module.
    if volume <= 0:
        reasons.append(R_ZERO_VOLUME)
        if floor is None:
            reasons.append(R_NO_FLOOR)
        if _error_rate_breached(error_rate, max_error_rate):
            reasons.append(R_ERROR_RATE)
        return {"status": DEGRADED, "reasons": reasons, "dimensions": dimensions}

    if _error_rate_breached(error_rate, max_error_rate):
        reasons.append(R_ERROR_RATE)
        return {"status": DEGRADED, "reasons": reasons, "dimensions": dimensions}

    if floor is None:
        # Positive volume, but nothing to judge it against.  Saying OK here
        # would be an unearned pass; saying DEGRADED would be a false alarm.
        reasons.append(R_NO_FLOOR)
        return {"status": UNVERIFIED, "reasons": reasons, "dimensions": dimensions}

    if volume < floor:
        reasons.append(R_BELOW_FLOOR)
        return {"status": DEGRADED, "reasons": reasons, "dimensions": dimensions}

    reasons.append(R_ABOVE_FLOOR)
    if process_state == PROCESS_UNKNOWN:
        reasons.append(R_PROCESS_UNKNOWN)
    return {"status": OK, "reasons": reasons, "dimensions": dimensions}


def _error_rate_breached(error_rate, max_error_rate):
    if error_rate is None or max_error_rate is None:
        return False
    try:
        return float(error_rate) > float(max_error_rate)
    except (TypeError, ValueError):
        return False


def evaluate(capability, observation=None, now=None):
    """Grade a ``loop-*`` capability from its recorded or supplied observation.

    The capability's ``throughput`` block declares where the signal lives and
    what floor was derived from it; *observation* (when supplied) overrides the
    recorded numbers with a fresh reading.
    """
    throughput = capability.get("throughput") or {}
    observed = dict(throughput.get("observed") or {})
    if observation:
        observed.update(observation)

    floor_block = throughput.get("floor") or {}
    daily = observed.get("daily_counts")
    if isinstance(daily, dict) and daily:
        floor_block = derive_floor(
            daily,
            fraction=floor_block.get("fraction", FLOOR_FRACTION),
            min_days=floor_block.get("min_sample_days", MIN_FLOOR_SAMPLE_DAYS),
            now=now,
        )

    # ``volume`` is the general key; ``volume_24h`` is the common special case
    # and stays accepted so a 24h-windowed entry reads naturally.
    volume = observed.get("volume")
    if volume is None:
        volume = observed.get("volume_24h")

    verdict = resolve(
        process_state=observed.get("process_state", PROCESS_UNKNOWN),
        volume=volume,
        floor=floor_block.get("floor"),
        error_rate=observed.get("error_rate"),
        max_error_rate=throughput.get("max_error_rate", DEFAULT_MAX_ERROR_RATE),
        has_signal=bool(throughput.get("signal_path")) and throughput.get("signal_exists", True),
    )
    verdict["id"] = capability.get("id")
    verdict["floor_derivation"] = floor_block
    verdict["last_success"] = observed.get("last_success")
    verdict["burn"] = observed.get("burn")
    verdict["signal_path"] = throughput.get("signal_path")
    verdict["window"] = throughput.get("window", "24h")
    return verdict


# --------------------------------------------------------------------------
# readers — thin, and pure wherever the input can be handed in as text
# --------------------------------------------------------------------------

def launchd_state(label, listing):
    """Map ``launchctl list`` output to a process state.

    A label with a plist but no row in the listing is ``NOT_LOADED``, which is
    a real outage and not an absence of information — a loop daemon can sit in
    exactly this state indefinitely.
    """
    for line in (listing or "").splitlines():
        fields = line.split("\t") if "\t" in line else line.split()
        if len(fields) >= 3 and fields[2].strip() == label:
            return RUNNING
    return NOT_LOADED


def daily_counts(records, ts_field="ts", predicate=None):
    """Bucket timestamped records by UTC day.  Untimestamped records are dropped.

    Dropping them is the point: a record with no timestamp cannot be windowed,
    and a file's mtime is not a count.
    """
    counts = {}
    for record in records or ():
        if predicate is not None and not predicate(record):
            continue
        stamp = _utc(record.get(ts_field) if isinstance(record, dict) else None)
        if stamp is None:
            continue
        day = stamp.strftime("%Y-%m-%d")
        counts[day] = counts.get(day, 0) + 1
    return counts


def window_count(records, ts_field="ts", hours=24, predicate=None, now=None):
    """Count records inside the trailing *hours*.  Returns an int, never a guess."""
    current = _now(now)
    cutoff = current - datetime.timedelta(hours=hours)
    total = 0
    for record in records or ():
        if predicate is not None and not predicate(record):
            continue
        stamp = _utc(record.get(ts_field) if isinstance(record, dict) else None)
        if stamp is None or stamp < cutoff:
            continue
        total += 1
    return total


def read_jsonl(path, limit=None):
    """Read a JSONL file, skipping torn lines.  Returns [] when absent."""
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
    except OSError:
        return []
    if limit is not None and len(records) > limit:
        return records[-limit:]
    return records


# --------------------------------------------------------------------------
# registry access + reporting
# --------------------------------------------------------------------------

def load_loop_capabilities(dir=None):
    """Return the ``loop-*`` capability documents that declare a throughput block."""
    if dir is None:
        dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capabilities")
    entries = []
    try:
        filenames = sorted(name for name in os.listdir(dir)
                           if name.startswith(CAPABILITY_PREFIX) and name.endswith(".json"))
    except OSError:
        return []
    for filename in filenames:
        try:
            with open(os.path.join(dir, filename), encoding="utf-8") as handle:
                capability = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(capability, dict) and capability.get("throughput"):
            entries.append(capability)
    return entries


def render_table(verdicts):
    """One line per loop: the answer to 'is this loop actually working?'."""
    header = ("CAPABILITY", "STATUS", "24H VOL", "FLOOR", "WHERE", "SIGNAL")
    rows = [header]
    for verdict in verdicts:
        volume = verdict["dimensions"].get("volume")
        floor = verdict["dimensions"].get("floor")
        rows.append((
            str(verdict.get("id") or "-"),
            str(verdict.get("status") or "-"),
            "-" if volume is None else str(volume),
            "null" if floor is None else str(floor),
            str(verdict.get("host") or "-"),
            str(verdict.get("signal_path") or "(none)"),
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = []
    for index, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(header))))
    return "\n".join(lines)


def main(argv=None):
    verdicts = []
    for capability in load_loop_capabilities():
        verdict = evaluate(capability)
        verdict["host"] = capability.get("host")
        verdicts.append(verdict)
    if not verdicts:
        print("no loop-* capabilities with a throughput block")
        return 0
    print(render_table(verdicts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
