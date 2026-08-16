"""Composite lane runner for `make test-dev` / `make test-pr`.

Why a Python runner instead of a single pytest invocation
-----------------------------------------------------------
A lane is not one pytest selection, it is three, and they cannot share one `-m` expression
or one worker count:

  1. **impacted, parallel** -- the test files `scripts.testlanes.impacted` selects, minus the
     ones that are not xdist-safe. Run under `-n`.
  2. **impacted, serial**   -- `tests/doctrine` and anything else in
     `impacted.SERIAL_TEST_PREFIXES`. Its revert/counterfeit harness mutates shared fixture
     files in place; `pytest -n 8 tests/doctrine` produced 7 cross-worker mutation races and
     left `tests/doctrine/_fixtures/subject.py` dirty in the working tree. Selecting it and
     handing it to xdist anyway (which the first version of this runner did) is worse than
     not selecting it: it corrupts the checkout. It runs in its own SERIAL subprocess.
  3. **critical** -- `tests/acceptance` restricted to the lane's acceptance marker. pytest's
     `-m` filters uniformly across every path in one invocation, so this cannot be folded
     into (1).

Marker composition is mandatory in every step
---------------------------------------------
pytest's `-m` is store-not-append: a command-line `-m` REPLACES `pyproject.toml`'s `addopts`
expression. The first version of this runner passed the critical step a bare
`-m acceptance_smoke`, which silently re-admitted `live`/`perf`/`counterfeit_gate` tests one
layer below the very bug the lane architecture exists to prevent. Every step now goes through
`compose_markers()`, and `tests/scripts/test_lane_marker_superset.py` proves the composed
expression each lane actually passes to pytest is a superset of the default exclusion.

Exit codes are load-bearing
---------------------------
A lane that reports success because a subprocess was SIGKILLed is worse than no lane. Python
reports a killed child as a NEGATIVE returncode, so `max([-9, 0])` == 0 -- the first version
of this runner turned a killed run into a green one. `lane_exit_code()` normalises signals to
128+N, and a step that claims success without leaving a parseable JUnit part is a failure too.

    test-dev  target p50 <=5s   impacted (-n8)  + serial bucket + acceptance_smoke (-n4)
    test-pr   target p50 <=60s  impacted (-n16) + serial bucket + acceptance_smoke/daily (-n8)

Both lanes are scoped by what changed, so their cost is a function of the change set, not a
constant. See TESTING.md for the per-scenario measurements.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from scripts.testlanes.impacted import DEFERRED_TEST_PREFIXES, impacted

REPO = Path(__file__).resolve().parents[2]

# DERIVED, never duplicated. A literal copy of this list has now drifted THREE times: it
# lives in pyproject.toml addopts, in the Makefile's FAST_LANE_MARKERS, and it used to be
# hardcoded here too -- which is how `feature_health` came to be excluded by the first two
# and admitted by this one, caught by tests/scripts/test_lane_marker_superset.py during
# integration. Deriving it from addopts makes the superset property structural rather than
# a thing three files have to remember. The lane additionally excludes `smoke` (serial lanes
# own process-spawning tests).
# Why this matters at all: pytest's `-m` is store-not-append, so a command-line -m REPLACES
# addopts instead of adding to it; a lane whose expression is not a superset silently
# readmits live/perf/counterfeit tests.
def _compose_fast_lane_markers() -> str:
    """`not smoke and (<pyproject addopts expression>)` -- a superset by construction."""
    import tomllib

    with (REPO / "pyproject.toml").open("rb") as fh:
        addopts = tomllib.load(fh)["tool"]["pytest"]["ini_options"]["addopts"]
    match = re.search(r"-m\s*'([^']*)'|-m\s*\"([^\"]*)\"", addopts)
    if not match:  # addopts carries no marker expression; excluding smoke is still correct
        return "not smoke"
    expr = (match.group(1) or match.group(2)).strip()
    return f"not smoke and ({expr})"


FAST_LANE_MARKERS = _compose_fast_lane_markers()

# pytest's own "no tests were collected" exit status. For an impacted subset that the lane's
# markers happen to deselect entirely, that is an empty selection, not a failure.
PYTEST_NO_TESTS_COLLECTED = 5

# EX_SOFTWARE: the step exited 0 but produced no parseable JUnit part, so we cannot know what
# ran. Treated as a lane failure rather than assumed green.
EXIT_UNTRUSTWORTHY_REPORT = 70


def compose_markers(extra: str | None = None) -> str:
    """Compose the lane's base exclusion with an optional additional selection.

    NEVER build a `-m` string any other way in this module. `-m` replaces `addopts`, so
    `-m "acceptance_smoke"` on its own re-admits every marker `pyproject.toml` excludes.
    """
    if not extra:
        return FAST_LANE_MARKERS
    return f"({FAST_LANE_MARKERS}) and ({extra})"


LANES: dict[str, dict[str, object]] = {
    "dev": {
        # Measured: some package dirs are light (tests/scope collects in ~0.5s) but
        # execute heavier than that (tests/scope itself: 20.5s serial vs 9.6s at -n4 --
        # subprocess/git-heavy tests, not a collection-floor problem). A small worker
        # pool pays off more often than it costs, so this is NOT serial.
        "impacted_workers": 8,
        "critical_marker": "acceptance_smoke",
        "critical_workers": 4,
        "critical_timeout": "60",
        # A pre-commit loop is explicitly not a gate: when the impact analysis cannot
        # explain a changed file, say so loudly and keep going. (A too-broad closure still
        # escalates -- that is a cost decision, not a coverage one. See run_lane().)
        "escalate_on_incomplete": False,
    },
    "pr": {
        # Upper bound, clamped to the host's cores in run_lane(): 16 was
        # hardcoded for the 24-core estate runner and 4x-oversubscribed the
        # 4-vCPU GitHub runners — module-scoped index builds and nested-pytest
        # spawners then blow their 180s timeouts (measured 2026-08-11..14).
        "impacted_workers": 16,
        "critical_marker": "acceptance_smoke or acceptance_daily",
        "critical_workers": 8,
        "critical_timeout": "300",
        # test-pr is the last thing that runs before review sees the change, so an
        # incomplete impact analysis escalates it to the whole fast-lane tree instead of
        # quietly testing a subset that provably does not cover the diff.
        "escalate_on_incomplete": True,
    },
}


@dataclass
class Step:
    name: str
    returncode: int
    junit: Path
    expect_tests: bool = True


def _run_pytest(name: str, args: list[str], junit: Path, expect_tests: bool = True) -> Step:
    # sys.executable, not `uv run pytest`: this module is already invoked as `uv run
    # python -m scripts.testlanes.run_lane`, which resolves/activates the venv once.
    # Re-shelling through `uv run` per subprocess pays a second sync-check per call for
    # no benefit -- measured ~0.1s each, and a lane makes 2-3 of these calls.
    cmd = [sys.executable, "-m", "pytest", "-q", *args, f"--junitxml={junit}"]
    print(f"+ [{name}] {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, cwd=REPO, check=False)
    return Step(name=name, returncode=proc.returncode, junit=junit, expect_tests=expect_tests)


def junit_testcase_count(path: Path) -> int | None:
    """Number of <testcase> elements, or None if the file is missing/unparseable.

    None means "we do not know what this step ran" -- never 0, which would read as "it ran
    nothing successfully".
    """
    if not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    return sum(1 for _ in root.iter("testcase"))


def step_exit_code(step: Step) -> int:
    """Normalised, never-optimistic exit status for one lane step.

    * A killed child comes back from subprocess as a negative returncode; POSIX shells
      report that as 128+signal. Leaving it negative made `max()` pick 0 over -9, i.e.
      turned SIGKILL into SUCCESS.
    * pytest's exit 5 (nothing collected) is an empty selection, not a failure -- but the
      step must still have produced a report.
    * A step that claims success without a parseable JUnit part cannot be trusted to have
      run anything, so it fails.
    """
    rc = step.returncode
    if rc < 0:
        return 128 + (-rc)
    count = junit_testcase_count(step.junit)
    if rc == PYTEST_NO_TESTS_COLLECTED:
        return 0 if count is not None else EXIT_UNTRUSTWORTHY_REPORT
    if rc == 0 and (count is None or (step.expect_tests and count == 0)):
        return EXIT_UNTRUSTWORTHY_REPORT
    return rc


def lane_exit_code(steps: list[Step]) -> int:
    """Worst normalised status across the lane. No steps at all is itself a failure: a lane
    that ran nothing has not shown that anything passes."""
    if not steps:
        return EXIT_UNTRUSTWORTHY_REPORT
    return max(step_exit_code(step) for step in steps)


def merge_junit(parts: list[Path], dest: Path) -> list[Path]:
    """Concatenate each part's <testsuite> elements under one <testsuites> root, so
    downstream readers (scripts/bench_lane.py's parse_junit, the duration store) see one
    artifact per lane instead of having to know how many subprocesses a lane used.

    Returns the parts that could NOT be merged. Callers must treat a non-empty result as a
    failure -- silently skipping a missing part is how a killed run reports success.
    """
    combined = ET.Element("testsuites")
    missing: list[Path] = []
    for part in parts:
        try:
            root = ET.parse(part).getroot()
        except (ET.ParseError, OSError):
            missing.append(part)
            continue
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        combined.extend(suites)
    dest.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(combined).write(dest, encoding="utf-8", xml_declaration=True)
    return missing


def _banner(lines: list[str]) -> None:
    width = max(len(line) for line in lines) + 4
    print("!" * width, file=sys.stderr)
    for line in lines:
        print(f"! {line.ljust(width - 4)} !", file=sys.stderr)
    print("!" * width, file=sys.stderr)


def _clamped_workers(configured: int) -> int:
    """Clamp the configured xdist width to the host's CPU count.

    The config values were sized for the 24-core estate runner; a
    GitHub-hosted runner has 4 vCPUs, and 4x oversubscription makes the
    heavyweight fixtures (module-scoped repo-index builds, nested-pytest
    spawners) blow their 180s timeouts — a red that reads as a candidate
    defect but is pure scheduling (measured 2026-08-11..14). The configured
    value stays the UPPER bound so the estate runner keeps its width.
    """
    cores = os.cpu_count() or 1
    return max(1, min(int(configured), cores))


def run_lane(
    lane: str,
    junit: Path,
    base: str | None,
    changed_override: list[str] | None = None,
) -> int:
    cfg = LANES[lane]
    result = impacted(base, changed_override=changed_override)
    node_ids: list[str] = result["impacted"]
    serial_ids: list[str] = result["serial"]

    if result["deferred"]:
        print(
            f"note: {len(result['deferred'])} impacted test file(s) live in quarantined suites "
            f"the fast lanes do not run ({', '.join(DEFERRED_TEST_PREFIXES)}); they are covered "
            "by `make test-full` / `make test-nightly`.",
            file=sys.stderr,
        )
    if result["no_test_edge"]:
        print(
            f"note: {len(result['no_test_edge'])} changed doc/asset file(s) are read by no test.",
            file=sys.stderr,
        )

    # `closure_too_broad` escalates in EVERY lane: the analysis is not in doubt, it is
    # saying most of the suite is impacted, and at that point handing pytest hundreds of
    # explicit paths costs more than letting it collect the tree once.
    # `unresolved_input` is a hole in the analysis; only a lane that claims to cover the
    # diff (test-pr) has to escalate for it -- test-dev says so loudly and keeps going.
    escalate = bool(result["closure_too_broad"]) or (
        bool(result["unresolved_input"]) and bool(cfg["escalate_on_incomplete"])
    )
    if result["full_required"]:
        _banner(
            [
                "IMPACT ANALYSIS DOES NOT COVER THIS DIFF ON ITS OWN.",
                *[f"  - {reason}" for reason in result["full_reasons"]],
                (
                    f"ESCALATING `test-{lane}` to the whole fast-lane tree."
                    if escalate
                    else f"`test-{lane}` is a pre-commit loop, not a gate: continuing with the "
                    "subset above."
                ),
                "Run `make test-full` before merging.",
            ]
        )

    with tempfile.TemporaryDirectory(prefix="lane-junit-") as tmp:
        tmp_path = Path(tmp)
        steps: list[Step] = []

        if escalate:
            # The whole tree the fast lane is allowed to touch, minus the quarantined suites
            # (same --ignore set as `make test-fast`) and minus the serial-only suites, which
            # get their own serial step below. acceptance_smoke/daily are part of the tree, so
            # no separate critical step is needed here.
            ignores = [f"--ignore={p}" for p in (*DEFERRED_TEST_PREFIXES, "tests/doctrine")]
            steps.append(
                _run_pytest(
                    "escalated-tree",
                    [
                        "tests",
                        "-m",
                        compose_markers(),
                        "--timeout=180",
                        "-n",
                        str(_clamped_workers(cfg["impacted_workers"])),
                        "--dist",
                        "worksteal",
                        *ignores,
                    ],
                    tmp_path / "tree.xml",
                )
            )
            steps.append(
                _run_pytest(
                    "doctrine-serial",
                    ["tests/doctrine", "-m", compose_markers(), "--timeout=180"],
                    tmp_path / "doctrine.xml",
                )
            )
        else:
            if node_ids:
                args = [*node_ids, "-m", compose_markers(), "--timeout=180"]
                workers = _clamped_workers(cfg["impacted_workers"])
                if workers:
                    args = ["-n", str(workers), "--dist", "worksteal", *args]
                steps.append(
                    _run_pytest(
                        "impacted-parallel",
                        args,
                        tmp_path / "impacted.xml",
                        # markers may legitimately deselect the whole subset
                        expect_tests=False,
                    )
                )
            else:
                print(
                    "no impacted test files -- running the critical set only", file=sys.stderr
                )

            if serial_ids:
                # NO -n HERE, EVER. See this module's docstring: tests/doctrine mutates
                # shared fixture files in place and races itself under xdist.
                print(
                    f"running {len(serial_ids)} impacted test file(s) SERIALLY "
                    "(not xdist-safe: in-place fixture mutation)",
                    file=sys.stderr,
                )
                steps.append(
                    _run_pytest(
                        "impacted-serial",
                        [*serial_ids, "-m", compose_markers(), "--timeout=180"],
                        tmp_path / "serial.xml",
                        expect_tests=False,
                    )
                )

            steps.append(
                _run_pytest(
                    "critical",
                    [
                        "tests/acceptance",
                        "-m",
                        compose_markers(str(cfg["critical_marker"])),
                        f"--timeout={cfg['critical_timeout']}",
                        "-n",
                        str(_clamped_workers(cfg["critical_workers"])),
                        "--dist",
                        "worksteal",
                    ],
                    tmp_path / "critical.xml",
                )
            )

        # Everything that reads a step's JUnit part must happen INSIDE this block: the
        # parts live in the temporary directory and stop existing the moment it closes,
        # and "the report is gone" is indistinguishable from "the step was killed".
        missing = merge_junit([step.junit for step in steps], junit)
        exit_code = lane_exit_code(steps)
        summary = [
            (step.name, step.returncode, step_exit_code(step), junit_testcase_count(step.junit))
            for step in steps
        ]

    for name, raw_rc, norm_rc, count in summary:
        print(f"  step {name}: rc={raw_rc} -> {norm_rc} tests={count}", file=sys.stderr)
    if missing:
        print(
            f"FAILURE: {len(missing)} lane step(s) produced no readable JUnit report "
            f"({', '.join(p.name for p in missing)}); the merged report at {junit} is "
            "INCOMPLETE and this lane's result cannot be trusted.",
            file=sys.stderr,
        )
        exit_code = max(exit_code, EXIT_UNTRUSTWORTHY_REPORT)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane", choices=sorted(LANES), required=True)
    ap.add_argument("--junit", required=True, type=Path)
    ap.add_argument(
        "--base", default=None, help="ref/sha to diff against (default: merge-base with main)"
    )
    ap.add_argument(
        "--changed",
        action="append",
        default=None,
        help="what-if: run the lane as though exactly these files had changed (repeatable). "
        "Used by TESTING.md's reproducible per-scenario lane benchmarks.",
    )
    args = ap.parse_args(argv)
    return run_lane(args.lane, args.junit, args.base, changed_override=args.changed)


if __name__ == "__main__":
    sys.exit(main())
