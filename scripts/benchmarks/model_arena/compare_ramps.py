"""Compare the same endpoint's concurrency ramp across two runs.

    python -m scripts.benchmarks.model_arena.compare_ramps <run-a> <run-b> [provider]

Used to answer "does raising llama.cpp's --parallel actually buy throughput, or
just spread the same tokens across more slots?" Prints aggregate throughput,
per-request speed and TTFT at each level side by side, plus the deltas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RESULTS_ROOT = REPO / "var" / "model-arena"


def load(run_id: str, provider: str) -> dict[int, dict]:
    path = RESULTS_ROOT / run_id / "stress.json"
    if not path.exists():
        raise SystemExit(f"no stress.json for run {run_id}")
    data = json.loads(path.read_text())
    if provider not in data:
        raise SystemExit(f"run {run_id} has no provider {provider}; has {list(data)}")
    return {lv["n"]: lv for lv in data[provider]["levels"]}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) < 2:
        print("usage: compare_ramps <run-a> <run-b> [provider]", file=sys.stderr)
        return 2
    run_a, run_b = args[0], args[1]
    provider = args[2] if len(args) > 2 else "qwen35"

    a, b = load(run_a, provider), load(run_b, provider)
    levels = sorted(set(a) | set(b))

    print(f"provider: {provider}")
    print(f"  A = {run_a}")
    print(f"  B = {run_b}\n")
    print(
        f"{'N':>4} | {'A sys tok/s':>12} {'B sys tok/s':>12} {'delta':>9} | "
        f"{'A per-req':>10} {'B per-req':>10} | {'A ttft':>8} {'B ttft':>8} | {'B err':>6}"
    )
    print("-" * 108)
    for n in levels:
        ra, rb = a.get(n), b.get(n)
        if ra is None or rb is None:
            one = ra if ra is not None else rb
            which = "A" if ra is not None else "B"
            if one is not None:
                print(
                    f"{n:>4} | only in {which}: sys={one['system_tps']} "
                    f"per-req={one['per_req_tps']}"
                )
            continue
        delta = rb["system_tps"] - ra["system_tps"]
        pct = (delta / ra["system_tps"] * 100) if ra["system_tps"] else 0.0
        print(
            f"{n:>4} | {ra['system_tps']:>12.1f} {rb['system_tps']:>12.1f} "
            f"{pct:>+8.0f}% | {ra['per_req_tps']:>10.1f} {rb['per_req_tps']:>10.1f} | "
            f"{_fmt(ra['ttft_p50']):>8} {_fmt(rb['ttft_p50']):>8} | {rb['failed']:>6}"
        )

    peak_a = max(a.values(), key=lambda r: r["system_tps"])
    peak_b = max(b.values(), key=lambda r: r["system_tps"])
    print(
        f"\npeak: A {peak_a['system_tps']:.1f} tok/s @N={peak_a['n']}   "
        f"B {peak_b['system_tps']:.1f} tok/s @N={peak_b['n']}   "
        f"gain {peak_b['system_tps'] / peak_a['system_tps']:.2f}x"
    )
    return 0


def _fmt(v: float | None) -> str:
    return f"{v:.2f}s" if v is not None else "n/a"


if __name__ == "__main__":
    raise SystemExit(main())
