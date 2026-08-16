from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from omniagentos.sessions import notify
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest

from .conftest import seed_session


def test_manifest_writes_exactly_one_terminal_record(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    sessions_dal.update_session_state(session_id, "running", expect="starting")
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "consequential", "write", "{}", "", "", None
    )
    sessions_dal._connection.execute(  # noqa: SLF001 - decision belongs to p2 in production
        "UPDATE approvals SET state='approved', decided_by='owner' WHERE id=?", (approval_id,)
    )
    sessions_dal.update_session_state(session_id, "completed", expect="running")
    writer = SessionManifest(tmp_path / "ledger")
    session = sessions_dal.get_session(session_id)
    assert session is not None
    approvals = sessions_dal.list_session_approvals(session_id)
    path = writer.write(session, approvals)
    writer.write(session, approvals, killed_by="must-not-append")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["session_id"] == session_id
    assert payload["final_state"] == "completed"
    assert payload["approvals_requested"] == 1
    assert payload["approvals_granted"] == 1
    assert payload["approvals_denied"] == 0
    assert payload["killed_by"] is None


def test_notification_transports_are_best_effort(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        notify.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(
        notify.httpx,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )
    notify.push("Approval required", "Session ses_test is waiting.", "https://ntfy.invalid")
