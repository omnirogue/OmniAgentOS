#!/usr/bin/env python3
"""System-wide pass/fail ledger — one rollup across the four verification lanes.

Read-only over already-computed artifacts (never runs tests itself, so it is
safe at any load):

  A. feature-health  var/feature-health/ledger-*.jsonl   per feature x tier
  B. livesim         var/livesim/ledger.jsonl            live-system behavior
  C. health-sentinel var/health-sentinel/latest.json     estate liveness
  D. northstar-cert  var/log/nscert-t{1,2}.log           mission distance

Outputs `var/feature-health/SYSTEM-LEDGER.md` (human view) and appends one
`system-ledger.v1` line to `var/feature-health/system-ledger-YYYYMM.jsonl`
(machine trail: what was green/red at each rollup, so "fixed vs broken" is
answerable over time). Sources that have never run render honestly as absent
("-" / null), never as a coerced pass.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parent.parent

_spec = importlib.util.spec_from_file_location("fh", _HERE / "fh.py")
assert _spec and _spec.loader
fh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fh)

SCHEMA = "system-ledger.v1"


def _now() -> datetime:
    return datetime.now(UTC)


# --- A. feature-health -------------------------------------------------------


TIERS = fh.TIERS

#: This module OWNS no reduction of its own. The stream key, the severity ladder
#: and the worst-per-cell collapse all live in fh.py and are imported here, so
#: the grid `fh.py summary`/LATEST.md render and the grid SYSTEM-LEDGER.md
#: renders cannot drift apart — they are the same two functions with two
#: different formatters on top.
StreamMap = dict[str, dict[str, dict[str, dict[str, Any]]]]


def feature_streams(
    streams: dict[tuple[str, str, str, str], dict[str, Any]] | None = None,
) -> StreamMap:
    """{feature: {tier: {env/stream: newest record of that stream}}}.

    Pass ``streams`` to reuse an already-taken snapshot; omitting it takes one.
    """
    matrix = fh.load_matrix()
    out: StreamMap = {feature: {tier: {} for tier in TIERS} for feature in matrix}
    for (feature, tier, _env, _stream), rec in (
        fh.latest_per_stream() if streams is None else streams
    ).items():
        if feature not in out:  # e.g. the __lane__ pseudo-feature
            out[feature] = {t: {} for t in TIERS}
        if tier not in out[feature]:
            continue
        out[feature][tier][fh.stream_label(rec)] = rec
    return out


def feature_grid(streams: StreamMap | None = None) -> dict[str, dict[str, dict[str, Any] | None]]:
    """{feature: {tier: the WORST newest-per-stream record, or None}}.

    Takes the stream map it should collapse. A caller that renders BOTH the
    collapsed cell and the per-stream map must pass the same snapshot to both
    (see :func:`build_rollup`): two reads of an append-only ledger a runner is
    writing to can return different states, and a row whose cell contradicts
    its own stream map is worse than either answer alone.
    """
    grid: dict[str, dict[str, dict[str, Any] | None]] = {}
    for feature, tiers in (feature_streams() if streams is None else streams).items():
        grid[feature] = {tier: _worst(list(recs.values())) for tier, recs in tiers.items()}
    return grid


def _worst(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The record with the loudest verdict; ties go to the newest of those."""
    if not records:
        return None
    return max(records, key=lambda rec: (fh.verdict_rank(rec), str(rec.get("ts") or "")))


def _cell_verdict(rec: dict[str, Any] | None) -> str:
    """This module's RENDERING of ``fh.verdict_class`` — the class plus its counts.

    The severity decision itself is fh.py's (status first, counts second,
    `MISS` for a run whose declared paths were not all on disk); this only
    decides how it reads in a markdown table.
    """
    klass = fh.verdict_class(rec)
    if klass == "ABSENT":
        return "-"
    if klass in ("ABORT", "ERR"):
        return klass
    if klass == "FAIL":
        assert rec is not None
        failed = int(rec.get("failed") or 0) + int(rec.get("errors") or 0)
        return f"FAIL({max(0, failed - int(rec.get('expected_failures') or 0))})"
    assert rec is not None
    passed = int(rec.get("passed") or 0)
    base = f"PASS({passed})" if passed else "EMPTY"
    return f"MISS+{base}" if klass == "MISS" else base


def verdict_rank(verdict: str) -> int:
    """Rank a RENDERED verdict string, using fh.py's one ladder."""
    for prefix in ("FAIL", "MISS", "PASS"):
        if verdict.startswith(prefix):
            return fh.VERDICT_RANK[prefix]
    return fh.VERDICT_RANK.get(verdict, fh.VERDICT_RANK["ABSENT"])


# --- B. livesim --------------------------------------------------------------


def livesim_rollup(path: Path) -> dict[str, dict[str, Any]]:
    """Newest run per category with pass/fail/other counts inside that run.

    AGGREGATE FIRST, SELECT SECOND. Counting into one live slot per category
    and resetting it whenever a different run's event arrives later loses the
    earlier events of the run that eventually wins: interleave A-fail, B-pass,
    A-pass and the slot reports run A with fail:0 — a run whose failure is
    erased by its own later pass. Two livesim runs CAN interleave in one
    append-only ledger, so the counters are built per (category, run_id) and
    only then is the run with the newest event chosen.
    """
    if not path.exists():
        return {}
    per_run: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn tail — reader survives, per house decoder rule
            cat = rec.get("category") or "unknown"
            run_id, ts = rec.get("run_id", ""), rec.get("ts", "")
            cur = per_run.get((cat, run_id))
            if cur is None:
                cur = per_run[(cat, run_id)] = {
                    "run_id": run_id,
                    "newest_ts": ts,
                    "pass": 0,
                    "fail": 0,
                    "other": 0,
                }
            cur["newest_ts"] = max(cur["newest_ts"], ts)
            status = rec.get("status")
            key = status if status in ("pass", "fail") else "other"
            cur[key] += 1
    runs: dict[str, dict[str, Any]] = {}
    for (cat, run_id), counts in per_run.items():
        best = runs.get(cat)
        # (newest event, then run_id) — a deterministic winner even when two
        # runs of one category share their newest timestamp.
        if best is None or (counts["newest_ts"], run_id) > (best["newest_ts"], best["run_id"]):
            runs[cat] = counts
    return runs


# --- C. health-sentinel ------------------------------------------------------


def sentinel_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "ts": data.get("ts"),
        "overall": data.get("overall"),
        "failing": data.get("failing", []),
        "warning": data.get("warning", []),
        "checks": {c["name"]: c["status"] for c in data.get("checks", []) if isinstance(c, dict)},
    }


# --- D. northstar-cert -------------------------------------------------------


def nscert_latest(log_paths: list[Path], tier: str) -> dict[str, Any] | None:
    """Newest recorder summary for THIS tier across the given run logs.

    The cadence script's default RUN_LOG is nscert-t1.log for every tier, so a
    stray t2 run can land its summary in t1's log (observed 2026-08-11). The
    run_id prefix is the tier truth — filter on it, never on which file the
    line sits in; run_ids embed UTC stamps, so max(run_id) is the newest.
    """
    summary: dict[str, Any] | None = None
    for log_path in log_paths:
        if not log_path.exists():
            continue
        with log_path.open(encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if '"pulse"' not in line and '"verdict"' not in line:
                    continue
                try:
                    candidate = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if not (isinstance(candidate, dict) and "verdict" in candidate):
                    continue
                run_id = str(candidate.get("run_id", ""))
                if not run_id.startswith(f"nscert-{tier}-"):
                    continue
                if summary is None or run_id > str(summary.get("run_id", "")):
                    summary = candidate
    if summary is None:
        return None
    counts: dict[str, int] = {}
    for result in summary.get("results", []):
        verdict = result.get("verdict", "?")
        counts[verdict] = counts.get(verdict, 0) + 1
    return {
        "run_id": summary.get("run_id"),
        "verdict": summary.get("verdict"),
        "reason": summary.get("reason"),
        "pulse": summary.get("pulse", {}),
        "counts": counts,
        "receipt_path": summary.get("receipt_path"),
    }


# --- rollup ------------------------------------------------------------------


def build_rollup(repo: Path) -> dict[str, Any]:
    # ONE snapshot of the ledger, and both views derived from it. Reading twice
    # (once for the collapsed cells, once for the stream map) lets a runner
    # appending between the reads produce a single frozen row whose cell says
    # PASS while its own stream map says FAIL — a self-contradicting receipt,
    # which is worse than either reading alone.
    streams = feature_streams(fh.latest_per_stream())
    return {
        "schema": SCHEMA,
        "ts": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "features": {
            feature: {tier: _cell_verdict(rec) for tier, rec in tiers.items()}
            for feature, tiers in feature_grid(streams).items()
        },
        # The collapsed cell above is a worst-of; this is what it was collapsed
        # FROM. The jsonl is the only copy of "what was red when", and a cell
        # string alone cannot answer which sub-run was the red one.
        "feature_streams": {
            feature: {
                tier: {label: _cell_verdict(rec) for label, rec in sorted(recs.items())}
                for tier, recs in tiers.items()
                if recs
            }
            for feature, tiers in streams.items()
        },
        "livesim": livesim_rollup(repo / "var" / "livesim" / "ledger.jsonl"),
        "sentinel": sentinel_snapshot(repo / "var" / "health-sentinel" / "latest.json"),
        "northstar": {
            tier: nscert_latest(
                [repo / "var" / "log" / "nscert-t1.log", repo / "var" / "log" / "nscert-t2.log"],
                tier,
            )
            for tier in ("t1", "t2")
        },
    }


def render_md(roll: dict[str, Any]) -> str:
    lines = [
        "# System Ledger — pass/fail across every lane",
        "",
        f"Rolled up {roll['ts']} by scripts/feature_health/system_ledger.py (read-only).",
        "Absent cells mean 'never measured', never 'passing'.",
        "",
        "## A. Features (feature-health lane: tier1 mechanical / tier2 live-LLM / tier3 UI+API, incl. production probes)",
        "",
        "| feature | tier1 | tier2 | tier3 |",
        "|---|---|---|---|",
    ]
    for feature, tiers in roll["features"].items():
        lines.append(f"| {feature} | {tiers['tier1']} | {tiers['tier2']} | {tiers['tier3']} |")
    lines += [
        "",
        "Each cell is the WORST of its sub-runs (isolated suite / live probes / Playwright are"
        " independent streams). Severity: FAIL > ERR/ABORT > MISS (incomplete coverage) >"
        " EMPTY > PASS. Multi-stream cells where the collapse hides something:",
        "",
    ]
    hidden = False
    for feature, tiers in sorted((roll.get("feature_streams") or {}).items()):
        for tier, streams in sorted(tiers.items()):
            ranks = {verdict_rank(verdict) for verdict in streams.values()}
            # All streams a plain PASS: the collapsed cell says everything.
            if len(streams) < 2 or (len(ranks) == 1 and ranks == {1}):
                continue
            hidden = True
            detail = ", ".join(f"{label} {verdict}" for label, verdict in sorted(streams.items()))
            lines.append(f"- {feature}/{tier}: {detail}")
    if not hidden:
        lines.append("- (none: every multi-stream cell is green across all its sub-runs)")
    lines += ["", "## B. Live system behavior (livesim, newest run per category)", ""]
    if roll["livesim"]:
        # `other` is rendered, not tracked-and-hidden: a run that is entirely
        # skips/xfails has pass 0 / fail 0 and would otherwise read as an
        # absent run rather than a run that measured nothing.
        lines += [
            "| category | newest run | pass | fail | other |",
            "|---|---|---|---|---|",
        ]
        for cat in sorted(roll["livesim"]):
            r = roll["livesim"][cat]
            lines.append(
                f"| {cat} | {r['newest_ts']} | {r['pass']} | {r['fail']} | {r.get('other', 0)} |"
            )
    else:
        lines.append("(no livesim ledger)")
    lines += ["", "## C. Estate liveness (health-sentinel)", ""]
    sen = roll["sentinel"]
    if sen:
        lines.append(
            f"Overall **{sen['overall']}** at {sen['ts']} — failing: "
            f"{', '.join(sen['failing']) or 'none'}; warning: {', '.join(sen['warning']) or 'none'}"
        )
    else:
        lines.append("(no sentinel snapshot)")
    lines += ["", "## D. Mission distance (NorthStar certification)", ""]
    for tier in ("t1", "t2"):
        ns = roll["northstar"][tier]
        if not ns:
            lines.append(f"- {tier}: (never run here)")
            continue
        pulse = ns.get("pulse") or {}
        dist = pulse.get("nsc.distance")
        dist_s = f"{dist:.2f}" if isinstance(dist, (int, float)) else "?"
        lines.append(
            f"- {tier}: **{ns['verdict']}** run {ns['run_id']} — distance {dist_s}, "
            f"gate_pass {pulse.get('nsc.gate_pass_rate', '?')}, counts {ns['counts'] or ns.get('reason')}"
        )
    lines.append("")
    return "\n".join(lines)


def append_jsonl(roll: dict[str, Any], out_dir: Path) -> Path:
    """flock LOCK_EX + fsync append, mirroring fh.py's ledger discipline."""
    out_dir.mkdir(parents=True, exist_ok=True)
    shard = out_dir / f"system-ledger-{_now().strftime('%Y%m')}.jsonl"
    payload = json.dumps(roll, sort_keys=True)
    with shard.open("a", encoding="utf-8") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        fp.write(payload + "\n")
        fp.flush()
        os.fsync(fp.fileno())
    return shard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write SYSTEM-LEDGER.md and append the jsonl shard (default: print only)",
    )
    parser.add_argument("--repo-root", type=Path, default=REPO)
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    roll = build_rollup(repo)
    md = render_md(roll)
    print(md)
    if args.write:
        out_dir = fh.var_dir()
        (out_dir / "SYSTEM-LEDGER.md").write_text(md, encoding="utf-8")
        shard = append_jsonl(roll, out_dir)
        print(f"[system-ledger] wrote {out_dir / 'SYSTEM-LEDGER.md'} and appended {shard}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
