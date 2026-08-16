"""M-40 — metacog multi-statement writes must be all-or-nothing.

The store's connection runs in autocommit mode, so ``Connection.commit()`` is a
no-op and every ``execute`` used to be its own transaction. Registering an
artifact writes an envelope plus one provenance edge per input, and a failure
mid-loop committed the envelope with a partial edge set. That state was
*permanent*, not merely temporary: the identity dedupe at the top of
``register_artifact`` matches the committed envelope on every retry and returns
early, so the missing edges were never written.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.metacog.config import clear_metacog_config_cache
from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetacogService:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    clear_metacog_config_cache()
    return MetacogService(store=MetacogStore(str(tmp_path / "metacog.db")))


def _counts(svc: MetacogService) -> tuple[int, int]:
    connection = svc.store._connection
    artifacts = connection.execute("SELECT COUNT(*) AS n FROM metacog_artifacts").fetchone()["n"]
    edges = connection.execute("SELECT COUNT(*) AS n FROM metacog_artifact_edges").fetchone()["n"]
    return int(artifacts), int(edges)


def test_artifact_and_its_provenance_edges_commit_together(svc: MetacogService) -> None:
    """A failed edge insert must leave no envelope behind."""
    parent = svc.register_artifact(
        artifact_type="code_diff", content='{"diff":"+a"}', task_id="t-atomic"
    )
    assert _counts(svc) == (1, 0)

    connection = svc.store._connection
    connection.execute(
        "CREATE TRIGGER block_edges BEFORE INSERT ON metacog_artifact_edges "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    try:
        with pytest.raises(sqlite3.Error):
            svc.register_artifact(
                artifact_type="code_diff",
                content='{"diff":"+b"}',
                task_id="t-atomic",
                provenance={"inputs": [parent.id]},
            )
    finally:
        connection.execute("DROP TRIGGER block_edges")

    # The child envelope rolled back with its edge — not committed edge-less.
    assert _counts(svc) == (1, 0)

    # Because nothing was committed, the retry is not swallowed by the identity
    # dedupe: it writes both the envelope and the edge.
    child = svc.register_artifact(
        artifact_type="code_diff",
        content='{"diff":"+b"}',
        task_id="t-atomic",
        provenance={"inputs": [parent.id]},
    )
    assert _counts(svc) == (2, 1)
    edge = connection.execute(
        "SELECT * FROM metacog_artifact_edges WHERE to_artifact = ?", (child.id,)
    ).fetchone()
    assert edge["from_artifact"] == parent.id


def test_failed_registration_leaves_the_connection_usable(svc: MetacogService) -> None:
    """A rolled-back scope must not strand an open transaction."""
    connection = svc.store._connection
    connection.execute(
        "CREATE TRIGGER block_artifacts BEFORE INSERT ON metacog_artifacts "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    try:
        with pytest.raises(sqlite3.Error):
            svc.register_artifact(
                artifact_type="code_diff", content='{"diff":"+c"}', task_id="t-usable"
            )
    finally:
        connection.execute("DROP TRIGGER block_artifacts")

    assert connection.in_transaction is False
    later = svc.register_artifact(
        artifact_type="code_diff", content='{"diff":"+c"}', task_id="t-usable"
    )
    assert svc.store.get_artifact(later.id) is not None
