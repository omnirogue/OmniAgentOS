from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate_connection
from omniagentos.notifications.dal import NotificationsDal


@pytest.fixture
def dal(tmp_path: Path) -> NotificationsDal:
    return NotificationsDal(str(tmp_path / "notify.db"))


def test_migration_creates_notifications_table(tmp_path: Path) -> None:
    connection = sqlite3.connect(str(tmp_path / "m.db"))
    version = migrate_connection(connection)
    assert version >= 30
    cols = {row[1] for row in connection.execute("PRAGMA table_info(notifications)").fetchall()}
    assert {
        "id",
        "kind",
        "title",
        "ref_type",
        "ref_id",
        "read_at",
        "acted_at",
        "payload_json",
    } <= cols


def test_create_mints_id_and_persists(dal: NotificationsDal) -> None:
    row = dal.create({"kind": "info", "title": "Hi", "body": "there"})
    assert row["id"].startswith("ntf_")
    assert row["created_at"]
    assert row["read_at"] is None
    fetched = dal.get(row["id"])
    assert fetched is not None
    assert fetched["title"] == "Hi"


def test_create_encodes_payload(dal: NotificationsDal) -> None:
    row = dal.create({"kind": "approval", "title": "A", "payload": {"approval_id": "apr_1"}})
    assert '"approval_id":"apr_1"' in row["payload_json"]


def test_create_rejects_bad_kind(dal: NotificationsDal) -> None:
    with pytest.raises(ValueError):
        dal.create({"kind": "nope", "title": "x"})


def test_list_newest_first_and_unread_filter(dal: NotificationsDal) -> None:
    a = dal.create({"kind": "info", "title": "a", "created_at": "2026-01-01T00:00:00Z"})
    b = dal.create({"kind": "info", "title": "b", "created_at": "2026-02-01T00:00:00Z"})
    listing = dal.list()
    assert [row["id"] for row in listing] == [b["id"], a["id"]]

    dal.mark_read(a["id"])
    unread = dal.list(unread_only=True)
    assert [row["id"] for row in unread] == [b["id"]]
    assert dal.unread_count() == 1


def test_mark_read_is_idempotent(dal: NotificationsDal) -> None:
    row = dal.create({"kind": "info", "title": "a"})
    first = dal.mark_read(row["id"])
    assert first is not None and first["read_at"] is not None
    stamp = first["read_at"]
    second = dal.mark_read(row["id"])
    assert second is not None and second["read_at"] == stamp


def test_mark_acted_also_marks_read(dal: NotificationsDal) -> None:
    row = dal.create({"kind": "approval", "title": "a"})
    acted = dal.mark_acted(row["id"])
    assert acted is not None
    assert acted["acted_at"] is not None
    assert acted["read_at"] is not None


def test_has_unresolved_for_ref_tracks_unread_only(dal: NotificationsDal) -> None:
    row = dal.create({"kind": "approval", "title": "a", "ref_type": "approval", "ref_id": "apr_1"})
    assert dal.has_unresolved_for_ref("approval", "apr_1") is True
    assert dal.has_unresolved_for_ref("approval", "apr_2") is False
    dal.mark_read(row["id"])
    assert dal.has_unresolved_for_ref("approval", "apr_1") is False


def test_has_for_ref_kind_is_read_agnostic_and_kind_scoped(dal: NotificationsDal) -> None:
    row = dal.create({"kind": "done", "title": "done", "ref_type": "board_task", "ref_id": "btk_1"})
    # Present regardless of kind mismatch / read state.
    assert dal.has_for_ref_kind("board_task", "btk_1", "done") is True
    assert dal.has_for_ref_kind("board_task", "btk_1", "approval") is False
    assert dal.has_for_ref_kind("board_task", "btk_2", "done") is False
    dal.mark_read(row["id"])
    # Unlike has_unresolved_for_ref, a READ done row still dedupes (no re-bell).
    assert dal.has_for_ref_kind("board_task", "btk_1", "done") is True


def test_shared_connection_writes_same_db(tmp_path: Path) -> None:
    connection = sqlite3.connect(str(tmp_path / "s.db"))
    connection.execute("PRAGMA journal_mode=WAL")
    dal_a = NotificationsDal(connection)
    dal_a.create({"kind": "info", "title": "x"})
    # A second DAL over the SAME connection sees the row (no split-brain).
    dal_b = NotificationsDal(connection)
    assert dal_b.unread_count() == 1
