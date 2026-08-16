"""Node transitions and per-call cost onto the existing ``events`` ledger.

No new table, no new stream, no new port: loop telemetry is written with
``store.insert_event`` and therefore rides ``GET /api/events`` (SSE) for free —
the route selects all types when no ``types=`` filter is given, so these
additive kinds reach the dashboard exactly like ``swarm.event`` and the mission
kinds do (contracts.MissionEvents precedent).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised whenever LangGraph is installed
    from langgraph.errors import GraphInterrupt as _GraphInterrupt

    _CONTROL_FLOW: tuple[type[BaseException], ...] = (_GraphInterrupt,)
except ImportError:  # pragma: no cover - observability is usable without LangGraph
    _CONTROL_FLOW = ()


class LoopEvents:
    """Additive loop event kinds (deliberately NOT members of Events.ALL)."""

    NODE = "loop.node"
    EFFECT = "loop.effect"
    APPROVAL = "loop.approval"
    MODEL_CALL = "loop.model_call"
    STATUS = "loop.status"

    ALL: tuple[str, ...] = (NODE, EFFECT, APPROVAL, MODEL_CALL, STATUS)


def emit(ctx: Any, kind: str, action: str, payload: dict[str, Any] | None = None) -> int:
    """Append one loop event. Never raises — telemetry must not kill a loop."""
    try:
        return int(
            ctx.store.insert_event(
                type=kind,
                actor=ctx.actor,
                action=action,
                target_type="loop",
                target_id=ctx.instance_id,
                payload={"template": ctx.template, **(payload or {})},
                trace_id=ctx.instance_id,
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("loop event %s/%s could not be written", kind, action)
        return 0


def observed(ctx: Any, name: str, fn: Any) -> Any:
    """Wrap a node callable so entry and exit both land on the events ledger.

    Requirement 8's "node transitions -> events rows": applied by the runtime to
    every node of every template, so observability is structural rather than a
    thing each template author must remember.
    """

    def node(state: Any, *args: Any, **kwargs: Any) -> Any:
        emit(ctx, LoopEvents.NODE, f"{name}.enter", {"tick": state.get("tick", 0)})
        try:
            result = fn(state, *args, **kwargs)
        except _CONTROL_FLOW:
            # interrupt() is control flow, not a fault: the park is already
            # recorded as loop.approval.requested. Do not log it as an error.
            emit(ctx, LoopEvents.NODE, f"{name}.parked", {})
            raise
        except Exception as exc:
            emit(
                ctx,
                LoopEvents.NODE,
                f"{name}.error",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        emit(ctx, LoopEvents.NODE, f"{name}.exit", {})
        return result

    node.__name__ = f"observed_{name}"
    return node


__all__ = ["LoopEvents", "emit", "observed"]
