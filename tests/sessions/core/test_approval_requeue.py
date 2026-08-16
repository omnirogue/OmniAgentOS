"""Expiry-requeue is per-EVENT work, not per-poll work.

``_service_approval`` re-reads every approval row on every 0.5s poll, and an
EXPIRED row is never removed from that list, so ``_requeue_expired_session_approval``
is re-entered forever once an approval has expired.  ``create_session_approval``
dedupes on ``action_hash``, so no extra rows are minted — but the operator alert
and the ``delivered_at`` stamp that follow it are NOT deduped by anything, and
firing them per poll is (a) a ~2/s notification storm and (b) a silent promotion
of the park to the 4x outer max-park ceiling, because a freshly stamped
``delivered_at`` makes every poll look like a fresh human delivery.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import MAX_PARK_MINUTES_ENV, SessionSupervisor

from .conftest import seed_session

POLLS = 20  # 10 seconds of real supervision at the default 0.5s poll interval


@pytest.fixture
def _no_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the requeue path: OMNIAGENTOS_SESSION_MAX_PARK_MINUTES=0 is a
    documented supported configuration, and it is also the shape with no
    max-park reap to end the loop."""
    monkeypatch.setenv(MAX_PARK_MINUTES_ENV, "0")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _parked_with_expired_approval(
    dal: SessionsDal, tmp_path: Path, session_id: str
) -> str:
    seed_session(dal, tmp_path, session_id=session_id, state="awaiting_approval")
    approval_id = dal.create_session_approval(
        session_id, "hash", "consequential", "risky", "{}", "high", "", None
    )
    dal.expire_approval(approval_id, "approval expired")
    return approval_id


def _force_ordering(dal: SessionsDal, session_id: str, *, pending_sorts_last: bool) -> None:
    """Pin the (created_at, id) ordering of the requeued PAIR.

    Both rows are written in the same second, so which one row-recency returns
    is otherwise a comparison of two random hex ids -- re-thrown on every run.
    A test that hopes for the hostile ordering is a coin toss dressed as a
    control, so the ordering is stated here instead.
    """
    high, low = "apr_zzzzzzzzzzzzzzzzzzzz", "apr_aaaaaaaaaaaaaaaaaaaa"
    rows = dal.list_session_approvals(session_id)
    pending = [row for row in rows if str(row["state"]) == "pending"]
    expired = [row for row in rows if str(row["state"]) == "expired"]
    assert len(pending) == 1 and len(expired) == 1, "arrange expects exactly one requeued pair"
    for row, forced in (
        (pending[0], high if pending_sorts_last else low),
        (expired[0], low if pending_sorts_last else high),
    ):
        dal._connection.execute(  # noqa: SLF001 - force the id tiebreak
            "UPDATE approvals SET id = ?, created_at = '2026-08-06T12:00:00Z' WHERE id = ?",
            (forced, str(row["id"])),
        )
    dal._connection.commit()  # noqa: SLF001
    latest = dal.get_latest_session_approval(session_id)
    assert latest is not None
    assert (str(latest["state"]) == "pending") is pending_sorts_last, "arrange failed"


def _pending_row(dal: SessionsDal, session_id: str) -> dict[str, object]:
    """The pending approval, addressed by state.

    ``get_latest_session_approval`` orders by (created_at, id) and both rows are
    written in the same second, so the id tiebreak decides which of the pair it
    returns -- not a basis for a deterministic assertion.
    """
    pending = [
        row for row in dal.list_session_approvals(session_id) if str(row["state"]) == "pending"
    ]
    assert len(pending) == 1, f"expected exactly one pending row, got {len(pending)}"
    return pending[0]


def _supervisor(
    dal: SessionsDal, tmp_path: Path, alerts: list[str]
) -> SessionSupervisor:
    return SessionSupervisor(
        dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        liveness=lambda _pid: True,
        pgid_child_counter=lambda _pid: 0,
        notifier=lambda title, _body: alerts.append(title),
    )


@pytest.mark.usefixtures("_no_ceiling")
def test_requeue_alerts_once_per_expiry_not_once_per_poll(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = "ses_requeue_storm"
    _parked_with_expired_approval(sessions_dal, tmp_path, session_id)
    alerts: list[str] = []
    supervisor = _supervisor(sessions_dal, tmp_path, alerts)

    for _ in range(POLLS):
        supervisor.run_once()

    assert len(alerts) == 1, f"{len(alerts)} alerts in {POLLS} polls is a poll-rate storm"
    rows = sessions_dal.list_session_approvals(session_id)
    assert {str(row["state"]) for row in rows} == {"expired", "pending"}
    assert len(rows) == 2, "one expiry must mint exactly one replacement row"


@pytest.mark.usefixtures("_no_ceiling")
@pytest.mark.parametrize("pending_sorts_last", [True, False])
def test_requeue_does_not_restamp_delivery_on_every_poll(
    sessions_dal: SessionsDal, tmp_path: Path, pending_sorts_last: bool
) -> None:
    """The delivery stamp is the max-park ceiling's input. A requeue that minted
    nothing must leave it exactly where the real delivery attempt put it.

    The ordering is FORCED, because the defective parent only re-stamps when the
    pending half of the pair is the row row-recency returns:

      * ``pending_sorts_last=True`` is the negative control -- the stamp
        assertion below fails against the parent commit, deterministically;
      * ``pending_sorts_last=False`` is the ordering where the parent skips the
        stamp for the WRONG reason (it stamps the expired row's sibling, i.e.
        nothing). The stamp assertion passes there on both sides, so this
        parametrization only earns its place as a control through the alert
        count, which the parent fails in either ordering.

    Left to chance, this test passed against the parent roughly half the time.
    """
    session_id = "ses_requeue_stamp"
    _parked_with_expired_approval(sessions_dal, tmp_path, session_id)
    alerts: list[str] = []
    supervisor = _supervisor(sessions_dal, tmp_path, alerts)

    supervisor.run_once()
    _force_ordering(sessions_dal, session_id, pending_sorts_last=pending_sorts_last)
    pending = _pending_row(sessions_dal, session_id)

    # Pin the stamp to a value no re-stamp could reproduce, then keep polling.
    sessions_dal.record_session_approval_delivery(
        str(pending["id"]), delivered=True, attempted_at="2000-01-01T00:00:00Z"
    )
    for _ in range(POLLS):
        supervisor.run_once()

    after = _pending_row(sessions_dal, session_id)
    assert str(after["id"]) == str(pending["id"]), "no new row may be minted per poll"
    assert after["delivered_at"] == "2000-01-01T00:00:00Z", (
        "the requeue path re-stamped delivered_at on a poll that minted nothing, "
        "which reads to the max-park ceiling as a fresh human delivery every poll"
    )
    assert after["delivery_attempted_at"] == "2000-01-01T00:00:00Z"
    assert len(alerts) == 1, (
        f"{len(alerts)} alerts across {POLLS + 1} polls: the requeue is per-EVENT work, "
        "and this assertion is what makes both orderings a control"
    )


@pytest.mark.usefixtures("_no_ceiling")
def test_a_genuinely_new_approval_still_alerts(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """The dedupe is on 'nothing was minted', not on 'this session already
    alerted once': a second, different expired action must still reach a human."""
    session_id = "ses_requeue_second"
    _parked_with_expired_approval(sessions_dal, tmp_path, session_id)
    alerts: list[str] = []
    supervisor = _supervisor(sessions_dal, tmp_path, alerts)

    supervisor.run_once()
    assert len(alerts) == 1

    other = sessions_dal.create_session_approval(
        session_id, "other-hash", "consequential", "other risky", "{}", "high", "", None
    )
    sessions_dal.expire_approval(other, "approval expired")
    supervisor.run_once()

    assert len(alerts) == 2, "a different expired action must mint and announce its own requeue"
