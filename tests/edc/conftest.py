"""Fixtures for the Executive Decision Center substrate (migration 130).

All stores share ONE :class:`SqliteStore` (one connection, one writer lock), the
composition the production wiring uses. The store auto-migrates on construction,
so migration 130 is applied for every test simply by building it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore
from omniagentos.edc.store import DecisionStore
from omniagentos.steward.store import StewardStore


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "edc.db"))


@pytest.fixture
def decisions(store: SqliteStore) -> DecisionStore:
    return DecisionStore(store)


@pytest.fixture
def steward(store: SqliteStore) -> StewardStore:
    return StewardStore(store)


@pytest.fixture
def employees(store: SqliteStore) -> dict[str, str]:
    """Seed the roster rows the owner FKs reference."""
    goals = CompanyGoalsStore(store)
    goals.ensure_employee(employee_id="emp_owner", name="the operator", role="operator")
    goals.ensure_employee(employee_id="emp_bob", name="Bob", role="candidate-author")
    goals.ensure_employee(employee_id="emp_alice", name="Alice", role="reviewer-merger")
    return {"owner": "emp_owner", "bob": "emp_bob", "alice": "emp_alice"}


def make_decision(**overrides: object) -> dict[str, object]:
    """A minimal valid Decision payload; override any field."""
    payload: dict[str, object] = {
        "owner_employee_id": "emp_owner",
        "source": "email",
        "source_ref": "msg-1",
        "title": "AWS payment method expired",
        "classification": "needs_owner",
        "recommended": {"kind": "reply", "human_line": "update the card"},
    }
    payload.update(overrides)
    return payload
