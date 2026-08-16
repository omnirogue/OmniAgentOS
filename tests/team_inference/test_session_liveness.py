"""Session→board liveness: owner-scoped, forward-only, idempotent."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.team import session_liveness
from omniagentos.team.store import TeamStore


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Any]:
    collab = CollabStore(str(tmp_path / "db.sqlite3"))
    goals = CompanyGoalsStore(collab._store)
    for emp in ("emp_owner", "emp_alice"):
        goals.ensure_employee(employee_id=emp, name=emp, role="team")
    return {"collab": collab, "team": TeamStore(collab._store)}


def _report(emp: str, description: str, *, age: float = 60.0) -> dict[str, Any]:
    return {
        "employee_id": emp,
        "host": "m",
        "_age_seconds": age,
        "sessions": [
            {"id": "s1", "active": True, "description": description, "harness": "claude"}
        ],
    }


def _card(collab: CollabStore, *, owner: str, title: str, ref: str | None = None) -> str:
    card = BoardTask(title=title, owner_employee_id=owner, ref=ref)
    collab.create_board_task(card)
    return card.id


def test_ref_match_advances_open_card_to_in_progress(env: dict[str, Any]) -> None:
    card_id = _card(env["collab"], owner="emp_owner", title="Fix retries", ref="U7")
    stats = session_liveness.run_liveness(
        team=env["team"], collab=env["collab"],
        reports={"emp_owner": _report("emp_owner", "working U7 fix retries tonight")},
    )
    assert stats["advanced"] == 1
    row = env["collab"].get_board_task(card_id)
    assert row["status"] == BoardTaskStatus.IN_PROGRESS.value
    assert env["team"].list_evidence(card_id)[0]["kind"] == "session"


def test_title_match_without_ref(env: dict[str, Any]) -> None:
    card_id = _card(env["collab"], owner="emp_owner", title="Fix webhook retries")
    session_liveness.run_liveness(
        team=env["team"], collab=env["collab"],
        reports={"emp_owner": _report("emp_owner", "fix webhook retries in the API layer")},
    )
    assert env["collab"].get_board_task(card_id)["status"] == BoardTaskStatus.IN_PROGRESS.value


def test_owner_scoping_prevents_cross_owner_moves(env: dict[str, Any]) -> None:
    card_id = _card(env["collab"], owner="emp_alice", title="Alice task", ref="UM1")
    session_liveness.run_liveness(
        team=env["team"], collab=env["collab"],
        reports={"emp_owner": _report("emp_owner", "working on UM1 Alice task")},
    )
    assert env["collab"].get_board_task(card_id)["status"] == BoardTaskStatus.OPEN.value


def test_terminal_and_review_cards_untouched(env: dict[str, Any]) -> None:
    card_id = _card(env["collab"], owner="emp_owner", title="Reviewing", ref="R1")
    env["collab"].update_board_task(
        card_id, {"status": BoardTaskStatus.AWAITING_APPROVAL.value}
    )
    session_liveness.run_liveness(
        team=env["team"], collab=env["collab"],
        reports={"emp_owner": _report("emp_owner", "R1 reviewing again")},
    )
    assert (
        env["collab"].get_board_task(card_id)["status"]
        == BoardTaskStatus.AWAITING_APPROVAL.value
    )


def test_double_run_is_idempotent(env: dict[str, Any]) -> None:
    card_id = _card(env["collab"], owner="emp_owner", title="Idem", ref="I1")
    for _ in range(2):
        session_liveness.run_liveness(
            team=env["team"], collab=env["collab"],
            reports={"emp_owner": _report("emp_owner", "I1 idem work")},
        )
    assert env["collab"].get_board_task(card_id)["status"] == BoardTaskStatus.IN_PROGRESS.value
    assert len(env["team"].list_evidence(card_id)) == 1  # (kind, repo, ref) idempotent


def test_stale_report_ignored(env: dict[str, Any]) -> None:
    card_id = _card(env["collab"], owner="emp_owner", title="Stale", ref="S1")
    stats = session_liveness.run_liveness(
        team=env["team"], collab=env["collab"],
        reports={"emp_owner": _report("emp_owner", "S1 stale", age=3 * 3600)},
    )
    assert stats["skipped_stale"] == 1
    assert env["collab"].get_board_task(card_id)["status"] == BoardTaskStatus.OPEN.value


def test_short_title_never_matches_by_containment(env: dict[str, Any]) -> None:
    card_id = _card(env["collab"], owner="emp_owner", title="api")
    session_liveness.run_liveness(
        team=env["team"], collab=env["collab"],
        reports={"emp_owner": _report("emp_owner", "building the therapist api integration")},
    )
    assert env["collab"].get_board_task(card_id)["status"] == BoardTaskStatus.OPEN.value


def test_lost_claim_race_skips_without_crash(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    card_id = _card(env["collab"], owner="emp_owner", title="Racy card work", ref="RC1")
    real_get = env["collab"].get_board_task
    monkeypatch.setattr(env["collab"], "claim_task", lambda *a, **k: False)
    monkeypatch.setattr(
        env["collab"],
        "get_board_task",
        lambda cid: {**real_get(cid), "status": BoardTaskStatus.BLOCKED.value},
    )
    stats = session_liveness.run_liveness(
        team=env["team"], collab=env["collab"],
        reports={"emp_owner": _report("emp_owner", "RC1 racy card work")},
    )
    assert stats["advanced"] == 0
    assert real_get(card_id)["status"] == BoardTaskStatus.OPEN.value


def test_update_failure_does_not_count_as_advanced(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _card(env["collab"], owner="emp_owner", title="Failing update case", ref="F1")
    def boom(*a: Any, **k: Any) -> None:
        raise ValueError("refused")
    monkeypatch.setattr(env["collab"], "update_board_task", boom)
    stats = session_liveness.run_liveness(
        team=env["team"], collab=env["collab"],
        reports={"emp_owner": _report("emp_owner", "F1 failing update case")},
    )
    assert stats["advanced"] == 0
    assert stats["evidence"] == 1  # evidence still records the observation
