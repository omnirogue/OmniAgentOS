"""Regression: a resumed orchestration must not re-notify the same approval forever.

``_renotify_orchestration_approvals`` runs on EVERY resume tick. It used to pass
``dedupe=False``, so one approval nobody had clicked yet accumulated a fresh row
AND a fresh desktop banner every few minutes -- observed live 2026-07-24: six
notifications for a single pending approval inside 75 minutes, across five
approvals at once.

The fix must keep the re-surfacing behaviour that the function exists for: once
the operator has READ (dismissed) the notification, a later resume is allowed to
raise it again while the approval is still pending.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.intake.service import _renotify_orchestration_approvals
from omniagentos.notifications.dal import NotificationsDal


def _seed(db_path: str) -> tuple[str, str]:
    migrate(db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    now = utc_now_iso()
    run_id, session_id, approval_id = "orch_run", "ses_pending", "apr_pending"
    connection.execute(
        "INSERT INTO sessions "
        "(id, source, project_dir, provider, state, created_at, updated_at, run_source) "
        "VALUES (?, 'bridge', '/tmp', 'claude', 'awaiting_approval', ?, ?, 'test')",
        (session_id, now, now),
    )
    connection.execute(
        "INSERT INTO approvals (id, action_class, proposed_action, params_json, risk, "
        "evidence, state, session_id, created_at) "
        "VALUES (?, 'irreversible', 'Bash', '{}', 'high', 'e', 'pending', ?, ?)",
        (approval_id, session_id, now),
    )
    connection.execute(
        "INSERT INTO orchestration_steps (run_id, seq, title, session_id, updated_at) "
        "VALUES (?, 0, 'step', ?, ?)",
        (run_id, session_id, now),
    )
    connection.commit()
    connection.close()
    return run_id, approval_id


def _unread(db_path: str, approval_id: str) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM notifications "
                "WHERE ref_type = 'approval' AND ref_id = ? AND read_at IS NULL",
                (approval_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_repeated_resumes_do_not_stack_notifications(tmp_path: Path) -> None:
    db_path = str(tmp_path / "omni.db")
    run_id, approval_id = _seed(db_path)

    for _ in range(5):
        _renotify_orchestration_approvals(db_path, run_id)

    assert _unread(db_path, approval_id) == 1, "each resume tick re-notified the approval"


def test_resume_re_surfaces_after_the_operator_dismisses(tmp_path: Path) -> None:
    """Dedupe must not make the reminder a one-shot: reading it re-arms the resurface."""
    db_path = str(tmp_path / "omni.db")
    run_id, approval_id = _seed(db_path)

    _renotify_orchestration_approvals(db_path, run_id)
    dal = NotificationsDal(db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT id FROM notifications WHERE ref_id = ?", (approval_id,)
    ).fetchone()
    connection.close()
    dal.mark_read(str(row["id"]))
    assert _unread(db_path, approval_id) == 0

    _renotify_orchestration_approvals(db_path, run_id)
    assert _unread(db_path, approval_id) == 1, "a dismissed reminder must resurface"
