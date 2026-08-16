#!/usr/bin/env python3
"""Reaper tracker — a read-only observer for OmniAgentOS's reaper stack.

the operator's concern: the session reaper may be killing legitimate sessions. This tool
gives that concern a durable, greppable evidence trail WITHOUT modifying any
reaper (product code is not touched this session).

It folds two sources into one append-only ledger
(`var/livesim/reaper-ledger.jsonl`) and prints a summary:

  1. The live runtime DB — every session terminalized by a reaper, attributed by
     `killed_by` (idle-reaper, budget, max-park, reconcile) or by the
     liveness-reaper's error-text signature. Each snapshot records counts by
     attribution, plus the newest N individual kills with age-at-kill so a human
     can see whether a killed session looked active right up to the kill.
  2. Reaper log lines — the A2 supervisor emits structured `reaper.kill`,
     `reaper.would_kill`, `reaper.defer`, `reaper.max_park` JSON events to the
     Python logs; the idle-reaper.sh emits JSONL to agent-inbox/idle-reaper.jsonl.
     These are parsed and normalized so dry-run 'would_kill' decisions are visible
     even when nothing was actually killed.

Run it on a schedule (see docs/testing/REAPER-TRACKING.md) or ad hoc:

    scripts/livesim/reaper_tracker.py snapshot     # append one snapshot + print
    scripts/livesim/reaper_tracker.py summary       # print the ledger summary
    scripts/livesim/reaper_tracker.py legitimacy     # heuristic 'legit kill?' view

Nothing here signals a process or writes to any product DB.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import livesim_common as lc  # noqa: E402

LEDGER = lc.var_dir() / "reaper-ledger.jsonl"
REAPER_KILLERS = ("idle-reaper", "budget", "max-park", "reconcile", "operator", "swarm-timeout")


def _log_globs() -> list[str]:
    """Where the A2 supervisor / api emit reaper.* events (best-effort).

    Derived from the live DB's serving checkout so this is not pinned to one
    machine's hardcoded paths: the runtime DB lives at <checkout>/var/runtime/
    state.sqlite3, so its logs are <checkout>/var/log/*.log. Overridable via
    LIVESIM_REAPER_LOG_GLOBS (colon-separated)."""
    override = os.environ.get("LIVESIM_REAPER_LOG_GLOBS")
    if override:
        return [g for g in override.split(":") if g]
    globs: list[str] = []
    try:
        # LIVE_DB = <checkout>/var/runtime/state.sqlite3, so parents[1] is
        # <checkout>/var and its log/ sibling is the serving checkout's log dir.
        var_dir = lc.LIVE_DB.parents[1]
        globs.append(str(var_dir / "log" / "*.log"))
    except (IndexError, ValueError):
        pass
    # Common sibling serving checkouts on this estate (harmless if absent).
    for base in ("/Users/youruser/OmniAgentOS", "/Users/youruser/OmniAgentOS-main"):
        globs.append(f"{base}/var/log/*.log")
    return list(dict.fromkeys(globs))


LOG_GLOBS = _log_globs()
IDLE_REAPER_JSONL = Path("/Users/youruser/Work/Ops/agent-inbox/idle-reaper.jsonl")

_EVENT_RE = re.compile(r'reaper\.(kill|would_kill|defer|max_park)\b.*?(\{.*\})')


def _db_snapshot() -> dict:
    db = lc.LIVE_DB
    if not db.exists():
        return {"error": f"live DB not found: {db}"}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        by_killer = {
            r["kb"]: r["n"]
            for r in conn.execute(
                "SELECT COALESCE(killed_by,'(none)') kb, COUNT(*) n FROM sessions "
                "WHERE killed_by IS NOT NULL GROUP BY kb"
            )
        }
        liveness_rows = conn.execute(
            "SELECT COUNT(*) n FROM sessions WHERE error LIKE '%reaped by liveness-reaper%'"
        ).fetchone()["n"]
        # Recent reaper kills with age-at-kill (updated_at - last_activity_at):
        # a SMALL age means the session looked active right up to the kill — the
        # 'legitimate session killed' signature.
        recent = []
        for r in conn.execute(
            "SELECT id, killed_by, state, created_at, last_activity_at, updated_at, "
            "substr(COALESCE(error,''),1,160) err FROM sessions "
            "WHERE killed_by IN ('idle-reaper','budget','max-park') "
            "ORDER BY updated_at DESC LIMIT 25"
        ):
            recent.append(
                {
                    "id": r["id"],
                    "killed_by": r["killed_by"],
                    "state": r["state"],
                    "created_at": r["created_at"],
                    "last_activity_at": r["last_activity_at"],
                    "updated_at": r["updated_at"],
                    "idle_at_kill_s": _age_at_kill(r["last_activity_at"], r["updated_at"]),
                    "error": r["err"],
                }
            )
        max_park_7d = conn.execute(
            "SELECT COUNT(*) n FROM sessions WHERE killed_by='max-park' "
            "AND updated_at >= datetime('now','-7 days')"
        ).fetchone()["n"]
        return {
            "by_killed_by": by_killer,
            "liveness_reaped_rows": liveness_rows,
            "max_park_last_7d": max_park_7d,
            "recent_reaper_kills": recent,
        }
    finally:
        conn.close()


def _age_at_kill(last_activity: str | None, updated: str | None) -> float | None:
    a = lc.digest  # unused; keep import warm
    del a
    from datetime import datetime

    def _p(v):
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    la, up = _p(last_activity), _p(updated)
    if la is None or up is None:
        return None
    return round(max(0.0, up - la), 1)


def _log_events(max_lines_per_file: int = 4000) -> dict:
    counts = {"kill": 0, "would_kill": 0, "defer": 0, "max_park": 0}
    samples: list[dict] = []
    files: list[str] = []
    for pattern in LOG_GLOBS:
        files.extend(glob.glob(pattern))
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-max_lines_per_file:]
        except OSError:
            continue
        for line in lines:
            m = _EVENT_RE.search(line)
            if not m:
                continue
            kind = m.group(1)
            counts[kind] = counts.get(kind, 0) + 1
            if len(samples) < 20:
                try:
                    samples.append(json.loads(m.group(2)))
                except ValueError:
                    pass
    idle_reaper_events = 0
    if IDLE_REAPER_JSONL.exists():
        for line in IDLE_REAPER_JSONL.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"kind"' in line:
                idle_reaper_events += 1
    return {
        "supervisor_log_counts": counts,
        "supervisor_log_samples": samples,
        "idle_reaper_sh_events": idle_reaper_events,
        "log_files_scanned": len(files),
    }


def snapshot() -> dict:
    rec = {
        "schema": "reaper-tracker.v1",
        "ts": lc.iso_now(),
        "git_sha": lc.git_sha(),
        "db": _db_snapshot(),
        "logs": _log_events(),
    }
    lc.append_jsonl(LEDGER, rec)
    return rec


def summary() -> dict:
    recs = lc.read_jsonl(LEDGER)
    if not recs:
        return {"snapshots": 0, "note": "no snapshots yet — run: reaper_tracker.py snapshot"}
    latest = recs[-1]
    return {
        "snapshots": len(recs),
        "latest_ts": latest["ts"],
        "by_killed_by": latest.get("db", {}).get("by_killed_by", {}),
        "max_park_last_7d": latest.get("db", {}).get("max_park_last_7d"),
        "liveness_reaped_rows": latest.get("db", {}).get("liveness_reaped_rows"),
        "supervisor_log_counts": latest.get("logs", {}).get("supervisor_log_counts", {}),
    }


def legitimacy() -> dict:
    """Heuristic: a reaper kill where the session was ACTIVE within the last
    2 minutes before the kill is a 'looked-legitimate' kill worth investigating.
    max-park kills are always flagged (approval-starvation, not agent-idleness)."""
    snap = _db_snapshot()
    flagged = []
    for k in snap.get("recent_reaper_kills", []):
        idle = k.get("idle_at_kill_s")
        looks_legit = k["killed_by"] == "max-park" or (idle is not None and idle < 120)
        if looks_legit:
            flagged.append({**k, "why": "max-park (approval starvation)" if k["killed_by"] == "max-park"
                            else f"active {idle}s before kill (< 120s)"})
    return {
        "flagged_looked_legitimate": len(flagged),
        "detail": flagged,
        "interpretation": "max-park kills and kills of recently-active sessions are the "
        "'killing legitimate sessions' signal; review each before trusting the reaper.",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reaper tracker (read-only observer)")
    ap.add_argument("cmd", choices=["snapshot", "summary", "legitimacy"], nargs="?", default="snapshot")
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        out = snapshot()
        print(json.dumps(summary(), indent=2))
        print(f"[reaper-tracker] appended snapshot -> {LEDGER}")
    elif args.cmd == "summary":
        out = summary()
        print(json.dumps(out, indent=2))
    else:
        out = legitimacy()
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
