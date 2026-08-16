"""Runnable definitions of the two named acceptance suites.

This module is BOTH documentation and an executable entry point, so the
commands in ``docs/acceptance/SUITES.md`` cannot drift from what actually
runs. There is no test in this file; it is a runner.

    Smoke Test Suite      -- after every commit. Fast (<60s), token-free.
    Daily Regression      -- nightly. One representative test per task category.

Run them:

    uv run python -m tests.acceptance.suites.smoke            # smoke
    uv run python -m tests.acceptance.suites.smoke --daily    # daily regression
    uv run python -m tests.acceptance.suites.smoke --list     # print, do not run

Or drive pytest directly with the same selectors (these are the single source
of truth -- the runner above just shells out to exactly these):

    Smoke:  uv run pytest -q tests/acceptance -m "acceptance_smoke"
    Daily:  uv run pytest -q tests/acceptance -m "acceptance_daily"

Both markers are registered in ``pyproject.toml`` and applied in the area test
modules, so ``pytest --collect-only -m acceptance_smoke`` will always show the
real membership rather than a list maintained by hand.
"""

from __future__ import annotations

import shlex
import subprocess
import sys

#: Fast, token-free confidence check to run after EVERY commit.
#: Budget: under 60 seconds on a laptop.
SMOKE_MARKER = "acceptance_smoke"
SMOKE_ARGV: tuple[str, ...] = (
    "-q",
    "tests/acceptance",
    "-m",
    SMOKE_MARKER,
    "--timeout=60",
)

#: One representative test per task category, run nightly. Superset of smoke.
DAILY_MARKER = "acceptance_daily"
DAILY_ARGV: tuple[str, ...] = (
    "-q",
    "tests/acceptance",
    "-m",
    f"{DAILY_MARKER} or {SMOKE_MARKER}",
    "--timeout=300",
)

#: The task categories the daily suite must cover, and the area that covers
#: each. Kept here so a new area cannot be added without deciding where it
#: belongs in the regression matrix.
DAILY_CATEGORIES: dict[str, str] = {
    "trace-and-logging": "tests/acceptance/test_11_trace.py",
    "learning-and-promotion": "tests/acceptance/test_13_learning.py",
    "benchmark-integrity": "tests/acceptance/test_14_benchmark.py",
    "end-to-end-orchestration": "tests/acceptance/test_15_e2e_smoke.py",
    "control-plane-invariants": "tests/acceptance/test_17_control_plane.py",
}


def _run(argv: tuple[str, ...]) -> int:
    return subprocess.call([sys.executable, "-m", "pytest", *argv])  # noqa: S603


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    daily = "--daily" in args
    selected = DAILY_ARGV if daily else SMOKE_ARGV
    if "--list" in args:
        name = "Daily Regression Suite" if daily else "Smoke Test Suite"
        print(f"{name}:")
        # shlex.quote so the printed line is copy-pasteable: the `-m` selector
        # contains spaces and would otherwise be split by the shell.
        print("  uv run pytest " + " ".join(shlex.quote(part) for part in selected))
        if daily:
            for category, path in sorted(DAILY_CATEGORIES.items()):
                print(f"    {category:26s} {path}")
        return 0
    return _run(selected)


if __name__ == "__main__":  # pragma: no cover -- CLI entry point
    raise SystemExit(main())
