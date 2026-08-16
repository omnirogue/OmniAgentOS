from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omniagentos.notifications import service
from omniagentos.notifications.dal import NotificationsDal


@pytest.fixture(autouse=True)
def _no_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_push", lambda *_a, **_k: None)


@pytest.fixture
def dal(tmp_path: Path) -> NotificationsDal:
    return NotificationsDal(str(tmp_path / "svc.db"))


def test_record_notification_persists(dal: NotificationsDal) -> None:
    nid = service.record_notification(kind="info", title="hi", dal=dal, push=False)
    assert nid is not None
    assert dal.get(nid) is not None


def test_record_notification_dedupes_unread_ref(dal: NotificationsDal) -> None:
    first = service.record_notification(
        kind="approval", title="a", ref_type="approval", ref_id="apr_1", dal=dal, push=False
    )
    second = service.record_notification(
        kind="approval", title="a", ref_type="approval", ref_id="apr_1", dal=dal, push=False
    )
    assert first is not None
    assert second is None  # deduped while the first is still unread
    assert len(dal.list()) == 1


def test_record_notification_never_raises_on_bad_target(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_k: Any) -> Any:
        raise RuntimeError("no db")

    monkeypatch.setattr(service, "_dal_for", boom)
    # Must degrade gracefully (best-effort contract), not raise.
    assert service.record_notification(kind="info", title="x", push=False) is None


def test_record_notification_result_distinguishes_dedupe_from_failure(
    dal: NotificationsDal, monkeypatch: pytest.MonkeyPatch
) -> None:
    persisted = service.record_notification_result(
        kind="approval",
        title="a",
        ref_type="approval",
        ref_id="apr_result",
        dal=dal,
        push=False,
    )
    assert persisted.status == "persisted"
    assert persisted.notification_id is not None
    assert persisted.error is None

    deduped = service.record_notification_result(
        kind="approval",
        title="a",
        ref_type="approval",
        ref_id="apr_result",
        dal=dal,
        push=False,
    )
    assert deduped.status == "deduped"
    assert deduped.notification_id is None
    assert deduped.error is None

    def boom(**_kwargs: Any) -> Any:
        raise RuntimeError("notification database unavailable")

    monkeypatch.setattr(service, "_dal_for", boom)
    failed = service.record_notification_result(kind="info", title="x", push=False)
    assert failed.status == "failed"
    assert failed.notification_id is None
    assert failed.error == "notification database unavailable"


def test_guarded_result_validates_and_reports_only_after_commit(
    dal: NotificationsDal,
) -> None:
    seen: list[str] = []

    def guard(connection: sqlite3.Connection) -> None:
        assert connection is dal._connection
        assert connection.in_transaction is True
        seen.append("guard")

    result = service.record_notification_result(
        kind="info",
        title="guarded",
        ref_type="run",
        ref_id="run_guarded",
        dal=dal,
        push=False,
        persistence_guard=guard,
        on_persisted=lambda: seen.append("persisted"),
    )

    assert result.status == "persisted"
    assert seen == ["guard", "persisted"]
    assert dal._connection.in_transaction is False
    assert dal.get(result.notification_id or "") is not None


def test_guarded_result_fetch_failure_rolls_back_before_commit(
    dal: NotificationsDal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result-fetch fault cannot leave a landed row reported as failed."""

    def fail_fetch(_notification_id: str) -> dict[str, Any]:
        raise sqlite3.OperationalError("guarded result fetch failed")

    monkeypatch.setattr(dal, "_stored", fail_fetch)
    result = service.record_notification_result(
        kind="info",
        title="must roll back",
        ref_type="run",
        ref_id="run_fetch_fault",
        dal=dal,
        push=False,
        persistence_guard=lambda connection: (
            connection.in_transaction or pytest.fail("guard must run in the write transaction")
        ),
    )

    assert result.status == "failed"
    assert result.error == "guarded result fetch failed"
    assert dal._connection.in_transaction is False
    assert (
        dal._connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE ref_id = ?",
            ("run_fetch_fault",),
        ).fetchone()[0]
        == 0
    )


def test_legacy_result_fetch_failure_also_rolls_back_before_commit(
    dal: NotificationsDal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unleased audit notices use the same truthful commit boundary."""

    def fail_fetch(_notification_id: str) -> dict[str, Any]:
        raise sqlite3.OperationalError("legacy result fetch failed")

    monkeypatch.setattr(dal, "_stored", fail_fetch)
    result = service.record_notification_result(
        kind="info",
        title="legacy must roll back",
        ref_type="audit",
        ref_id="audit_fetch_fault",
        dal=dal,
        push=False,
        dedupe=False,
    )

    assert result.status == "failed"
    assert result.error == "legacy result fetch failed"
    assert dal._connection.in_transaction is False
    assert (
        dal._connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE ref_id = ?",
            ("audit_fetch_fault",),
        ).fetchone()[0]
        == 0
    )


def test_post_commit_observer_failure_cannot_contradict_persisted_result(
    dal: NotificationsDal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ancillary observer is never part of the persistence truth boundary."""

    def fail_observer() -> None:
        raise RuntimeError("post-commit observer failed")

    def fail_logging(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("observer logging failed")

    monkeypatch.setattr(service.logger, "exception", fail_logging)
    result = service.record_notification_result(
        kind="info",
        title="observer fault",
        ref_type="run",
        ref_id="run_observer_fault",
        dal=dal,
        push=False,
        persistence_guard=lambda connection: (
            connection.in_transaction or pytest.fail("guard must run in the write transaction")
        ),
        on_persisted=fail_observer,
    )

    assert result.status == "persisted"
    assert result.error is None
    assert dal.get(result.notification_id or "") is not None


def test_post_commit_push_and_logging_faults_preserve_persisted_result(
    dal: NotificationsDal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivery and its own logging can both fail after commit without lying."""
    from omniagentos.sessions import notify

    # The module's autouse fixture suppresses real pushes. Restore the production
    # seam so this regression exercises the public push=True path end to end.
    monkeypatch.undo()

    def fail_push(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("post-commit delivery failed")

    def fail_logging(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("post-commit delivery logging failed")

    monkeypatch.setattr(notify, "push", fail_push)
    monkeypatch.setattr(service.logger, "debug", fail_logging)

    result = service.record_notification_result(
        kind="info",
        title="landed before delivery",
        ref_type="improvement",
        ref_id="imp_push_logging_fault",
        dal=dal,
        push=True,
        dedupe=False,
    )

    assert result.status == "persisted"
    assert result.notification_id is not None
    assert result.error is None
    assert (
        dal._connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE ref_id = ?",
            ("imp_push_logging_fault",),
        ).fetchone()[0]
        == 1
    )
    assert dal._connection.in_transaction is False


def test_guarded_dedupe_is_final_without_running_persisted_observer(
    dal: NotificationsDal,
) -> None:
    first = service.record_notification(
        kind="info",
        title="existing",
        ref_type="run",
        ref_id="run_guarded_dedupe",
        dal=dal,
        push=False,
    )
    assert first is not None

    observer_calls: list[str] = []
    result = service.record_notification_result(
        kind="info",
        title="dedupe",
        ref_type="run",
        ref_id="run_guarded_dedupe",
        dal=dal,
        push=False,
        dedupe=True,
        persistence_guard=lambda connection: (
            connection.in_transaction
            or pytest.fail("guard must precede dedupe in the write transaction")
        ),
        on_persisted=lambda: observer_calls.append("unexpected"),
    )

    assert result.status == "deduped"
    assert result.error is None
    assert observer_calls == []
    assert len(dal.list()) == 1


class _ConnectionFaultProxy:
    """Delegate SQLite operations while injecting commit/rollback faults."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        commit_error: Exception | None = None,
        rollback_error: Exception | None = None,
    ) -> None:
        self.connection = connection
        self.commit_error = commit_error
        self.rollback_error = rollback_error

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()
        if self.rollback_error is not None:
            raise self.rollback_error


def test_rollback_fault_never_masks_guard_rejection(
    dal: NotificationsDal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = dal._connection
    monkeypatch.setattr(
        dal,
        "_connection",
        _ConnectionFaultProxy(
            inner,
            rollback_error=RuntimeError("secondary rollback fault"),
        ),
    )

    def reject(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("authoritative guard rejection")

    with pytest.raises(RuntimeError, match="authoritative guard rejection"):
        service.record_notification_result(
            kind="info",
            title="guard rejected",
            ref_type="run",
            ref_id="run_guard_rollback",
            dal=dal,
            push=False,
            persistence_guard=reject,
        )

    assert inner.in_transaction is False
    assert (
        inner.execute(
            "SELECT COUNT(*) FROM notifications WHERE ref_id = ?",
            ("run_guard_rollback",),
        ).fetchone()[0]
        == 0
    )


def test_rollback_fault_never_masks_commit_failure(
    dal: NotificationsDal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inner = dal._connection
    monkeypatch.setattr(
        dal,
        "_connection",
        _ConnectionFaultProxy(
            inner,
            commit_error=sqlite3.OperationalError("authoritative commit failure"),
            rollback_error=RuntimeError("secondary rollback fault"),
        ),
    )

    result = service.record_notification_result(
        kind="info",
        title="commit rejected",
        ref_type="run",
        ref_id="run_commit_rollback",
        dal=dal,
        push=False,
        persistence_guard=lambda _connection: None,
    )

    assert result.status == "failed"
    assert result.error == "authoritative commit failure"
    assert inner.in_transaction is False
    assert (
        inner.execute(
            "SELECT COUNT(*) FROM notifications WHERE ref_id = ?",
            ("run_commit_rollback",),
        ).fetchone()[0]
        == 0
    )


def test_dedupe_commit_failure_is_not_misreported_or_rollback_masked(
    dal: NotificationsDal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = service.record_notification(
        kind="info",
        title="existing",
        ref_type="run",
        ref_id="run_dedupe_commit_fault",
        dal=dal,
        push=False,
    )
    assert first is not None

    inner = dal._connection
    monkeypatch.setattr(
        dal,
        "_connection",
        _ConnectionFaultProxy(
            inner,
            commit_error=sqlite3.OperationalError("dedupe commit failure"),
            rollback_error=RuntimeError("secondary rollback fault"),
        ),
    )
    result = service.record_notification_result(
        kind="info",
        title="must not claim deduped",
        ref_type="run",
        ref_id="run_dedupe_commit_fault",
        dal=dal,
        push=False,
        dedupe=True,
        persistence_guard=lambda _connection: None,
    )

    assert result.status == "failed"
    assert result.error == "dedupe commit failure"
    assert inner.in_transaction is False
    assert (
        inner.execute(
            "SELECT COUNT(*) FROM notifications WHERE ref_id = ?",
            ("run_dedupe_commit_fault",),
        ).fetchone()[0]
        == 1
    )


def test_guard_rejection_precedes_dedupe_and_propagates(
    dal: NotificationsDal,
) -> None:
    first = service.record_notification(
        kind="info",
        title="existing",
        ref_type="run",
        ref_id="run_guard_reject",
        dal=dal,
        push=False,
    )
    assert first is not None

    def reject(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("stale notification owner")

    with pytest.raises(RuntimeError, match="stale notification owner"):
        service.record_notification_result(
            kind="info",
            title="must not dedupe stale owner",
            ref_type="run",
            ref_id="run_guard_reject",
            dal=dal,
            push=False,
            dedupe=True,
            persistence_guard=reject,
        )

    assert len(dal.list()) == 1
    assert dal._connection.in_transaction is False


def test_notify_approval_requested_links_approval(dal: NotificationsDal) -> None:
    nid = service.notify_approval_requested(
        approval_id="apr_9",
        proposed_action="Write",
        action_class="consequential",
        source="runner",
        dal=dal,
        push=False,
    )
    row = dal.get(nid or "")
    assert row is not None
    assert row["kind"] == "approval"
    assert row["ref_type"] == "approval"
    assert row["ref_id"] == "apr_9"
    assert '"approval_id":"apr_9"' in row["payload_json"]


def test_resolve_target_pending_approval_is_actionable() -> None:
    notification = {"ref_type": "approval", "ref_id": "apr_1"}
    target = service.resolve_target(notification, lambda _id: {"id": "apr_1", "state": "pending"})
    assert target["actionable"] is True
    assert target["resolved"] is False
    assert target["approval_id"] == "apr_1"
    assert target["href"] == "/approvals"


def test_resolve_target_decided_approval_shows_outcome() -> None:
    notification = {"ref_type": "approval", "ref_id": "apr_1"}
    target = service.resolve_target(notification, lambda _id: {"id": "apr_1", "state": "approved"})
    assert target["actionable"] is False
    assert target["resolved"] is True
    assert target["state"] == "approved"


def test_resolve_target_missing_approval_is_not_resolved() -> None:
    """Live lookup returning None must not render as favourable resolved.

    Governing filter: absent must never render as good. Counterfeit that would
    fake a weaker fix: set resolved=True / state='resolved' for a missing row
    so the panel looks "done". Require resolved=False, actionable=False, and an
    explicit non-favourable state marker (not resolved/approved/rejected/expired).
    """
    notification = {"ref_type": "approval", "ref_id": "apr_gone"}
    target = service.resolve_target(notification, lambda _id: None)
    assert target["resolved"] is False, (
        f"absent approval reported resolved={target['resolved']}; absent as favourable"
    )
    assert target["actionable"] is False
    assert target["state"] == "missing"
    assert target["state"] not in {"resolved", "approved", "rejected", "expired", "pending"}
    assert target["href"] == "/approvals"
    assert target["approval_id"] == "apr_gone"

    # Positive control: a real decided approval still reports its outcome.
    decided = service.resolve_target(
        notification, lambda _id: {"id": "apr_gone", "state": "approved"}
    )
    assert decided["resolved"] is True
    assert decided["state"] == "approved"


def test_resolve_target_approval_lookup_failure_is_not_resolved() -> None:
    """Lookup failure is not a favourable 'resolved' outcome.

    Counterfeit that would fake a weaker fix: set ``resolved=False`` only when
    the exception message matches a fixed string, or set ``state='resolved'``
    while flipping the boolean (panel still reads as done). Require:
    - ``resolved is False``
    - ``actionable is False`` (do not invent a live Approve action either)
    - ``state`` is an explicit unmeasured marker, not ``resolved`` / ``approved``
    - missing (None) is also not favourable, and is distinct from unavailable
    """
    notification = {"ref_type": "approval", "ref_id": "apr_1"}

    def boom(_id: str) -> dict[str, Any]:
        raise RuntimeError("sessions database unavailable")

    target = service.resolve_target(notification, boom)
    assert target["resolved"] is False
    assert target["actionable"] is False
    assert target["state"] not in {"resolved", "approved", "rejected", "expired", "pending"}
    assert target["state"] in {"unknown", "unavailable", "lookup_failed"}
    assert target["href"] == "/approvals"

    # Missing row is not favourable either, and is a distinct state from failure.
    missing = service.resolve_target(notification, lambda _id: None)
    assert missing["resolved"] is False
    assert missing["state"] == "missing"
    assert missing["state"] != target["state"]


def test_resolve_target_no_approval_lookup_is_not_resolved() -> None:
    """No lookup wired is a non-result — distinct from live missing AND not resolved.

    Counterfeit the reviewer applied that stayed green against a weaker test:

        approval_lookup = lambda _id: None  # unwired as live missing

    That collapses "lookup never wired" into ``state='missing'``. Require:

    - ``resolved is False`` / ``actionable is False``
    - ``state is None`` (not wired — no measurement attempted)
    - live miss is ``state='missing'`` and **not identical** to the unwired target

    Failing-on-revert: replace the early ``return target`` with
    ``approval_lookup = lambda _id: None`` (or call through a always-None lambda).
    """
    notification = {"ref_type": "approval", "ref_id": "apr_unwired"}
    target = service.resolve_target(notification, None)
    assert target["resolved"] is False, (
        f"no lookup wired reported resolved={target['resolved']}; non-result as favourable"
    )
    assert target["actionable"] is False
    assert target.get("state") is None, (
        f"no lookup wired reported state={target.get('state')!r}; "
        "unwired must leave state=None, not invent missing/unavailable/resolved"
    )
    assert target.get("state") not in {"resolved", "approved", "rejected", "expired", "missing"}
    assert target["href"] == "/approvals"
    assert target["approval_id"] == "apr_unwired"

    # Live missing is also not favourable (governing filter), and MUST differ.
    missing = service.resolve_target(notification, lambda _id: None)
    assert missing["resolved"] is False
    assert missing["state"] == "missing"
    assert target["state"] != missing["state"], (
        f"no-lookup must not be identical to live missing; unwired={target!r} missing={missing!r}"
    )
    assert target != missing, (
        "unwired target must not equal live-missing target; "
        "counterfeit `approval_lookup = lambda _id: None` collapses them"
    )


def test_build_approval_lookup_open_failure_is_not_resolved() -> None:
    """Production-safe factory: open failure must raise into resolve_target.

    Counterfeit: catch open errors and return ``lambda _id: None`` (the old
    route factory). That path reports every approval resolved when sessions is
    down. This test binds build_approval_lookup + resolve_target together.
    """
    notification = {"ref_type": "approval", "ref_id": "apr_probe"}

    def boom_open() -> Any:
        raise RuntimeError("sessions database unavailable")

    lookup = service.build_approval_lookup(boom_open)
    assert lookup is not None
    target = service.resolve_target(notification, lookup)
    assert target["resolved"] is False
    assert target["actionable"] is False
    assert target["state"] == "unavailable"

    # open_reader returns None → same unavailable path, not resolved.
    none_lookup = service.build_approval_lookup(lambda: None)
    none_target = service.resolve_target(notification, none_lookup)
    assert none_target["resolved"] is False
    assert none_target["state"] == "unavailable"

    # No opener at all → not wired.
    assert service.build_approval_lookup(None) is None


def test_build_approval_lookup_missing_getter_is_unavailable() -> None:
    """Reader without callable get_approval_by_id must surface as unavailable.

    Governing contract of build_approval_lookup: a reader that cannot perform
    point lookup is a measurement failure, not a live miss. Counterfeit that
    would fake a weaker fix (and the exact mutation that previously passed):

        return lambda _id: None  # missing getter masquerades as missing

    That path yields state='missing' via resolve_target. Require unavailable
    (raised lookup), not missing, and not a favourable resolved outcome.
    """
    notification = {"ref_type": "approval", "ref_id": "apr_no_getter"}

    class _ReaderWithoutGetter:
        """Sessions-shaped object that has no get_approval_by_id."""

        pass

    class _ReaderWithNonCallableGetter:
        get_approval_by_id = "not-a-callable"  # type: ignore[assignment]

    for opener in (
        lambda: _ReaderWithoutGetter(),
        lambda: _ReaderWithNonCallableGetter(),
    ):
        lookup = service.build_approval_lookup(opener)
        assert lookup is not None, "missing getter must still return a live lookup"
        # Direct contract: the lookup itself raises (measurement failure).
        raised = False
        try:
            lookup("apr_no_getter")
        except Exception:
            raised = True
        assert raised, (
            "missing get_approval_by_id must raise on lookup; "
            "returning None collapses unavailable into live missing"
        )
        target = service.resolve_target(notification, lookup)
        assert target["resolved"] is False
        assert target["actionable"] is False
        assert target["state"] == "unavailable", (
            f"missing getter reported state={target['state']!r}; "
            "must be unavailable, not missing/resolved"
        )
        # Distinct from a live miss on a real getter.
        assert target["state"] != "missing"


def test_build_approval_lookup_healthy_reader_is_preserved() -> None:
    """Positive control: a working get_approval_by_id must remain the lookup.

    Failing-on-revert target (reviewer mutation that previously stayed green):
        return lambda _id: None  # MUTATION: healthy reader forced to live-missing

    That over-correction makes every healthy lookup appear missing and still
    passed the full lane. Require the factory to return the real getter and
    resolve_target to surface a real pending approval as actionable.
    """
    notification = {"ref_type": "approval", "ref_id": "apr_live"}

    class _HealthyReader:
        def get_approval_by_id(self, approval_id: str) -> dict[str, Any]:
            return {
                "id": approval_id,
                "state": "pending",
                "action_class": "consequential",
                "proposed_action": "deploy",
            }

    lookup = service.build_approval_lookup(lambda: _HealthyReader())
    assert lookup is not None
    row = lookup("apr_live")
    assert row is not None, (
        "healthy reader must return the approval row; "
        "forcing None collapses a live pending approval into missing"
    )
    assert row["state"] == "pending"
    target = service.resolve_target(notification, lookup)
    assert target["state"] == "pending"
    assert target["actionable"] is True
    assert target["resolved"] is False
    assert target["action_class"] == "consequential"


def test_build_board_lookup_open_failure_is_not_missing() -> None:
    """Board factory open failure must raise into resolve_target as unavailable.

    Counterfeit: catch open errors and return ``lambda _id: None`` (old route
    factory). That path reports every board card as measured missing when the
    collab store is down — identical shape to a genuine miss.
    """
    notification = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload": {"files_count": 2},
    }

    def boom_open() -> Any:
        raise RuntimeError("collab store unavailable")

    lookup = service.build_board_lookup(boom_open)
    assert lookup is not None
    target = service.resolve_target(notification, None, lookup)
    assert target["resolved"] is False
    assert target["state"] == "unavailable"
    assert target["href"] == "/board"

    none_lookup = service.build_board_lookup(lambda: None)
    none_target = service.resolve_target(notification, None, none_lookup)
    assert none_target["resolved"] is False
    assert none_target["state"] == "unavailable"

    assert service.build_board_lookup(None) is None

    # Distinct from genuine live missing.
    missing = service.resolve_target(notification, None, lambda _id: None)
    assert missing["state"] == "missing"
    assert missing["state"] != target["state"]


def test_build_board_lookup_missing_getter_is_unavailable() -> None:
    """Store without callable get_board_task must surface as unavailable.

    Reviewer mutation that previously stayed green on the full lane:

        return None  # REVIEW-MUTATION: absent board getter as live missing

    (or ``return lambda _id: None``). That collapses measurement failure into
    live missing. Require the builder to return a raising lookup and
    resolve_target to report ``state='unavailable'``, distinct from missing.
    """
    notification = {
        "ref_type": "board_task",
        "ref_id": "btk_no_getter",
        "payload": {"files_count": 2},
    }

    class _StoreWithoutGetter:
        """Collab-shaped object that has no get_board_task."""

        pass

    class _StoreWithNonCallableGetter:
        get_board_task = "not-a-callable"  # type: ignore[assignment]

    for opener in (
        lambda: _StoreWithoutGetter(),
        lambda: _StoreWithNonCallableGetter(),
    ):
        lookup = service.build_board_lookup(opener)
        assert lookup is not None, "missing getter must still return a live lookup"
        raised = False
        try:
            lookup("btk_no_getter")
        except Exception:
            raised = True
        assert raised, (
            "missing get_board_task must raise on lookup; "
            "returning None collapses unavailable into live missing"
        )
        target = service.resolve_target(notification, None, lookup)
        assert target["resolved"] is False
        assert target["state"] == "unavailable", (
            f"missing board getter reported state={target['state']!r}; "
            "must be unavailable, not missing/resolved"
        )
        assert target["state"] != "missing"
        assert target["href"] == "/board"


def test_build_board_lookup_healthy_store_is_preserved() -> None:
    """Positive control: a working get_board_task must remain the lookup."""
    notification = {
        "ref_type": "board_task",
        "ref_id": "btk_live",
        "payload": {"files_count": 1},
    }

    class _HealthyStore:
        def get_board_task(self, task_id: str) -> dict[str, Any]:
            return {"id": task_id, "status": "done", "title": "Shipped"}

    lookup = service.build_board_lookup(lambda: _HealthyStore())
    assert lookup is not None
    row = lookup("btk_live")
    assert row is not None
    assert row["status"] == "done"
    target = service.resolve_target(notification, None, lookup)
    assert target["state"] == "done"
    assert target["resolved"] is True
    assert target["task_title"] == "Shipped"


def test_resolve_target_approval_missing_state_is_not_actionable_pending() -> None:
    """Approval row with absent/empty state is unmeasured, not actionable pending.

    Counterfeit that would fake a weaker fix (and the independent spot check):
        state = str(approval.get("state") or "pending")
    That invents an Approve button from a non-result.
    """
    notification = {"ref_type": "approval", "ref_id": "apr_no_state"}

    missing_state = service.resolve_target(
        notification, lambda _id: {"id": "apr_no_state", "action_class": "consequential"}
    )
    assert missing_state["resolved"] is False
    assert missing_state["actionable"] is False, (
        f"absent state reported actionable={missing_state['actionable']}; "
        "unknown/absent must not present favourably as pending"
    )
    assert missing_state["state"] != "pending"
    assert missing_state["state"] == "unavailable"

    empty_state = service.resolve_target(
        notification, lambda _id: {"id": "apr_no_state", "state": "", "action_class": "x"}
    )
    assert empty_state["resolved"] is False
    assert empty_state["actionable"] is False
    assert empty_state["state"] == "unavailable"

    # Positive control: real pending remains actionable.
    pending = service.resolve_target(
        notification, lambda _id: {"id": "apr_no_state", "state": "pending"}
    )
    assert pending["state"] == "pending"
    assert pending["actionable"] is True
    assert pending["resolved"] is False


def test_resolve_target_run_and_session_hrefs() -> None:
    run = service.resolve_target({"ref_type": "run", "ref_id": "run_1"}, None)
    assert run["href"] == "/runs/run_1"
    session = service.resolve_target({"ref_type": "session", "ref_id": "ses_1"}, None)
    assert session["href"] == "/sessions/ses_1"


# --- notify_task_done (C0) ----------------------------------------------------


def test_notify_task_done_persists_board_task_row(dal: NotificationsDal) -> None:
    nid = service.notify_task_done(
        board_task_id="btk_1",
        task_title="Ship the report",
        files_count=3,
        workspace="/tmp/ws",
        run_id="orch_1",
        session_id=None,
        dal=dal,
        push=False,
    )
    row = dal.get(nid or "")
    assert row is not None
    assert row["kind"] == "done"
    assert row["ref_type"] == "board_task"
    assert row["ref_id"] == "btk_1"
    assert '"files_count":3' in row["payload_json"]
    assert '"task_title":"Ship the report"' in row["payload_json"]


def test_notify_task_done_dedupes_kind_even_after_read(dal: NotificationsDal) -> None:
    # Both intake emit points fire for one completion -> exactly one bell, and the
    # dedupe must hold even once the operator has READ the first (unlike the
    # unread-only ref dedupe used by live sources).
    first = service.notify_task_done(board_task_id="btk_9", dal=dal, push=False)
    assert first is not None
    dal.mark_read(first)
    second = service.notify_task_done(board_task_id="btk_9", dal=dal, push=False)
    assert second is None
    assert len([r for r in dal.list() if r["ref_id"] == "btk_9"]) == 1


def test_resolve_target_board_task_deep_links_and_resolves() -> None:
    notification = {
        "ref_type": "board_task",
        "ref_id": "btk_1",
        "payload": {"files_count": 4, "task_title": "payload title"},
    }
    target = service.resolve_target(
        notification, None, lambda _id: {"id": "btk_1", "status": "done", "title": "Ship it"}
    )
    assert target["href"] == "/board?task=btk_1&files=1"
    assert target["state"] == "done"
    assert target["resolved"] is True
    assert target["board_task_id"] == "btk_1"
    assert target["files_count"] == 4
    # A live board title wins over the payload snapshot.
    assert target["task_title"] == "Ship it"


def test_resolve_target_board_task_missing_card_degrades_to_board() -> None:
    notification = {"ref_type": "board_task", "ref_id": "btk_gone", "payload": {"files_count": 2}}
    target = service.resolve_target(notification, None, lambda _id: None)
    # Card gone (archived/pruned): degrade the href but never a dead link, and
    # keep the payload-derived count. Measured missing is not a lookup failure.
    assert target["href"] == "/board"
    assert target["files_count"] == 2
    assert target["state"] == "missing"
    assert target["resolved"] is False


def test_resolve_target_board_task_lookup_failure_is_not_missing() -> None:
    """Board lookup exception must not be identical to a genuine missing card.

    Independent spot check: both previously returned state=None, resolved=False,
    href='/board'. Require unavailable vs missing so unreadable ≠ measured empty.
    """
    notification = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload": {"files_count": 2, "task_title": "payload title"},
    }

    def boom(_id: str) -> Any:
        raise RuntimeError("board database unavailable")

    failed = service.resolve_target(notification, None, boom)
    missing = service.resolve_target(notification, None, lambda _id: None)

    assert failed["href"] == "/board"
    assert failed["files_count"] == 2
    assert failed["resolved"] is False
    assert failed["state"] == "unavailable", (
        f"board lookup failure reported state={failed['state']!r}; "
        "must be unavailable, not collapsed into missing"
    )

    assert missing["href"] == "/board"
    assert missing["state"] == "missing"
    assert missing["resolved"] is False
    assert (failed["state"], failed["resolved"], failed["href"]) != (
        missing["state"],
        missing["resolved"],
        missing["href"],
    ) or failed["state"] != missing["state"], (
        "board lookup failure must not be identical to genuine missing card"
    )
    # Explicit identity check the probe used.
    assert not (
        failed["state"] == missing["state"]
        and failed["resolved"] == missing["resolved"]
        and failed["href"] == missing["href"]
    ), "board_failure_identical_to_missing must be False"


def test_resolve_target_board_task_legacy_two_arg_still_deep_links() -> None:
    # Existing 2-arg callers (no board_lookup) still get a valid deep link.
    target = service.resolve_target({"ref_type": "board_task", "ref_id": "btk_2"}, None)
    assert target["href"] == "/board?task=btk_2&files=1"


def test_resolve_target_board_task_reads_payload_from_raw_json() -> None:
    # The raw DB row carries payload_json (not a parsed payload) -- files_count
    # must still survive to the target.
    notification = {
        "ref_type": "board_task",
        "ref_id": "btk_3",
        "payload_json": '{"files_count":7,"task_title":"raw"}',
    }
    target = service.resolve_target(notification, None)
    assert target["files_count"] == 7
    assert target["task_title"] == "raw"


def test_malformed_payload_json_is_not_identical_to_empty() -> None:
    """Unparseable / absent / blank payload must not render like genuine empty.

    Governing defect class: missing/unreadable source reported identically to a
    genuinely empty one. Production counterfeits:

    - ``except: return {}`` / non-object → ``{}``
    - null / blank / missing source → ``{}``
    - pre-parsed ``{}`` trusted over surviving malformed ``payload_json``

    Genuine empty is **only** an explicit empty object (``"{}"`` / ``{}``).
    """
    empty_row = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload_json": "{}",
    }
    malformed_row = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload_json": "{not-json",
    }
    non_object_row = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload_json": '["not", "an", "object"]',
    }
    # Collapse counterfeit: pre-parsed {} coexists with unreadable raw. Raw
    # must win — otherwise unreadable≡empty when an outer layer "helps".
    collapsed_malformed_row = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload": {},
        "payload_json": "{not-json",
    }
    empty_with_preparsed = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload": {},
        "payload_json": "{}",
    }
    # Absent / blank must not collapse to genuine empty (reviewer probe).
    absent_row = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
    }
    null_raw_row = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload_json": None,
    }
    blank_raw_row = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload_json": "   ",
    }
    empty_string_raw_row = {
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload_json": "",
    }

    empty_payload = service._notification_payload(empty_row)
    malformed_payload = service._notification_payload(malformed_row)
    non_object_payload = service._notification_payload(non_object_row)
    collapsed_payload = service._notification_payload(collapsed_malformed_row)
    empty_pre_payload = service._notification_payload(empty_with_preparsed)
    absent_payload = service._notification_payload(absent_row)
    null_raw_payload = service._notification_payload(null_raw_row)
    blank_raw_payload = service._notification_payload(blank_raw_row)
    empty_string_payload = service._notification_payload(empty_string_raw_row)

    assert empty_payload == {}, f"genuine empty must be {{}}; got {empty_payload!r}"
    assert empty_pre_payload == {}, (
        f"genuine empty+preparsed must be {{}}; got {empty_pre_payload!r}"
    )
    assert malformed_payload is None, (
        f"malformed JSON must not collapse to empty dict; got {malformed_payload!r}"
    )
    assert non_object_payload is None, (
        f"non-object JSON must not collapse to empty dict; got {non_object_payload!r}"
    )
    assert collapsed_payload is None, (
        f"pre-parsed {{}} must not hide malformed payload_json; got {collapsed_payload!r}"
    )
    assert absent_payload is None, (
        f"absent payload source must not collapse to empty dict; got {absent_payload!r}"
    )
    assert null_raw_payload is None, (
        f"null payload_json must not collapse to empty dict; got {null_raw_payload!r}"
    )
    assert blank_raw_payload is None, (
        f"whitespace-only payload_json must not collapse to empty dict; got {blank_raw_payload!r}"
    )
    assert empty_string_payload is None, (
        f"empty-string payload_json must not collapse to empty dict; got {empty_string_payload!r}"
    )
    assert malformed_payload != empty_payload, (
        "malformed_payload must not be identical to genuine empty_payload"
    )
    assert non_object_payload != empty_payload, (
        "non-object payload must not be identical to genuine empty_payload"
    )
    assert collapsed_payload != empty_pre_payload, (
        "collapsed-malformed must not be identical to genuine empty+preparsed"
    )
    assert absent_payload != empty_payload, "helper_absent must not equal helper_valid_empty"
    assert null_raw_payload != empty_payload
    assert blank_raw_payload != empty_payload
    assert empty_string_payload != empty_payload

    # Live board miss + empty payload vs live miss + unreadable/absent payload.
    empty_target = service.resolve_target(empty_row, None, lambda _id: None)
    malformed_target = service.resolve_target(malformed_row, None, lambda _id: None)
    non_object_target = service.resolve_target(non_object_row, None, lambda _id: None)
    collapsed_target = service.resolve_target(collapsed_malformed_row, None, lambda _id: None)
    empty_pre_target = service.resolve_target(empty_with_preparsed, None, lambda _id: None)
    absent_target = service.resolve_target(absent_row, None, lambda _id: None)
    blank_target = service.resolve_target(blank_raw_row, None, lambda _id: None)

    assert empty_target.get("state") == "missing"
    assert empty_target.get("task_title") is None
    assert empty_target.get("files_count") is None
    assert empty_target.get("payload_state") in (None, "ok", "empty"), (
        f"genuine empty must not be marked unreadable; target={empty_target!r}"
    )
    assert empty_pre_target.get("payload_state") in (None, "ok", "empty"), (
        f"genuine empty+preparsed must not be marked unreadable; target={empty_pre_target!r}"
    )

    assert malformed_target.get("payload_state") == "unavailable", (
        f"malformed payload reported payload_state={malformed_target.get('payload_state')!r}; "
        f"target={malformed_target!r}; must be unavailable, not measured empty"
    )
    assert non_object_target.get("payload_state") == "unavailable", (
        f"non-object payload reported payload_state={non_object_target.get('payload_state')!r}; "
        f"target={non_object_target!r}"
    )
    assert collapsed_target.get("payload_state") == "unavailable", (
        f"collapsed-malformed reported payload_state={collapsed_target.get('payload_state')!r}; "
        f"target={collapsed_target!r}"
    )
    assert absent_target.get("payload_state") == "unavailable", (
        f"absent payload reported payload_state={absent_target.get('payload_state')!r}; "
        f"target={absent_target!r}"
    )
    assert blank_target.get("payload_state") == "unavailable", (
        f"blank payload reported payload_state={blank_target.get('payload_state')!r}; "
        f"target={blank_target!r}"
    )
    assert malformed_target != empty_target, (
        "malformed target must not be identical to genuine empty target; "
        f"empty={empty_target!r} malformed={malformed_target!r}"
    )
    assert non_object_target != empty_target, (
        "non-object target must not be identical to genuine empty target"
    )
    assert collapsed_target != empty_pre_target, (
        "collapsed-malformed target must not be identical to genuine empty+preparsed target; "
        f"empty={empty_pre_target!r} collapsed={collapsed_target!r}"
    )
    assert absent_target != empty_target, (
        "absent target must not be identical to genuine empty target; "
        f"empty={empty_target!r} absent={absent_target!r}"
    )
    assert blank_target != empty_target, (
        "blank target must not be identical to genuine empty target"
    )


def test_serialize_notification_preserves_payload_and_wires_builders() -> None:
    """Owned production serializer: raw evidence survives; builders are called.

    Counterfeits this binds:

    1. Route-shaped collapse: pop payload_json, ``except: payload={}``, then
       resolve_target — makes empty/malformed/absent identical.
    2. Fail-open factory: catch open errors → ``lambda _id: None`` (live missing).
    3. Built-never-wired builders: serialize must call build_*_lookup.

    Failing-on-revert: restore collapse-on-error payload parse inside
    serialize_notification, or replace build_*_lookup with lambda:None factories.
    """
    empty_row = {
        "id": "ntf_e",
        "kind": "done",
        "title": "empty",
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload_json": "{}",
        "read_at": None,
        "acted_at": None,
    }
    malformed_row = {
        **empty_row,
        "id": "ntf_m",
        "title": "malformed",
        "payload_json": "{not-json",
    }
    absent_row = {
        "id": "ntf_a",
        "kind": "done",
        "title": "absent",
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "read_at": None,
        "acted_at": None,
    }

    empty_out = service.serialize_notification(empty_row)
    malformed_out = service.serialize_notification(malformed_row)
    absent_out = service.serialize_notification(absent_row)

    assert empty_out["payload"] == {}, f"genuine empty body payload; got {empty_out['payload']!r}"
    assert empty_out["target"].get("payload_state") in (None, "ok", "empty")
    assert empty_out["target"]["state"] is None  # no board lookup wired
    assert "payload_json" not in empty_out

    assert malformed_out["payload"] is None, (
        f"malformed must not serialize as {{}}; got {malformed_out['payload']!r}"
    )
    assert malformed_out["target"].get("payload_state") == "unavailable"
    assert malformed_out != empty_out
    assert malformed_out["target"] != empty_out["target"], (
        "api_malformed must not equal api_empty after owned serialize; "
        f"empty={empty_out['target']!r} malformed={malformed_out['target']!r}"
    )

    assert absent_out["payload"] is None, (
        f"absent must not serialize as {{}}; got {absent_out['payload']!r}"
    )
    assert absent_out["target"].get("payload_state") == "unavailable"
    assert absent_out["target"] != empty_out["target"], (
        "api_absent must not equal api_empty after owned serialize"
    )

    # Builders wired: open failure → unavailable, not live missing.
    approval_row = {
        "id": "ntf_apr",
        "kind": "approval",
        "title": "need approval",
        "ref_type": "approval",
        "ref_id": "apr_probe",
        "payload_json": "{}",
        "read_at": None,
        "acted_at": None,
    }

    def boom_open() -> Any:
        raise RuntimeError("sessions database unavailable")

    open_fail = service.serialize_notification(approval_row, open_approval_reader=boom_open)
    assert open_fail["target"]["resolved"] is False
    assert open_fail["target"]["state"] == "unavailable", (
        f"route_open_failure must be unavailable, not missing; target={open_fail['target']!r}"
    )

    class _MissingReader:
        def get_approval_by_id(self, _id: str) -> None:
            return None

    live_miss = service.serialize_notification(
        approval_row, open_approval_reader=lambda: _MissingReader()
    )
    assert live_miss["target"]["state"] == "missing"
    assert live_miss["target"]["state"] != open_fail["target"]["state"], (
        "open failure must not equal live missing on owned serialize path"
    )

    # Unwired openers → approval state stays None (not missing).
    unwired = service.serialize_notification(approval_row)
    assert unwired["target"]["state"] is None
    assert unwired["target"]["resolved"] is False
    assert unwired["target"]["state"] != live_miss["target"]["state"]

    # Board missing-getter path is reached via serialize → build_board_lookup.
    board_row = {
        "id": "ntf_b",
        "kind": "done",
        "title": "board",
        "ref_type": "board_task",
        "ref_id": "btk_probe",
        "payload_json": '{"files_count":1}',
        "read_at": None,
        "acted_at": None,
    }

    class _NoGetter:
        pass

    board_bad = service.serialize_notification(board_row, open_board_store=lambda: _NoGetter())
    assert board_bad["target"]["state"] == "unavailable"
    assert board_bad["target"]["href"] == "/board"


def test_package_exports_serializer_and_builders() -> None:
    """Package ``__init__`` must re-export serialize/builder production symbols.

    Counterfeit this binds: helpers exist only on ``service`` while package
    ``__all__`` omits them (export change is cosmetic / never used). Reviewer
    mutation that drops those exports must fail this test.

    Failing-on-revert: remove ``serialize_notification`` /
    ``build_approval_lookup`` / ``build_board_lookup`` from
    ``omniagentos/notifications/__init__.py``.
    """
    import omniagentos.notifications as notifications_pkg

    required = (
        "serialize_notification",
        "build_approval_lookup",
        "build_board_lookup",
    )
    for name in required:
        assert name in notifications_pkg.__all__, (
            f"{name} missing from package __all__ — export reverted?"
        )
        exported = getattr(notifications_pkg, name, None)
        assert exported is not None, f"package attribute {name} missing — export reverted?"
        assert exported is getattr(service, name), (
            f"package.{name} is not the service implementation"
        )
        # Public import form must resolve to the same service symbol.
        from importlib import import_module

        pkg = import_module("omniagentos.notifications")
        assert getattr(pkg, name) is getattr(service, name)


# --- notify_run_terminal_failure (failed/cancelled swarm runs) ----------------


def test_notify_run_terminal_failure_persists_board_task_row(dal: NotificationsDal) -> None:
    nid = service.notify_run_terminal_failure(
        run_id="swr_1",
        status="failed",
        goal="Build the widget",
        board_task_id="btk_f1",
        workspace="/tmp/ws",
        dal=dal,
        push=False,
    )
    row = dal.get(nid or "")
    assert row is not None
    assert row["kind"] == "swarm_failed"
    assert row["severity"] == "warning"
    assert row["title"] == "Swarm run failed"
    assert row["body"] == "Build the widget"
    assert row["ref_type"] == "board_task"
    assert row["ref_id"] == "btk_f1"
    assert '"run_id":"swr_1"' in row["payload_json"]
    assert '"status":"failed"' in row["payload_json"]


def test_notify_run_terminal_failure_cancelled_title_and_run_ref(dal: NotificationsDal) -> None:
    # No board card: the run itself is the persisted target so a card-less run
    # still surfaces (and still dedupes) instead of vanishing.
    nid = service.notify_run_terminal_failure(
        run_id="swr_2", status="cancelled", dal=dal, push=False
    )
    row = dal.get(nid or "")
    assert row is not None
    assert row["title"] == "Swarm run cancelled"
    assert row["ref_type"] == "run"
    assert row["ref_id"] == "swr_2"
    assert "swr_2" in row["body"]  # goal-less body still names the run


def test_notify_run_terminal_failure_dedupes_kind_even_after_read(dal: NotificationsDal) -> None:
    # A crash-resumed coordinator can re-observe the same terminal state; the
    # second emit must no-op even once the operator has READ the first.
    first = service.notify_run_terminal_failure(
        run_id="swr_3", status="failed", board_task_id="btk_f3", dal=dal, push=False
    )
    assert first is not None
    dal.mark_read(first)
    second = service.notify_run_terminal_failure(
        run_id="swr_3", status="failed", board_task_id="btk_f3", dal=dal, push=False
    )
    assert second is None
    assert len([r for r in dal.list() if r["ref_id"] == "btk_f3"]) == 1


def test_notify_run_terminal_failure_not_blocked_by_done_row(dal: NotificationsDal) -> None:
    # Kind-aware dedupe: an existing done bell on the same card must not
    # swallow a swarm_failed alert (and vice versa) -- they are distinct events.
    assert service.notify_task_done(board_task_id="btk_mix", dal=dal, push=False) is not None
    nid = service.notify_run_terminal_failure(
        run_id="swr_4", status="failed", board_task_id="btk_mix", dal=dal, push=False
    )
    assert nid is not None
    kinds = sorted(r["kind"] for r in dal.list() if r["ref_id"] == "btk_mix")
    assert kinds == ["done", "swarm_failed"]


def test_notify_run_terminal_failure_not_suppressed_by_longhaul_blocked(
    dal: NotificationsDal,
) -> None:
    # F2 collision pin: longhaul's transient capacity-wait notification is
    # kind='blocked' on the SAME board_task ref. The swarm failure bell must
    # use its own kind ('swarm_failed') so neither bell can ever dedupe the
    # other away -- an existing blocked row (read or unread) must not swallow
    # the failure bell, and the failure bell must not stop a later blocked row.
    first_blocked = service.record_notification(
        kind="blocked",
        title="Task waiting on capacity",
        body="",
        severity="warning",
        ref_type="board_task",
        ref_id="btk_lh",
        payload={"task_id": "btk_lh", "source": "longhaul"},
        dal=dal,
        push=False,
    )
    assert first_blocked is not None
    nid = service.notify_run_terminal_failure(
        run_id="swr_lh", status="failed", board_task_id="btk_lh", dal=dal, push=False
    )
    assert nid is not None
    row = dal.get(nid)
    assert row is not None and row["kind"] == "swarm_failed"
    # And the reverse: a longhaul blocked emit after the failure bell still
    # lands once earlier rows are read (record_notification's own guard is
    # unread-only and kind-agnostic; the swarm_failed row must not make the
    # suppression permanent -- kind-aware read-agnostic dedupe applies only to
    # swarm_failed itself).
    dal.mark_read(first_blocked)
    dal.mark_read(nid)
    assert (
        service.record_notification(
            kind="blocked",
            title="Task waiting on capacity",
            body="",
            severity="warning",
            ref_type="board_task",
            ref_id="btk_lh",
            payload={"task_id": "btk_lh", "source": "longhaul"},
            dal=dal,
            push=False,
        )
        is not None
    )
    kinds = sorted(r["kind"] for r in dal.list() if r["ref_id"] == "btk_lh")
    assert kinds == ["blocked", "blocked", "swarm_failed"]
