"""Live memcert axis certification bench (manifest carriers MEM-A-01..MEM-H-01).

``live``-marked (excluded by default addopts): one shared OpenRouter benchmark
run of the ``system`` arm over a deterministic fixture world, then one test per
ability axis A-H asserting the axis mean meets its configs/memcert/bars.yaml
bar, plus an instrument-health test (error rows <= 10%).

Skip discipline (tests/livesim/categories/test_northstar_cert.py idiom): when
OPENROUTER_API_KEY is absent the whole module SKIPS with an explicit reason —
never a silent green. A missing bar or summary cell is a pytest.fail, never a
pass (absence is not favorable evidence).

Environment knobs:
    MEMCERT_LIVE_MODEL   (default qwen/qwen3-coder-flash — allow-listed cheap model)
    MEMCERT_LIVE_TRIALS  (default 1)
    MEMCERT_LIVE_SEEDS   (default "42", comma-separated ints)
    MEMCERT_LIVE_SPLIT   (default dev)
    MEMCERT_RESULTS_DIR  (default <repo>/var/memcert/runs; run lands in live-<utc>/)
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

REPO_ROOT = Path(__file__).resolve().parents[2]
BARS_PATH = REPO_ROOT / "configs" / "memcert" / "bars.yaml"


def _load(name: str, rel: str):
    path = REPO_ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: run_bench.py declares dataclasses under
    # `from __future__ import annotations`, and dataclass field-type resolution
    # on 3.12 looks the defining module up via sys.modules[cls.__module__].
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load("memcert_run_bench_live_test", "scripts/memcert/run_bench.py")


def _load_bars() -> dict[str, float]:
    import yaml

    data = yaml.safe_load(BARS_PATH.read_text(encoding="utf-8")) or {}
    bars = data.get("bars") or {}
    return {str(axis): float(bar) for axis, bar in bars.items()}


@pytest.fixture(scope="module")
def live_run() -> dict[str, Any]:
    """ONE shared live benchmark run: system arm, openrouter adapter, all axes."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip(
            "OPENROUTER_API_KEY absent: memcert live bench needs a real OpenRouter "
            "credential and is skipped explicitly (never fake-green)"
        )
    model = os.environ.get("MEMCERT_LIVE_MODEL", "qwen/qwen3-coder-flash")
    trials = int(os.environ.get("MEMCERT_LIVE_TRIALS", "1"))
    seeds = [int(s) for s in os.environ.get("MEMCERT_LIVE_SEEDS", "42").split(",") if s.strip()]
    split = os.environ.get("MEMCERT_LIVE_SPLIT", "dev")
    results_dir = os.environ.get("MEMCERT_RESULTS_DIR")
    base = Path(results_dir) if results_dir else REPO_ROOT / "var" / "memcert" / "runs"
    out_dir = base / f"live-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"

    result = RUNNER.run(
        models=[model],
        arms=["system"],
        axes=list(RUNNER.core.AXES),
        trials=trials,
        split=split,
        seeds=seeds,
        out_dir=out_dir,
        adapter="openrouter",
    )
    if result.refused:
        pytest.fail(
            f"live run refused (unchanged-input guard): {out_dir}/summary.json already "
            "exists — the timestamped out dir should always be fresh"
        )
    return {"result": result, "model": model, "bars": _load_bars()}


def _assert_axis_meets_bar(live_run: dict[str, Any], axis: str) -> None:
    bars = live_run["bars"]
    if axis not in bars:
        pytest.fail(
            f"configs/memcert/bars.yaml carries no bar for axis {axis} "
            "(absence is not favorable evidence)"
        )
    model = live_run["model"]
    summary = live_run["result"].summary or {}
    cell = (summary.get("axes") or {}).get(f"{axis}/system/{model}")
    if not cell or cell.get("mean") is None:
        pytest.fail(
            f"no graded summary cell for axis {axis} arm=system model={model} "
            "(absence is not favorable evidence — the axis was not measured)"
        )
    mean = float(cell["mean"])
    bar = bars[axis]
    assert mean >= bar, (
        f"axis {axis} mean {mean:.4f} is below its certification bar {bar:.4f} "
        f"(n_rows={cell.get('n_rows')}, verdicts={cell.get('verdicts')})"
    )


def test_axis_a_meets_bar(live_run: dict[str, Any]) -> None:
    _assert_axis_meets_bar(live_run, "A")


def test_axis_b_meets_bar(live_run: dict[str, Any]) -> None:
    _assert_axis_meets_bar(live_run, "B")


def test_axis_c_meets_bar(live_run: dict[str, Any]) -> None:
    _assert_axis_meets_bar(live_run, "C")


def test_axis_d_meets_bar(live_run: dict[str, Any]) -> None:
    _assert_axis_meets_bar(live_run, "D")


def test_axis_e_meets_bar(live_run: dict[str, Any]) -> None:
    _assert_axis_meets_bar(live_run, "E")


def test_axis_f_meets_bar(live_run: dict[str, Any]) -> None:
    _assert_axis_meets_bar(live_run, "F")


def test_axis_g_meets_bar(live_run: dict[str, Any]) -> None:
    _assert_axis_meets_bar(live_run, "G")


def test_axis_h_meets_bar(live_run: dict[str, Any]) -> None:
    _assert_axis_meets_bar(live_run, "H")


def test_run_instrument_healthy(live_run: dict[str, Any]) -> None:
    """Adapter-error rows are instrument noise, never scores; cap them at 10%."""
    summary = live_run["result"].summary or {}
    row_count = int(summary.get("row_count") or 0)
    error_count = int(summary.get("error_count") or 0)
    if row_count == 0:
        pytest.fail("live run produced zero result rows — instrument failure, not a pass")
    error_ratio = error_count / row_count
    assert error_ratio <= 0.10, (
        f"instrument unhealthy: {error_count}/{row_count} rows errored "
        f"({error_ratio:.1%} > 10%); parked pairs: {summary.get('parked_pairs')}"
    )
