"""Regression guard for the "-m is store-not-append" bug (Phase 1, TESTING.md).

pytest's `-m` on the command line REPLACES pyproject.toml's `addopts` marker expression
rather than adding to it. A bare `-m "not smoke"` on a fast lane's command line once
silently readmitted 9 live/perf tests (202.0s of a 3,033.9s cumulative run, including the
run's longest single test) and fired live Jira/OpenHands/Anthropic/Ollama calls on every
dev run. `Makefile`'s `FAST_LANE_MARKERS` was written to compose instead of override; this
test makes sure every lane built on top of it (test-fast, test-dev, test-pr, all via
`scripts/testlanes/run_lane.py`) stays a superset of `pyproject.toml`'s default exclusion,
and that the Makefile and the Python lane runner cannot drift into two different answers.
"""

from __future__ import annotations

import ast
import itertools
import re
from pathlib import Path

import pytest

from scripts.testlanes.run_lane import FAST_LANE_MARKERS as RUN_LANE_MARKERS
from scripts.testlanes.run_lane import LANES, compose_markers

ROOT = Path(__file__).resolve().parents[2]


def _extract_make_var(text: str, name: str) -> str:
    match = re.search(rf"^{name}\s*:=\s*(.+)$", text, re.MULTILINE)
    assert match, f"{name} not found in Makefile"
    return match.group(1).strip()


def _extract_addopts_marker(pyproject_text: str) -> str:
    match = re.search(r"addopts\s*=\s*\"-m\s+'([^']+)'\"", pyproject_text)
    assert match, "addopts -m expression not found in pyproject.toml"
    return match.group(1).strip()


def _markers_in(expr: str) -> set[str]:
    tree = ast.parse(expr, mode="eval")
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def _eval_expr(expr: str, present: set[str]) -> bool:
    tree = ast.parse(expr, mode="eval")
    names = {name: (name in present) for name in _markers_in(expr)}
    code = compile(tree, "<marker-expr>", "eval")
    return bool(eval(code, {"__builtins__": {}}, names))  # noqa: S307 -- trusted config strings, not user input


def test_makefile_fast_lane_markers_matches_the_lane_runner_constant() -> None:
    """`make test-fast` and `make test-dev`/`make test-pr` must agree on what "fast lane"
    excludes, or the lanes silently diverge.

    This asserts LOGICAL EQUIVALENCE, not string equality. It used to compare the two
    strings, which was a valid proxy only while both were hand-written literals. run_lane.py
    now DERIVES its expression from pyproject's addopts (a copy of this list had already
    drifted three ways -- pyproject and the Makefile excluded `feature_health` while the
    runner's literal did not), so the two are deliberately worded differently and must be
    compared by meaning. Brute-forcing the marker universe is strictly stronger than string
    equality: it also catches two identical-looking expressions that disagree, and it fails
    loudly if either side stops mentioning a marker the other excludes."""
    makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
    makefile_expr = _extract_make_var(makefile_text, "FAST_LANE_MARKERS")

    universe = sorted(_markers_in(makefile_expr) | _markers_in(RUN_LANE_MARKERS))
    assert universe, "no markers parsed out of either expression -- test is not exercising anything"

    for bits in itertools.product([False, True], repeat=len(universe)):
        present = {name for name, on in zip(universe, bits, strict=True) if on}
        assert _eval_expr(makefile_expr, present) == _eval_expr(RUN_LANE_MARKERS, present), (
            f"Makefile FAST_LANE_MARKERS and run_lane.FAST_LANE_MARKERS disagree for "
            f"markers={sorted(present) or '(none)'}:\n"
            f"  Makefile : {makefile_expr}\n"
            f"  run_lane : {RUN_LANE_MARKERS}"
        )


def _assert_superset_of_addopts(lane_expr: str) -> None:
    """ "Superset" means: for every combination of markers present on a hypothetical test,
    whenever pyproject.toml's addopts would already exclude it, `lane_expr` must exclude it
    too (it may exclude MORE -- e.g. it also excludes `smoke` -- never less). Brute-forced
    over the small marker universe referenced by either expression, so this is exact, not
    sampled."""
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyproject_expr = _extract_addopts_marker(pyproject_text)

    universe = sorted(_markers_in(pyproject_expr) | _markers_in(lane_expr))
    assert universe, "no markers parsed out of either expression -- test is not exercising anything"

    for bits in itertools.product([False, True], repeat=len(universe)):
        present = {name for name, on in zip(universe, bits, strict=True) if on}
        pyproject_included = _eval_expr(pyproject_expr, present)
        lane_included = _eval_expr(lane_expr, present)
        if lane_included:
            assert pyproject_included, (
                f"marker combo {sorted(present)!r} is INCLUDED by the fast lane "
                f"expression ({lane_expr!r}) but EXCLUDED by pyproject.toml addopts "
                f"({pyproject_expr!r}) -- the lane is not a superset of the default "
                "exclusion and would readmit live/perf/live_ollama/live/counterfeit_gate "
                "tests. This is exactly the bug documented in the Makefile's "
                "FAST_LANE_MARKERS comment -- do not reintroduce it."
            )


def test_fast_lane_markers_is_a_superset_of_pyproject_addopts() -> None:
    _assert_superset_of_addopts(RUN_LANE_MARKERS)


@pytest.mark.parametrize("lane", sorted(LANES))
def test_every_marker_expression_a_lane_actually_passes_to_pytest_is_composed(lane: str) -> None:
    """The constant being right is not enough: what matters is the string each lane's
    subprocess really receives.

    `run_lane.py` shipped with its critical step passing a BARE `-m acceptance_smoke`, which
    -- because pytest's `-m` replaces `addopts` rather than adding to it -- re-admitted
    live/perf/counterfeit_gate tests one layer below the lane the check was meant to protect.
    This walks every expression the runner can emit, including the acceptance-marker one.
    """
    critical = str(LANES[lane]["critical_marker"])
    for expr in (compose_markers(), compose_markers(critical)):
        _assert_superset_of_addopts(expr)


def test_composition_actually_narrows_the_selection() -> None:
    """Guard against `compose_markers` degenerating into a no-op that returns its argument:
    the composed expression must still exclude what addopts excludes, AND still select the
    marker asked for."""
    composed = compose_markers("acceptance_smoke")
    assert _eval_expr(composed, {"acceptance_smoke"}) is True
    assert _eval_expr(composed, {"acceptance_smoke", "live"}) is False
    assert _eval_expr(composed, {"acceptance_smoke", "counterfeit_gate"}) is False
    assert _eval_expr(composed, set()) is False
    # ... and the bare expression the old code used would NOT have excluded those.
    assert _eval_expr("acceptance_smoke", {"acceptance_smoke", "live"}) is True
