#!/usr/bin/env python3
"""known_traps — turn the rejection archive into a brief-embeddable traps block.

    python3 bridge/known_traps.py --top 8          # markdown block for builder briefs
    python3 bridge/known_traps.py --stats          # one-pass rate approximation
    python3 bridge/known_traps.py --top 8 --days 30

WHY: the estate measured SIX defect classes recurring across every planner
lineage, and 64 of 90 gate refusals were mechanics someone had already hit.
Every rejection artifact carries {class, remedy, reason} — but nothing fed
them FORWARD into the next builder's context, so each builder re-discovered
them at full price. This tool is the mechanical feed: the Implementer embeds
its output in every builder brief (PROMPT Step 2), so a builder starts knowing
the last month's refusal classes instead of re-committing them.

Sources (read-only, best-effort — a missing source is reported, never fatal):
  * <queue>/rejected/*.json                — this project's rejections
  * ~/.omniagentos/ops/Research/_estate/rejections.jsonl — cross-project conclusions
  * <queue>/ledger.jsonl                   — merged/rejected events for --stats

The one-pass rate is an APPROXIMATION and labeled as such: artifact ids change
when payloads are corrected (supersedes chains), so per-id retry tracking
undercounts. Rejections-per-merge in the window is the honest coarse signal.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess

# The four sibling readers (integrity, janitor, pr_reconcile, file_proposal)
# all put this file's own directory on sys.path and import siblings flat,
# because these modules are invoked as SCRIPTS and `bridge` is then not
# resolvable as a package. This module had no shim; it needs one to reuse
# the shared ledger decoder rather than grow a fifth private copy.
import sys  # noqa: E402
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger_read import parse_events  # noqa: E402

DEFAULT_QUEUE = Path.home() / "OmniAgentOS" / "var" / "loopqueue"
ESTATE_REJECTIONS = Path.home() / "Work" / "Ops" / "Research" / "_estate" / "rejections.jsonl"


def _parse_ts(stamp: str) -> float | None:
    # Stamps here are UTC ("...Z"). timegm, never mktime: mktime reads the
    # struct as LOCAL time, shifting every event by the UTC offset — measured
    # here as a 4h hole that silently excluded the newest events from windows.
    import calendar
    try:
        cleaned = stamp.replace("Z", "").split(".")[0]
        return calendar.timegm(time.strptime(cleaned, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, AttributeError, OverflowError):
        return None


def load_rejections(queue: Path, since: float) -> list:
    out = []
    rej_dir = queue / "rejected"
    if rej_dir.is_dir():
        for path in rej_dir.glob("*.json"):
            try:
                rec = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            ts = _parse_ts(rec.get("at") or rec.get("rejected_at") or rec.get("ts") or "")
            if ts is None or ts >= since:
                out.append(rec)
    if ESTATE_REJECTIONS.exists():
        try:
            for line in ESTATE_REJECTIONS.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                for rec in parse_events(line)[0]:
                    ts = _parse_ts(rec.get("at") or rec.get("ts") or "")
                    if ts is None or ts >= since:
                        out.append(rec)
        except OSError:
            pass
    return out


def traps_block(rejections: list, top: int) -> str:
    by_class: dict = defaultdict(list)
    for rec in rejections:
        cls = str(rec.get("class") or _event_detail(rec).get("class") or "unclassified")
        by_class[cls].append(rec)

    ranked = sorted(by_class.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
    if not ranked:
        return ("KNOWN TRAPS: no rejection records found in the window — either the "
                "archive is empty or the sources are unreadable. Absence of traps "
                "listed is NOT absence of traps.")

    lines = ["KNOWN TRAPS — recurring refusal classes from the rejection archive.",
             "A candidate that re-commits one of these is refused at full price; "
             "check your work against each BEFORE writing the envelope.", ""]
    for cls, recs in ranked:
        remedies = Counter(str(r.get("remedy") or "").strip()
                           for r in recs if r.get("remedy"))
        remedy = remedies.most_common(1)[0][0] if remedies else "see archive"
        reasons = [str(r.get("reason") or "").strip() for r in recs if r.get("reason")]
        example = max(reasons, key=len, default="")[:220]
        lines.append(f"* {cls} (x{len(recs)}) — usual remedy: {remedy}")
        if example:
            lines.append(f"    e.g. {example}")
    return "\n".join(lines)


def stats(queue: Path, since: float) -> str:
    merged = rejected = 0
    per_id_rejects: Counter = Counter()
    ledger = queue / "ledger.jsonl"
    if not ledger.exists():
        return "stats: no ledger.jsonl found — nothing to measure (not a clean bill)"
    for raw_line in re.split(rb"\r\n?|\n", ledger.read_bytes()):
        line = raw_line.decode()
        for rec in parse_events(line)[0]:
            ts = _parse_ts(rec.get("ts") or rec.get("at") or "")
            if ts is not None and ts < since:
                continue
            event = rec.get("event")
            if event == "merged":
                merged += 1
            elif event == "rejected":
                rejected += 1
                if rec.get("id"):
                    per_id_rejects[rec["id"]] += 1
    repeat_ids = sum(1 for c in per_id_rejects.values() if c > 1)
    ratio = f"{rejected / merged:.1f}" if merged else "n/a (0 merges)"
    return ("one-pass signal (APPROXIMATE — corrected payloads change ids, so "
            "per-id retries undercount):\n"
            f"  merged events:            {merged}\n"
            f"  rejected events:          {rejected}\n"
            f"  rejections per merge:     {ratio}\n"
            f"  ids rejected >1x (same input re-tried): {repeat_ids}\n"
            "  Falling rejections-per-merge over successive windows = the "
            "traps feed is working; rising = new failure classes, read the archive.")


OFFLOAD_RECEIPTS = Path.home() / "Work" / "Ops" / "agent-inbox" / "offload-receipts.jsonl"


import re as _re  # noqa: E402


def _event_detail(ev: dict) -> dict:
    """`detail` as a dict, whatever the record actually holds.

    `ev.get("detail") or {}` is NOT enough: a non-empty string is truthy, so it
    survives the `or` and then has no `.get`. That exact shape took the queue
    publisher down estate-wide on 2026-08-09 when a producer appended `detail`
    as a string. `ledger.jsonl` is append-only, so those records are permanent
    and every reader has to tolerate them.

    A malformed detail costs the event its detail-derived fields and NOTHING
    else. The caller still sees the event, so it still counts for status and
    WIP -- dropping it would under-count WIP and invent headroom, which is the
    dangerous direction.
    """
    d = ev.get("detail")
    return d if isinstance(d, dict) else {}

_SHA_RE = _re.compile(r"\b[0-9a-f]{8,40}\b")


def _merge_sha(rec: dict) -> str | None:
    detail = rec.get("detail")
    if isinstance(detail, dict):
        for key in ("merge_sha", "sha", "tip"):
            val = str(detail.get(key) or "")
            if _SHA_RE.fullmatch(val):
                return val
        blob = json.dumps(detail)
    else:
        blob = str(detail or "")
    match = _SHA_RE.search(blob)
    return match.group(0) if match else None


def _volume(repo: Path, windows: list) -> list:
    """Lines/files per landing: diff between CONSECUTIVE landing SHAs, chained
    across window boundaries so the first landing of a window diffs against the
    last landing before it. A SHA git cannot resolve is counted unmeasured —
    never as zero lines."""
    all_shas = sorted((ts, sha) for win in windows for ts, sha in win["merge_shas"])
    stats_by_sha: dict = {}
    prev = None
    for _ts, sha in all_shas:
        if prev and prev != sha:
            try:
                proc = subprocess.run(
                    ["git", "-C", str(repo), "diff", "--numstat", f"{prev}..{sha}"],
                    capture_output=True, text=True, timeout=30, check=False)
            except (OSError, subprocess.TimeoutExpired):
                proc = None
            if proc and proc.returncode == 0:
                adds = dels = files = 0
                for line in proc.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) == 3:
                        files += 1
                        if parts[0].isdigit():
                            adds += int(parts[0])
                        if parts[1].isdigit():
                            dels += int(parts[1])
                stats_by_sha[sha] = {"adds": adds, "dels": dels, "files": files}
        prev = sha
    for win in windows:
        sizes = [stats_by_sha[s] for _t, s in win["merge_shas"] if s in stats_by_sha]
        measured = len(sizes)
        win["volume"] = {
            "measured_landings": measured,
            "unmeasured_landings": len(win["merge_shas"]) - measured,
            "lines_added": sum(s["adds"] for s in sizes) if sizes else None,
            "lines_deleted": sum(s["dels"] for s in sizes) if sizes else None,
            "files_touched": sum(s["files"] for s in sizes) if sizes else None,
            "median_lines_per_landing": (sorted(s["adds"] + s["dels"] for s in sizes)
                                         [measured // 2] if sizes else None),
            "max_lines_per_landing": (max(s["adds"] + s["dels"] for s in sizes)
                                      if sizes else None),
        }
    return windows


def _window_events(queue: Path, start: float, end: float) -> dict:
    counts: Counter = Counter()
    claimed_at: dict = {}
    cycle_minutes: list = []
    merge_shas: list = []
    ledger = queue / "ledger.jsonl"
    if not ledger.exists():
        return {"error": "no ledger.jsonl"}
    for raw_line in re.split(rb"\r\n?|\n", ledger.read_bytes()):
        line = raw_line.decode()
        for rec in parse_events(line)[0]:
            ts = _parse_ts(rec.get("ts") or rec.get("at") or "")
            if ts is None:
                continue
            event, ident = rec.get("event"), rec.get("id")
            # Claims are remembered regardless of window so a claim just before the
            # window still yields a cycle time for a merge inside it.
            if event == "claimed" and ident:
                claimed_at.setdefault(ident, ts)
            if not (start <= ts < end):
                continue
            counts[event] += 1
            if event == "merged":
                sha = _merge_sha(rec)
                if sha:
                    merge_shas.append((ts, sha))
            if event in ("merged", "completed", "rejected", "closed") and ident and ident in claimed_at:
                # `completed` is a terminal event too (ruling D13a), and `closed` is
                # the finding-side terminal — findings are claimable, so claim→closed
                # is the common finding cycle and belongs in this stat alongside
                # merged/rejected.
                cycle_minutes.append((ts - claimed_at[ident]) / 60.0)
    hours = max((end - start) / 3600.0, 0.01)
    merged = counts.get("merged", 0)
    return {
        "merged": merged, "rejected": counts.get("rejected", 0),
        "proposed": counts.get("proposed", 0), "claimed": counts.get("claimed", 0),
        "merges_per_hour": round(merged / hours, 2),
        "rej_per_merge": round(counts.get("rejected", 0) / merged, 1) if merged else None,
        "median_claim_to_terminal_min": (round(sorted(cycle_minutes)[len(cycle_minutes) // 2], 1)
                                         if cycle_minutes else None),
        "merge_shas": merge_shas,
    }


def _queue_depth_delta(start: float, end: float) -> dict | None:
    """First vs last telemetry sample in the window: is the queue draining?"""
    if not OFFLOAD_RECEIPTS.exists():
        return None
    first = last = None
    try:
        for line in OFFLOAD_RECEIPTS.read_text().splitlines():
            for rec in parse_events(line)[0]:
                if rec.get("kind") not in ("advise", "sample") or not rec.get("queued"):
                    continue
                ts = _parse_ts(rec.get("at") or "")
                if ts is None or not (start <= ts < end):
                    continue
                if first is None:
                    first = rec["queued"]
                last = rec["queued"]
    except OSError:
        return None
    if not first or not last or first is last:
        return None
    keys = ("candidates", "proposals")
    return {k: {"start": first.get(k), "end": last.get(k)} for k in keys}


def throughput(queue: Path, hours: float) -> str:
    now = time.time()
    cur = _window_events(queue, now - hours * 3600, now)
    prev = _window_events(queue, now - 2 * hours * 3600, now - hours * 3600)
    if "error" in cur:
        return f"throughput: {cur['error']}"
    _volume(queue.parent.parent, [prev, cur])
    lines = [f"THROUGHPUT — last {hours:g}h vs the {hours:g}h before it "
             "(ledger is the source of truth; a merge is a landing on main)"]
    fields = (("merged", "merges"), ("merges_per_hour", "merges/hour"),
              ("rejected", "rejections"), ("rej_per_merge", "rejections per merge"),
              ("claimed", "claims"), ("proposed", "proposals filed"),
              ("median_claim_to_terminal_min", "median claim->terminal (min)"))
    for key, label in fields:
        lines.append(f"  {label:<30} {cur.get(key)}   (prev: {prev.get(key)})")
    vol_fields = (("lines_added", "lines added"), ("lines_deleted", "lines deleted"),
                  ("files_touched", "files touched"),
                  ("median_lines_per_landing", "median lines/landing"),
                  ("max_lines_per_landing", "biggest landing (lines)"))
    for key, label in vol_fields:
        lines.append(f"  {label:<30} {cur['volume'].get(key)}   "
                     f"(prev: {prev['volume'].get(key)})")
    for win, name in ((cur, "current"), (prev, "previous")):
        if win["volume"]["unmeasured_landings"]:
            lines.append(f"  NOTE: {win['volume']['unmeasured_landings']} {name}-window "
                         "landing(s) unmeasured (SHA unresolvable) — volume is a floor, "
                         "not a total")
    drain = _queue_depth_delta(now - hours * 3600, now)
    if drain:
        for k, v in drain.items():
            arrow = "DRAINING" if (v["end"] or 0) < (v["start"] or 0) else \
                    "growing" if (v["end"] or 0) > (v["start"] or 0) else "flat"
            lines.append(f"  queue {k:<24} {v['start']} -> {v['end']}   ({arrow})")
    else:
        lines.append("  queue depth: no telemetry samples in window (advisor coverage "
                     "starts 2026-08-08 ~14:50Z)")
    lines.append("  Read: throughput is improving iff merges/hour is up AND "
                 "rejections-per-merge is flat-or-down AND queues drain. Any one "
                 "alone can be gamed by the others.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rejection archive -> builder-brief traps block")
    ap.add_argument("--queue", default=str(DEFAULT_QUEUE))
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--throughput", action="store_true",
                    help="current vs previous window: merges/hour, cycle time, queue drain")
    ap.add_argument("--hours", type=float, default=24,
                    help="window size for --throughput")
    args = ap.parse_args(argv)
    queue = Path(args.queue).expanduser()
    since = time.time() - args.days * 86400
    if args.throughput:
        print(throughput(queue, args.hours))
        return 0
    if args.stats:
        print(stats(queue, since))
        return 0
    print(traps_block(load_rejections(queue, since), args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
