"""The selector's safety contract, asserted against the real repository.

Every test here exists because of a specific way this feature has already failed:

* ``_get_critical_tests`` shipped twice as ``return set()``. The reviewer restored the
  stub and the suite stayed green, so the always-run guarantee was dead config nobody
  could detect. :func:`test_critical_set_covers_every_guaranteed_area` and
  :func:`test_subset_selection_contains_every_critical_test` read the real
  ``tests/`` tree through the real default code path (``critical_tests=None``), so a stub
  turns them red — verified by restoring the stub, not by assuming.
* the selector defaulted to "none" on unresolvable input, which is a false-negative
  machine. Everything below that ends in ``_forces_full`` pins the opposite default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.tia.coverage_map import CoverageMap
from scripts.tia.selector import (
    ALWAYS_RUN_PATTERNS,
    FULL,
    SUBSET,
    CriticalPatternError,
    Selection,
    _get_critical_tests,
    critical_pattern_matches,
    force_full_reason,
    is_test_file,
    select_tests,
    validate_critical_patterns,
)

REPO = Path(__file__).resolve().parents[2]

#: Guarantees the always-run set must keep, stated independently of the pattern strings
#: so that DELETING a pattern (not just typo-ing one) turns this suite red.
GUARANTEED_AREAS: tuple[str, ...] = (
    "tests/doctrine/",
    "tests/counterfeits/",
    "tests/gates/",
    "tests/gates_scripts/",
    "tests/acceptance/",
    "tests/certification/",
)

#: The security guarantees do not live in a directory of their own, so an area check
#: cannot see them: `tests/acceptance/test_19_path_security_registry.py` happens to
#: satisfy "some test with 'secur' in its name" through the *acceptance* pattern, and the
#: first version of this suite stayed green when all three security patterns were
#: deleted. Each witness below is matched by exactly one pattern and by no directory
#: pattern, so deleting that pattern turns this red.
SECURITY_WITNESSES: tuple[tuple[str, str], ...] = (
    ("tests/**/test_*security*.py", "tests/swarm/test_planner_verifier_security.py"),
    ("tests/**/test_*secret*.py", "tests/policy/test_secret_registry.py"),
    ("tests/**/test_path_containment*.py", "tests/api/test_path_containment.py"),
)

#: A source file that certainly exists, used as the "changed" input for subset cases.
MAPPED_SOURCE = "omniagentos/roles.py"


@pytest.fixture
def repo_map() -> CoverageMap:
    """A minimal map over the real repo: one source file, one covering test file."""
    return CoverageMap(
        source_to_tests={MAPPED_SOURCE: frozenset({"tests/test_capabilities.py"})},
        test_count=1,
    )


# --------------------------------------------------------------------------------------
# the always-run set (the part that was a stub twice)
# --------------------------------------------------------------------------------------
def test_critical_set_is_not_empty_in_this_repo() -> None:
    critical = _get_critical_tests(REPO)
    assert critical, "_get_critical_tests returned nothing — the always-run guarantee is dead"
    assert len(critical) >= 20, f"suspiciously small always-run set: {sorted(critical)}"


@pytest.mark.parametrize("area", GUARANTEED_AREAS)
def test_critical_set_covers_every_guaranteed_area(area: str) -> None:
    critical = _get_critical_tests(REPO)
    assert any(path.startswith(area) for path in critical), (
        f"no always-run test under {area}; a pattern was deleted or the tree moved"
    )


@pytest.mark.parametrize(("pattern", "witness"), SECURITY_WITNESSES)
def test_critical_set_covers_each_security_pattern(pattern: str, witness: str) -> None:
    assert (REPO / witness).is_file(), f"{witness} moved; pick a new witness for {pattern}"
    assert witness in _get_critical_tests(REPO), (
        f"{witness} is not always-run; the {pattern!r} guarantee is gone"
    )


@pytest.mark.parametrize(("pattern", "witness"), SECURITY_WITNESSES)
def test_each_security_witness_is_reached_only_by_its_own_pattern(
    pattern: str, witness: str
) -> None:
    """Keeps the test above honest: no other pattern may be covering for this one."""
    others = [p for p in ALWAYS_RUN_PATTERNS if p != pattern]
    reached = set()
    for other in others:
        reached |= critical_pattern_matches(REPO, other)
    assert witness not in reached, (
        f"{witness} is also matched by another always-run pattern, so deleting "
        f"{pattern!r} would not be detected — choose a different witness"
    )
    assert witness in critical_pattern_matches(REPO, pattern)


@pytest.mark.parametrize("pattern", ALWAYS_RUN_PATTERNS)
def test_every_always_run_pattern_matches_real_test_files(pattern: str) -> None:
    assert critical_pattern_matches(REPO, pattern), (
        f"{pattern!r} matches nothing under {REPO}: a dead pattern silently disables an "
        "always-run guarantee"
    )


def test_validate_critical_patterns_raises_on_a_dead_pattern() -> None:
    with pytest.raises(CriticalPatternError):
        validate_critical_patterns(REPO, [*ALWAYS_RUN_PATTERNS, "tests/no_such_dir/**/test_*.py"])


@pytest.mark.parametrize("pattern", ALWAYS_RUN_PATTERNS)
def test_subset_selection_contains_every_critical_test(
    pattern: str, repo_map: CoverageMap
) -> None:
    """A subset selection is a superset of the always-run set — through the real path.

    ``critical_tests`` is left at its default so this exercises ``_get_critical_tests``
    itself; passing a set in would make the assertion vacuous.
    """
    selection = select_tests([MAPPED_SOURCE], repo_map, REPO)
    assert selection.mode == SUBSET, f"expected a subset, got {selection.mode}: {selection.reasons}"
    assert selection.tests is not None
    expected = critical_pattern_matches(REPO, pattern)
    assert expected, f"{pattern!r} matched nothing"
    assert expected <= selection.tests, (
        f"always-run pattern {pattern!r} was not forced into the selection: "
        f"missing {sorted(expected - selection.tests)[:5]}"
    )


def test_selection_is_the_critical_set_plus_the_mapped_tests(repo_map: CoverageMap) -> None:
    selection = select_tests([MAPPED_SOURCE], repo_map, REPO)
    assert selection.tests is not None
    assert "tests/test_capabilities.py" in selection.tests
    assert selection.tests == _get_critical_tests(REPO) | {"tests/test_capabilities.py"}


def test_an_empty_critical_set_degrades_to_full_not_to_a_bare_subset(
    repo_map: CoverageMap,
) -> None:
    """The guard that makes a future stub loud instead of silent."""
    selection = select_tests([MAPPED_SOURCE], repo_map, REPO, critical_tests=set())
    assert selection.mode == FULL
    assert "always-run" in " ".join(selection.reasons)


# --------------------------------------------------------------------------------------
# FULL is the default answer for everything unresolvable
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "conftest.py",
        "tests/conftest.py",
        "tests/api/conftest.py",
        "pyproject.toml",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "setup.py",
        "uv.lock",
        "poetry.lock",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements/base.txt",
        "Makefile",
        ".coveragerc",
        "sitecustomize.py",
        "migrations/0100_add_thing.sql",
        "migrations-staging/0101_next.sql",
        "omniagentos/db/migrations/0099_thing.py",
        ".github/workflows/ci.yml",
        ".gitlab-ci.yml",
        "omniagentos/testpolicy/pytest_plugin.py",
    ],
)
def test_config_and_registry_paths_force_full(path: str, repo_map: CoverageMap) -> None:
    assert force_full_reason(path) is not None, f"{path} should force FULL on its own"
    selection = select_tests([path], repo_map, REPO)
    assert selection.mode == FULL, f"{path} did not force FULL: {selection.reasons}"


def test_path_absent_from_the_map_forces_full(repo_map: CoverageMap) -> None:
    selection = select_tests(["omniagentos/nowhere/unmapped.py"], repo_map, REPO)
    assert selection.mode == FULL
    assert "absent from the coverage map" in " ".join(selection.reasons)


def test_non_python_path_absent_from_the_map_forces_full(repo_map: CoverageMap) -> None:
    for path in ("scripts/launch-env.sh", "configs/coverage_policy.yaml", "docs/TESTING.md"):
        selection = select_tests([path], repo_map, REPO)
        assert selection.mode == FULL, f"{path} was resolved without evidence"


def test_one_unresolvable_path_forces_full_for_the_whole_change(repo_map: CoverageMap) -> None:
    selection = select_tests([MAPPED_SOURCE, "ops/whatever.yaml"], repo_map, REPO)
    assert selection.mode == FULL


def test_missing_map_forces_full() -> None:
    assert select_tests([MAPPED_SOURCE], None, REPO).mode == FULL


def test_empty_map_forces_full() -> None:
    assert select_tests([MAPPED_SOURCE], CoverageMap(source_to_tests={}), REPO).mode == FULL


def test_empty_change_set_forces_full_never_none(repo_map: CoverageMap) -> None:
    selection = select_tests([], repo_map, REPO)
    assert selection.mode == FULL
    assert selection.pytest_paths() == ("tests",)


def test_blank_change_entries_do_not_read_as_a_change_set(repo_map: CoverageMap) -> None:
    assert select_tests(["", "   "], repo_map, REPO).mode == FULL


# --------------------------------------------------------------------------------------
# changed test files
# --------------------------------------------------------------------------------------
def test_changed_test_file_selects_itself(repo_map: CoverageMap) -> None:
    selection = select_tests(["tests/test_drive.py"], repo_map, REPO)
    assert selection.mode == SUBSET
    assert selection.tests is not None
    assert "tests/test_drive.py" in selection.tests


def test_changed_non_test_file_under_tests_forces_full(repo_map: CoverageMap) -> None:
    """A harness/fixture module under tests/ is not a test and has no map entry."""
    selection = select_tests(["tests/counterfeits/harness.py"], repo_map, REPO)
    assert selection.mode == FULL


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/api/test_thing.py", True),
        ("tests/api/thing_test.py", True),
        ("tests/api/helpers.py", False),
        ("tests/conftest.py", False),
        ("omniagentos/api/test_thing.py", False),
        ("tests/api/test_thing.txt", False),
    ],
)
def test_is_test_file(path: str, expected: bool) -> None:
    assert is_test_file(path) is expected


# --------------------------------------------------------------------------------------
# the Selection type refuses to express "run nothing"
# --------------------------------------------------------------------------------------
def test_an_empty_subset_cannot_be_constructed() -> None:
    with pytest.raises(ValueError):
        Selection(mode=SUBSET, tests=frozenset(), reasons=("whatever",))


def test_a_full_selection_cannot_carry_a_test_list() -> None:
    with pytest.raises(ValueError):
        Selection(mode=FULL, tests=frozenset({"tests/x.py"}), reasons=("whatever",))


def test_a_selection_must_state_a_reason() -> None:
    with pytest.raises(ValueError):
        Selection(mode=FULL, tests=None, reasons=())


def test_full_selection_pytest_paths_is_the_suite_root() -> None:
    assert Selection.full_run("because").pytest_paths() == ("tests",)


def test_fraction_over_an_empty_universe_is_undefined() -> None:
    subset = Selection.subset_run({"tests/a/test_x.py"}, ("r",))
    assert subset.fraction_of(0) is None
    assert subset.fraction_of([]) is None
    assert Selection.full_run("r").fraction_of(0) is None


def test_fraction_of_a_full_run_is_one_and_of_a_subset_is_the_ratio() -> None:
    assert Selection.full_run("r").fraction_of(10) == 1.0
    assert Selection.subset_run({"tests/a/test_x.py", "tests/a/test_y.py"}, ("r",)).fraction_of(
        10
    ) == pytest.approx(0.2)


def test_covers_is_true_for_everything_under_a_full_run() -> None:
    assert Selection.full_run("r").covers("tests/anything/test_z.py") is True
    assert Selection.subset_run({"tests/a/test_x.py"}, ("r",)).covers("tests/a/test_y.py") is False
