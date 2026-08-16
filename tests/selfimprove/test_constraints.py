"""append_constraint / append_constraint_from_run_dir
(omniagentos/selfimprove/constraints.py)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omniagentos.selfimprove.constraints import append_constraint, append_constraint_from_run_dir
from omniagentos.selfimprove.errors import UnverifiedCaptureError
from omniagentos.selfimprove.models import GateStatus
from omniagentos.selfimprove.paths import PathEscapesRootError, constraints_path

from .helpers import sample_gate, write_status_json


def test_append_constraint_creates_file_with_header_and_entry(tmp_path: Path) -> None:
    gate = sample_gate()

    path = append_constraint(
        "OmniAgentOS",
        "Always run pytest -q before claiming a migration is safe.",
        gate,
        constraints_dir=str(tmp_path),
    )

    # NOTE: the directory is `<slug(OmniAgentOS)>`, not the literal project
    # name — safe_slug() always suffixes a digest (F4, path.safe_slug
    # injectivity fix), so compare against the real helper rather than a
    # hardcoded slug.
    assert path == constraints_path("OmniAgentOS", constraints_dir=str(tmp_path))
    assert path.parent.parent == tmp_path
    content = path.read_text(encoding="utf-8")
    assert content.startswith("# CONSTRAINTS — OmniAgentOS")
    assert "Always run pytest -q before claiming a migration is safe." in content
    assert gate.source_run_id in content
    assert "gate evidence" in content


def test_append_constraint_appends_without_rewriting_prior_content(tmp_path: Path) -> None:
    gate = sample_gate()

    path = append_constraint("proj", "Rule one.", gate, constraints_dir=str(tmp_path))
    original = path.read_text(encoding="utf-8")
    # simulate a human hand-edit above the auto-grown section
    path.write_text(original + "\nHuman note: keep this.\n", encoding="utf-8")

    append_constraint("proj", "Rule two.", gate, constraints_dir=str(tmp_path))

    final = path.read_text(encoding="utf-8")
    assert "Human note: keep this." in final
    assert "Rule one." in final
    assert "Rule two." in final
    assert final.index("Rule one.") < final.index("Rule two.")


def test_append_constraint_refuses_unverified_gate_and_writes_nothing(tmp_path: Path) -> None:
    gate = sample_gate(status=GateStatus.FAILED)

    with pytest.raises(UnverifiedCaptureError):
        append_constraint("proj", "Some rule.", gate, constraints_dir=str(tmp_path))

    assert not (tmp_path / "proj").exists()


def test_append_constraint_rejects_blank_rule(tmp_path: Path) -> None:
    gate = sample_gate()

    with pytest.raises(ValueError, match="non-empty"):
        append_constraint("proj", "   ", gate, constraints_dir=str(tmp_path))


def test_append_constraint_refuses_pending_gate_and_writes_nothing(tmp_path: Path) -> None:
    gate = sample_gate(status=GateStatus.PENDING)

    with pytest.raises(UnverifiedCaptureError):
        append_constraint("proj", "Some rule.", gate, constraints_dir=str(tmp_path))

    assert not (tmp_path / "proj").exists()


def test_append_constraint_is_idempotent_for_a_repeated_rule(tmp_path: Path) -> None:
    """F5: calling append_constraint twice with the same (project, rule)
    must not append a second duplicate entry."""
    gate = sample_gate()

    path = append_constraint("proj", "Rule.", gate, constraints_dir=str(tmp_path))
    append_constraint("proj", "Rule.", gate, constraints_dir=str(tmp_path))
    append_constraint("proj", "Rule.", gate, constraints_dir=str(tmp_path))

    content = path.read_text(encoding="utf-8")
    assert content.count("- Rule.") == 1


def test_append_constraint_repeated_call_returns_same_path(tmp_path: Path) -> None:
    gate = sample_gate()

    first = append_constraint("proj", "Rule.", gate, constraints_dir=str(tmp_path))
    second = append_constraint("proj", "Rule.", gate, constraints_dir=str(tmp_path))

    assert first == second


def test_append_constraint_distinct_rules_for_same_project_both_land(tmp_path: Path) -> None:
    gate = sample_gate()

    path = append_constraint("proj", "Rule one.", gate, constraints_dir=str(tmp_path))
    append_constraint("proj", "Rule two.", gate, constraints_dir=str(tmp_path))

    content = path.read_text(encoding="utf-8")
    assert content.count("- Rule one.") == 1
    assert content.count("- Rule two.") == 1


def test_append_constraint_refuses_pre_existing_symlinked_project_directory(tmp_path: Path) -> None:
    """F3: a pre-existing symlink at <constraints_dir>/<project-slug> must
    not let a write escape constraints_dir."""
    gate = sample_gate()
    constraints_dir = tmp_path / "constraints"
    constraints_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    slug_dir_name = constraints_path("proj", constraints_dir=str(constraints_dir)).parent.name
    (constraints_dir / slug_dir_name).symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathEscapesRootError):
        append_constraint("proj", "Some rule.", gate, constraints_dir=str(constraints_dir))

    assert not (outside / "CONSTRAINTS.md").exists()


def test_append_constraint_refuses_pre_existing_symlinked_constraints_file(tmp_path: Path) -> None:
    gate = sample_gate()
    constraints_dir = tmp_path / "constraints"
    outside_file = tmp_path / "outside-CONSTRAINTS.md"
    outside_file.write_text("do not touch\n", encoding="utf-8")
    target = constraints_path("proj", constraints_dir=str(constraints_dir))
    target.parent.mkdir(parents=True)
    target.symlink_to(outside_file)

    with pytest.raises((PathEscapesRootError, OSError)):
        append_constraint("proj", "Some rule.", gate, constraints_dir=str(constraints_dir))

    assert outside_file.read_text(encoding="utf-8") == "do not touch\n"


def test_append_constraint_concurrent_first_writes_do_not_lose_entries(tmp_path: Path) -> None:
    """F6: two callers racing to create the SAME project's CONSTRAINTS.md
    for the first time must not truncate/overwrite each other's entry —
    every rule submitted must survive."""
    gate = sample_gate()
    constraints_dir = tmp_path / "constraints"
    n = 12
    rules = [f"Concurrent rule number {i}." for i in range(n)]

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(
            pool.map(
                lambda rule: append_constraint(
                    "shared-proj", rule, gate, constraints_dir=str(constraints_dir)
                ),
                rules,
            )
        )

    path = constraints_path("shared-proj", constraints_dir=str(constraints_dir))
    content = path.read_text(encoding="utf-8")
    assert content.count("# CONSTRAINTS — shared-proj") == 1
    for rule in rules:
        assert content.count(f"- {rule}") == 1, f"missing or duplicated: {rule!r}"


def test_append_constraint_survives_a_simulated_partial_prior_write(tmp_path: Path) -> None:
    """A process interrupted mid-write in a PRIOR run can leave a truncated
    fragment on disk; the next call must still succeed and append a
    well-formed new entry rather than crashing or silently losing it."""
    gate = sample_gate()

    path = append_constraint("proj", "Rule one.", gate, constraints_dir=str(tmp_path))
    original = path.read_text(encoding="utf-8")
    # Simulate a crash mid-write() that left a truncated final fragment.
    path.write_text(original[:-5], encoding="utf-8")

    append_constraint("proj", "Rule two.", gate, constraints_dir=str(tmp_path))

    final = path.read_text(encoding="utf-8")
    assert "- Rule two." in final


def test_project_names_are_slugged_for_the_directory(tmp_path: Path) -> None:
    gate = sample_gate()

    path = append_constraint("weird/project name!!", "Rule.", gate, constraints_dir=str(tmp_path))

    assert path.parent.parent == tmp_path
    assert "/" not in path.parent.name
    assert path.parent.is_relative_to(tmp_path)


def test_append_constraint_from_run_dir_reads_real_status_json(tmp_path: Path) -> None:
    run_dir = tmp_path / "session-1"
    write_status_json(run_dir, state="done")
    constraints_dir = tmp_path / "constraints"

    path = append_constraint_from_run_dir(
        str(run_dir), "proj", "Rule from a real run dir.", constraints_dir=str(constraints_dir)
    )

    assert "Rule from a real run dir." in path.read_text(encoding="utf-8")


def test_append_constraint_from_run_dir_refuses_when_state_is_failed(tmp_path: Path) -> None:
    run_dir = tmp_path / "session-2"
    write_status_json(run_dir, state="failed")
    constraints_dir = tmp_path / "constraints"

    with pytest.raises(UnverifiedCaptureError):
        append_constraint_from_run_dir(
            str(run_dir), "proj", "Should not land.", constraints_dir=str(constraints_dir)
        )

    assert not constraints_dir.exists()
