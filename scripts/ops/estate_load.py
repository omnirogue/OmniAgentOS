#!/usr/bin/env python3
"""estate_load.py — the offload-before-overload back-off primitive (the operator, 2026-08-13).

Every orchestrator calls this BEFORE a heavy spawn (a test suite, a build, >2
concurrent agents, a workflow fan-out). One number decides: **1-min load ÷
logical cores**, thresholded exactly as the estate doctrine rules
(~/.omniagentos/ops/Offload-Before-Overload-Doctrine-2026-08-13.md):

* ratio < 0.6      → ``green`` — proceed, exit 0
* 0.6 ≤ ratio ≤ 0.8 → ``amber`` — halve the fan-out or offload, exit 1
* ratio > 0.8      → ``red``   — do NOT spawn locally, exit 2 (no "it's quick"
  exception — an overloaded box turns green tests red)

An UNMEASURABLE local load exits amber (1), never green: telemetry that could
not be read must not authorise a spawn (favourable absence).

Modes
-----
* default        — this box: prints ``<load1> <cores> <ratio> <verdict>``.
* ``--fleet``    — additionally reads ``var/workqueue.sqlite3`` READ-ONLY and
  prints one line per enrolled machine with the same ratio+verdict, plus a
  ``best: <machine>`` placement hint (lowest ratio; fresh heartbeat only —
  telemetry older than 10 minutes is ``unknown`` and NEVER healthy or best;
  drained machines claim nothing, so they are never best either). A missing or
  unreadable queue DB degrades gracefully: the fleet section reports
  unavailable and the LOCAL verdict still decides the exit code.
* ``--json``     — the same facts for tooling.

The exit code is ALWAYS the local verdict — this is a "may I spawn HERE" gate;
the fleet listing is the "then where instead" answer.

Load sources: ``sysctl -n vm.loadavg`` on darwin, ``/proc/loadavg`` on linux,
``os.getloadavg()`` as the fallback for both. Machine rows use the same
doctrine ratio (``last_load1 / ncpu``) — deliberately NOT the worker's claim
ceiling (``ceiling_fraction * ncpu``): this tool answers "where is headroom",
the ceiling answers "may a worker claim", and the two must stay independently
readable.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "var" / "workqueue.sqlite3"

GREEN_BELOW = 0.6
AMBER_MAX = 0.8
#: A heartbeat older than this is UNKNOWN — stale telemetry must never read as
#: healthy, and an offline box must never be recommended as a target.
STALE_AFTER_S = 600.0

EXIT_GREEN = 0
EXIT_AMBER = 1
EXIT_RED = 2

_VERDICT_EXIT = {"green": EXIT_GREEN, "amber": EXIT_AMBER, "red": EXIT_RED, "unknown": EXIT_AMBER}


def verdict_for(ratio: float | None) -> str:
    """Doctrine thresholds on 1-min load ÷ cores. ``None`` is ``unknown``."""
    if ratio is None:
        return "unknown"
    if ratio < GREEN_BELOW:
        return "green"
    if ratio <= AMBER_MAX:
        return "amber"
    return "red"


def read_load1() -> float | None:
    """This box's 1-minute load average, per the doctrine's named sources."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "vm.loadavg"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            # "{ 4.60 3.98 3.68 }"
            parts = out.stdout.strip().strip("{} \n").split()
            if parts:
                return float(parts[0])
        elif sys.platform.startswith("linux"):
            return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, subprocess.SubprocessError, IndexError):
        pass
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def read_cores() -> int:
    return os.cpu_count() or 1


#: Distinguishes "argument not provided" from an explicit ``None`` ("load could
#: not be measured") — an injected None must stay None, never re-read the box.
_UNSET: Any = object()


def local_report(load1: float | None | Any = _UNSET, cores: int | None = None) -> dict[str, Any]:
    measured: float | None = read_load1() if load1 is _UNSET else load1
    cores = read_cores() if cores is None else cores
    ratio = round(measured / cores, 3) if measured is not None and cores > 0 else None
    return {"load1": measured, "cores": cores, "ratio": ratio, "verdict": verdict_for(ratio)}


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def fleet_report(
    db_path: str | Path, now: datetime | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """Rows for every enrolled machine, read-only, plus an error string.

    Returns ``([], reason)`` when the queue DB is absent/unreadable — the
    caller degrades to a local-only verdict instead of failing.
    """
    path = Path(db_path)
    if not path.exists():
        return [], f"queue DB not found at {path} — fleet telemetry unavailable"
    now = now or datetime.now(UTC)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT machine_id, ncpu, last_load1, last_seen_at, drain "
                "FROM wq_machines ORDER BY machine_id"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [], f"queue DB unreadable ({exc}) — fleet telemetry unavailable"

    machines: list[dict[str, Any]] = []
    for row in rows:
        ncpu = int(row["ncpu"]) if row["ncpu"] else None
        load1 = float(row["last_load1"]) if row["last_load1"] is not None else None
        seen = _parse_ts(row["last_seen_at"])
        age_s = (now - seen).total_seconds() if seen is not None else None
        stale = age_s is None or age_s > STALE_AFTER_S
        ratio: float | None = None
        if not stale and load1 is not None and ncpu:
            ratio = round(load1 / ncpu, 3)
        machines.append(
            {
                "machine": str(row["machine_id"]),
                "load1": load1,
                "cores": ncpu,
                "ratio": ratio,
                # Stale telemetry is UNKNOWN by construction, never healthy.
                "verdict": "unknown" if stale else verdict_for(ratio),
                "stale": stale,
                "age_s": round(age_s) if age_s is not None else None,
                "drain": bool(row["drain"]),
            }
        )
    return machines, None


def best_machine(machines: list[dict[str, Any]]) -> str | None:
    """Lowest known ratio among fresh, non-draining machines. None if no candidate."""
    candidates = [
        m for m in machines if not m["stale"] and not m["drain"] and m["ratio"] is not None
    ]
    if not candidates:
        return None
    return str(min(candidates, key=lambda m: float(m["ratio"]))["machine"])


def _fmt(value: Any, spec: str = "{}") -> str:
    return "—" if value is None else spec.format(value)


def render(local: dict[str, Any], machines: list[dict[str, Any]] | None, best: str | None) -> str:
    lines = [
        f"{_fmt(local['load1'], '{:.2f}')} {local['cores']} "
        f"{_fmt(local['ratio'], '{:.2f}')} {local['verdict']}"
    ]
    if machines is not None:
        for m in machines:
            note = " (stale)" if m["stale"] else (" (draining)" if m["drain"] else "")
            lines.append(
                f"{m['machine']:<28} {_fmt(m['load1'], '{:.2f}'):>7} {_fmt(m['cores']):>4} "
                f"{_fmt(m['ratio'], '{:.2f}'):>6} {m['verdict']}{note}"
            )
        lines.append(f"best: {best}" if best else "best: none (no fresh machine telemetry)")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="estate_load.py",
        description=(
            "Offload-before-overload gate: 1m-load/cores verdict for this box "
            "(exit 0 green <0.6, 1 amber 0.6-0.8, 2 red >0.8), and with --fleet "
            "the same verdict for every enrolled wq machine plus a placement hint."
        ),
    )
    parser.add_argument(
        "--fleet",
        action="store_true",
        help="also read var/workqueue.sqlite3 (read-only) and print per-machine verdicts",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--db",
        default=os.environ.get("WQ_DB") or str(DEFAULT_DB),
        help="queue sqlite path for --fleet (default: env WQ_DB, else var/workqueue.sqlite3)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    local = local_report()
    machines: list[dict[str, Any]] | None = None
    best: str | None = None
    fleet_error: str | None = None
    if args.fleet:
        machines, fleet_error = fleet_report(args.db)
        best = best_machine(machines)
        if fleet_error:
            print(f"estate_load: {fleet_error}", file=sys.stderr)
    if args.json:
        payload: dict[str, Any] = {"local": local}
        if args.fleet:
            payload["fleet"] = machines or []
            payload["best"] = best
            payload["fleet_error"] = fleet_error
        print(json.dumps(payload, indent=1))
    else:
        print(render(local, machines, best))
    # The exit code is the LOCAL verdict: this is the "may I spawn HERE" gate.
    return _VERDICT_EXIT[str(local["verdict"])]


if __name__ == "__main__":
    raise SystemExit(main())
