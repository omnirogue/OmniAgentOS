from __future__ import annotations

import concurrent.futures
import io
import json
import sys
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.longhaul.steering import SteeringManager
from omniagentos.longhaul.store import LonghaulStore
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.steering_marker import steering_marker_path


@pytest.fixture
def manager(tmp_path: Path) -> SteeringManager:
    db_path = str(tmp_path / "steering.db")
    migrate(db_path)
    store = LonghaulStore(db_path)
    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO board_tasks (id,title,status,created_at,updated_at,lane) "
        "VALUES ('btk_steer','Steer me','in_progress',?,?, 'longhaul')",
        (now, now),
    )
    store._connection.commit()
    return SteeringManager(store)


def test_steering_crud_receipts_and_formatting(manager: SteeringManager) -> None:
    turn = manager.append_turn("btk_steer", "user", "Run the focused regression tests.")
    assert manager.pending_turns("btk_steer")[0]["id"] == turn["id"]
    rendered = manager.format_for_injection(manager.pending_turns("btk_steer"))
    assert "Run the focused regression tests." in rendered
    assert manager.mark_delivered(turn["id"], "ses_one") is True
    assert manager.pending_turns("btk_steer") == []


def test_scope_sequence_delivery_receipt(manager: SteeringManager) -> None:
    turn = manager.append_turn("btk_steer", "user", "Keep the public API compatible.")
    assert manager.mark_delivered(("btk_steer", int(turn["seq"])), "ses_two")
    stored = manager.store.task_turns("btk_steer", limit=1)[0]
    delivery = json.loads(stored["meta_json"])["delivery"]
    assert delivery["session_id"] == "ses_two"
    assert delivery["delivered_at"]


def test_enqueue_live_session_and_atomic_concurrent_claim(
    manager: SteeringManager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    dal = SessionsDal(manager.store._db_path)
    now = utc_now_iso()
    dal.create_session(
        {
            "id": "ses_live",
            "source": "bridge",
            "project_dir": str(Path.cwd()),
            "provider": "claude",
            "state": "running",
            "cost_usd": 0,
            "kill_requested": 0,
            "created_at": now,
            "updated_at": now,
        }
    )
    manager.enqueue_for_live_session("ses_live", "Use the new fixture.")
    marker = steering_marker_path("ses_live")
    assert marker.is_file()

    def claim() -> list[dict]:
        local = SessionsDal(manager.store._db_path)
        try:
            return local.claim_and_mark_session_messages("ses_live")
        finally:
            local.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claims = [future.result() for future in [pool.submit(claim), pool.submit(claim)]]
    assert sorted(len(items) for items in claims) == [0, 1]
    assert claims[0] or claims[1]
    assert not marker.exists()
    dal.close()


def test_post_tool_hook_maps_additional_context(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from omniagentos.sessions import hook_client
    from omniagentos.sessions.steering_marker import mark_steering_pending

    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    mark_steering_pending("provider-ref")
    monkeypatch.setattr(
        hook_client,
        "_post",
        lambda *args, **kwargs: {
            "decision": "allow",
            "action_hash": "",
            "additional_context": "Operator steering: re-run tests.",
        },
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "provider-ref",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest"},
                    "cwd": str(Path.cwd()),
                }
            )
        ),
    )
    hook_client.main()
    payload = json.loads(capsys.readouterr().out)
    output = payload["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    assert output["additionalContext"] == "Operator steering: re-run tests."
