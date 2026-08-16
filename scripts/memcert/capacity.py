#!/usr/bin/env python3
"""memcert MEM-I capacity-curve carrier (DESIGN §3 axis I, built in v2).

Axis I asks: how does memory quality degrade as the store grows? The
deterministic half measures retrieval SUFFICIENCY (scripts/memcert/sufficiency.py)
across the generator's scales — S (8 main + 6 distractor sessions), M (16+12),
L (32+24) — for the production-shape arms. The live half (axis scores vs
scale) rides the cadence via run_bench ``--scale``; this module is the
network-free curve that certifies on every merge.

The certified properties (tests/memcert/test_capacity.py):
- bounded degradation: the system arm's pooled sufficiency at L stays within a
  configured fraction of its S value (a memory system whose retrieval collapses
  4x the corpus is capacity-broken);
- the axis-D recency spine holds at EVERY scale.

CLI::

    python scripts/memcert/capacity.py --seeds 42 --arms system \
        [--scales S,M,L] [--budget 12000] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:  # imported as a package member: `scripts.memcert.capacity`
    from . import sufficiency
except ImportError:  # pragma: no cover - bare-script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sufficiency  # type: ignore[no-redef]


def capacity_curve(
    seeds: list[int],
    arms: list[str],
    *,
    scales: tuple[str, ...] = ("S", "M", "L"),
    budget_tokens: int = 12000,
) -> dict[str, Any]:
    """Per-arm, per-scale sufficiency summaries plus pooled means."""
    curve: dict[str, Any] = {
        "seeds": seeds,
        "budget_tokens": budget_tokens,
        "scales": list(scales),
        "arms": {arm: {} for arm in arms},
    }
    for scale in scales:
        result = sufficiency.run_sufficiency(
            seeds, arms, scale=scale, budget_tokens=budget_tokens
        )
        for arm in arms:
            summary = result["arms"][arm]["summary"]
            total_n = sum(cell["n"] for cell in summary.values())
            pooled = (
                sum(cell["sufficiency"] * cell["n"] for cell in summary.values()) / total_n
                if total_n
                else 0.0
            )
            curve["arms"][arm][scale] = {
                "summary": summary,
                "pooled_sufficiency": round(pooled, 4),
            }
    return curve


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="memcert MEM-I capacity curve (deterministic)")
    p.add_argument("--seeds", default="42", help="comma-separated world seeds")
    p.add_argument("--arms", default="system", help="comma-separated arms")
    p.add_argument("--scales", default="S,M,L", help="comma-separated scales")
    p.add_argument("--budget", type=int, default=12000)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)

    seeds = [int(s) for s in str(args.seeds).split(",") if s.strip()]
    arms = [a.strip() for a in str(args.arms).split(",") if a.strip()]
    scales = tuple(s.strip() for s in str(args.scales).split(",") if s.strip())
    if not seeds or not arms or not scales:
        print("refused: need at least one seed, arm, and scale", file=sys.stderr)
        return 2

    curve = capacity_curve(seeds, arms, scales=scales, budget_tokens=args.budget)
    for arm in arms:
        cells = " ".join(
            f"{scale}={curve['arms'][arm][scale]['pooled_sufficiency']:.2f}"
            for scale in scales
        )
        print(f"capacity {arm}: {cells}")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(curve, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


__all__ = ["capacity_curve"]

if __name__ == "__main__":
    raise SystemExit(main())
