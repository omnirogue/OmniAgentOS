from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from tests.support.db_template import make_store


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "revenue.db")
