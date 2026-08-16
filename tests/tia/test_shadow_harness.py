"""The shadow harness must audit the shipped selector, and must not invent numbers.

Two defects from the rejected attempts are pinned here:

* the harness reimplemented selection, so it audited a program nobody ships and diverged
  from it in the unsafe direction. :func:`test_replay_calls_the_real_selector` replaces
  ``scripts.tia.selector.select_tests`` and asserts the replacement is what runs;
  a harness with its own copy of the rules passes straight through it.
* headline metrics read 1.0 / PASS over empty sets. Every ``_rate`` case below asserts
  ``None``, and :func:`test_assert_flags_fail_on_an_undefined_metric` asserts that an
  ``--assert-*`` threshold over an undefined metric FAILS rather than passing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from scripts.tia import selector as selector_mod
from scripts.tia.coverage_map import SCHEMA_VERSION, CoverageMap

from .conftest import git, write

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "tia_shadow.py"


def _load_shadow() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tia_shadow_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shadow() -> ModuleType:
    return _load_shadow()


@pytest.fixture
def repo_with_map(synthetic_repo: Path) -> Path:
    """The synthetic repo plus a coverage map covering one of its two modules."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "commit": None,
        "generated_at": None,
        "test_count": 2,
        "source_to_tests": {
            "pkg/mod_a.py": ["tests/unit/test_alpha.py"],
            "pkg/mod_b.py": ["tests/unit/test_beta.py"],
        },
        "excluded_files": {},
        "unresolved_contexts": [],
    }
    target = synthetic_repo / "var" / "test-selection" / "coverage_map.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return synthetic_repo


# --------------------------------------------------------------------------------------
# it calls the selector it audits
# --------------------------------------------------------------------------------------
def test_replay_calls_the_real_selector(
    shadow: ModuleType, repo_with_map: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The *primary* selection must come from the shipped selector.

    Asserting only "the selector was called at some point" is not enough: replay also
    consults it for the proxy oracle, so a harness that reimplemented the primary
    selection and kept the proxy call would still look green. The first recorded call
    must therefore carry the commit's whole changed-file set.
    """
    calls: list[tuple[str, ...]] = []
    real = selector_mod.select_tests

    def spy(changed, cmap, root, critical_tests=None):  # type: ignore[no-untyped-def]
        calls.append(tuple(changed))
        return real(changed, cmap, root, critical_tests)

    monkeypatch.setattr(selector_mod, "select_tests", spy)
    cmap = CoverageMap.load(repo_with_map / "var" / "test-selection" / "coverage_map.json")
    head = git(repo_with_map, "rev-parse", "HEAD").strip()
    shadow.replay_commit(repo_with_map, head, cmap, shadow.test_universe(repo_with_map), {})
    assert calls, "the harness never called the selector at all"
    assert calls[0] == ("pkg/mod_a.py", "tests/unit/test_alpha.py"), (
        f"the primary selection did not go through the shipped selector: first call was {calls[0]}"
    )


def test_replay_result_is_exactly_what_the_selector_returns(
    shadow: ModuleType, repo_with_map: Path
) -> None:
    """Divergence detector: the audited answer must equal the shipped answer."""
    cmap = CoverageMap.load(repo_with_map / "var" / "test-selection" / "coverage_map.json")
    head = git(repo_with_map, "rev-parse", "HEAD").strip()
    changed = ("pkg/mod_a.py", "tests/unit/test_alpha.py")
    direct = selector_mod.select_tests(changed, cmap, repo_with_map)
    audit = shadow.replay_commit(repo_with_map, head, cmap, shadow.test_universe(repo_with_map), {})
    assert audit.mode == direct.mode
    assert direct.tests is not None
    assert audit.selected == len(direct.tests)
    assert audit.reasons == list(direct.reasons[:6])


def test_replay_uses_the_default_critical_path(
    shadow: ModuleType, repo_with_map: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It must not pass its own ``critical_tests`` in, or the always-run set is unaudited."""
    seen: list[object] = []
    real = selector_mod.select_tests

    def spy(changed, cmap, root, critical_tests=None):  # type: ignore[no-untyped-def]
        seen.append(critical_tests)
        return real(changed, cmap, root, critical_tests)

    monkeypatch.setattr(selector_mod, "select_tests", spy)
    cmap = CoverageMap.load(repo_with_map / "var" / "test-selection" / "coverage_map.json")
    head = git(repo_with_map, "rev-parse", "HEAD").strip()
    shadow.replay_commit(repo_with_map, head, cmap, frozenset(), {})
    assert seen and all(value is None for value in seen)


# --------------------------------------------------------------------------------------
# rates over empty sets
# --------------------------------------------------------------------------------------
def test_rate_over_an_empty_denominator_is_undefined(shadow: ModuleType) -> None:
    assert shadow._rate(0, 0) is None
    assert shadow._rate(3, 0) is None
    assert shadow._rate(0, 4) == 0.0


def test_aggregate_of_no_commits_reports_undefined_not_perfect(shadow: ModuleType) -> None:
    summary = shadow.aggregate([], universe_size=0)
    assert summary["false_negative_rate"] is None
    assert summary["median_selection_fraction"] is None
    assert summary["full_run_fraction"] is None
    assert summary["proxy_miss_rate"] is None
    assert summary["proxy_miss_rate_subset_only"] is None


def test_false_negative_rate_without_failure_records_is_undefined(shadow: ModuleType) -> None:
    audits = [
        shadow.CommitAudit(
            sha="a" * 40,
            changed_files=1,
            mode=selector_mod.SUBSET,
            selected=2,
            selection_fraction=0.2,
        )
    ]
    summary = shadow.aggregate(audits, universe_size=10)
    assert summary["false_negative_rate"] is None, "no oracle must not read as zero misses"
    assert summary["median_selection_fraction"] == pytest.approx(0.2)


def test_a_failure_that_was_not_selected_is_counted_as_a_false_negative(
    shadow: ModuleType,
) -> None:
    audits = [
        shadow.CommitAudit(
            sha="a" * 40,
            changed_files=1,
            mode=selector_mod.SUBSET,
            selected=1,
            selection_fraction=0.1,
            failed_tests=["tests/x/test_a.py", "tests/x/test_b.py"],
            missed_failures=["tests/x/test_b.py"],
        )
    ]
    summary = shadow.aggregate(audits, universe_size=10)
    assert summary["failed_tests_total"] == 2
    assert summary["false_negatives"] == 1
    assert summary["false_negative_rate"] == pytest.approx(0.5)


def test_full_runs_never_produce_false_negatives(
    shadow: ModuleType, repo_with_map: Path
) -> None:
    cmap = CoverageMap.load(repo_with_map / "var" / "test-selection" / "coverage_map.json")
    head = git(repo_with_map, "rev-parse", "HEAD").strip()
    write(repo_with_map, "pyproject.toml", "[tool.pytest.ini_options]\naddopts = ''\n")
    git(repo_with_map, "add", "-A")
    git(repo_with_map, "commit", "-q", "-m", "touch pyproject")
    head = git(repo_with_map, "rev-parse", "HEAD").strip()
    audit = shadow.replay_commit(
        repo_with_map,
        head,
        cmap,
        frozenset({"tests/unit/test_alpha.py"}),
        {head: ["tests/unit/test_beta.py::test_sub"]},
    )
    assert audit.mode == selector_mod.FULL
    assert audit.failed_tests == ["tests/unit/test_beta.py"]
    assert audit.missed_failures == []


def test_a_missed_failure_is_recorded_per_commit(
    shadow: ModuleType, repo_with_map: Path
) -> None:
    cmap = CoverageMap.load(repo_with_map / "var" / "test-selection" / "coverage_map.json")
    head = git(repo_with_map, "rev-parse", "HEAD").strip()  # touches pkg/mod_a.py + its test
    audit = shadow.replay_commit(
        repo_with_map,
        head,
        cmap,
        shadow.test_universe(repo_with_map),
        {head: ["tests/unit/test_beta.py::test_sub"]},
    )
    assert audit.mode == selector_mod.SUBSET, audit.reasons
    assert audit.missed_failures == ["tests/unit/test_beta.py"]


# --------------------------------------------------------------------------------------
# end to end over a real (tiny) history
# --------------------------------------------------------------------------------------
def test_run_shadow_over_a_real_history(shadow: ModuleType, repo_with_map: Path) -> None:
    report = shadow.run_shadow(
        repo_with_map,
        repo_with_map / "var" / "test-selection" / "coverage_map.json",
        commit_count=10,
    )
    summary = report["summary"]
    assert summary["commits_replayed"] == 2
    assert summary["commits_errored"] == 0
    assert summary["test_universe"] == len(shadow.test_universe(repo_with_map))
    assert 0.0 < summary["median_selection_fraction"] <= 1.0
    assert summary["false_negative_rate"] is None
    assert len(report["commits"]) == 2
    modes = {commit["mode"] for commit in report["commits"]}
    assert selector_mod.SUBSET in modes, "the newest commit is fully mapped and must subset"


def test_proxy_oracle_uses_source_only_selection(
    shadow: ModuleType, repo_with_map: Path
) -> None:
    """The author changed pkg/mod_a.py and tests/unit/test_alpha.py together.

    Shown only the source path, the selector must reach the test the author reached.
    """
    cmap = CoverageMap.load(repo_with_map / "var" / "test-selection" / "coverage_map.json")
    head = git(repo_with_map, "rev-parse", "HEAD").strip()
    audit = shadow.replay_commit(
        repo_with_map, head, cmap, shadow.test_universe(repo_with_map), {}
    )
    assert audit.proxy_expected_tests == ["tests/unit/test_alpha.py"]
    assert audit.proxy_missed_tests == []


def test_test_universe_finds_only_test_files(shadow: ModuleType, repo_with_map: Path) -> None:
    universe = shadow.test_universe(repo_with_map)
    assert "tests/unit/test_alpha.py" in universe
    assert all(selector_mod.is_test_file(path) for path in universe)


def test_node_ids_fold_to_files(shadow: ModuleType) -> None:
    assert shadow.node_id_to_file("tests/a/test_b.py::TestC::test_d") == "tests/a/test_b.py"
    assert shadow.node_id_to_file("tests/a/test_b.py") == "tests/a/test_b.py"


# --------------------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------------------
class _Args:
    def __init__(self, **kwargs: object) -> None:
        self.min_commits = 0
        self.assert_fn_rate = None
        self.assert_median_selection_lt = None
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_assert_flags_fail_on_an_undefined_metric(shadow: ModuleType) -> None:
    summary = shadow.aggregate([], universe_size=0)
    problems = shadow._check_assertions(
        summary, _Args(assert_fn_rate=0.0, assert_median_selection_lt=0.4)
    )
    assert len(problems) == 2
    assert any("UNDEFINED" in problem for problem in problems)


def test_assert_flags_fail_when_too_few_commits_were_replayed(shadow: ModuleType) -> None:
    summary = shadow.aggregate([], universe_size=0)
    problems = shadow._check_assertions(summary, _Args(min_commits=50))
    assert any("need >= 50" in problem for problem in problems)


def test_assert_flags_pass_on_a_clean_defined_result(shadow: ModuleType) -> None:
    audits = [
        shadow.CommitAudit(
            sha="a" * 40,
            changed_files=1,
            mode=selector_mod.SUBSET,
            selected=1,
            selection_fraction=0.1,
            failed_tests=["tests/x/test_a.py"],
            missed_failures=[],
        )
    ]
    summary = shadow.aggregate(audits, universe_size=10)
    assert summary["false_negative_rate"] == 0.0
    assert (
        shadow._check_assertions(
            summary, _Args(assert_fn_rate=0.0, assert_median_selection_lt=0.4)
        )
        == []
    )
