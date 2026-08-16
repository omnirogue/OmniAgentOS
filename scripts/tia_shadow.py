#!/usr/bin/env python3
"""Shadow audit of the coverage-based selector over replayed history.

This script **calls the selector it audits**. It imports
``scripts.tia.selector`` and invokes ``selector.select_tests`` with
``critical_tests=None``, i.e. the production default path including the always-run
lookup — it does not contain, and must never contain, its own copy of the selection
rules. The previous attempt at this harness reimplemented selection and diverged from
the shipped selector in the *unsafe* direction, so it audited a program nobody runs.
``tests/tia/test_shadow_harness.py::test_replay_calls_the_real_selector`` pins that by
patching the selector module and asserting the patch is what runs.

What it measures, per replayed commit
-------------------------------------
* **false negatives** — a test that FAILED on the full run of that commit and that the
  selector would NOT have run. This is the only metric that decides whether selection is
  safe, and it needs real failure records (``--failures``). This repo stores none: there
  is no CI, no JUnit archive and no per-commit verdict ledger, so on a bare checkout the
  rate is reported as ``null`` (undefined over an empty set) and **never as 0.0**. An
  absent oracle is not a passing oracle.
* **proxy misses** — for commits that changed source *and* tests together, whether the
  selector, shown only the source paths, picks the test files the author changed
  alongside them. That is the author's own coupling judgement used as a stand-in oracle.
  It is reported under `proxy_` names and is not a substitute for the metric above.
* **selection fraction** — selected test files / total test files, per commit, median
  over the replay. FULL counts as 1.0. Undefined (``null``) if the suite is empty.

Usage::

    python scripts/tia_shadow.py --commits 60 --map var/test-selection/coverage_map.json
    python scripts/tia_shadow.py --commits 50 --assert-fn-rate 0 --assert-median-selection-lt 0.40

An ``--assert-*`` flag whose metric is undefined FAILS. "No data" must not read as PASS.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.tia import changes  # noqa: E402
from scripts.tia import coverage_map as coverage_map_mod  # noqa: E402
from scripts.tia import selector as selector_mod  # noqa: E402

DEFAULT_COMMITS = 60
DEFAULT_MIN_COMMITS = 50


@dataclass
class CommitAudit:
    """One replayed commit."""

    sha: str
    changed_files: int
    mode: str
    selected: int | None
    selection_fraction: float | None
    reasons: list[str] = field(default_factory=list)
    failed_tests: list[str] = field(default_factory=list)
    missed_failures: list[str] = field(default_factory=list)
    proxy_expected_tests: list[str] = field(default_factory=list)
    proxy_missed_tests: list[str] = field(default_factory=list)
    error: str | None = None


def test_universe(repo_root: Path) -> frozenset[str]:
    """Every test file pytest could collect, as repo-relative paths."""
    found: set[str] = set()
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        return frozenset()
    for path in tests_dir.rglob("*.py"):
        rel = path.relative_to(repo_root).as_posix()
        if selector_mod.is_test_file(rel):
            found.add(rel)
    return frozenset(found)


def node_id_to_file(node_id: str) -> str:
    """Fold a pytest node id to its test file (selection granularity)."""
    return node_id.split("::", 1)[0].replace("\\", "/")


def load_failures(path: str | Path | None) -> dict[str, list[str]]:
    """``{sha: [failed node id, ...]}``. Absent file -> no records, not zero failures."""
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("--failures must be a JSON object of {commit sha: [node id, ...]}")
    return {str(sha): [str(n) for n in nodes or []] for sha, nodes in payload.items()}


def replay_commit(
    repo_root: Path,
    sha: str,
    cmap: coverage_map_mod.CoverageMap,
    universe: frozenset[str],
    failures: dict[str, list[str]],
) -> CommitAudit:
    """Replay one commit through the real selector."""
    try:
        changed = changes.changed_files_for_commit(repo_root, sha)
    except changes.GitError as exc:
        return CommitAudit(
            sha=sha,
            changed_files=0,
            mode="error",
            selected=None,
            selection_fraction=None,
            error=str(exc),
        )

    selection = selector_mod.select_tests(changed, cmap, repo_root)
    audit = CommitAudit(
        sha=sha,
        changed_files=len(changed),
        mode=selection.mode,
        selected=len(universe) if selection.is_full else len(selection.tests or ()),
        selection_fraction=selection.fraction_of(universe),
        reasons=list(selection.reasons[:6]),
    )

    failed_files = sorted({node_id_to_file(node) for node in failures.get(sha, [])})
    audit.failed_tests = failed_files
    audit.missed_failures = [f for f in failed_files if not selection.covers(f)]

    # Proxy oracle: source-only selection vs the tests the author changed alongside.
    source_changed = [
        path
        for path in changed
        if not selector_mod.is_test_file(path) and not path.startswith("tests/")
    ]
    test_changed = [path for path in changed if selector_mod.is_test_file(path)]
    expected = [path for path in test_changed if (repo_root / path).is_file()]
    if source_changed and expected:
        proxy_selection = selector_mod.select_tests(source_changed, cmap, repo_root)
        audit.proxy_expected_tests = expected
        audit.proxy_missed_tests = [f for f in expected if not proxy_selection.covers(f)]
    return audit


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate over an empty denominator is undefined, not 0.0 and not 1.0."""
    if denominator <= 0:
        return None
    return numerator / denominator


def aggregate(audits: list[CommitAudit], universe_size: int) -> dict[str, Any]:
    replayed = [a for a in audits if a.error is None]
    fractions = [a.selection_fraction for a in replayed if a.selection_fraction is not None]
    full_runs = [a for a in replayed if a.mode == selector_mod.FULL]
    subset_runs = [a for a in replayed if a.mode == selector_mod.SUBSET]

    total_failed = sum(len(a.failed_tests) for a in replayed)
    total_missed = sum(len(a.missed_failures) for a in replayed)
    commits_with_failures = [a for a in replayed if a.failed_tests]

    proxy_expected = sum(len(a.proxy_expected_tests) for a in replayed)
    proxy_missed = sum(len(a.proxy_missed_tests) for a in replayed)
    proxy_subset = [a for a in subset_runs if a.proxy_expected_tests]
    proxy_subset_expected = sum(len(a.proxy_expected_tests) for a in proxy_subset)
    proxy_subset_missed = sum(len(a.proxy_missed_tests) for a in proxy_subset)

    return {
        "commits_requested": len(audits),
        "commits_replayed": len(replayed),
        "commits_errored": len(audits) - len(replayed),
        "test_universe": universe_size,
        "full_runs": len(full_runs),
        "subset_runs": len(subset_runs),
        "full_run_fraction": _rate(len(full_runs), len(replayed)),
        "median_selection_fraction": statistics.median(fractions) if fractions else None,
        "mean_selection_fraction": statistics.fmean(fractions) if fractions else None,
        "min_selection_fraction": min(fractions) if fractions else None,
        # The metric that decides safety. None means "no oracle", not "no misses".
        "failure_records_available": bool(total_failed) or bool(commits_with_failures),
        "failed_tests_total": total_failed,
        "false_negatives": total_missed if total_failed else 0,
        "false_negative_rate": _rate(total_missed, total_failed),
        # Proxy oracle. Clearly labelled; not a safety guarantee.
        "proxy_expected_tests": proxy_expected,
        "proxy_missed_tests": proxy_missed,
        "proxy_miss_rate": _rate(proxy_missed, proxy_expected),
        "proxy_miss_rate_subset_only": _rate(proxy_subset_missed, proxy_subset_expected),
        "proxy_commits_subset_only": len(proxy_subset),
    }


def run_shadow(
    repo_root: Path,
    map_path: Path,
    commit_count: int,
    failures_path: str | Path | None = None,
    ref: str = "HEAD",
) -> dict[str, Any]:
    cmap = coverage_map_mod.CoverageMap.load(map_path)
    universe = test_universe(repo_root)
    failures = load_failures(failures_path)
    shas = changes.recent_commits(repo_root, commit_count, ref)
    audits = [replay_commit(repo_root, sha, cmap, universe, failures) for sha in shas]
    summary = aggregate(audits, len(universe))
    summary["map"] = {
        "path": str(map_path),
        "commit": cmap.commit,
        "generated_at": cmap.generated_at,
        "source_files": len(cmap.source_to_tests),
        "excluded_files": len(cmap.excluded_files),
        "tests_in_map": cmap.test_count,
    }
    return {"summary": summary, "commits": [asdict(a) for a in audits]}


def _check_assertions(summary: dict[str, Any], args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    if summary["commits_replayed"] < args.min_commits:
        problems.append(
            f"replayed {summary['commits_replayed']} commits, need >= {args.min_commits}"
        )
    if args.assert_fn_rate is not None:
        rate = summary["false_negative_rate"]
        if rate is None:
            problems.append(
                "false-negative rate is UNDEFINED (no failure records supplied via "
                "--failures); an absent oracle cannot satisfy --assert-fn-rate"
            )
        elif rate > args.assert_fn_rate:
            problems.append(f"false-negative rate {rate:.4f} > {args.assert_fn_rate}")
    if args.assert_median_selection_lt is not None:
        median = summary["median_selection_fraction"]
        if median is None:
            problems.append(
                "median selection fraction is UNDEFINED (empty test universe or no "
                "replayed commits); that is not a pass"
            )
        elif median >= args.assert_median_selection_lt:
            problems.append(
                f"median selection fraction {median:.4f} >= {args.assert_median_selection_lt}"
            )
    return problems


def _fmt(value: Any) -> str:
    if value is None:
        return "undefined (empty set)"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--repo", default=str(REPO_ROOT))
    parser.add_argument("--commits", type=int, default=DEFAULT_COMMITS)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument(
        "--map", default=str(REPO_ROOT / coverage_map_mod.DEFAULT_MAP_RELPATH)
    )
    parser.add_argument("--failures", default=None, help="JSON {sha: [failed node id]}")
    parser.add_argument("--out", default=None, help="write the full report JSON here")
    parser.add_argument("--min-commits", type=int, default=DEFAULT_MIN_COMMITS)
    parser.add_argument("--assert-fn-rate", type=float, default=None)
    parser.add_argument("--assert-median-selection-lt", type=float, default=None)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    try:
        report = run_shadow(repo_root, Path(args.map), args.commits, args.failures, args.ref)
    except coverage_map_mod.CoverageMapError as exc:
        print(f"tia_shadow: {exc}", file=sys.stderr)
        print(
            "build one with: uv run --with coverage python -m scripts.tia.build_map",
            file=sys.stderr,
        )
        return 2

    summary = report["summary"]
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")

    print("=== TIA shadow audit (records only; selects nothing, gates nothing) ===")
    for key in (
        "commits_replayed",
        "commits_errored",
        "test_universe",
        "full_runs",
        "subset_runs",
        "full_run_fraction",
        "median_selection_fraction",
        "mean_selection_fraction",
        "min_selection_fraction",
        "failed_tests_total",
        "false_negatives",
        "false_negative_rate",
        "proxy_expected_tests",
        "proxy_missed_tests",
        "proxy_miss_rate",
        "proxy_miss_rate_subset_only",
        "proxy_commits_subset_only",
    ):
        print(f"{key:32s} {_fmt(summary[key])}")
    if summary["false_negative_rate"] is None:
        print(
            "\nfalse_negative_rate is undefined: no per-commit failure records exist in "
            "this repo.\nSupply --failures to measure it; do not read 'undefined' as 'zero'."
        )

    problems = _check_assertions(summary, args)
    if problems:
        print("\nFAIL:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
