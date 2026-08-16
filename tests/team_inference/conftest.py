from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.team.store import TeamStore


@pytest.fixture
def collab_store(tmp_path: Path) -> CollabStore:
    return CollabStore(str(tmp_path / "inference.db"))


@pytest.fixture
def team_store(collab_store: CollabStore) -> TeamStore:
    return TeamStore(collab_store._store)


@pytest.fixture
def employees(collab_store: CollabStore) -> dict[str, str]:
    goals = CompanyGoalsStore(collab_store._store)
    roster = {"alice": "emp_alice", "bob": "emp_bob"}
    for name, employee_id in roster.items():
        goals.ensure_employee(employee_id=employee_id, name=name.title(), role="engineer")
    return roster
