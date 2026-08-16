"""Integrity of the frozen fixture corpus (T7.0).

The three properties every fixture must have for a baseline to mean anything:

* it is **frozen** — its content digest matches ``frozen_digests.json``, so a
  number captured last month is comparable to one captured today;
* it **discriminates** — the seed FAILS acceptance (otherwise the fixture
  measures nothing) and the reference solution PASSES it (otherwise the fixture
  is unpassable and every arm scores zero for the wrong reason);
* it is **hermetic** — running it reads nothing outside its own workspace.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.benchmarks.fixtures import (
    FIXTURES_DIR,
    Fixture,
    apply_solution,
    fixture_set_digest,
    load_fixtures,
    materialize,
)
from scripts.benchmarks.freeze import build_manifest
from scripts.benchmarks.runner import run_acceptance

FIXTURES = load_fixtures(FIXTURES_DIR)
FIXTURE_IDS = [f.id for f in FIXTURES]


def test_corpus_is_not_empty() -> None:
    assert len(FIXTURES) >= 6, f"expected at least 6 fixtures, found {FIXTURE_IDS}"


def test_corpus_covers_the_required_shapes() -> None:
    kinds = {f.kind for f in FIXTURES}
    for required in ("refactor", "bugfix", "migration"):
        assert required in kinds, f"corpus is missing a {required!r} fixture: {sorted(kinds)}"


def test_corpus_matches_frozen_digests() -> None:
    """Editing a fixture without re-freezing invalidates every captured baseline."""
    frozen = build_manifest(FIXTURES_DIR)
    from scripts.benchmarks.capture_baseline import load_frozen_digests, verify_frozen

    on_disk = load_frozen_digests()
    assert on_disk, "tests/benchmarks/frozen_digests.json is missing; run freeze --write"
    assert verify_frozen(FIXTURES, on_disk) == []
    assert on_disk["set_digest"] == frozen["set_digest"]
    assert on_disk["set_digest"] == fixture_set_digest(FIXTURES)


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_metadata_is_complete(fixture: Fixture) -> None:
    assert fixture.prompt.strip(), f"{fixture.id} has an empty prompt"
    assert fixture.declared_files, f"{fixture.id} declares no files (scope is unscorable)"
    assert fixture.repo_rev, f"{fixture.id} has no repo revision pin"
    assert fixture.acceptance_paths
    assert fixture.solution_dir.is_dir(), f"{fixture.id} has no reference solution"


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_seed_fails_acceptance(fixture: Fixture, tmp_path: Path) -> None:
    """A fixture whose seed already passes measures nothing."""
    workspace = materialize(fixture, tmp_path / "seed-only")
    outcome = run_acceptance(fixture, workspace)
    assert not outcome["passed"], (
        f"{fixture.id}: the seed workspace already passes acceptance — "
        "the fixture cannot discriminate between arms"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_reference_solution_passes_acceptance(fixture: Fixture, tmp_path: Path) -> None:
    """A fixture no correct patch can pass is a broken measurement, not a hard task."""
    workspace = materialize(fixture, tmp_path / "solved")
    apply_solution(fixture, workspace)
    outcome = run_acceptance(fixture, workspace)
    assert outcome["passed"], (
        f"{fixture.id}: the reference solution does NOT pass acceptance\n{outcome['tail']}"
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_solution_stays_inside_declared_files(fixture: Fixture, tmp_path: Path) -> None:
    """The declared set must be sufficient — otherwise every arm is scored as
    undisciplined for doing the only thing that works."""
    from scripts.benchmarks.observe import (
        diff_snapshots,
        snapshot,
        undeclared_changes,
    )

    workspace = materialize(fixture, tmp_path / "scope")
    before = snapshot(workspace)
    apply_solution(fixture, workspace)
    diff = diff_snapshots(before, snapshot(workspace))
    assert undeclared_changes(diff, fixture.declared_files) == [], (
        f"{fixture.id}: the reference solution touches files outside declared_files"
    )
    assert diff.touched, f"{fixture.id}: the reference solution changes nothing"


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_visible_checks_are_restored_before_grading(fixture: Fixture, tmp_path: Path) -> None:
    """An arm that edits a visible check must not be able to buy a pass with it."""
    workspace = materialize(fixture, tmp_path / "tamper")
    apply_solution(fixture, workspace)
    tampered = []
    for rel in fixture.acceptance_paths:
        target = workspace / rel
        if target.exists():
            target.write_text("def test_free_pass() -> None:\n    assert True\n", encoding="utf-8")
            tampered.append(rel)
    outcome = run_acceptance(fixture, workspace)
    assert outcome["passed"], f"{fixture.id}: overlay did not restore {tampered}"
    for rel in tampered:
        assert "test_free_pass" not in (workspace / rel).read_text(encoding="utf-8")


def test_fixture_seeds_are_not_collected_by_this_suite() -> None:
    """Seeds contain deliberately failing checks; the repo suite must never run them."""
    conftest = FIXTURES_DIR / "conftest.py"
    assert conftest.is_file()
    assert "collect_ignore_glob" in conftest.read_text(encoding="utf-8")
    for fixture in FIXTURES:
        for path in list(fixture.seed_dir.rglob("*.py")) + list(fixture.accept_dir.rglob("*.py")):
            assert not path.name.startswith("test_"), (
                f"{path} would be collected by the repo test run; name it check_*.py"
            )


def test_freeze_check_command_passes() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.benchmarks.freeze", "--check"],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
