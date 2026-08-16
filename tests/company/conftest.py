"""Shared fixtures for orgdims company_* tests."""

from __future__ import annotations

import pytest

from omniagentos.reliability.store import SqliteReliabilityStore
from tests.support.db_template import migrated_db


@pytest.fixture
def db_path(tmp_path):
    """A migrated, empty sqlite db file path (never var/omniagentos.db).

    Copied from a pre-migrated template rather than re-applying all 86
    migrations per test; ``SqliteReliabilityStore`` still runs
    ``migrate_connection`` on the copy, so the schema and the checksum
    verification are unchanged.
    """
    return migrated_db(SqliteReliabilityStore, tmp_path / "test.db")


@pytest.fixture
def store(db_path):
    """A SqliteReliabilityStore over the migrated tmp_path db file."""
    s = SqliteReliabilityStore(db_path)
    yield s
    s._connection.close()


@pytest.fixture
def vault_dir(tmp_path):
    d = tmp_path / "vault"
    d.mkdir()
    return str(d)
