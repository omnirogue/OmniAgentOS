"""Structural validation of the production-reset data file, independent of the importer.

This is the file's own contract: every task carries the fields the importer and a human reading
the board both need, refs are unique (they are the idempotency key), every hierarchy/goal
reference resolves inside the file, and the roster/company vocabularies match what the live
schema actually accepts. A defect here is a defect in the DATA, not in the import logic — keeping
it a separate test module from ``test_import.py`` means a broken file fails fast without needing a
database at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

DATA_DIR = Path(__file__).resolve().parents[2] / "omniagentos" / "team" / "data"
YAML_PATH = DATA_DIR / "production_reset_2026_08_10.yaml"
DEV_QUEUE_YAML_PATH = DATA_DIR / "dev_queue_2026_08_13.yaml"
ALL_YAML_PATHS = (YAML_PATH, DEV_QUEUE_YAML_PATH)

KNOWN_OWNERS = frozenset({"emp_owner", "emp_alice", "emp_bob"})
KNOWN_COMPANY_SLUGS = frozenset(
    {"initech", "globex", "acmeuni", "hooli", "omniagentos", "personal"}
)
REQUIRED_TASK_FIELDS = ("ref", "owner", "title", "acceptance_criteria", "size")


def _load(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module", params=ALL_YAML_PATHS, ids=lambda p: p.name)
def data(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Every structural contract test below runs once per file in ``ALL_YAML_PATHS``.

    Tests that assert something true of ONE specific file only (the production file's header
    text, its exact 3-row baseline stanza) load that file directly instead of using this fixture.
    """
    return _load(request.param)


def test_yaml_loads_as_a_mapping_with_the_three_top_level_keys(data: dict[str, Any]) -> None:
    for key in ("companies", "goals", "tasks"):
        assert key in data
        assert isinstance(data[key], list)
        assert data[key], f"{key} must not be empty"


def test_yaml_header_documents_machine_and_human_owned_paths_storage() -> None:
    text = YAML_PATH.read_text(encoding="utf-8")
    assert "`org_json.owned_paths` for machine attribution" in text
    assert 'trailing "Owned paths:" line for humans' in text


def test_companies_are_a_subset_of_the_six_known_slugs(data: dict[str, Any]) -> None:
    slugs = {str(c["slug"]) for c in data["companies"]}
    assert slugs, "companies list is empty"
    assert slugs <= KNOWN_COMPANY_SLUGS
    for company in data["companies"]:
        assert str(company["slug"]).strip()
        assert str(company["name"]).strip()


def test_goal_companies_are_also_a_subset_of_the_six_known_slugs(data: dict[str, Any]) -> None:
    for goal in data["goals"]:
        assert str(goal["company"]) in KNOWN_COMPANY_SLUGS, goal["ref"]


def test_goal_refs_are_unique_and_parents_resolve(data: dict[str, Any]) -> None:
    refs = [str(g["ref"]) for g in data["goals"]]
    assert len(refs) == len(set(refs)), "duplicate goal ref"
    ref_set = set(refs)
    for goal in data["goals"]:
        parent = goal.get("parent")
        if parent:
            assert str(parent) in ref_set, f"goal {goal['ref']} names an unresolved parent"


def test_goal_horizons_and_owners_are_valid(data: dict[str, Any]) -> None:
    for goal in data["goals"]:
        assert goal["horizon"] in ("long_term", "short_term"), goal["ref"]
        assert goal["owner"] in KNOWN_OWNERS, goal["ref"]
        if goal["horizon"] == "short_term":
            assert goal.get("parent"), f"short_term goal {goal['ref']} has no parent"


def test_every_task_has_the_required_fields(data: dict[str, Any]) -> None:
    for task in data["tasks"]:
        for field_name in REQUIRED_TASK_FIELDS:
            assert field_name in task, f"task missing {field_name!r}: {task.get('ref')}"
            if field_name != "acceptance_criteria":
                # acceptance_criteria is legitimately "" for the baseline stanza (see
                # scripts/import-reset-queue.py's docstring on why the create-time done-gate
                # requires that); every other required field must be genuinely non-empty.
                assert str(task[field_name]).strip(), f"{field_name} is empty: {task['ref']}"


def test_task_refs_are_unique(data: dict[str, Any]) -> None:
    refs = [str(t["ref"]) for t in data["tasks"]]
    assert len(refs) == len(set(refs)), "duplicate task ref"


def test_parent_task_ref_always_names_a_task_defined_in_this_file(data: dict[str, Any]) -> None:
    refs = {str(t["ref"]) for t in data["tasks"]}
    for task in data["tasks"]:
        parent_ref = task.get("parent_task_ref")
        if parent_ref:
            assert str(parent_ref) in refs, f"task {task['ref']} names unresolved parent {parent_ref!r}"
            # The schema's hierarchy is exactly one level deep: a parent has no parent.
            parent = next(t for t in data["tasks"] if str(t["ref"]) == str(parent_ref))
            assert not parent.get("parent_task_ref"), (
                f"{parent_ref} is itself a subtask — hierarchy must stay one level deep"
            )


def test_depends_on_always_names_a_task_defined_in_this_file(data: dict[str, Any]) -> None:
    refs = {str(t["ref"]) for t in data["tasks"]}
    for task in data["tasks"]:
        for dep in task.get("depends_on") or []:
            assert str(dep) in refs, f"task {task['ref']} depends on unresolved ref {dep!r}"


def test_task_owners_are_in_the_known_roster(data: dict[str, Any]) -> None:
    # A pool card (owner: null) is deliberately unowned — see
    # test_ownerless_pool_tasks_carry_goal_ref_and_acceptance_criteria below.
    for task in data["tasks"]:
        owner = task["owner"]
        if owner is not None:
            assert owner in KNOWN_OWNERS, task["ref"]


def test_ownerless_pool_tasks_carry_goal_ref_and_acceptance_criteria(
    data: dict[str, Any],
) -> None:
    """A pool card with no owner and no goal_ref/acceptance_criteria can never be claimed
    sensibly off the board — every unowned task must carry both."""
    for task in data["tasks"]:
        if task.get("owner") is None:
            assert task.get("goal_ref"), f"ownerless task {task['ref']} has no goal_ref"
            assert str(task.get("acceptance_criteria") or "").strip(), (
                f"ownerless task {task['ref']} has no acceptance_criteria"
            )


def test_task_sizes_are_in_the_schema_vocabulary(data: dict[str, Any]) -> None:
    for task in data["tasks"]:
        assert task["size"] in ("S", "M", "L"), task["ref"]


def test_task_goal_ref_always_resolves_when_present(data: dict[str, Any]) -> None:
    goal_refs = {str(g["ref"]) for g in data["goals"]}
    for task in data["tasks"]:
        goal_ref = task.get("goal_ref")
        if goal_ref:
            assert str(goal_ref) in goal_refs, f"task {task['ref']} names unresolved goal {goal_ref!r}"


def test_blocked_tasks_carry_a_non_empty_blocked_reason(data: dict[str, Any]) -> None:
    """Mirrors the live store rule (``CollabStore._validate_team_rules``): an owned card entering
    Blocked without a reason is refused, so the data file must never describe one."""
    for task in data["tasks"]:
        if task.get("status") == "blocked":
            assert str(task.get("blocked_reason") or "").strip(), (
                f"blocked task {task['ref']} has no blocked_reason"
            )


def test_open_and_done_tasks_carry_no_blocked_reason(data: dict[str, Any]) -> None:
    for task in data["tasks"]:
        if task.get("status") in ("open", "done"):
            assert not task.get("blocked_reason"), (
                f"non-blocked task {task['ref']} unexpectedly carries a blocked_reason"
            )


def test_baseline_tasks_are_done_with_empty_acceptance_and_evidence_block(
    data: dict[str, Any],
) -> None:
    baseline = [t for t in data["tasks"] if str(t.get("source")) == "baseline-2026-08-03"]
    if not baseline:
        # Only production_reset_2026_08_10.yaml carries the baseline stanza — a curated queue
        # file (e.g. dev_queue_2026_08_13.yaml) legitimately has none.
        return
    assert len(baseline) == 3, "expected exactly 3 baseline summary tasks"
    seen_owners = set()
    for task in baseline:
        assert task["status"] == "done"
        assert task["acceptance_criteria"] == ""
        evidence = task.get("baseline_evidence")
        assert evidence and evidence.get("ref") and evidence.get("title")
        seen_owners.add(task["owner"])
    assert seen_owners == KNOWN_OWNERS


def test_parents_exist_for_every_declared_parent(data: dict[str, Any]) -> None:
    """The brief's own phrasing: 'parents exist for every parent_task_id ref'."""
    refs = {str(t["ref"]) for t in data["tasks"]}
    parent_refs = {str(t["parent_task_ref"]) for t in data["tasks"] if t.get("parent_task_ref")}
    missing = parent_refs - refs
    assert not missing, f"parent refs with no defining task: {sorted(missing)}"
