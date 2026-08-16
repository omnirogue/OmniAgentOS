"""Generate the daily ledger rollup and optionally publish it to the vault."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from typing import Any

from omniagentos.contracts import RunManifest, default_ledger_dir, default_vault_dir


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def build_rollup(manifests: list[RunManifest]) -> dict[str, Any]:
    """Build a stable, JSON-serializable summary of *manifests*."""
    states: Counter[str] = Counter()
    arms: Counter[str] = Counter()
    harnesses: Counter[str] = Counter()
    costs = 0.0
    tokens = 0
    wall_ms = 0
    for manifest in manifests:
        states[_value(manifest.state)] += 1
        if manifest.arm is not None:
            arms[_value(manifest.arm)] += 1
        harnesses[_value(manifest.harness.harness)] += 1
        if manifest.usage is not None:
            costs += manifest.usage.cost_usd or 0.0
            tokens += (manifest.usage.input_tokens or 0) + (manifest.usage.output_tokens or 0)
            wall_ms += manifest.usage.wall_ms
    return {
        "count": len(manifests),
        "by_state": dict(sorted(states.items())),
        "by_arm": dict(sorted(arms.items())),
        "by_harness": dict(sorted(harnesses.items())),
        "total_est_cost_usd": costs,
        "total_est_tokens": tokens,
        "avg_wall_ms": wall_ms / len(manifests) if manifests else 0.0,
    }


def _since_filter(manifests: list[RunManifest], since: str | None) -> list[RunManifest]:
    if since is None:
        return manifests
    return [m for m in manifests if (m.finished_at or m.started_at or "") >= since]


def run_report(*, dry_run: bool = False, since: str | None = None) -> dict[str, Any]:
    """Read manifests and print/publish today's benchmark report."""
    # Local imports keep p05/p06 optional for the default test environment.
    from omniagentos.ledger import read_manifests  # type: ignore[attr-defined]

    manifests = _since_filter(read_manifests(default_ledger_dir()), since)
    rollup = build_rollup(manifests)
    bench_id = f"morning-{date.today().isoformat()}"
    intended_path = f"benchmarks/{bench_id}.md"
    if dry_run:
        print(json.dumps({"rollup": rollup, "intended_note_path": intended_path}, sort_keys=True))
        return rollup

    from omniagentos.vault import render_benchmark_note, write_note  # type: ignore[attr-defined]

    relpath, content = render_benchmark_note(bench_id, manifests)
    write_note(default_vault_dir(), relpath, content)
    print(json.dumps({"rollup": rollup, "note_path": relpath}, sort_keys=True))
    return rollup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print without writing")
    parser.add_argument("--since", help="include manifests at or after this ISO timestamp")
    args = parser.parse_args(argv)
    run_report(dry_run=args.dry_run, since=args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
