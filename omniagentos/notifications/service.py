"""The single write seam every notification source routes through.

Contract: **persist first, then push.** :func:`record_notification` writes the
durable ``notifications`` row (the inspectable target) and only then fires the
best-effort OS/push notification. The legacy API swallows persistence failures
so a notification never breaks the flow that raised it; reliability callers use
the truthful result API to distinguish failure from an intentional dedupe. A
delivery failure likewise never affects the persisted record.

Every source (runner approval escalation, session hook-eval approval, Steward
HIGH/critical alert, supervisor session-awaiting-approval park) calls one of the
thin ``notify_*`` helpers here, so the linkage between a notification and its
real approval/run/session/alert is established in exactly one place.

**Record everything, deliver selectively.** Persistence is unconditional; the
push half is gated by :func:`should_push` (see ``PUSH_KINDS``). The feed stays
the complete record — the device only buzzes for what needs a human.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock, local
from typing import Any, Literal

from omniagentos.contracts import default_db_path, new_id
from omniagentos.notifications.dal import (
    NotificationPersistenceGuardRejected,
    NotificationsDal,
)
from omniagentos.notifications.policy import PUSH_KINDS, PUSH_SEVERITIES, should_push

logger = logging.getLogger(__name__)

NOTIFICATION_KINDS = ("approval", "alert", "escalation", "done", "blocked", "info", "swarm_failed")

# --- delivery allowlist -------------------------------------------------------
#
# The DURABLE feed records EVERYTHING, unchanged: every kind, every severity,
# every row, exactly as before. The allowlist gates only the best-effort PUSH
# (macOS banner / ntfy), i.e. what is allowed to buzz a device.
#
# The predicate and its constants now live in
# :mod:`omniagentos.notifications.policy` (with the full rationale), because the
# TRANSPORT enforces the same rule for the direct callers that never reach this
# seam -- and the transport must not import this module. They are re-exported
# here unchanged, so ``service.should_push`` / ``service.PUSH_KINDS`` remain the
# public spelling for every existing caller and test.

# The kinds the topbar bell counts as "needs a decision" (GET
# /api/notifications/count?actionable=1). Deliberately the SAME set as
# PUSH_KINDS: what is worth a buzz is exactly what is worth a badge, so the
# badge and the phone can never disagree about what is outstanding.
ACTIONABLE_KINDS = PUSH_KINDS


# A referenced approval in any of these states is no longer actionable: the panel
# renders the outcome instead of an Approve button (so a resolved approval never
# looks like a live, empty queue).
_RESOLVED_APPROVAL_STATES = frozenset({"approved", "rejected", "expired"})

# ``approval_reminder`` is the T-24h re-push (see
# :mod:`omniagentos.notifications.approval_reminders`). It is a DISTINCT
# ref_type carrying the SAME approval id, so the ref-keyed dedupe cannot swallow
# the reminder behind the original approval row -- and it resolves through the
# identical approval branch below, so the reminder is just as actionable and
# just as deep-linkable as the notification it is reminding about.
_APPROVAL_REF_TYPES = frozenset({"approval", "approval_reminder"})

# A board card in one of these terminal states is "resolved" for a done-kind
# notification: the deep link still opens its files, but the row reads as done.
_RESOLVED_BOARD_STATES = frozenset({"done", "cancelled"})

# Path-keyed DALs own their own connection and live for the process, so a strong
# module-global reference is correct for them.
_DAL_CACHE: dict[str, tuple[sqlite3.Connection | None, NotificationsDal]] = {}

# Connection-keyed DALs are cached THREAD-LOCALLY, not in a module-global dict.
#
# Phase 2 gave every thread its own sqlite3.Connection. A module-global
# {id(connection): (connection, dal)} cache holds a STRONG reference, so it
# pinned one connection per thread forever and silently defeated the
# _ConnectionHolder/WeakSet reclaim that same change introduced. Before Phase 2
# that was harmless -- one connection for the process lifetime meant exactly one
# entry -- but it now grows with every distinct thread that records a
# notification, which at Phase 3's target concurrency is precisely backwards.
#
# weakref.WeakKeyDictionary is NOT an option: sqlite3.Connection does not support
# weak references. Thread-local storage gives the same lifetime for free -- the
# slot dies with the thread, so nothing outlives its owner -- and it mirrors how
# SqliteStore already holds the connections themselves.
#
# The identity re-check is still required WITHIN a thread: a thread can outlive a
# store rebuild and be handed a different connection, and writing through a DAL
# wrapping a closed handle would fail silently.
_CONN_DAL_LOCAL = local()

_DAL_LOCK = RLock()


@dataclass(frozen=True)
class NotificationRecordResult:
    """Truthful persistence result for callers that cannot accept silent loss.

    The legacy :func:`record_notification` API intentionally maps both
    ``deduped`` and ``failed`` to ``None``. Reliability orchestration needs to
    distinguish those cases so a lost alert becomes durable degraded state
    without changing the best-effort contract for existing notification sources.
    """

    status: Literal["persisted", "deduped", "failed"]
    notification_id: str | None = None
    error: str | None = None


class NotificationPersistenceError(RuntimeError):
    """A notification could not be durably persisted."""


class NotificationPersistenceGuardUnavailable(RuntimeError):
    """The selected notification target cannot enforce guarded persistence."""


def _dal_for(
    *,
    dal: NotificationsDal | None = None,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> NotificationsDal:
    """Resolve the DAL to write through, sharing/caching connections.

    Priority: an explicit ``dal`` > a shared ``connection`` (keyed by identity so
    the same connection reuses one DAL) > a ``db_path`` (or the process default),
    cached so repeated notifications from one source do not re-open/re-migrate.
    """
    if dal is not None:
        return dal
    if connection is not None:
        cached = getattr(_CONN_DAL_LOCAL, "entry", None)
        if cached is not None and cached[0] is connection:
            return cached[1]
        built = NotificationsDal(connection)
        _CONN_DAL_LOCAL.entry = (connection, built)
        return built
    path = db_path or default_db_path()
    key = f"path:{path}"
    with _DAL_LOCK:
        entry = _DAL_CACHE.get(key)
        if entry is None:
            entry = (None, NotificationsDal(path))
            _DAL_CACHE[key] = entry
        return entry[1]


def _push(
    title: str,
    body: str,
    *,
    kind: str | None = None,
    severity: str | None = None,
    subtitle: str | None = None,
    group: str | None = None,
) -> None:
    """Best-effort OS/ntfy delivery; a failure never touches persistence.

    The single delivery chokepoint: every push in this module goes through here,
    so the :func:`should_push` allowlist is applied in exactly one place (a new
    emit site cannot forget it). ``kind``/``severity`` default to ``None``,
    which the allowlist reads as "not deliverable" -- fail CLOSED, so an
    unlabelled call is a silent row rather than an unfiltered buzz.

    The labels are ALSO forwarded to the transport, which re-applies the same
    allowlist to its phone (ntfy) leg for the direct callers that bypass this
    seam. Forwarding keeps a seam-originated push labelled, so it is never
    mistaken for one of those anonymous calls and silently dropped.
    """
    if not should_push(kind, severity):
        return
    try:
        from omniagentos.sessions import notify

        notify.push(
            title,
            body,
            subtitle=subtitle,
            group=group,
            kind=kind,
            severity=severity,
        )
    except Exception:  # noqa: BLE001 - delivery must never break the caller
        try:
            logger.debug("notification push failed", exc_info=True)
        except Exception:  # noqa: BLE001 - logging is best-effort too
            pass


def push_group(ref_type: str | None, ref_id: str | None) -> str | None:
    """Coalescing key for a banner: one live banner per referenced entity.

    A source that re-fires for the same approval/session/event REPLACES its
    banner instead of stacking another copy in Notification Center.

    Public because coalescing only works if every writer about the same entity
    mints the SAME key: a source that pushes directly (rather than through
    :func:`record_notification`) must call this, not spell its own.
    """
    if not ref_type or not ref_id:
        return None
    return f"omniagentos.{ref_type}.{ref_id}"


def push_subtitle(severity: str) -> str:
    """The banner's second line: severity first, so "critical" reads before the
    detail rather than being buried in a truncated message."""
    return severity.upper() if severity in ("critical", "high") else severity


def record_notification_result(
    *,
    kind: str,
    title: str,
    body: str = "",
    severity: str = "info",
    ref_type: str | None = None,
    ref_id: str | None = None,
    payload: dict[str, Any] | None = None,
    dal: NotificationsDal | None = None,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    push: bool = True,
    dedupe: bool = True,
    persistence_guard: Callable[[sqlite3.Connection], None] | None = None,
    on_persisted: Callable[[], None] | None = None,
) -> NotificationRecordResult:
    """Persist a notification and report persisted, deduped, or failed.

    This is the truthful API for reliability callers. It preserves best-effort
    delivery by returning ``failed`` instead of raising, while making failure
    distinguishable from an intentional dedupe no-op.

    ``dedupe`` skips the write when an UNREAD notification already targets the
    same ``(ref_type, ref_id)``, so a source that re-fires for one approval or
    session cannot flood the feed.

    ``persistence_guard`` is the reliability-only fenced contract. The real DAL
    evaluates it after ``BEGIN IMMEDIATE`` and before dedupe/insert, keeping the
    same transaction locked through commit. Guard rejection is re-raised (for
    L03 this is ``LeaseConflict``), never converted into best-effort ``failed``.
    ``on_persisted`` is a best-effort post-commit observer. It runs only after a
    guarded or legacy insert has committed, and its failure is reported
    separately without changing the authoritative ``persisted`` outcome.
    """
    severity_label = push_subtitle(severity)
    # Per-event emission cap (lifecycle hygiene). Shadow never suppresses;
    # enforce suppresses and increments an observable counter.
    try:
        from omniagentos.notifications.cap_retention import check_emission_cap

        event_key = f"{kind}:{ref_type or ''}:{ref_id or title}"
        cap = check_emission_cap(event_key)
        if cap.suppressed and not cap.allowed:
            logger.info(
                "notification suppressed by per-event cap (key=%s total=%s)",
                event_key,
                cap.suppression_total,
            )
            return NotificationRecordResult(
                status="deduped",
                error=f"capped:{cap.suppression_total}",
            )
    except Exception:  # noqa: BLE001 - cap must never break notification sources
        logger.debug("emission cap check failed open", exc_info=True)

    try:
        target = _dal_for(dal=dal, connection=connection, db_path=db_path)
    except Exception as exc:  # noqa: BLE001 - report, do not break the source
        logger.debug("could not open notifications DAL", exc_info=True)
        if push:
            _push(
                title,
                body,
                kind=kind,
                severity=severity,
                subtitle=severity_label,
                group=push_group(ref_type, ref_id),
            )
        return NotificationRecordResult(status="failed", error=str(exc))

    try:
        notification_id = new_id("ntf")
        persisted_result = NotificationRecordResult(
            status="persisted",
            notification_id=notification_id,
        )
        deduped_result = NotificationRecordResult(status="deduped")
        row = {
            "id": notification_id,
            "kind": kind,
            "title": title,
            "body": body,
            "severity": severity,
            "ref_type": ref_type,
            "ref_id": ref_id,
            "payload": payload or {},
        }
        if persistence_guard is not None:
            guarded_create = getattr(target, "create_guarded", None)
            if not callable(guarded_create):
                raise NotificationPersistenceGuardUnavailable(
                    "notification target does not support atomic guarded persistence"
                )
            status, _stored = guarded_create(
                row,
                guard=persistence_guard,
                dedupe_ref=((ref_type, ref_id) if dedupe and ref_type and ref_id else None),
            )
            if status == "deduped":
                return deduped_result
        else:
            if dedupe and ref_type and ref_id and target.has_unresolved_for_ref(ref_type, ref_id):
                return deduped_result
            target.create(row)
        # The immutable persisted result and its ID were constructed before the
        # DAL transaction. No post-COMMIT row access or result construction can
        # contradict the authoritative persistence outcome.
        result = persisted_result
    except NotificationPersistenceGuardRejected as exc:
        raise exc.cause from exc
    except NotificationPersistenceGuardUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - report, do not break the source
        logger.debug("failed to persist notification (kind=%s)", kind, exc_info=True)
        result = NotificationRecordResult(status="failed", error=str(exc))

    if result.status == "persisted" and on_persisted is not None:
        try:
            on_persisted()
        except Exception:  # noqa: BLE001 - a committed row stays persisted
            try:
                logger.exception("post-commit notification observer failed (kind=%s)", kind)
            except Exception:  # noqa: BLE001 - logging cannot rewrite commit truth
                pass
    # Production call path for retention: opportunistic, throttled, fail-open.
    # A mechanism nothing invokes does not exist — wire the sweep on the same
    # write seam that already hosts the emission cap.
    if result.status == "persisted":
        try:
            from omniagentos.notifications.cap_retention import maybe_run_retention_sweep

            sweep_result = maybe_run_retention_sweep(target)
            if sweep_result is not None and sweep_result.status == "failed":
                logger.warning(
                    "notification retention sweep failed: %s",
                    sweep_result.error or "unknown error",
                )
        except Exception:  # noqa: BLE001 - retention hygiene must never break emission
            logger.debug("notification retention sweep failed open", exc_info=True)
    if push:
        _push(
            title,
            body,
            kind=kind,
            severity=severity,
            subtitle=severity_label,
            group=push_group(ref_type, ref_id),
        )
    return result


def record_notification(
    *,
    kind: str,
    title: str,
    body: str = "",
    severity: str = "info",
    ref_type: str | None = None,
    ref_id: str | None = None,
    payload: dict[str, Any] | None = None,
    dal: NotificationsDal | None = None,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    push: bool = True,
    dedupe: bool = True,
) -> str | None:
    """Persist a notification, preserving the legacy best-effort return contract.

    Existing sources receive an id only for a newly persisted row and ``None``
    for either dedupe or failure. Callers that must distinguish those outcomes
    use :func:`record_notification_result`.
    """
    result = record_notification_result(
        kind=kind,
        title=title,
        body=body,
        severity=severity,
        ref_type=ref_type,
        ref_id=ref_id,
        payload=payload,
        dal=dal,
        connection=connection,
        db_path=db_path,
        push=push,
        dedupe=dedupe,
    )
    return result.notification_id if result.status == "persisted" else None


def notify_approval_requested(
    *,
    approval_id: str,
    proposed_action: str,
    action_class: str,
    source: str,
    severity: str = "warning",
    risk: str = "",
    session_id: str | None = None,
    run_id: str | None = None,
    dal: NotificationsDal | None = None,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    push: bool = True,
) -> str | None:
    """Persist an approval-kind notification linked to a real approval row.

    The persisted target is the approval itself (``ref_type='approval'``,
    ``ref_id=approval_id``), and ``payload.approval_id`` lets the client jump
    straight to (and approve) it via the existing decide route.
    """
    body = f"{action_class}: {proposed_action}".strip()
    return record_notification(
        kind="approval",
        title="Approval required",
        body=body,
        severity=severity,
        ref_type="approval",
        ref_id=approval_id,
        payload={
            "approval_id": approval_id,
            "action_class": action_class,
            "proposed_action": proposed_action,
            "risk": risk,
            "source": source,
            "session_id": session_id,
            "run_id": run_id,
        },
        dal=dal,
        connection=connection,
        db_path=db_path,
        push=push,
    )


def notify_alert(
    *,
    alert_id: str,
    title: str,
    body: str,
    severity: str,
    rule: str = "",
    dal: NotificationsDal | None = None,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    push: bool = True,
) -> str | None:
    """Persist an alert-kind notification linked to a Steward alert row."""
    return record_notification(
        kind="alert",
        title=title,
        body=body,
        severity=severity,
        ref_type="alert",
        ref_id=str(alert_id),
        payload={"alert_id": str(alert_id), "rule": rule, "severity": severity},
        dal=dal,
        connection=connection,
        db_path=db_path,
        push=push,
    )


def notify_session_awaiting(
    *,
    session_id: str,
    dal: NotificationsDal | None = None,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    push: bool = True,
) -> str | None:
    """Persist an escalation notification for a session parked awaiting approval.

    Linked to the session (``ref_type='session'``) because this park may have no
    approval row of its own (e.g. an unreachable-hook fail-closed park); the
    session detail is the inspectable target the operator opens.
    """
    return record_notification(
        kind="escalation",
        title="Session awaiting approval",
        body=f"Session {session_id} is waiting for approval.",
        severity="warning",
        ref_type="session",
        ref_id=session_id,
        payload={"session_id": session_id},
        dal=dal,
        connection=connection,
        db_path=db_path,
        push=push,
    )


def notify_task_done(
    *,
    board_task_id: str,
    task_title: str = "",
    files_count: int = 0,
    workspace: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    dal: NotificationsDal | None = None,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    push: bool = True,
) -> str | None:
    """Persist a done-kind notification linked to a finished board card.

    The persisted target is the card itself (``ref_type='board_task'``,
    ``ref_id=board_task_id``); :func:`resolve_target` turns it into the deep
    link ``/board?task={id}&files=1`` so the operator lands directly on the
    card's files drawer.

    DEDUPE (kind-aware, read-agnostic): a completion is observed by BOTH intake
    emit points -- the orchestration lifecycle and ``reconcile_board`` -- so this
    no-ops when a ``done`` notification already targets this card, even one the
    operator has already read (:meth:`NotificationsDal.has_for_ref_kind`),
    guaranteeing exactly one "task complete" bell per card.
    """
    try:
        target = _dal_for(dal=dal, connection=connection, db_path=db_path)
    except Exception:  # noqa: BLE001 - no DAL means no persistence; degrade gracefully
        logger.debug("could not open notifications DAL for done notification", exc_info=True)
        if push:
            _push(
                "Task complete",
                task_title or "A task finished.",
                kind="done",
                severity="info",
                group=push_group("board_task", board_task_id),
            )
        return None

    try:
        if target.has_for_ref_kind("board_task", board_task_id, "done"):
            return None
    except Exception:  # noqa: BLE001 - a dedupe-check failure must not drop the notification
        logger.debug("done-notification dedupe check failed; recording anyway", exc_info=True)

    return record_notification(
        kind="done",
        title="Task complete",
        body=task_title or "A task finished.",
        severity="info",
        ref_type="board_task",
        ref_id=board_task_id,
        payload={
            "files_count": files_count,
            "workspace": workspace,
            "run_id": run_id,
            "session_id": session_id,
            "task_title": task_title,
        },
        dal=target,
        push=push,
        # Our kind-aware dedupe above supersedes record_notification's ref-only,
        # unread-only guard (which would wrongly match a live approval on the
        # same card, or miss an already-read done row).
        dedupe=False,
    )


def notify_run_terminal_failure(
    *,
    run_id: str,
    status: str,
    goal: str = "",
    board_task_id: str | None = None,
    workspace: str | None = None,
    dal: NotificationsDal | None = None,
    connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    push: bool = True,
) -> str | None:
    """Persist a ``swarm_failed``-kind notification for a swarm run that went
    terminal WITHOUT completing (``status`` in ``failed`` / ``cancelled``).

    The counterpart to :func:`notify_task_done` for the unhappy terminals: the
    scheduler is the sole owner of swarm-card status (``reconcile_board`` skips
    swarm cards), so without this emit a failed or cancelled run is fully
    silent to the operator.

    The persisted target is the board card when the run has one
    (``ref_type='board_task'`` -- :func:`resolve_target` deep-links to the
    card), otherwise the run itself (``ref_type='run'``, ``ref_id=run_id``) so
    a card-less run still surfaces and still dedupes.

    DEDUPE (kind-aware, read-agnostic, mirroring :func:`notify_task_done`): a
    coordinator crash-resume, a re-fired terminal path, or the supervisor's
    stale-heartbeat sweep can all observe the same terminal state, so this
    no-ops when a ``swarm_failed`` notification already targets this entity --
    even one the operator has already read -- guaranteeing exactly one failure
    bell per card/run (coordinator-finally + sweep double-emission is
    structurally impossible).

    KIND: ``swarm_failed`` is deliberately distinct from ``blocked`` --
    longhaul's transient capacity-wait notification uses ``blocked`` on the
    same ``board_task`` refs, and sharing the kind would let the read-agnostic
    dedupe here permanently suppress one bell whenever the other had fired.
    """
    normalized = str(status or "").strip().lower()
    title = "Swarm run cancelled" if normalized == "cancelled" else "Swarm run failed"
    body = goal.strip() or f"Swarm run {run_id} ended with status '{normalized or 'failed'}'."
    ref_type = "board_task" if board_task_id else "run"
    ref_id = board_task_id or run_id

    try:
        target = _dal_for(dal=dal, connection=connection, db_path=db_path)
    except Exception:  # noqa: BLE001 - no DAL means no persistence; degrade gracefully
        logger.debug("could not open notifications DAL for run-failure notification", exc_info=True)
        if push:
            _push(
                title,
                body,
                kind="swarm_failed",
                severity="warning",
                group=push_group(ref_type, ref_id),
            )
        return None

    try:
        if target.has_for_ref_kind(ref_type, ref_id, "swarm_failed"):
            return None
    except Exception:  # noqa: BLE001 - a dedupe-check failure must not drop the notification
        logger.debug("run-failure dedupe check failed; recording anyway", exc_info=True)

    return record_notification(
        kind="swarm_failed",
        title=title,
        body=body,
        severity="warning",
        ref_type=ref_type,
        ref_id=ref_id,
        payload={
            "run_id": run_id,
            "status": normalized,
            "goal": goal,
            "workspace": workspace,
            "board_task_id": board_task_id,
        },
        dal=target,
        push=push,
        # The kind-aware dedupe above supersedes record_notification's ref-only,
        # unread-only guard (which would wrongly match a live approval, done
        # bell, or longhaul capacity-wait on the same card, or miss an
        # already-read swarm_failed row).
        dedupe=False,
    )


def _notification_payload(notification: dict[str, Any]) -> dict[str, Any] | None:
    """The notification's payload as a dict, or None when the source is absent/unreadable.

    Raw DB rows carry ``payload_json``; some callers pre-parse into ``payload``.
    When **both** are present, the raw column is authoritative: a pre-parsed
    ``{}`` may be a collapse-on-error counterfeit that must not hide an
    unreadable source.

    **Three-valued return (do not collapse):**

    - ``dict`` (including genuine ``{}``) — payload was readable as a JSON object;
    - ``None`` — source is **absent**, **blank**, or **unparseable** as a JSON
      object (null raw, whitespace-only raw, missing keys, malformed JSON,
      non-object JSON, non-dict pre-parsed value). Callers MUST NOT treat
      ``None`` as measured empty fields; that is the unreadable≡empty defect
      class.

    Genuine empty is an **explicit** empty object: ``payload_json="{}"`` or a
    pre-parsed ``payload={}``. The DAL stores that form via
    ``setdefault("payload_json", "{}")``. Null, blank, or missing sources are
    not the same as that measured empty object.
    """
    # Prefer raw when the key is present — even if a pre-parsed dict coexists.
    if "payload_json" in notification:
        raw = notification.get("payload_json")
        if raw is None:
            # Explicit null raw: source absent. A coexisting pre-parsed dict may
            # still be a recoverable measurement; otherwise this is not {}.
            payload = notification.get("payload")
            if isinstance(payload, dict):
                return payload
            return None
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return None
        if not raw.strip():
            # Blank / whitespace-only raw is absent, not genuine empty {}.
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            # Unreadable source — not the same as genuine empty {}.
            return None
        if isinstance(parsed, dict):
            return parsed
        # Parsed but not an object (list/string/number/bool/null).
        return None

    # No raw column: use pre-parsed payload only.
    if "payload" in notification:
        payload = notification.get("payload")
        if isinstance(payload, dict):
            return payload
        # Explicit null or non-dict pre-parsed is absent/unreadable, not {}.
        return None
    # Completely missing source — not a measured empty object.
    return None


def build_approval_lookup(open_reader: Callable[[], Any] | None) -> Any:
    """Build ``id -> approval row | None`` without collapsing open failure to missing.

    Production factories that catch open errors and return ``lambda _id: None``
    convert "sessions DB unavailable" into "every approval is gone/resolved".
    That is the non-result-as-favourable-result defect. This builder:

    - returns ``None`` when no opener is provided (caller not wired);
    - returns a callable that **raises** when the reader cannot be opened, so
      :func:`resolve_target` marks ``state='unavailable'`` / ``resolved=False``;
    - returns the reader's point lookup when open succeeds.

    Genuine missing rows still return ``None`` from a live lookup; callers must
    not treat that absence as a favourable ``resolved`` outcome — see
    :func:`resolve_target`.
    """
    if open_reader is None:
        return None

    try:
        reader = open_reader()
    except Exception as exc:  # noqa: BLE001 - surface as lookup failure, not missing

        def _unavailable(_id: str, *, _cause: BaseException = exc) -> None:
            raise RuntimeError("sessions database unavailable") from _cause

        return _unavailable

    if reader is None:

        def _unavailable_none(_id: str) -> None:
            raise RuntimeError("sessions database unavailable")

        return _unavailable_none

    getter = getattr(reader, "get_approval_by_id", None)
    if not callable(getter):

        def _unavailable_getter(_id: str) -> None:
            raise RuntimeError("sessions database unavailable")

        return _unavailable_getter
    return getter


def build_board_lookup(open_store: Callable[[], Any] | None) -> Any:
    """Build ``id -> board card row | None`` without collapsing open failure to missing.

    Mirrors :func:`build_approval_lookup`. Production factories that catch store
    open errors and return ``lambda _id: None`` convert "collab store down"
    into "every board card is measured missing". That is the same non-result-
    as-favourable-result defect. This builder:

    - returns ``None`` when no opener is provided (caller not wired);
    - returns a callable that **raises** when the store cannot be opened, so
      :func:`resolve_target` marks ``state='unavailable'`` (not ``missing``);
    - returns the store's point lookup when open succeeds.

    Genuine missing cards still return ``None`` from a live lookup.
    """
    if open_store is None:
        return None

    try:
        store = open_store()
    except Exception as exc:  # noqa: BLE001 - surface as lookup failure, not missing

        def _unavailable(_id: str, *, _cause: BaseException = exc) -> None:
            raise RuntimeError("collab store unavailable") from _cause

        return _unavailable

    if store is None:

        def _unavailable_none(_id: str) -> None:
            raise RuntimeError("collab store unavailable")

        return _unavailable_none

    getter = getattr(store, "get_board_task", None)
    if not callable(getter):

        def _unavailable_getter(_id: str) -> None:
            raise RuntimeError("collab store unavailable")

        return _unavailable_getter
    return getter


_LOOKUP_UNSET: Any = object()


def serialize_notification(
    row: dict[str, Any],
    *,
    open_approval_reader: Callable[[], Any] | None = None,
    open_board_store: Callable[[], Any] | None = None,
    approval_lookup: Any = _LOOKUP_UNSET,
    board_lookup: Any = _LOOKUP_UNSET,
) -> dict[str, Any]:
    """Enrich one notification DB row for a client response.

    This is the **production serialization path** for list/read responses:

    - When lookups are not supplied, builds them via :func:`build_approval_lookup`
      / :func:`build_board_lookup` so open failures raise into
      :func:`resolve_target` as ``state='unavailable'`` rather than
      masquerading as live missing (``lambda _id: None`` counterfeit).
    - Resolves payload three-valuedly **without** first popping
      ``payload_json`` and collapsing parse failures to ``{}``. Unreadable,
      absent, and blank sources stay distinct from genuine empty
      ``payload_json="{}"``.

    Callers that serialize notification rows for clients should use this (or
    the same sequence) rather than a local fail-open factory + collapse-on-
    error payload parse. Optional ``approval_lookup`` / ``board_lookup`` let a
    caller inject already-built lookups without re-opening.
    """
    value = dict(row)
    # Three-valued payload for the client body. Keep raw payload_json so
    # resolve_target / _notification_payload still see the authoritative source.
    parsed = _notification_payload(value)
    # Explicit None: do not write {} — that would invent a measured empty object.
    value["payload"] = parsed
    value["read"] = value.get("read_at") is not None
    value["acted"] = value.get("acted_at") is not None

    # Wire the builders whenever lookups were not supplied — a mechanism
    # nothing invokes does not exist.
    if approval_lookup is _LOOKUP_UNSET:
        approval_lookup = build_approval_lookup(open_approval_reader)
    if board_lookup is _LOOKUP_UNSET:
        board_lookup = build_board_lookup(open_board_store)
    value["target"] = resolve_target(value, approval_lookup, board_lookup)
    # Client body carries parsed payload + target; drop raw column after use.
    value.pop("payload_json", None)
    return value


def resolve_target(
    notification: dict[str, Any],
    approval_lookup: Any,
    board_lookup: Any = None,
) -> dict[str, Any]:
    """Compute the client-facing target for one notification row.

    ``approval_lookup`` is a callable ``id -> approval row | None`` (the sessions
    reader's ``get_approval_by_id``), or ``None`` when no lookup is wired.
    Prefer :func:`build_approval_lookup` so open failures raise rather than
    masquerading as missing rows. For an approval-linked notification
    (``ref_type`` ``approval`` OR the T-24h ``approval_reminder``, which carries
    the same approval id) it resolves the CURRENT approval state so the panel
    can:

    - show an inline Approve/Reject action while it is still ``pending``;
    - show the OUTCOME (approved/rejected/expired) once resolved;
    - show ``state='missing'`` with ``resolved=False`` when a live lookup
      returns ``None`` — absence is not a favourable "already resolved" result
      (governing filter: absent must never render as good);
    - show ``state=None`` with ``resolved=False`` when no lookup is wired -- a
      non-result must not invent a favourable resolved outcome; and
    - show ``state='unavailable'`` with ``resolved=False`` when the lookup
      raises -- a measurement failure is not a favourable "already resolved"
      outcome (sessions DB down must not clear every Approve button).

    ``board_lookup`` (DEFAULTED to ``None`` so existing 2-arg callers keep
    working) is the equivalent ``id -> board card row | None`` for a done-kind
    ``board_task`` notification: it resolves the card's current status into the
    target ``state`` and deep-links to ``/board?task={id}&files=1`` (the files
    drawer). A genuine missing card reports ``state='missing'`` with href
    ``/board``; a lookup exception reports ``state='unavailable'`` (also
    ``/board``) — never collapse unreadable into measured missing. Payload
    ``files_count`` / ``task_title`` still come from the notification itself
    when the payload is readable; an unreadable payload sets
    ``payload_state='unavailable'`` and must not look like a genuine empty
    ``{}`` snapshot.
    """
    ref_type = notification.get("ref_type")
    ref_id = notification.get("ref_id")
    target: dict[str, Any] = {
        "type": ref_type,
        "id": ref_id,
        "resolved": False,
        "actionable": False,
        "state": None,
    }
    if not ref_type or not ref_id:
        return target

    if ref_type in _APPROVAL_REF_TYPES:
        target["approval_id"] = ref_id
        target["href"] = "/approvals"
        if approval_lookup is None:
            # No lookup wired: do not invent a resolved outcome from a non-result.
            # Production callers must pass None (not lambda: None) when the
            # sessions reader cannot be opened — see :func:`build_approval_lookup`.
            return target
        try:
            approval = approval_lookup(ref_id)
        except Exception:  # noqa: BLE001 - a lookup failure must not break the list
            # Could not measure. Reporting resolved=True here is the classic
            # non-result-as-favourable-result defect (every approval looks done
            # when the sessions DB is down). Leave unresolved and mark unknown.
            target["state"] = "unavailable"
            target["resolved"] = False
            target["actionable"] = False
            return target
        if approval is None:
            # Live lookup ran and found nothing. Absence is not a favourable
            # terminal outcome — do not render as resolved/good. Keep href so
            # the row is not a dead link; do not invent an Approve action either.
            target["state"] = "missing"
            target["resolved"] = False
            target["actionable"] = False
        else:
            raw_state = approval.get("state")
            if raw_state is None or (isinstance(raw_state, str) and raw_state.strip() == ""):
                # Absent/empty state is unmeasured — defaulting to "pending"
                # would invent an actionable Approve button from a non-result.
                target["state"] = "unavailable"
                target["resolved"] = False
                target["actionable"] = False
            else:
                state = str(raw_state)
                target["state"] = state
                target["resolved"] = state in _RESOLVED_APPROVAL_STATES
                target["actionable"] = state == "pending"
                target["action_class"] = approval.get("action_class")
                target["proposed_action"] = approval.get("proposed_action")
                target["session_id"] = approval.get("session_id")
                target["run_id"] = approval.get("run_id")

    elif ref_type == "board_task":
        payload = _notification_payload(notification)
        target["board_task_id"] = ref_id
        # Explicit None handling: unreadable payload is not measured empty.
        # Bare truthiness would make None take the false/empty branch.
        if payload is None:
            target["task_title"] = None
            target["files_count"] = None
            target["payload_state"] = "unavailable"
        else:
            target["task_title"] = payload.get("task_title")
            target["files_count"] = payload.get("files_count")
        deep_link = f"/board?task={ref_id}&files=1"
        board = None
        lookup_attempted = board_lookup is not None
        lookup_failed = False
        if lookup_attempted:
            try:
                board = board_lookup(ref_id)
            except Exception:  # noqa: BLE001 - a lookup failure must not break the list
                # Measurement failure is not the same as a genuine missing card.
                lookup_failed = True
                board = None
        if board is not None:
            state = str(board.get("status") or "")
            target["state"] = state
            target["resolved"] = state in _RESOLVED_BOARD_STATES
            if board.get("title"):
                target["task_title"] = board.get("title")
            target["href"] = deep_link
        elif lookup_failed:
            # Board source unreadable: degrade href, mark unavailable, do not
            # collapse into the same shape as a measured missing card.
            target["state"] = "unavailable"
            target["resolved"] = False
            target["href"] = "/board"
        elif lookup_attempted:
            # Live lookup ran and found nothing (archived/pruned). Degrade the
            # href but keep payload-derived title/count; state is measured missing.
            target["state"] = "missing"
            target["resolved"] = False
            target["href"] = "/board"
        else:
            # No lookup wired (legacy 2-arg call): the id is known and valid, so
            # deep-link on it rather than degrading.
            target["href"] = deep_link
    elif ref_type == "run":
        target["href"] = f"/runs/{ref_id}"
    elif ref_type == "session":
        target["href"] = f"/sessions/{ref_id}"
    elif ref_type == "alert":
        target["href"] = "/alerts"
    return target


__all__ = [
    "ACTIONABLE_KINDS",
    "NOTIFICATION_KINDS",
    "PUSH_KINDS",
    "PUSH_SEVERITIES",
    "NotificationPersistenceError",
    "NotificationPersistenceGuardUnavailable",
    "NotificationRecordResult",
    "build_approval_lookup",
    "build_board_lookup",
    "notify_alert",
    "notify_approval_requested",
    "notify_run_terminal_failure",
    "notify_session_awaiting",
    "notify_task_done",
    "push_group",
    "push_subtitle",
    "record_notification",
    "record_notification_result",
    "resolve_target",
    "serialize_notification",
    "should_push",
]
