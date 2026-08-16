"""A permission-denied exit is routed by the ACTION it was denied, not by row order.

``_process_finished`` compares the process's denied ``action_hash`` against an
approval row to decide whether the session is parked (a human still owes a
decision on that action) or failed (nothing outstanding covers it). It used to
read whichever row sorted last. With a second, unrelated pending approval open
-- two different tool calls, two pending rows, which ``create_session_approval``
dedupes only when they share an ``action_hash`` -- that row answers for an
action it has nothing to do with, and the session is FAILED with a live approval
still pending on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import (
    SessionState,
    SessionSupervisor,
    _ManagedProcess,
)

from .conftest import seed_session

_DENIED_ID = "apr_aaaaaaaaaaaaaaaaaaaa"  # sorts FIRST: row recency never picks it
_UNRELATED_ID = "apr_zzzzzzzzzzzzzzzzzzzz"
_SAME_SECOND = "2026-08-06T12:00:00Z"


@pytest.fixture
def supervisor(sessions_dal: SessionsDal, tmp_path: Path) -> SessionSupervisor:
    return SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: True,
        pgid_child_counter=lambda _pid: 0,
        notifier=lambda _title, _body: True,
    )


def _handle(session_id: str, *, denied: set[str], resumed: bool = False) -> _ManagedProcess:
    return _ManagedProcess(
        session_id=session_id,
        process=None,  # type: ignore[arg-type] - _process_finished never touches it here
        resumed=resumed,
        permission_denied=True,
        denied_action_hashes=denied,
    )


def _force_id(dal: SessionsDal, approval_id: str, forced: str) -> str:
    dal._connection.execute(  # noqa: SLF001 - force the adversarial ordering
        "UPDATE approvals SET id = ?, created_at = ? WHERE id = ?",
        (forced, _SAME_SECOND, approval_id),
    )
    dal._connection.commit()  # noqa: SLF001
    return forced


def _outcomes(
    supervisor: SessionSupervisor,
) -> tuple[list[tuple[str, Any]], list[str]]:
    finished: list[tuple[str, Any]] = []
    parked: list[str] = []
    supervisor._finish = lambda session_id, state, **_kwargs: finished.append(  # type: ignore[method-assign]
        (session_id, state)
    )
    supervisor._mark_awaiting = lambda session_id: parked.append(session_id)  # type: ignore[method-assign]
    supervisor._notify_longhaul_terminal = lambda *_a, **_kw: None  # type: ignore[method-assign]
    return finished, parked


def test_an_unrelated_pending_approval_cannot_fail_a_denied_session(
    sessions_dal: SessionsDal, supervisor: SessionSupervisor, tmp_path: Path
) -> None:
    session_id = "ses_denied_unrelated"
    seed_session(sessions_dal, tmp_path, session_id=session_id, state="running")
    denied = sessions_dal.create_session_approval(
        session_id, "denied-hash", "consequential", "denied action", "{}", "high", "", None
    )
    unrelated = sessions_dal.create_session_approval(
        session_id, "unrelated-hash", "consequential", "other action", "{}", "high", "", None
    )
    _force_id(sessions_dal, denied, _DENIED_ID)
    _force_id(sessions_dal, unrelated, _UNRELATED_ID)
    latest = sessions_dal.get_latest_session_approval(session_id)
    assert latest is not None and str(latest["id"]) == _UNRELATED_ID, "arrange failed"
    finished, parked = _outcomes(supervisor)

    supervisor._process_finished(_handle(session_id, denied={"denied-hash"}), 1)  # noqa: SLF001

    assert (parked, finished) == ([session_id], []), (
        "an unrelated pending approval answered for the denied action and shredded "
        "a session that still owed a human a decision"
    )


def test_a_denied_action_with_no_approval_row_still_fails(
    sessions_dal: SessionsDal, supervisor: SessionSupervisor, tmp_path: Path
) -> None:
    """The fix must not turn 'nothing covers this denial' into a park."""
    session_id = "ses_denied_uncovered"
    seed_session(sessions_dal, tmp_path, session_id=session_id, state="running")
    other = sessions_dal.create_session_approval(
        session_id, "other-hash", "consequential", "other action", "{}", "high", "", None
    )
    _force_id(sessions_dal, other, _UNRELATED_ID)
    finished, parked = _outcomes(supervisor)

    supervisor._process_finished(_handle(session_id, denied={"absent-hash"}), 1)  # noqa: SLF001

    assert parked == []
    assert [session_id for session_id, _state in finished] == [session_id]
    assert finished[0][1] is SessionState.FAILED


def test_the_requeued_half_of_a_pair_is_preferred_over_its_expired_twin(
    sessions_dal: SessionsDal, supervisor: SessionSupervisor, tmp_path: Path
) -> None:
    """Both halves route to a park, but only deterministically once the match is
    on the action rather than on a random id tiebreak."""
    session_id = "ses_denied_pair"
    seed_session(sessions_dal, tmp_path, session_id=session_id, state="running")
    expired = sessions_dal.create_session_approval(
        session_id, "paired-hash", "consequential", "risky", "{}", "high", "", None
    )
    sessions_dal.expire_approval(expired, "approval expired")
    pending = sessions_dal.create_session_approval(
        session_id, "paired-hash-2", "consequential", "risky", "{}", "high", "", None
    )
    sessions_dal._connection.execute(  # noqa: SLF001 - same action, forced hash + ordering
        'UPDATE approvals SET params_json = \'{"action_hash":"paired-hash"}\' WHERE id = ?',
        (pending,),
    )
    sessions_dal._connection.commit()  # noqa: SLF001
    _force_id(sessions_dal, expired, _UNRELATED_ID)  # expired sorts last
    _force_id(sessions_dal, pending, _DENIED_ID)
    finished, parked = _outcomes(supervisor)

    supervisor._process_finished(_handle(session_id, denied={"paired-hash"}), 1)  # noqa: SLF001

    assert (parked, finished) == ([session_id], [])
    states = {
        str(row["id"]): str(row["state"]) for row in sessions_dal.list_session_approvals(session_id)
    }
    assert states[_DENIED_ID] == "pending", (
        "the pending half of the pair must be the row the denial is routed on, so no "
        "second replacement row is minted"
    )


def test_a_session_with_no_approvals_at_all_is_unchanged(
    sessions_dal: SessionsDal, supervisor: SessionSupervisor, tmp_path: Path
) -> None:
    """An unreachable hook mints no row; that park is preserved (favourable-absence
    guard: 'no rows' must not read the same as 'no matching row')."""
    session_id = "ses_denied_norows"
    seed_session(sessions_dal, tmp_path, session_id=session_id, state="running")
    finished, parked = _outcomes(supervisor)

    supervisor._process_finished(_handle(session_id, denied={"denied-hash"}), 1)  # noqa: SLF001

    assert (parked, finished) == ([session_id], [])
