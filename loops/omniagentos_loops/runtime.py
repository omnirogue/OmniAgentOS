"""LoopContext + the tick driver: build, resume, park, report.

One invocation of :func:`run_once` == one *tick* of one loop instance, in one
short-lived OS process. That is the P6 shape (launchd fires a process; all
history lives in the checkpointer) and it is why a loop survives reboots,
crashes, and deploys without any resident supervisor.

Three entry states, resolved from the checkpoint alone:

* **parked on an interrupt** — resolve the approvals row. Undecided => stay
  parked and exit cleanly. Decided/expired => resume with that verdict.
* **mid-run** (``snapshot.next`` non-empty after a crash) — ``invoke(None)``
  replays from the last committed superstep (P1: 7 ms).
* **idle** — start a fresh tick on the same ``thread_id``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from omniagentos.contracts import DASHBOARD_ORIGIN, default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.policy import PolicyConfig, load_policy
from omniagentos_loops import approvals, lease
from omniagentos_loops.contracts import (
    BlockedLoopError,
    LoopState,
    LoopStatus,
    RunReport,
    initial_state,
)
from omniagentos_loops.observability import LoopEvents, emit, observed
from omniagentos_loops.paths import checkpoint_path, require_safe_name
from omniagentos_loops.tools import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class LoopContext:
    """Everything a loop instance's nodes are allowed to touch.

    Deliberately explicit: a node reaches the world only through ``tools``
    (policy-gated), ``store`` (control plane), ``notifier`` (paging) and
    ``model`` (budget-capped). There is no ambient client and no ``os.environ``
    credential read anywhere in this package — connectors resolve their own
    secrets behind ``omniagentos.connectors.broker``.
    """

    instance_id: str
    template: str
    params: dict[str, Any] = field(default_factory=dict)
    store: Any = None
    policy_cfg: PolicyConfig | None = None
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    denied_tools: frozenset[str] = frozenset()
    notifier: Callable[[str], Any] | None = None
    dashboard_origin: str = DASHBOARD_ORIGIN
    project_id: str | None = None
    org_company_id: str | None = None

    def __post_init__(self) -> None:
        require_safe_name(self.instance_id, kind="instance")
        require_safe_name(self.template, kind="template")
        if self.policy_cfg is None:
            self.policy_cfg = load_policy()
        if self.notifier is None:
            self.notifier = _default_notifier

    @property
    def actor(self) -> str:
        """Identity stamped on every event/receipt this instance writes.

        ``loop:*`` is an automation identity by construction, so it can never
        satisfy an ``always_human`` approval (policy._is_automation_identity
        rejects bot/automation/system schemes and self-approval).
        """
        return f"loop:{self.instance_id}"

    def model(self, **kwargs: Any) -> Any:
        from omniagentos_loops.models import LoopModel

        return LoopModel(self, **kwargs)


@dataclass(frozen=True)
class DeferredPage:
    """A page the worker recorded but deliberately did not deliver."""

    ok: bool = False
    channel: str = "deferred"
    detail: str = "deferred to the control plane (worker holds no credentials)"


def _default_notifier(text: str) -> Any:
    """Record the page; do NOT deliver it from inside the worker.

    Delivery needs the Slack webhook URL, and the webhook URL *is* the
    credential. The worker is launched with a scrubbed environment precisely so
    that no credential crosses the process boundary, which means it cannot — and
    must not — post. ``omniagentos/scheduler/loop_jobs.py`` runs in the
    scheduler process, which legitimately holds that secret, and delivers the
    page there after the tick reports ``parked``.
    """
    return DeferredPage()


@dataclass(frozen=True)
class LoopTemplate:
    """A named graph builder — the reusable primitive a workflow instantiates."""

    name: str
    family: str
    required_tools: tuple[str, ...]
    build: Callable[[LoopContext], Any]
    description: str = ""


def _pending_interrupt(snapshot: Any) -> Any | None:
    interrupts = getattr(snapshot, "interrupts", None) or ()
    if interrupts:
        return interrupts[0]
    for task in getattr(snapshot, "tasks", ()) or ():
        task_interrupts = getattr(task, "interrupts", None) or ()
        if task_interrupts:
            return task_interrupts[0]
    return None


def _interrupt_payload(pending: Any) -> dict[str, Any]:
    value = getattr(pending, "value", None)
    return value if isinstance(value, dict) else {}


def _status_of(values: dict[str, Any]) -> LoopStatus:
    """Terminal status of one tick, read from the graph's final state. FAIL-CLOSED.

    A tick that reports NO status, or a status this runtime does not recognise,
    has not told us it succeeded — it has told us nothing. Defaulting that to
    ``COMPLETED`` (the previous behaviour) put the most favourable outcome at
    the exact point where the loop grades itself: a template path that forgets
    to stamp a status, a node that returns ``{}`` on an unhandled branch, or an
    enum drift after a deploy all rendered as accepted work. The absence of a
    result is not a result.
    """
    raw = str((values or {}).get("status") or "")
    try:
        return LoopStatus(raw)
    except ValueError:
        logger.warning(
            "loop tick produced no usable terminal status (%r); failing closed as FAILED", raw
        )
        return LoopStatus.FAILED


def compile_template(template: LoopTemplate, ctx: LoopContext, conn: sqlite3.Connection) -> Any:
    """Compile *template* against a per-family SqliteSaver."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    graph = template.build(ctx)
    return graph.compile(checkpointer=SqliteSaver(conn))


def open_checkpoint(family: str) -> sqlite3.Connection:
    return sqlite3.connect(str(checkpoint_path(family)), check_same_thread=False)


def thread_id(ctx: LoopContext, template: LoopTemplate) -> str:
    """Checkpoint thread key, namespaced by template.

    A family's checkpoint file is shared by every instance of that family, so a
    bare instance id lets two DIFFERENT templates (or a row retargeted from one
    template to another) resume each other's half-finished graphs. The template
    name is part of the key for the same reason it is part of the approval id.
    """
    return f"{template.name}:{ctx.instance_id}"


def run_once(ctx: LoopContext, template: LoopTemplate) -> RunReport:
    """Drive one tick of ``ctx.instance_id``, under this instance's lease.

    Never raises for business faults. A tick that cannot take the lease is a
    NON-RESULT (``IDLE``): another worker is already advancing this instance, so
    the correct behaviour is to step aside quietly, not to report a failure that
    would count against the routine's acceptance floor.
    """
    missing = [name for name in template.required_tools if name not in ctx.tools.names()]
    if missing:
        return RunReport(
            ctx.instance_id,
            template.name,
            LoopStatus.FAILED,
            detail=f"instance is missing required tools: {sorted(missing)}",
        )

    try:
        with lease.held(template.name, ctx.instance_id):
            return _run_locked(ctx, template)
    except BlockedLoopError as exc:
        # NOT a failure of this tick's logic and NOT a non-result the system can
        # shrug at: something the system owns (a dead credential, a revoked
        # grant) makes every future tick pointless until a human fixes it. It is
        # reported ADVERSE precisely so the acceptance floor pauses the routine
        # and pages, instead of the loop idling green forever.
        emit(ctx, LoopEvents.STATUS, "loop.blocked", {"cause": exc.cause, "detail": exc.detail})
        return RunReport(
            ctx.instance_id,
            template.name,
            LoopStatus.BLOCKED,
            detail=f"{exc.cause}: {exc.detail}",
        )
    except lease.LeaseHeld as exc:
        emit(ctx, LoopEvents.STATUS, "loop.lease_held", {"holder": exc.holder})
        return RunReport(
            ctx.instance_id,
            template.name,
            LoopStatus.IDLE,
            detail=f"another worker holds this instance: {exc.holder}",
        )


def _run_locked(ctx: LoopContext, template: LoopTemplate) -> RunReport:
    config = {"configurable": {"thread_id": thread_id(ctx, template)}}
    with closing(open_checkpoint(template.family)) as conn:
        graph = compile_template(template, ctx, conn)
        snapshot = graph.get_state(config)
        pending = _pending_interrupt(snapshot)
        resumed = False

        if pending is not None:
            payload = _interrupt_payload(pending)
            row_id = str(payload.get("approval_id") or "")
            outcome = approvals.resolve_for_resume(ctx, row_id) if row_id else None
            if outcome is None:
                return RunReport(
                    ctx.instance_id,
                    template.name,
                    LoopStatus.FAILED,
                    detail="parked interrupt carries no approval id",
                )
            if not outcome.terminal:
                emit(
                    ctx,
                    LoopEvents.STATUS,
                    "loop.parked",
                    {"approval_id": row_id, "reason": outcome.reason},
                )
                return RunReport(
                    ctx.instance_id,
                    template.name,
                    LoopStatus.PARKED,
                    detail=outcome.reason,
                    approval_id=row_id,
                    stage=str(payload.get("node") or ""),
                )
            from langgraph.types import Command

            resumed = True
            values = graph.invoke(Command(resume=outcome.as_resume()), config, durability="sync")
        elif snapshot.next:
            resumed = True
            values = graph.invoke(None, config, durability="sync")
        else:
            start: LoopState = initial_state(ctx.instance_id, template.name, ctx.params)
            start["tick"] = int((snapshot.values or {}).get("tick") or 0) + 1
            values = graph.invoke(start, config, durability="sync")

        return _report(ctx, template, values, resumed=resumed)


def _report(
    ctx: LoopContext, template: LoopTemplate, values: dict[str, Any], *, resumed: bool
) -> RunReport:
    interrupts = values.get("__interrupt__") if isinstance(values, dict) else None
    if interrupts:
        payload = _interrupt_payload(interrupts[0])
        row_id = str(payload.get("approval_id") or "")
        emit(ctx, LoopEvents.STATUS, "loop.parked", {"approval_id": row_id})
        return RunReport(
            ctx.instance_id,
            template.name,
            LoopStatus.PARKED,
            detail="awaiting human approval",
            approval_id=row_id or None,
            resumed=resumed,
            stage=str(payload.get("node") or ""),
        )

    status = _status_of(values)
    detail = str(values.get("error") or "")
    if status is LoopStatus.FAILED and not detail:
        raw = str(values.get("status") or "")
        if raw != LoopStatus.FAILED.value:
            detail = f"tick produced no usable terminal status ({raw!r}) — failing closed"
    emit(ctx, LoopEvents.STATUS, f"loop.{status.value}", {"detail": detail})
    return RunReport(
        ctx.instance_id,
        template.name,
        status,
        detail=detail,
        effects=list(values.get("effects") or []),
        resumed=resumed,
    )


def open_store(db_path: str | None = None) -> SqliteStore:
    """Control-plane store handle (the SAME database the API/runner use)."""
    return SqliteStore(db_path or default_db_path())


__all__ = [
    "DeferredPage",
    "LoopContext",
    "LoopTemplate",
    "compile_template",
    "observed",
    "open_checkpoint",
    "open_store",
    "run_once",
    "thread_id",
]
