"""M-24/M-40 — orgdims classification writes must be all-or-nothing.

Applying a classification to a board card is two writes: the dimension bundle
onto ``board_tasks.org_json`` and the audit row in ``org_classifications``. The
store's connection runs in autocommit mode, so ``Connection.commit()`` is a
no-op and the two used to commit independently. A failure between them left a
card reclassified with no provenance row (or an audit row for a card that was
never changed), and re-running the classifier repairs neither half — it just
writes the same split again.

``apply_classification_batch`` is the same defect multiplied: a mid-batch
failure on the fourth card persisted three org rows and two audit rows, an
arbitrary prefix that no resume path reconciles because the cursor advances
per page, not per card.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.orgdims.service import OrgDimsService


@pytest.fixture()
def svc(tmp_path: Path) -> OrgDimsService:
    return OrgDimsService(db_path=str(tmp_path / "org.db"))


@pytest.fixture()
def task_id(tmp_path: Path, svc: OrgDimsService) -> str:
    collab = CollabStore(str(tmp_path / "org.db"))
    task = BoardTask(title="Ship the billing migration", description="engineering work")
    collab.create_board_task(task)
    return str(task.id)


def _org_json(svc: OrgDimsService, task_id: str) -> str | None:
    row = svc.store._connection.execute(
        "SELECT org_json FROM board_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return None if row is None else row["org_json"]


def _audit_count(svc: OrgDimsService, task_id: str) -> int:
    return int(
        svc.store._connection.execute(
            "SELECT COUNT(*) AS n FROM org_classifications WHERE object_id = ?", (task_id,)
        ).fetchone()["n"]
    )


def test_applied_classification_and_its_audit_row_commit_together(
    svc: OrgDimsService, task_id: str
) -> None:
    """A failed audit insert must not leave the card reclassified."""
    before = _org_json(svc, task_id)
    assert _audit_count(svc, task_id) == 0

    connection = svc.store._connection
    connection.execute(
        "CREATE TRIGGER block_classifications BEFORE INSERT ON org_classifications "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    try:
        with pytest.raises(sqlite3.Error):
            svc.classify_board_task(
                task_id=task_id,
                title="Ship the billing migration",
                description="engineering work",
            )
    finally:
        connection.execute("DROP TRIGGER block_classifications")

    assert _org_json(svc, task_id) == before
    assert _audit_count(svc, task_id) == 0

    # The retry applies both halves.
    svc.classify_board_task(
        task_id=task_id,
        title="Ship the billing migration",
        description="engineering work",
    )
    assert _org_json(svc, task_id) != before
    assert _audit_count(svc, task_id) == 1


def test_bulk_reclassify_batch_does_not_persist_a_prefix(
    tmp_path: Path, svc: OrgDimsService
) -> None:
    """A failure on the fourth card must not leave the first three applied."""
    collab = CollabStore(str(tmp_path / "org.db"))
    ids: list[str] = []
    for n in range(4):
        task = BoardTask(title=f"Ship migration {n}", description="engineering work")
        collab.create_board_task(task)
        ids.append(str(task.id))

    before = {tid: _org_json(svc, tid) for tid in ids}
    connection = svc.store._connection
    # Abort on the last card only, so the batch fails with three cards already
    # written inside the scope — the exact 3-of-4 prefix review reproduced.
    connection.execute(
        "CREATE TRIGGER block_last BEFORE INSERT ON org_classifications "
        f"WHEN NEW.object_id = '{ids[-1]}' "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    try:
        with pytest.raises(sqlite3.Error):
            svc.bulk_reclassify(only_missing=False, limit=10, batch_size=10)
    finally:
        connection.execute("DROP TRIGGER block_last")

    for tid in ids:
        assert _org_json(svc, tid) == before[tid]
        assert _audit_count(svc, tid) == 0

    # Resume is idempotent: every card ends with exactly one audit row.
    svc.bulk_reclassify(only_missing=False, limit=10, batch_size=10)
    for tid in ids:
        assert _org_json(svc, tid) != before[tid]
        assert _audit_count(svc, tid) == 1
