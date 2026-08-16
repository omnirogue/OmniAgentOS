"""Intake test configuration.

C0 added a "task complete" notification emit to the two intake completion paths
(``_run_orchestration_with_lifecycle`` and ``reconcile_board``). Persisting the
notification is the tested behavior; the best-effort OS push that follows it is
NOT -- and on a developer's macOS it would pop a real ``terminal-notifier``
banner for every intake test that drives a card to done. The autouse fixture
below stubs only the delivery seam (``notifications.service._push``) so intake
tests never emit a real desktop notification, exactly like the notifications
suite's own ``_no_push`` fixture. Persistence (the ``notifications`` row) is
untouched, so the dedupe/emit assertions still hold.
"""

from __future__ import annotations

import pytest

from omniagentos.notifications import service as _notifications_service


@pytest.fixture(autouse=True)
def _no_notification_push(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_notifications_service, "_push", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _no_external_process_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Board tests must not scan the host process table or contend for locks.

    Multi-provider external discovery is covered by
    ``tests/sessions/test_discover_external_board.py`` with injected process lists.
    """
    monkeypatch.setenv("OMNIAGENTOS_DISCOVER_EXTERNAL", "0")
