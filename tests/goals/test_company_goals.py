"""Company goals spine — store + service unit tests (JG2-BE).

Three decisive rules live here:

* a ``short_term`` goal REQUIRES ``parent_goal_id``; the refusal happens BEFORE
  any write, so the store is byte-for-byte unchanged (row counts before ==
  after);
* that rule survives CONCURRENCY — two writers racing (one flipping the horizon,
  one clearing the parent) must not each validate a stale snapshot and both
  commit. Exactly one succeeds; the loser is refused (sol REWORK blocker);
* the company-goals store NEVER touches the pre-existing steward ``goals``
  table from ``007_steward.sql`` (counterfeit ``cf-steward-goals-repointed``
  repoints it and this file must go red).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.company_goals.models import (
    GOAL_STATUSES,
    HORIZONS,
    CompanyGoalValidationError,
)
from omniagentos.company_goals.seed_employees import SEED_EMPLOYEES, seed_employees
from omniagentos.company_goals.service import CompanyGoalsService
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore

STEWARD_TABLE = "goals"


@pytest.fixture
def dal(store: SqliteStore) -> CompanyGoalsStore:
    return CompanyGoalsStore(store)


@pytest.fixture
def service(dal: CompanyGoalsStore) -> CompanyGoalsService:
    return CompanyGoalsService(dal)


@pytest.fixture
def company(store: SqliteStore) -> str:
    return _seed_company(store)


def _seed_company(store: SqliteStore, company_id: str = "co_test") -> str:
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?,?,?,?,?)",
        (company_id, company_id.replace("co_", ""), "Test Co", "active", utc_now_iso()),
    )
    return company_id


def _counts(store: SqliteStore) -> dict[str, int]:
    return {
        table: int(
            store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        )
        for table in ("employees", "company_goals", "company_goal_jira_links")
    }


def _steward_goal_ids(store: SqliteStore) -> set[str]:
    return {
        str(row[0])
        for row in store._connection.execute(f"SELECT id FROM {STEWARD_TABLE}").fetchall()
    }


def _seed_steward_goal(store: SqliteStore, goal_id: str) -> None:
    """A row in the PRE-EXISTING steward goals table (007_steward.sql)."""
    store._connection.execute(
        f"INSERT INTO {STEWARD_TABLE} "  # noqa: S608
        "(id, name, description, north_star_json, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (goal_id, f"steward {goal_id}", "", "{}", "active", utc_now_iso(), utc_now_iso()),
    )


# ---------------------------------------------------------------------------
# DECISIVE: short_term requires a parent, and refusal writes nothing
# ---------------------------------------------------------------------------


def test_short_term_requires_parent_and_store_unchanged(
    store: SqliteStore, service: CompanyGoalsService, company: str
) -> None:
    before = _counts(store)

    with pytest.raises(CompanyGoalValidationError) as excinfo:
        service.create_goal(
            org_company_id=company, title="Ship JG2 backend", horizon="short_term"
        )

    assert "parent_goal_id" in str(excinfo.value)
    after = _counts(store)
    assert after == before, "a refused short_term goal must leave the store unchanged"
    assert after["company_goals"] == 0


def test_short_term_with_parent_persists(
    service: CompanyGoalsService, company: str
) -> None:
    parent = service.create_goal(
        org_company_id=company, title="Grow revenue", horizon="long_term"
    )
    assert parent.parent_goal_id is None

    child = service.create_goal(
        org_company_id=company,
        title="Ship JG2 backend",
        horizon="short_term",
        parent_goal_id=parent.id,
    )
    assert child.parent_goal_id == parent.id
    assert child.horizon == "short_term"
    assert child.status == "active"
    assert child.id.startswith("cgl_")

    fetched = service.get_goal(child.id)
    assert fetched is not None
    assert fetched.title == "Ship JG2 backend"
    assert [g.id for g in service.list_goals(org_company_id=company, horizon="short_term")] == [
        child.id
    ]


def test_patching_a_goal_to_short_term_without_a_parent_is_refused(
    store: SqliteStore, service: CompanyGoalsService, company: str
) -> None:
    """The invariant belongs to the ROW, not just to the create path."""
    goal = service.create_goal(org_company_id=company, title="North star", horizon="long_term")

    with pytest.raises(CompanyGoalValidationError):
        service.update_goal(goal.id, horizon="short_term")

    assert service.get_goal(goal.id).horizon == "long_term"

    child = service.create_goal(
        org_company_id=company, title="Sub", horizon="short_term", parent_goal_id=goal.id
    )
    with pytest.raises(CompanyGoalValidationError):
        service.update_goal(child.id, parent_goal_id=None)
    assert service.get_goal(child.id).parent_goal_id == goal.id


def test_unknown_horizon_and_status_are_refused_app_side(
    service: CompanyGoalsService, company: str
) -> None:
    assert set(HORIZONS) == {"long_term", "short_term"}
    with pytest.raises(CompanyGoalValidationError):
        service.create_goal(org_company_id=company, title="Bogus", horizon="mid_term")

    goal = service.create_goal(org_company_id=company, title="Real", horizon="long_term")
    with pytest.raises(CompanyGoalValidationError):
        service.update_goal(goal.id, status="not-a-status")
    for status in GOAL_STATUSES:
        assert service.update_goal(goal.id, status=status).status == status


def test_unknown_company_or_parent_is_refused_before_write(
    store: SqliteStore, service: CompanyGoalsService, company: str
) -> None:
    before = _counts(store)
    with pytest.raises(CompanyGoalValidationError):
        service.create_goal(org_company_id="co_ghost", title="Orphan", horizon="long_term")
    with pytest.raises(CompanyGoalValidationError):
        service.create_goal(
            org_company_id=company,
            title="Orphan child",
            horizon="short_term",
            parent_goal_id="cgl_ghost",
        )
    assert _counts(store) == before


def test_goal_cannot_parent_itself_or_close_a_cycle(
    service: CompanyGoalsService, company: str
) -> None:
    root = service.create_goal(org_company_id=company, title="Root", horizon="long_term")
    mid = service.create_goal(
        org_company_id=company, title="Mid", horizon="short_term", parent_goal_id=root.id
    )
    leaf = service.create_goal(
        org_company_id=company, title="Leaf", horizon="short_term", parent_goal_id=mid.id
    )

    with pytest.raises(CompanyGoalValidationError):
        service.update_goal(mid.id, parent_goal_id=mid.id)
    with pytest.raises(CompanyGoalValidationError):
        service.update_goal(root.id, parent_goal_id=leaf.id)

    assert service.get_goal(mid.id).parent_goal_id == root.id


def test_parent_from_another_company_is_refused(
    store: SqliteStore, service: CompanyGoalsService, company: str
) -> None:
    """Goals ladder INSIDE a company — a cross-company parent is not a ladder."""
    other = _seed_company(store, "co_other")
    foreign_parent = service.create_goal(
        org_company_id=other, title="Other company north star", horizon="long_term"
    )
    before = _counts(store)

    with pytest.raises(CompanyGoalValidationError) as excinfo:
        service.create_goal(
            org_company_id=company,
            title="Cross-company child",
            horizon="short_term",
            parent_goal_id=foreign_parent.id,
        )
    assert "company" in str(excinfo.value)
    assert _counts(store) == before

    local_parent = service.create_goal(
        org_company_id=company, title="Local north star", horizon="long_term"
    )
    child = service.create_goal(
        org_company_id=company,
        title="Local child",
        horizon="short_term",
        parent_goal_id=local_parent.id,
    )
    with pytest.raises(CompanyGoalValidationError):
        service.update_goal(child.id, parent_goal_id=foreign_parent.id)
    assert service.get_goal(child.id).parent_goal_id == local_parent.id


# ---------------------------------------------------------------------------
# DECISIVE (sol REWORK blocker): the invariant survives concurrent writers
# ---------------------------------------------------------------------------


def _invariant_holds(store: SqliteStore, goal_id: str) -> bool:
    row = store._connection.execute(
        "SELECT horizon, parent_goal_id FROM company_goals WHERE id = ?", (goal_id,)
    ).fetchone()
    return not (row["horizon"] == "short_term" and row["parent_goal_id"] is None)


def _racing_goal(service: CompanyGoalsService, company: str) -> str:
    """A long_term goal WITH a parent: both racing edits are legal on it alone."""
    parent = service.create_goal(org_company_id=company, title="Root", horizon="long_term")
    target = service.create_goal(
        org_company_id=company,
        title="Racy",
        horizon="long_term",
        parent_goal_id=parent.id,
    )
    return target.id


def test_concurrent_patch_cannot_strand_a_short_term_goal_without_a_parent(
    store: SqliteStore,
    service: CompanyGoalsService,
    company: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two in-process writers, deliberately interleaved at the validation seam.

    Writer A flips ``horizon`` to ``short_term``; writer B clears
    ``parent_goal_id``. EITHER edit alone is legal on the starting row; together
    they produce a parentless ``short_term`` goal — the exact defect that
    read-merge-validate-write over a STALE snapshot admits.

    The first validator is held for a bounded moment so B is guaranteed to reach
    its own read while A is still mid-update. If A's read/validate/write is not
    one atomic unit, B reads the pre-A row and both writers commit. The hold is
    bounded (never a barrier) so the correct implementation — which keeps the
    write lock across the whole sequence — simply makes B wait instead of
    deadlocking.
    """
    from omniagentos.company_goals import service as service_module

    target_id = _racing_goal(service, company)

    real_validate = service_module.validate_goal_shape
    entered_validation = threading.Event()
    call_lock = threading.Lock()
    calls = {"n": 0}

    def interleaving_validate(**kwargs: Any) -> list[str]:
        with call_lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            entered_validation.set()
            time.sleep(0.4)
        return real_validate(**kwargs)

    monkeypatch.setattr(service_module, "validate_goal_shape", interleaving_validate)

    outcomes: dict[str, str] = {}
    outcome_lock = threading.Lock()

    def writer(name: str, wait_for_first: bool, **patch: Any) -> None:
        # A separate service/DAL per thread over the SAME SqliteStore — exactly
        # how the API serves two concurrent requests.
        local = CompanyGoalsService(CompanyGoalsStore(store))
        if wait_for_first:
            entered_validation.wait(timeout=10)
        try:
            local.update_goal(target_id, **patch)
            outcome = "committed"
        except CompanyGoalValidationError:
            outcome = "refused"
        with outcome_lock:
            outcomes[name] = outcome

    threads = [
        threading.Thread(
            target=writer, args=("flip_horizon", False), kwargs={"horizon": "short_term"}
        ),
        threading.Thread(
            target=writer, args=("clear_parent", True), kwargs={"parent_goal_id": None}
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "writer thread deadlocked"

    assert sorted(outcomes) == ["clear_parent", "flip_horizon"]
    assert sorted(outcomes.values()) == ["committed", "refused"], (
        f"exactly one writer may win, got {outcomes}"
    )
    assert _invariant_holds(store, target_id), (
        "concurrent patches produced a parentless short_term goal"
    )


# Child writer for the cross-process probe. The delay is injected HERE, in
# test-owned code, so production carries no test seam: it wraps the service
# module's validator exactly like the in-process probe does.
_PROCESS_WRITER = """
import sys, time
from omniagentos.company_goals import service as service_module
from omniagentos.company_goals.models import CompanyGoalValidationError
from omniagentos.company_goals.service import CompanyGoalsService
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore

db_path, goal_id, mode, start_at, delay = (
    sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5])
)

real_validate = service_module.validate_goal_shape
state = {"n": 0}


def slow_validate(**kwargs):
    state["n"] += 1
    if state["n"] == 1 and delay:
        time.sleep(delay)
    return real_validate(**kwargs)


service_module.validate_goal_shape = slow_validate

service = CompanyGoalsService(CompanyGoalsStore(SqliteStore(db_path)))
patch = {"horizon": "short_term"} if mode == "flip" else {"parent_goal_id": None}
time.sleep(max(0.0, start_at - time.time()))
try:
    service.update_goal(goal_id, **patch)
    print("committed", flush=True)
except CompanyGoalValidationError:
    print("refused", flush=True)
"""


def test_concurrent_patch_across_processes_is_also_serialized(
    store: SqliteStore, service: CompanyGoalsService, company: str
) -> None:
    """The same probe across PROCESSES — an in-process lock cannot cover this.

    Only the ``BEGIN IMMEDIATE`` write lock plus an in-transaction re-read makes
    the second writer see the first writer's committed row.
    """
    db_path = store._db_path
    target_id = _racing_goal(service, company)

    repo_root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo_root)}
    start_at = time.time() + 0.75
    # The flipper holds its validation for 0.6s (well inside the 5s busy
    # timeout), so the clearer is guaranteed to reach its own read while the
    # flipper is mid-update.
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _PROCESS_WRITER,
                db_path,
                target_id,
                mode,
                str(start_at),
                delay,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(repo_root),
        )
        for mode, delay in (("flip", "0.6"), ("clear", "0"))
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, f"writer crashed: {stderr}"
        outputs.append(stdout.strip())

    assert sorted(outputs) == ["committed", "refused"], (
        f"exactly one process may win, got {outputs}"
    )
    assert _invariant_holds(store, target_id)


# ---------------------------------------------------------------------------
# COUNTERFEIT TARGET: the steward goals table is not ours
# ---------------------------------------------------------------------------


def test_store_never_touches_steward_goals_table(
    store: SqliteStore, service: CompanyGoalsService, company: str
) -> None:
    """cf-steward-goals-repointed: repointing the store at ``goals`` must go RED.

    The steward table from 007_steward.sql belongs to the Horizon-4 steward.
    Its exact row-id set must be identical before and after company-goals writes.
    """
    _seed_steward_goal(store, "goal_steward_1")
    _seed_steward_goal(store, "goal_steward_2")
    before_ids = _steward_goal_ids(store)
    assert before_ids == {"goal_steward_1", "goal_steward_2"}

    try:
        parent = service.create_goal(
            org_company_id=company, title="Company north star", horizon="long_term"
        )
        child = service.create_goal(
            org_company_id=company,
            title="This quarter",
            horizon="short_term",
            parent_goal_id=parent.id,
        )
        service.update_goal(child.id, status="paused")
        service.create_jira_link(
            goal_id=child.id, jira_project_key="HOO", link_kind="project"
        )
    except sqlite3.Error as exc:  # pragma: no cover — counterfeit path
        pytest.fail(f"company-goals write reached the steward goals table: {exc}")

    assert _steward_goal_ids(store) == before_ids, (
        "steward goals table mutated by company_goals writes — "
        "the company-goals store must never touch it"
    )
    assert _counts(store)["company_goals"] == 2
    assert {goal.id for goal in service.list_goals(org_company_id=company)} == {
        parent.id,
        child.id,
    }
    assert not (before_ids & {parent.id, child.id})


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------


def test_seed_employees_is_idempotent(dal: CompanyGoalsStore) -> None:
    first = seed_employees(dal)
    assert first["created"] == 4
    second = seed_employees(dal)
    assert second["created"] == 0

    employees = dal.list_employees()
    # list_employees orders by ``name ASC`` under SQLite's case-sensitive
    # BINARY collation, so uppercase-leading names sort before "the operator".
    assert [e["name"] for e in employees] == ["Alice", "Bob", "Frank", "the operator"]
    assert {e["id"] for e in employees} == {spec["id"] for spec in SEED_EMPLOYEES}
    assert all(e["jira_account_id"] is None for e in employees)
    assert all(e["status"] == "active" for e in employees)


def test_seed_employees_adopts_an_existing_row_by_name(dal: CompanyGoalsStore) -> None:
    """Another lane may have created 'the operator' first — never duplicate the person."""
    dal.create_employee(name="the operator", role="operator")
    counts = seed_employees(dal)
    assert counts["created"] == 3
    names = [e["name"] for e in dal.list_employees()]
    assert names.count("the operator") == 1


def test_seed_employees_is_safe_under_simultaneous_boots(store: SqliteStore) -> None:
    """Two API processes booting at once both seed silently (atomic upsert).

    Check-then-insert loses this race: both callers see "absent", both INSERT,
    and the loser takes an IntegrityError on the UNIQUE name — a crash in the
    startup path.
    """
    barrier = threading.Barrier(2, timeout=10)
    results: list[dict[str, int]] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def boot() -> None:
        dal = CompanyGoalsStore(store)
        barrier.wait()
        try:
            counts = seed_employees(dal)
        except BaseException as exc:  # noqa: BLE001 — collected for assertion
            with lock:
                failures.append(exc)
            return
        with lock:
            results.append(counts)

    threads = [threading.Thread(target=boot) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert failures == [], f"simultaneous seeding raised: {failures!r}"
    assert len(results) == 2
    # The roster is seeded exactly once between them, whichever wins each row.
    assert sum(counts["created"] for counts in results) == 4
    assert sum(counts["existing"] for counts in results) == 4
    employees = CompanyGoalsStore(store).list_employees()
    assert [e["name"] for e in employees] == ["Alice", "Bob", "Frank", "the operator"]


# ---------------------------------------------------------------------------
# Jira links
# ---------------------------------------------------------------------------


def test_jira_links_create_list_delete(service: CompanyGoalsService, company: str) -> None:
    goal = service.create_goal(org_company_id=company, title="Root", horizon="long_term")
    link = service.create_jira_link(
        goal_id=goal.id, jira_project_key="HOO", link_kind="project"
    )
    assert link.jira_issue_key is None
    assert link.id.startswith("cgj_")

    issue_link = service.create_jira_link(
        goal_id=goal.id,
        jira_project_key="HOO",
        jira_issue_key="HOO-12",
        link_kind="issue",
    )
    assert {link.id, issue_link.id} == {row.id for row in service.list_jira_links(goal.id)}

    with pytest.raises(CompanyGoalValidationError):
        service.create_jira_link(
            goal_id=goal.id, jira_project_key="HOO", link_kind="project"
        )
    with pytest.raises(CompanyGoalValidationError):
        service.create_jira_link(
            goal_id="cgl_ghost", jira_project_key="ACM", link_kind="project"
        )

    assert service.delete_jira_link(goal.id, link.id) is True
    assert [row.id for row in service.list_jira_links(goal.id)] == [issue_link.id]
    assert service.delete_jira_link(goal.id, link.id) is False


def test_issue_key_must_belong_to_its_project_key(
    store: SqliteStore, service: CompanyGoalsService, company: str
) -> None:
    """A Jira issue key names its own project — 'HOO-42' is not an ACM issue."""
    goal = service.create_goal(org_company_id=company, title="Root", horizon="long_term")
    before = _counts(store)

    with pytest.raises(CompanyGoalValidationError) as excinfo:
        service.create_jira_link(
            goal_id=goal.id,
            jira_project_key="ACM",
            jira_issue_key="HOO-42",
            link_kind="issue",
        )
    assert "jira_issue_key" in str(excinfo.value)
    with pytest.raises(CompanyGoalValidationError):
        service.create_jira_link(
            goal_id=goal.id,
            jira_project_key="HOO",
            jira_issue_key="HOO",  # no issue number at all
            link_kind="issue",
        )
    assert _counts(store) == before


def test_the_same_issue_cannot_be_linked_twice_under_different_project_keys(
    store: SqliteStore, service: CompanyGoalsService, company: str
) -> None:
    """Service refuses it; migration 098's (goal_id, issue) index is the backstop."""
    goal = service.create_goal(org_company_id=company, title="Root", horizon="long_term")
    service.create_jira_link(
        goal_id=goal.id,
        jira_project_key="HOO",
        jira_issue_key="HOO-42",
        link_kind="issue",
    )
    with pytest.raises(CompanyGoalValidationError):
        service.create_jira_link(
            goal_id=goal.id,
            jira_project_key="HOO",
            jira_issue_key="HOO-42",
            link_kind="issue",
        )

    # Far side: the index refuses it even if a writer bypasses the service and
    # smuggles the duplicate in under another project key.
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO company_goal_jira_links "
            "(id, goal_id, jira_project_key, jira_issue_key, link_kind, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("cgj_smuggled", goal.id, "ACM", "HOO-42", "issue", utc_now_iso()),
        )
    assert _counts(store)["company_goal_jira_links"] == 1


def test_list_endpoints_honour_a_limit(
    dal: CompanyGoalsStore, service: CompanyGoalsService, company: str
) -> None:
    goal = service.create_goal(org_company_id=company, title="Root", horizon="long_term")
    for index in range(3):
        service.create_jira_link(
            goal_id=goal.id,
            jira_project_key="HOO",
            jira_issue_key=f"HOO-{index}",
            link_kind="issue",
        )
    seed_employees(dal)

    assert len(service.list_jira_links(goal.id, limit=2)) == 2
    assert len(service.list_jira_links(goal.id)) == 3
    assert len(service.list_employees(limit=1)) == 1
    assert len(service.list_employees()) == 4
