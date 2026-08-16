"""H-22 / M-24 — OrgDims lock preservation + bounded bulk reclassify.

Focused regressions for:
- Auto-classification must not wipe ``locked_fields`` or overwrite locked values
  (H-22). Operator (human_edit) fields are preserved the same way.
- Bulk reclassify uses bounded transactions (no per-row commit loop) and an
  idempotent id cursor so a restart can resume cleanly (M-24).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.orgdims.service import OrgDimsService
from omniagentos.orgdims.store import OrgDimsStore


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "orgdims-lock.db")


@pytest.fixture()
def svc(db_path: str) -> OrgDimsService:
    s = OrgDimsService(db_path=db_path)
    s.ensure_seeded()
    return s


def _make_task(
    collab: CollabStore,
    *,
    title: str,
    description: str = "",
    discipline: str | None = None,
) -> BoardTask:
    task = BoardTask(
        title=title,
        description=description or title,
        discipline=discipline,
    )
    collab.create_board_task(task)
    return task


# --- H-22: locked + operator fields ----------------------------------------


def test_reclassify_preserves_locked_fields(svc: OrgDimsService, db_path: str) -> None:
    """Locked workstream must survive a reclassify that would otherwise flip it."""
    collab = CollabStore(db_path)
    task = _make_task(
        collab,
        title="Implement API endpoint",
        description="backend engineering work",
        discipline="coding",
    )
    # Operator sets creative + locks primary_workstream (and keeps the lock list).
    svc.set_board_dimensions(
        task.id,
        classification={
            "primary_workstream": "creative",
            "domains": ["copy"],
            "priority": "high",
            "risk_class": "read_only",
            "lifecycle": "ready",
        },
        locked_fields=["primary_workstream", "domains"],
    )
    before = svc.get_board_dimensions(task.id)
    assert before is not None
    assert before.classification.primary_workstream == "creative"
    assert before.locked_fields == ["primary_workstream", "domains"]

    # Reclassify with strong engineering signal — must not clobber locks.
    result = svc.classify_board_task(
        task_id=task.id,
        title="Implement artifact registration API",
        description="Add SHA-256 endpoints and unit tests in omniagentos",
        discipline="coding",
        apply=True,
    )
    assert result.bundle.classification.primary_workstream == "creative"
    assert result.bundle.classification.domains == ["copy"]
    assert result.bundle.locked_fields == ["primary_workstream", "domains"]
    # Unlocked fields may still update (priority was high/locked-not).
    persisted = svc.get_board_dimensions(task.id)
    assert persisted is not None
    assert persisted.classification.primary_workstream == "creative"
    assert persisted.locked_fields == ["primary_workstream", "domains"]
    assert "primary_workstream" not in result.auto_applied


def test_reclassify_preserves_human_edit_without_explicit_locks(
    svc: OrgDimsService, db_path: str
) -> None:
    """human_edit provenance protects set fields even when locked_fields is empty."""
    collab = CollabStore(db_path)
    task = _make_task(
        collab,
        title="Webinar script draft",
        description="creative copy",
        discipline="creatives",
    )
    svc.set_board_dimensions(
        task.id,
        classification={
            "primary_workstream": "marketing",
            "priority": "urgent",
            "risk_class": "bounded_external",
            "lifecycle": "ready",
        },
        # No locked_fields — provenance.source becomes human_edit.
    )
    before = svc.get_board_dimensions(task.id)
    assert before is not None
    assert before.provenance.source == "human_edit"
    assert before.classification.primary_workstream == "marketing"

    result = svc.classify_board_task(
        task_id=task.id,
        title="Implement backend API",
        description="coding engineering endpoint",
        discipline="coding",
        apply=True,
    )
    assert result.bundle.classification.primary_workstream == "marketing"
    assert result.bundle.classification.priority == "urgent"
    assert result.bundle.classification.risk_class == "bounded_external"
    # Locks list stays empty (operator did not set any) but is not wiped to null.
    assert result.bundle.locked_fields == []
    persisted = svc.get_board_dimensions(task.id)
    assert persisted is not None
    assert persisted.classification.primary_workstream == "marketing"


def test_partial_lock_allows_unlocked_fields_to_update(svc: OrgDimsService, db_path: str) -> None:
    collab = CollabStore(db_path)
    task = _make_task(
        collab,
        title="Campaign brief",
        description="advertising meta ads",
        discipline="advertising",
    )
    svc.set_board_dimensions(
        task.id,
        classification={
            "primary_workstream": "advertising",
            "priority": "low",
            "risk_class": "read_only",
            "lifecycle": "ready",
        },
        locked_fields=["primary_workstream"],
    )
    result = svc.classify_board_task(
        task_id=task.id,
        title="Urgent advertising campaign launch",
        description="high priority publish campaign to meta",
        discipline="advertising",
        priority="high",
        apply=True,
    )
    assert result.bundle.classification.primary_workstream == "advertising"
    assert result.bundle.locked_fields == ["primary_workstream"]
    # priority was not locked — explicit priority arg should apply.
    assert result.bundle.classification.priority == "high"


# --- M-24: bounded transactions + cursor resume ----------------------------


def test_bulk_reclassify_uses_bounded_batch_commits(svc: OrgDimsService, db_path: str) -> None:
    """N cards flush via apply_classification_batch, never per-row store commits."""
    collab = CollabStore(db_path)
    n = 12
    for i in range(n):
        _make_task(
            collab,
            title=f"Backend API endpoint {i}",
            description="coding engineering work",
            discipline="coding",
        )

    batch_sizes: list[int] = []
    real_batch = svc.store.apply_classification_batch
    per_row_writes = {"set_board_org": 0, "save_classification": 0}
    real_set = svc.store.set_board_org
    real_save = svc.store.save_classification

    def _spy_batch(items):  # type: ignore[no-untyped-def]
        batch_sizes.append(len(items))
        return real_batch(items)

    def _spy_set(*args, **kwargs):  # type: ignore[no-untyped-def]
        per_row_writes["set_board_org"] += 1
        return real_set(*args, **kwargs)

    def _spy_save(*args, **kwargs):  # type: ignore[no-untyped-def]
        per_row_writes["save_classification"] += 1
        return real_save(*args, **kwargs)

    svc.store.apply_classification_batch = _spy_batch  # type: ignore[method-assign]
    svc.store.set_board_org = _spy_set  # type: ignore[method-assign]
    svc.store.save_classification = _spy_save  # type: ignore[method-assign]
    try:
        out = svc.bulk_reclassify(only_missing=True, limit=50, batch_size=5, cursor=None)
    finally:
        svc.store.apply_classification_batch = real_batch  # type: ignore[method-assign]
        svc.store.set_board_org = real_set  # type: ignore[method-assign]
        svc.store.save_classification = real_save  # type: ignore[method-assign]

    assert out["classified"] >= n
    assert out["batches_committed"] == (n + 4) // 5  # ceil(n/5)
    assert batch_sizes == [5, 5, 2]
    # Critical: bulk path must not call the per-row commit helpers.
    assert per_row_writes["set_board_org"] == 0
    assert per_row_writes["save_classification"] == 0
    assert out["done"] is True
    assert out["next_cursor"] is None


def test_bulk_reclassify_cursor_resume_is_idempotent(svc: OrgDimsService, db_path: str) -> None:
    """Restart mid-bulk continues from next_cursor without redoing prior ids."""
    collab = CollabStore(db_path)
    tasks = [
        _make_task(
            collab,
            title=f"Task {i} engineering",
            description="backend coding",
            discipline="coding",
        )
        for i in range(7)
    ]
    ids_sorted = sorted(t.id for t in tasks)

    page1 = svc.bulk_reclassify(only_missing=True, limit=3, batch_size=2, cursor=None)
    assert page1["scanned"] == 3
    assert page1["done"] is False
    assert page1["next_cursor"] is not None
    cursor = page1["next_cursor"]
    assert cursor in ids_sorted

    # First page cards now have workstreams.
    first_ids = ids_sorted[:3]
    for tid in first_ids:
        bundle = svc.get_board_dimensions(tid)
        assert bundle is not None
        assert bundle.classification.primary_workstream

    page2 = svc.bulk_reclassify(only_missing=True, limit=3, batch_size=2, cursor=cursor)
    assert page2["scanned"] == 3
    # All remaining on this page should classify (still missing before page2).
    assert page2["classified"] >= 1

    svc.bulk_reclassify(only_missing=True, limit=3, batch_size=2, cursor=page2["next_cursor"])
    # After full scan, a re-run with only_missing skips already-filled cards.
    full_again = svc.bulk_reclassify(only_missing=True, limit=50, batch_size=5, cursor=None)
    assert full_again["classified"] == 0
    assert full_again["skipped"] >= 7
    assert full_again["done"] is True

    for tid in ids_sorted:
        bundle = svc.get_board_dimensions(tid)
        assert bundle is not None
        assert bundle.classification.primary_workstream == "engineering"


def test_bulk_reclassify_preserves_locks_across_batches(svc: OrgDimsService, db_path: str) -> None:
    collab = CollabStore(db_path)
    locked_task = _make_task(
        collab,
        title="Locked creative card",
        description="should stay creative",
        discipline="creatives",
    )
    plain = _make_task(
        collab,
        title="Plain engineering card",
        description="backend coding",
        discipline="coding",
    )
    # Pre-seed lock on one card (with workstream → only_missing would skip;
    # force only_missing=False so bulk tries both).
    svc.set_board_dimensions(
        locked_task.id,
        classification={
            "primary_workstream": "creative",
            "priority": "normal",
            "risk_class": "read_only",
            "lifecycle": "ready",
        },
        locked_fields=["primary_workstream"],
    )

    out = svc.bulk_reclassify(only_missing=False, limit=50, batch_size=1, cursor=None)
    assert out["classified"] >= 2
    locked = svc.get_board_dimensions(locked_task.id)
    plain_b = svc.get_board_dimensions(plain.id)
    assert locked is not None
    assert locked.classification.primary_workstream == "creative"
    assert locked.locked_fields == ["primary_workstream"]
    assert plain_b is not None
    assert plain_b.classification.primary_workstream == "engineering"


def test_apply_classification_batch_writes_all_rows(db_path: str) -> None:
    """Store-level batch writer persists every card in one call (no per-row API)."""
    store = OrgDimsStore(db_path)
    store.seed_taxonomy()
    collab = CollabStore(db_path)
    tasks = [_make_task(collab, title=f"Batch {i}", discipline="coding") for i in range(4)]
    from omniagentos.orgdims.classify import ClassificationService

    clf = ClassificationService(store)
    items = []
    for t in tasks:
        r = clf.classify_text(
            object_type="board_task",
            object_id=t.id,
            title=t.title,
            description=t.description or "",
            discipline=t.discipline,
            apply=False,
        )
        items.append((t.id, r.bundle, r))

    # Empty batch is a pure no-op (no exception, no rows).
    assert store.apply_classification_batch([]) == []

    cids = store.apply_classification_batch(items)
    assert len(cids) == 4
    assert len(set(cids)) == 4
    for t in tasks:
        b = store.get_board_org(t.id)
        assert b is not None
        assert b.classification.primary_workstream
