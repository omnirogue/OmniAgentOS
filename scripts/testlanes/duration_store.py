"""Persistent per-test duration store + LPT shard planner.

Why this exists (Phase 4, docs/testing/TESTING_SPEED_PLAN.md): this repo already writes
JUnit XML with per-test durations on every fast-lane / lane run (var/test-reports/*.xml).
Nothing before this reads it back. This module does two things:

  1. `update`  -- fold every testcase's <time> from one or more JUnit files into a
     persistent JSON map (var/test-reports/duration-store.json, gitignored: this is a
     local-machine cache, not project state). Durations here vary run-to-run by up to
     3x under load (measured, TESTING.md), so a single last-seen value is not honest --
     the store keeps an exponential moving average (EWMA) plus the raw last-seen value
     and a sample count, and callers can pick whichever they trust more.

  2. `plan`    -- longest-processing-time-first (LPT) bin-packing: sort tests by known
     duration descending, repeatedly assign the next test to whichever shard currently
     has the smallest total. This is the classic ~4/3-approximation to the optimal
     makespan and it is the entire reason a paid test-splitting service (Knapsack Pro
     etc., see TESTING_SPEED_PLAN.md section 6) is unnecessary here: the input this repo
     needs (a persistent duration history) already exists in var/test-reports/*.xml, it
     was just never read.

Unknown tests (no duration history) are assigned the median known duration, not zero --
treating an unknown test as free is exactly the "unmeasured cost is not zero cost"
mistake this repo's own acceptance suite guards against elsewhere
(test_unmeasured_cost_is_null_not_zero, tests/acceptance/test_14_benchmark.py).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_STORE = REPO / "var" / "test-reports" / "duration-store.json"
DEFAULT_GLOB = "var/test-reports/*.xml"
EWMA_ALPHA = 0.3  # weight on the newest sample; higher = adapts faster, noisier


def _node_id(testcase: ET.Element) -> str:
    classname = testcase.get("classname", "")
    name = testcase.get("name", "")
    return f"{classname}::{name}" if classname else name


def load_store(path: Path = DEFAULT_STORE) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt local cache should never break the lane it's trying to speed up.
        return {}


def save_store(store: dict[str, dict[str, Any]], path: Path = DEFAULT_STORE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ingest_junit(store: dict[str, dict[str, Any]], xml_path: Path) -> int:
    """Merge one JUnit file's testcase durations into `store` in place. Returns the
    number of testcases folded in (0 for a missing/unparseable file -- callers may be
    globbing over a directory that legitimately has nothing yet)."""
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError):
        return 0
    n = 0
    for testcase in root.iter("testcase"):
        node_id = _node_id(testcase)
        if not node_id:
            continue
        duration = float(testcase.get("time") or 0.0)
        prev = store.get(node_id)
        if prev is None:
            store[node_id] = {"ewma": duration, "last": duration, "n": 1}
        else:
            prev["ewma"] = round(EWMA_ALPHA * duration + (1 - EWMA_ALPHA) * prev["ewma"], 4)
            prev["last"] = duration
            prev["n"] = prev.get("n", 1) + 1
        n += 1
    return n


def update(xml_paths: list[Path], store_path: Path = DEFAULT_STORE) -> dict[str, dict[str, Any]]:
    store = load_store(store_path)
    total = 0
    for xml_path in xml_paths:
        total += ingest_junit(store, xml_path)
    save_store(store, store_path)
    print(
        f"ingested {total} testcase durations from {len(xml_paths)} file(s) -> {store_path}",
        file=sys.stderr,
    )
    return store


def duration_of(store: dict[str, dict[str, Any]], node_id: str, fallback: float) -> float:
    entry = store.get(node_id)
    if entry is None:
        return fallback
    return float(entry.get("ewma", entry.get("last", fallback)))


def known_fraction(node_ids: list[str], store: dict[str, dict[str, Any]]) -> float | None:
    """Share of `node_ids` that have a recorded duration -- how much of a plan is measured
    rather than guessed.

    Returns **None** for an empty input, not 1.0. A rate over an empty set is undefined;
    reporting "100% of nothing is known" is how a zero-work run scores itself perfect (see
    `test_unmeasured_cost_is_null_not_zero` in tests/acceptance/test_14_benchmark.py).
    """
    if not node_ids:
        return None
    return round(sum(1 for n in node_ids if n in store) / len(node_ids), 4)


def lpt_plan(node_ids: list[str], shards: int, store: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Longest-processing-time-first bin packing over `node_ids` into `shards` buckets."""
    if shards < 1:
        raise ValueError("shards must be >= 1")
    known = [duration_of(store, n, -1.0) for n in node_ids]
    known_positive = [d for d in known if d >= 0]
    fallback = statistics.median(known_positive) if known_positive else 0.01
    weighted = sorted(
        ((duration_of(store, n, fallback), n) for n in node_ids),
        key=lambda pair: pair[0],
        reverse=True,
    )
    buckets: list[list[str]] = [[] for _ in range(shards)]
    totals = [0.0] * shards
    for duration, node_id in weighted:
        idx = min(range(shards), key=lambda i: totals[i])
        buckets[idx].append(node_id)
        totals[idx] += duration
    return buckets


def _cmd_update(args: argparse.Namespace) -> int:
    xml_paths = [Path(p) for p in args.xml] if args.xml else sorted(REPO.glob(args.glob))
    xml_paths = [p for p in xml_paths if p.exists()]
    if not xml_paths:
        print(f"no JUnit files matched (glob={args.glob!r}, --xml={args.xml})", file=sys.stderr)
        return 1
    update(xml_paths, Path(args.store))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    store = load_store(Path(args.store))
    if args.tests:
        node_ids = [
            line.strip() for line in Path(args.tests).read_text().splitlines() if line.strip()
        ]
    else:
        node_ids = [line.strip() for line in sys.stdin if line.strip()]
    if not node_ids:
        print("no test node ids on stdin/--tests", file=sys.stderr)
        return 1
    buckets = lpt_plan(node_ids, args.shards, store)
    share = known_fraction(node_ids, store)
    print(
        "# measured share of this plan: "
        + ("undefined (no tests requested)" if share is None else f"{share:.1%}")
        + " -- the rest is estimated at the median known duration, never at zero",
        file=sys.stderr,
    )
    for i, bucket in enumerate(buckets):
        total = sum(duration_of(store, n, 0.0) for n in bucket)
        print(f"# shard {i}: {len(bucket)} tests, ~{total:.1f}s known/estimated", file=sys.stderr)
        out_path = Path(args.out_prefix + f"{i}.txt") if args.out_prefix else None
        text = "\n".join(bucket) + ("\n" if bucket else "")
        if out_path:
            out_path.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    store = load_store(Path(args.store))
    ranked = sorted(store.items(), key=lambda kv: kv[1].get("ewma", 0.0), reverse=True)
    for node_id, entry in ranked[: args.top]:
        print(f"{entry.get('ewma', 0.0):8.3f}s  (n={entry.get('n', 0):>4})  {node_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", default=str(DEFAULT_STORE), help="path to the duration store JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_update = sub.add_parser("update", help="fold JUnit XML durations into the store")
    p_update.add_argument("--xml", nargs="*", help="explicit JUnit XML path(s)")
    p_update.add_argument(
        "--glob", default=DEFAULT_GLOB, help="glob (relative to repo root) if --xml is not given"
    )
    p_update.set_defaults(func=_cmd_update)

    p_plan = sub.add_parser("plan", help="LPT-shard a list of test node ids")
    p_plan.add_argument("--shards", type=int, required=True)
    p_plan.add_argument("--tests", help="file of node ids, one per line (default: stdin)")
    p_plan.add_argument(
        "--out-prefix",
        default=None,
        help="write shard-<i>.txt files instead of stdout, e.g. var/test-reports/shard-",
    )
    p_plan.set_defaults(func=_cmd_plan)

    p_stats = sub.add_parser("stats", help="print the slowest known tests in the store")
    p_stats.add_argument("--top", type=int, default=25)
    p_stats.set_defaults(func=_cmd_stats)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
