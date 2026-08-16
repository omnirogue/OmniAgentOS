"""release_claim: the additive CAS counterpart to claim_task (WP1 re-enqueue path)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team.store import TeamStore


@pytest.fixture
def collab(tmp_path: Path) -> CollabStore:
    return CollabStore(str(tmp_path / "release.db"))


def _open_card(collab: CollabStore) -> str:
    task = BoardTask(title="claimable", status=BoardTaskStatus.OPEN)
    collab.create_board_task(task)
    return task.id


def test_claim_release_claim_round_trip(collab: CollabStore) -> None:
    task_id = _open_card(collab)

    assert collab.claim_task(task_id, "agt_one", 0) is True
    claimed = collab.get_board_task(task_id)
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == "agt_one"
    assert claimed["claim_version"] == 1

    assert collab.release_claim(task_id) is True
    released = collab.get_board_task(task_id)
    assert released["status"] == "open"
    assert released["claimed_by"] is None
    assert released["claim_version"] == 2  # bumped: a stale claimant's CAS must lose

    # The stale claimant retrying with the old version loses ...
    assert collab.claim_task(task_id, "agt_one", 1) is False
    # ... and a fresh claim at the current version wins.
    assert collab.claim_task(task_id, "agt_two", 2) is True
    assert collab.get_board_task(task_id)["claimed_by"] == "agt_two"


def test_release_is_cas_when_expect_version_given(collab: CollabStore) -> None:
    task_id = _open_card(collab)
    collab.claim_task(task_id, "agt_one", 0)

    assert collab.release_claim(task_id, expect_version=99) is False
    assert collab.get_board_task(task_id)["status"] == "claimed"
    assert collab.release_claim(task_id, expect_version=1) is True
    assert collab.get_board_task(task_id)["status"] == "open"


def test_release_covers_in_progress_but_not_open_or_terminal(collab: CollabStore) -> None:
    task_id = _open_card(collab)
    assert collab.release_claim(task_id) is False  # open: nothing to release

    collab.claim_task(task_id, "agt_one", 0)
    collab.update_board_task(task_id, {"status": "in_progress"})
    assert collab.release_claim(task_id) is True
    assert collab.get_board_task(task_id)["status"] == "open"

    collab.update_board_task(task_id, {"status": "done"})
    assert collab.release_claim(task_id) is False
    assert collab.get_board_task(task_id)["status"] == "done"


def test_update_board_task_still_refuses_claim_writes(collab: CollabStore) -> None:
    """release_claim must NOT weaken the CAS-only claim surface."""
    task_id = _open_card(collab)
    with pytest.raises(ValueError):
        collab.update_board_task(task_id, {"claimed_by": "agt_sneaky"})
    with pytest.raises(ValueError):
        collab.update_board_task(task_id, {"status": "claimed"})


def test_release_records_prior_status_and_actor(collab: CollabStore) -> None:
    task_id = _open_card(collab)
    team = TeamStore(collab._store)
    collab.claim_task(task_id, "agt_one", 0)
    collab.update_board_task(task_id, {"status": "in_progress"})

    assert collab.release_claim(task_id, actor="emp_bob") is True
    event = team.list_events(task_id)[-1]
    assert event["event"] == "status_change"
    assert event["actor"] == "emp_bob"
    assert event["from_status"] == "in_progress"
    assert event["to_status"] == "open"
    assert event["note"] == "claim released"
