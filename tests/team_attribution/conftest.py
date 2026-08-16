"""Fixtures for Team Work OS evidence attribution tests."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore
from tests.support.db_template import make_store


@pytest.fixture
def collab_store(tmp_path: Path) -> CollabStore:
    return make_store(CollabStore, tmp_path / "team-attribution.db")


@pytest.fixture
def store(collab_store: CollabStore) -> SqliteStore:
    return collab_store._store


@pytest.fixture
def team_store(store: SqliteStore) -> TeamStore:
    return TeamStore(store)


@pytest.fixture
def make_card(collab_store: CollabStore) -> Callable[..., BoardTask]:
    def factory(**fields: Any) -> BoardTask:
        fields.setdefault("title", "A card")
        card = BoardTask(**fields)
        collab_store.create_board_task(card)
        return card

    return factory


@pytest.fixture
def make_open_task() -> Callable[..., dict]:
    def factory(**fields: Any) -> dict:
        task = {
            "id": "btk_default",
            "ref": None,
            "status": "open",
            "owner_employee_id": None,
            "result_ref": None,
            "run_id": None,
            "created_at": "2026-08-10T12:00:00Z",
            "updated_at": "2026-08-10T12:00:00Z",
            "owned_paths": [],
            "branches": [],
        }
        task.update(fields)
        return task

    return factory


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "owner@initech.example"], check=True
    )
    return repo
