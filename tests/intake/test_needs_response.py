"""§2 Needs Response — strict predicate + ranking.

Needs Response is ONLY conversations that cannot proceed until the operator
acts. Blocked cards and open suggestions are backlog and must not appear.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.intake.needs_response import (
    is_needs_response,
    list_needs_response_payload,
    select_needs_response,
)
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.intake.service import reconcile_board
from omniagentos.sessions.dal import SessionsDal


def test_is_needs_response_excludes_blocked_and_open() -> None:
    assert is_needs_response({"status": "awaiting_approval"}) is True
    assert is_needs_response({"status": "blocked"}) is False
    assert is_needs_response({"status": "open"}) is False
    assert is_needs_response({"status": "in_progress"}) is False
    assert (
        is_needs_response(
            {
                "status": "in_progress",
                "pending_approval": {
                    "id": "apr_x",
                    "command": "echo hi",
                    "action_class": "read_only",
                },
            }
        )
        is True
    )
    assert (
        is_needs_response({"status": "in_progress", "work": {"state": "awaiting_approval"}}) is True
    )


def test_is_needs_response_rejects_stale_approval_on_terminal_card() -> None:
    """A blocked/done/cancelled card with a leftover pending_approval is NOT Needs Response.

    Otherwise a failed session that never voided its approval inflates the band
    past the '~1 not 104' premise.
    """
    stale = {
        "id": "apr_stale",
        "command": "rm -rf /tmp/x",
        "action_class": "irreversible",
    }
    assert is_needs_response({"status": "blocked", "pending_approval": stale}) is False
    assert is_needs_response({"status": "done", "pending_approval": stale}) is False
    assert is_needs_response({"status": "cancelled", "pending_approval": stale}) is False
    assert is_needs_response({"status": "pending", "pending_approval": stale}) is False


def test_select_returns_one_not_one_hundred_four() -> None:
    """Definition of pass #2: Needs Response returns 1, not 104."""
    bird = {
        "id": "btk_bird",
        "title": "make a bird clock on my desktop",
        "status": "awaiting_approval",
        "priority": "normal",
        "pending_approval": {
            "id": "apr_bird",
            "command": "cat > ~/Desktop/outputs/bird-clock.html << 'EOF'",
            "action_class": "sandboxed_creation",
            "created_at": "2026-07-25T10:00:00Z",
        },
    }
    blocked = [
        {"id": f"btk_blocked_{i}", "status": "blocked", "priority": "normal"} for i in range(49)
    ]
    open_cards = [
        {"id": f"btk_open_{i}", "status": "open", "priority": "normal"} for i in range(54)
    ]
    selected = select_needs_response([bird, *blocked, *open_cards])
    assert len(selected) == 1
    assert selected[0]["id"] == "btk_bird"
    command = selected[0]["pending_approval"]["command"]
    assert "bird-clock" in command
    assert command != "Bash"

    payload = list_needs_response_payload([bird, *blocked, *open_cards])
    assert payload["count"] == 1
    assert len(payload["items"]) == 1


def test_rank_orders_riskier_before_safer() -> None:
    safe = {
        "id": "btk_safe",
        "status": "awaiting_approval",
        "priority": "normal",
        "pending_approval": {
            "id": "apr_safe",
            "command": "ls",
            "action_class": "read_only",
            "created_at": "2026-07-25T08:00:00Z",
        },
    }
    risky = {
        "id": "btk_risky",
        "status": "awaiting_approval",
        "priority": "normal",
        "pending_approval": {
            "id": "apr_risky",
            "command": "rm -rf /tmp/x",
            "action_class": "irreversible",
            "created_at": "2026-07-25T12:00:00Z",
        },
    }
    selected = select_needs_response([safe, risky])
    assert [row["id"] for row in selected] == ["btk_risky", "btk_safe"]


def test_reconcile_and_filter_session_parked_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: session awaiting_approval lands in Needs Response with real command."""
    from omniagentos.api.routes import sessions as sessions_routes

    db = str(tmp_path / "needs-response.db")
    collab = CollabStore(db)
    sessions = SessionsDal(db)
    orchestrations = OrchestrationsDal(db)
    monkeypatch.setattr(sessions_routes, "get_sessions_dal", lambda: sessions)

    session_id = "ses_bird"
    sessions.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": "/tmp",
            "state": "awaiting_approval",
            "model": "sol",
            "last_activity_at": "2026-07-25T10:00:00Z",
        }
    )
    # Pending approval with the real command in params_json (not proposed_action).
    collab._store.create_approval(
        {
            "id": "apr_bird",
            "session_id": session_id,
            "run_id": None,
            "task_id": None,
            "step_seq": None,
            "action_class": "sandboxed_creation",
            "proposed_action": "Bash",  # useless — the card must not show this
            "params_json": json.dumps(
                {"command": "cat > ~/Desktop/outputs/bird-clock.html << 'EOF'"}
            ),
            "risk": "Writes a file to Desktop",
            "evidence": "PreToolUse",
            "state": "pending",
            "created_at": "2026-07-25T10:00:00Z",
        }
    )
    # Noise: blocked card and open card must not enter Needs Response.
    collab.create_board_task(BoardTask(title="failed work", status=BoardTaskStatus.BLOCKED))
    collab.create_board_task(BoardTask(title="backlog item", status=BoardTaskStatus.OPEN))
    parked = BoardTask(
        title="make a bird clock on my desktop",
        result_ref=session_id,
        status=BoardTaskStatus.IN_PROGRESS,  # stale; reconcile projects it
    )
    collab.create_board_task(parked)

    board = reconcile_board(
        collab._store,
        collab,
        sessions_dal=sessions,
        orchestrations_dal=orchestrations,
    )
    payload = list_needs_response_payload(board)

    assert payload["count"] == 1, payload
    item = payload["items"][0]
    assert item["id"] == parked.id
    assert item["status"] == "awaiting_approval"
    assert item["pending_approval"] is not None
    assert "bird-clock" in item["pending_approval"]["command"]
    assert item["pending_approval"]["command"] != "Bash"

    sessions.close()
    orchestrations.close()


def test_run_awaiting_approval_maps_to_board_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_RUN_TO_BOARD must not collapse run awaiting_approval into in_progress."""
    from omniagentos.api.routes import sessions as sessions_routes

    db = str(tmp_path / "run-park.db")
    collab = CollabStore(db)
    sessions = SessionsDal(db)
    orchestrations = OrchestrationsDal(db)
    monkeypatch.setattr(sessions_routes, "get_sessions_dal", lambda: sessions)

    store = collab._store
    task_id = "tsk_run_park"
    run_id = "run_park"
    store.create_task(
        {
            "id": task_id,
            "title": "parked run",
            "created_at": "2026-07-25T00:00:00Z",
            "updated_at": "2026-07-25T00:00:00Z",
        }
    )
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "harness": "mock",
            "state": "awaiting_approval",
            "created_at": "2026-07-25T00:00:00Z",
            "updated_at": "2026-07-25T00:01:00Z",
            "queued_at": "2026-07-25T00:00:00Z",
            "trace_id": "trc_park",
        }
    )
    store.create_approval(
        {
            "id": "apr_run",
            "run_id": run_id,
            "task_id": task_id,
            "step_seq": 0,
            "action_class": "consequential",
            "proposed_action": "Bash",
            "params_json": json.dumps({"command": "touch /tmp/proof.txt"}),
            "risk": "",
            "evidence": "",
            "state": "pending",
            "created_at": "2026-07-25T00:01:00Z",
        }
    )
    card = BoardTask(title="run needs you", status=BoardTaskStatus.IN_PROGRESS)
    collab.create_board_task(card)
    collab.update_board_task(card.id, {"run_id": run_id})

    board = reconcile_board(store, collab, sessions_dal=sessions, orchestrations_dal=orchestrations)
    by_id = {row["id"]: row for row in board}
    row = by_id[card.id]
    assert row["status"] == "awaiting_approval"
    assert row["pending_approval"] is not None
    assert "proof.txt" in row["pending_approval"]["command"]

    payload = list_needs_response_payload(board)
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == card.id

    sessions.close()
    orchestrations.close()
