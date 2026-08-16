"""Swarm activity emitter (WP6a): writes ``swarm.event`` rows via ``Store.insert_event``.

Implements the :class:`omniagentos.swarm.contracts.SwarmEmitter` protocol — the
planner/scheduler/router call ``emit(run_id, action, payload)`` and never see or
handle a failure: like ``intake.service._emit_board_event``, emission here is
best-effort observability and must never interrupt control flow (a slot worker
mid-spawn cannot be taken down by a full events table or a transient sqlite
lock).

Payload conventions (``contracts/swarm-api.md`` is the authoritative doc): a
payload MAY carry ``task_id``, ``provider``, ``model``, an account LABEL (a
human-readable name — NEVER a credential path or secret value), ``seconds``,
and ``reason``, whichever apply to the action. Callers must never put a
filesystem path to a credential, nor the credential itself, into a payload.
"""

from __future__ import annotations

import logging
from typing import Any

from omniagentos.contracts import Store
from omniagentos.swarm.contracts import SWARM_EVENT_ACTIONS, SWARM_EVENT_KIND

LOG = logging.getLogger(__name__)


class StoreSwarmEmitter:
    """The real ``SwarmEmitter``: writes ``swarm.event`` rows via ``Store.insert_event``.

    One instance wraps one :class:`~omniagentos.contracts.Store`. Every row is
    written with ``target_type="swarm_run"``/``target_id=run_id`` so
    ``GET /api/swarm/{id}/activity`` (and any future consumer) can query it the
    same way ``get_events_for_run`` already does for ``target_type='run'``.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def emit(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> None:
        """Record one ``swarm.event`` row for ``run_id``. Must never raise.

        ``action`` is not hard-validated against :data:`SWARM_EVENT_ACTIONS` — a
        future action name landing before this module is updated must still be
        observable rather than silently dropped — but an unrecognized action is
        logged once at DEBUG so a typo is still discoverable in development.
        """
        if action not in SWARM_EVENT_ACTIONS:
            LOG.debug("swarm.event with unrecognized action %r for run %s", action, run_id)
        try:
            self._store.insert_event(
                SWARM_EVENT_KIND,
                "swarm",
                action,
                target_type="swarm_run",
                target_id=run_id,
                payload=payload or {},
                execution_id=run_id,
            )
        except Exception:  # noqa: BLE001 -- emission is best-effort observability, never control flow.
            LOG.debug(
                "swarm activity emit failed for run %s action %s",
                run_id,
                action,
                exc_info=True,
            )


__all__ = ["StoreSwarmEmitter"]
