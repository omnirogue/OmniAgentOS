"""Closed P2 executor registry for consequential EDC actions.

Only :func:`execute_send` imports the capability broker on the email-send path.
The SHA approval binds the exact MIME preview while a one-use durable campaign
grant is the broker authority (review F02). External I/O is bracketed by the
F03 recovery state machine in :func:`run_executor`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol

from omniagentos.contracts import utc_now_iso
from omniagentos.edc.store import DecisionConflictError, DecisionStore, machine_spec
from omniagentos.grants import GrantsStore


class _RetryableEffectError(RuntimeError):
    """The provider definitely refused or was unavailable before accepting."""


class Executor(Protocol):
    consequential: bool

    def preview(self, decision: dict[str, Any]) -> dict[str, Any]: ...

    def execute(
        self, decision: dict[str, Any], *, store: DecisionStore, actor: str
    ) -> dict[str, Any]: ...

    def verify(self, result: dict[str, Any]) -> dict[str, Any]: ...


def safe_error(exc: BaseException) -> str:
    """A non-secret error label safe for owner/DB/Slack/HTTP egress (review F2).

    Never the raw ``str(exc)`` — a provider/library message can carry a secret
    (a token echoed back, a signed URL, a mailbox address). Emits the exception
    TYPE plus, when present, the broker's non-secret ``reason`` code
    (``BrokerDenied.reason`` is a fixed vocabulary, safe to surface).
    """
    label = type(exc).__name__
    reason = str(getattr(exc, "reason", "") or "").strip()
    return f"{label}:{reason}" if reason else label


def draft_sha256(draft: dict[str, Any]) -> str:
    """Stable digest of the owner-visible recipient, subject, and body."""
    approved = {key: str(draft.get(key) or "") for key in ("to", "subject", "body")}
    return hashlib.sha256(
        json.dumps(approved, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _mime(draft: dict[str, Any], *, effect_key: str) -> tuple[str, str]:
    message = EmailMessage()
    message["To"] = str(draft.get("to") or "")
    message["Subject"] = str(draft.get("subject") or "")
    # NOTE: Gmail does NOT dedupe on Message-ID — resending a message with the
    # same Message-ID delivers it again. The real single-fire guarantees are the
    # one-use broker grant (max_actions=1) and the open→in_progress CAS; the
    # stable Message-ID is only a best-effort provider-visible correlation hint.
    message["Message-ID"] = f"<{effect_key}@edc.omniagentos.local>"
    message.set_content(str(draft.get("body") or ""))
    mime = message.as_string()
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
    return mime, raw


def preview_send(decision: dict[str, Any]) -> dict[str, Any]:
    """Render show-before-act from the held draft; never calls broker dry_run."""
    draft = decision.get("draft") or {}
    sha = draft_sha256(draft)
    effect_key = f"edc-{decision['id']}-{sha}"
    mime, _raw = _mime(draft, effect_key=effect_key)
    return {
        "kind": "send_email",
        "draft_sha256": sha,
        "effect_key": effect_key,
        "mime": mime,
    }


def _provider_id(result: dict[str, Any]) -> str:
    body = result.get("body")
    if not isinstance(body, dict):
        return ""
    return str(body.get("id") or body.get("messageId") or "")


def execute_send(decision: dict[str, Any], *, store: DecisionStore, actor: str) -> dict[str, Any]:
    """Send one SHA-approved draft through a one-use broker campaign grant."""
    # GREP INVARIANT: this is the only broker import on the EDC send path.
    from omniagentos.connectors import broker

    if decision.get("owner_employee_id") != actor:
        raise PermissionError("only the decision owner may send its reply")
    draft = dict(decision.get("draft") or {})
    sha = draft_sha256(draft)
    if not sha or draft.get("sha256") != sha or draft.get("approved_sha256") != sha:
        raise PermissionError("draft is not approved at its current sha256")
    recipient = str(draft.get("to") or "").strip()
    account = str(decision.get("source_account") or "").strip()
    if not account.startswith("gmail_"):
        raise ValueError("the owner's source account has no approved Gmail send capability")
    from omniagentos.edc.accounts import accounts_map

    if not any(
        binding.source_account == account and binding.owner_employee_id == actor
        for binding in accounts_map().values()
    ):
        raise PermissionError("the Gmail send capability does not belong to the decision owner")
    capability = f"{account}.send"
    preview = preview_send(decision)
    mime, raw = _mime(draft, effect_key=preview["effect_key"])
    if preview["mime"] != mime:
        raise RuntimeError("MIME preview changed before execution")

    expires = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
    grants = GrantsStore(store._store)
    grant = grants.create_grant(
        capability,
        label=f"EDC reply {decision['id']}",
        target_set=[recipient],
        approval_id=f"edc:{decision['id']}:{sha}",
        max_actions=1,
        # The grant schema requires a spend ceiling and treats an exact zero
        # ceiling as exhausted. Gmail has no broker-priced spend, so use the
        # smallest positive bound while the extracted call spend remains 0.
        max_spend_usd=0.01,
        max_recipients=1,
        expires_at=expires,
        metadata={
            "generation": 0,
            "action_class": "consequential",
            "effect_key": preview["effect_key"],
            "draft_sha256": sha,
        },
    )
    result = broker.call(
        capability,
        [capability],
        method="POST",
        path="/gmail/v1/users/me/messages/send",
        body={"raw": raw},
        approval_token=str(grant["id"]),
        grant_store=grants,
        generation=0,
        audit_store=store._store,
        audit_context=broker.AuditContext(
            holder=f"human:{actor.removeprefix('emp_')}",
            request_id=preview["effect_key"],
            correlation_id=preview["effect_key"],
            grant_id=str(grant["id"]),
        ),
    )
    if not result.get("ok"):
        status = int(result.get("status") or 0)
        if status == 429 or status >= 500:
            raise _RetryableEffectError(f"Gmail send returned transient status {status}")
        raise RuntimeError(f"Gmail send returned terminal status {status}")
    return {
        **preview,
        "broker_grant_id": grant["id"],
        "provider_message_id": _provider_id(result),
        "sent_at": utc_now_iso(),
    }


def verify_send(result: dict[str, Any]) -> dict[str, Any]:
    """P2 records the provider id; the read-only sent-mail probe remains P3."""
    return {
        "verified": False,
        "provider_message_id": result.get("provider_message_id", ""),
        "reason": "provider accepted send; outcome probe pending",
    }


class _SendExecutor:
    consequential = True

    def preview(self, decision: dict[str, Any]) -> dict[str, Any]:
        return preview_send(decision)

    def execute(
        self, decision: dict[str, Any], *, store: DecisionStore, actor: str
    ) -> dict[str, Any]:
        return execute_send(decision, store=store, actor=actor)

    def verify(self, result: dict[str, Any]) -> dict[str, Any]:
        return verify_send(result)


# ---------------------------------------------------------------------------
# P3 — Delegate (the /task family) and Defer (board pool | workqueue).
#
# Both are ~glue over the canonical primitives: delegate creates ONE owned board
# card via the public ``team.tasks`` seam (F07) and links it to the decision;
# defer either queues an ownerless pool card (the operator only) or enqueues a workqueue
# unit for repo-shaped machine work. Neither imports the capability broker — the
# GREP INVARIANT (only ``execute_send`` imports it) stays true across edc/*.py.
# ---------------------------------------------------------------------------

#: EDC classification -> board-card priority. URGENT decisions carry an urgent
#: card; the maybe/ignore rows never reach delegate (they are not surfaced).
_PRIORITY_BY_CLASS: dict[str, str] = {
    "urgent": "urgent",
    "needs_owner": "high",
    "maybe": "normal",
    "ignore": "low",
}

#: The operator id — the only owner the matrix lets add to the SHARED queue.
_OPERATOR_EMPLOYEE_ID = "emp_owner"


def _collab_on(store: DecisionStore) -> Any:
    """A ``CollabStore`` bound to the SAME ``SqliteStore`` the decision uses.

    Binding the existing store (rather than constructing ``CollabStore(path)``)
    keeps one connection registry and one writer lock across the decision and
    its board card, so the card write and the linkage update serialize with the
    decision's own transactions instead of racing a second store instance.
    """
    from omniagentos.collab.store import CollabStore

    collab = CollabStore.__new__(CollabStore)
    collab._store = store._store
    return collab


def _card_excerpt(decision: dict[str, Any]) -> str:
    """A short, non-secret card description carrying the decision back-link.

    The decision id is the machine link; the context excerpt is truncated so a
    long untrusted body never becomes the card description wholesale.
    """
    context = str(decision.get("context") or "").strip()[:280]
    tail = f"decision:{decision['id']}"
    return f"{context}\n\n{tail}" if context else tail


# -- delegate ---------------------------------------------------------------


def preview_delegate(decision: dict[str, Any]) -> dict[str, Any]:
    """Show-before-act for a delegation: the assignee and the EDC card ref."""
    execution = decision.get("execution") or {}
    return {
        "kind": "delegate",
        "assignee": str(execution.get("assignee") or ""),
        "board_task_ref": f"EDC-{decision.get('number')}",
    }


def execute_delegate(
    decision: dict[str, Any], *, store: DecisionStore, actor: str
) -> dict[str, Any]:
    """Create the linked board card for a delegation (no DM — the wrapper DMs).

    Permission matrix FIRST: only the owner delegates, never to themselves, and
    only to someone on the active roster. Then ONE canonical owned card via the
    public ``team.tasks`` seam, linked to the decision both ways
    (``board_task_id`` + ``board_task_ref``). ``source='decision'`` so the v4
    Work-vs-Tasks scoring counts a completed EDC delegation as real Work — the
    zero-point ``task-adhoc`` source would mis-score it (F07).
    """
    from omniagentos.team import tasks as team_tasks

    if decision.get("owner_employee_id") != actor:
        raise PermissionError("only the decision owner may delegate it")
    # C1 re-entrancy guard: the board card is created BEFORE the CAS to
    # done_unverified, and card-create + linkage are separate writes. If a prior
    # attempt already linked a card (a retry after a failed/partial transition),
    # creating a second card would DUPLICATE the work. Re-drive the SAME linkage
    # instead so run_executor only completes the state transition.
    existing_task_id = decision.get("board_task_id")
    if existing_task_id:
        prior = dict(decision.get("execution") or {})
        return {
            "kind": "delegate",
            "assignee": str(decision.get("assignee_employee_id") or prior.get("assignee") or ""),
            "board_task_id": existing_task_id,
            "board_task_ref": str(
                decision.get("board_task_ref") or f"EDC-{decision.get('number')}"
            ),
            "delegated_at": utc_now_iso(),
            "reentrant": True,
        }
    execution = dict(decision.get("execution") or {})
    assignee = str(execution.get("assignee") or "").strip()
    if not assignee:
        raise ValueError("delegate requires an assignee")
    if assignee == actor:
        raise PermissionError("a decision cannot be delegated to its own owner")
    collab = _collab_on(store)
    if assignee not in team_tasks.active_employee_ids(store._store):
        raise PermissionError(f"{assignee} is not on the active roster")

    recommended = decision.get("recommended") or {}
    number = decision["number"]
    ref = f"EDC-{number}"
    company = str(decision.get("company_slug") or "")
    goal_id = team_tasks.resolve_company_goal(collab, company) if company else None
    title = str(decision.get("title") or "(no subject)")
    task = team_tasks.assign_adhoc_task(
        collab,
        title=f"[EDC] {title}",
        description=_card_excerpt(decision),
        owner_employee_id=assignee,
        actor=actor,
        goal_id=goal_id,
        ref=ref,
        acceptance_criteria=str(recommended.get("human_line") or ""),
        due_date=decision.get("deadline_at"),
        priority=_PRIORITY_BY_CLASS.get(str(decision.get("classification") or ""), "normal"),
        source="decision",
    )
    store.update_decision(
        decision["id"],
        owner_employee_id=actor,
        fields={
            "board_task_id": task.id,
            "board_task_ref": ref,
            "assignee_employee_id": assignee,
        },
    )
    return {
        "kind": "delegate",
        "assignee": assignee,
        "board_task_id": task.id,
        "board_task_ref": ref,
        "delegated_at": utc_now_iso(),
    }


def verify_delegate(result: dict[str, Any]) -> dict[str, Any]:
    """A delegation's outcome is proven later by the card's ``verified_at``."""
    return {
        "verified": False,
        "board_task_id": result.get("board_task_id", ""),
        "reason": "card created; outcome verified when the card is verified",
    }


class _DelegateExecutor:
    consequential = True

    def preview(self, decision: dict[str, Any]) -> dict[str, Any]:
        return preview_delegate(decision)

    def execute(
        self, decision: dict[str, Any], *, store: DecisionStore, actor: str
    ) -> dict[str, Any]:
        return execute_delegate(decision, store=store, actor=actor)

    def verify(self, result: dict[str, Any]) -> dict[str, Any]:
        return verify_delegate(result)


def delegate(
    store: DecisionStore,
    decision: dict[str, Any],
    *,
    actor: str,
    assignee: str | None = None,
    notifier: Any = None,
    reverse_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run a delegation end-to-end: create the linked card, send ONE DM.

    ``decision`` must already be ``in_progress`` (``resolve(delegate)`` consumed
    the authority). ``assignee`` may be supplied here or already sit in the
    decision's ``execution.assignee``. The single DM is sent AFTER the card
    write returns — outside the effect boundary, so a Slack failure never undoes
    a created card, and the assign event note carries no ``owner:`` token, so no
    watcher DMs a second time (one action, one DM).
    """
    from omniagentos.team import tasks as team_tasks

    if assignee is not None:
        execution = dict(decision.get("execution") or {})
        execution["assignee"] = assignee
        decision = {**decision, "execution": execution}
    result = run_executor(store, decision, actor=actor, kind="delegate")
    execution = result.get("execution") or {}
    target = str(execution.get("assignee") or assignee or "")
    if notifier is not None and reverse_map is not None and target:
        ref = str(execution.get("board_task_ref") or f"EDC-{result.get('number')}")
        title = str(result.get("title") or "(no subject)")
        due = team_tasks.render_due(result.get("deadline_at"))
        team_tasks.send_dm(
            notifier,
            reverse_map,
            target,
            f"{team_tasks.display_name(actor)} assigned you {ref} — {title}{due}",
        )
    return result


# -- defer ------------------------------------------------------------------


def _machine_submit(decision: dict[str, Any], actor: str, spec: dict[str, Any]) -> dict[str, Any]:
    """A workqueue ``enqueue`` envelope for a repo-shaped deferred decision.

    ``idempotency_key = edc:<decision_id>`` makes a re-defer a no-op at the
    queue (``enqueue`` returns the existing unit with ``deduped=True``); the
    labels carry the decision back-link so the unit is attributable.
    """
    return {
        "idempotency_key": f"edc:{decision['id']}",
        "risk_class": str(spec.get("risk_class") or "standard"),
        "submitted_by": actor,
        "labels": ["edc", f"decision:{decision['id']}"],
        "repo_url": spec["repo_url"],
        "repo_slug": str(spec.get("repo_slug") or spec["repo_url"].rstrip("/").rsplit("/", 1)[-1]),
        "base_sha": spec["base_sha"],
        "branch": str(spec.get("branch") or f"edc/decision-{decision['number']}"),
        "owned_paths": spec["owned_paths"],
        "agent_profile": str(spec.get("agent_profile") or "sol-coder"),
        "acceptance_cmd": spec["acceptance_cmd"],
        "brief_inline": str(
            spec.get("brief_inline") or (decision.get("recommended") or {}).get("human_line") or ""
        ),
    }


def preview_defer(decision: dict[str, Any]) -> dict[str, Any]:
    """Show-before-act for a defer: which lane (shared queue vs machine)."""
    execution = decision.get("execution") or {}
    mode = str(execution.get("defer_mode") or "queue")
    return {"kind": "defer", "defer_mode": mode}


class _DeferExecutor:
    """Enqueue a deferred decision — shared-queue pool card, or a machine unit.

    Holds an optional workqueue store so the machine lane can be tested with an
    injected queue; production leaves it ``None`` and resolves ``WQ_DB`` lazily.
    """

    consequential = True

    def __init__(self, wq_store: Any = None) -> None:
        self._wq_store = wq_store

    def preview(self, decision: dict[str, Any]) -> dict[str, Any]:
        return preview_defer(decision)

    def _wq(self) -> Any:
        if self._wq_store is not None:
            return self._wq_store
        import os

        from omniagentos.workqueue.store import WorkQueueStore

        db = os.environ.get("WQ_DB")
        if not db:
            raise ValueError("defer:machine needs a workqueue store (set WQ_DB)")
        self._wq_store = WorkQueueStore(db)
        return self._wq_store

    def execute(
        self, decision: dict[str, Any], *, store: DecisionStore, actor: str
    ) -> dict[str, Any]:
        if decision.get("owner_employee_id") != actor:
            raise PermissionError("only the decision owner may defer it")
        execution = dict(decision.get("execution") or {})
        mode = str(execution.get("defer_mode") or "queue")
        if mode == "machine":
            spec = machine_spec(decision.get("recommended"))
            if spec is None:
                raise ValueError("defer:machine requires a repo-shaped recommended action")
            # C1 re-entrancy guard (machine): the wq enqueue is idempotent on
            # ``edc:<id>``, but a retry that already recorded ``wq_unit_id`` need
            # not touch the queue again — re-drive the same linkage.
            existing_unit = decision.get("wq_unit_id")
            if existing_unit:
                return {
                    "kind": "defer",
                    "defer_mode": "machine",
                    "wq_unit_id": existing_unit,
                    "deduped": True,
                    "deferred_at": utc_now_iso(),
                    "reentrant": True,
                }
            unit_id, deduped = self._wq().enqueue(_machine_submit(decision, actor, spec))
            store.update_decision(
                decision["id"], owner_employee_id=actor, fields={"wq_unit_id": unit_id}
            )
            return {
                "kind": "defer",
                "defer_mode": "machine",
                "wq_unit_id": unit_id,
                "deduped": deduped,
                "deferred_at": utc_now_iso(),
            }
        # C1 re-entrancy guard (queue): the pool card is created BEFORE the CAS to
        # done_unverified. A retry after a failed transition would create a SECOND
        # pool card — re-drive the same linkage instead of adding a duplicate.
        existing_task_id = decision.get("board_task_id")
        if existing_task_id:
            return {
                "kind": "defer",
                "defer_mode": "queue",
                "board_task_id": existing_task_id,
                "board_task_ref": str(
                    decision.get("board_task_ref") or f"EDC-{decision.get('number')}"
                ),
                "deferred_at": utc_now_iso(),
                "reentrant": True,
            }
        # Shared-queue pool card: the operator only (adding a queue card IS approval).
        if actor != _OPERATOR_EMPLOYEE_ID:
            raise PermissionError(
                "adding to the shared queue is the operator-only; delegate or snooze instead"
            )
        from omniagentos.team import tasks as team_tasks

        collab = _collab_on(store)
        company = str(decision.get("company_slug") or "")
        goal_id = team_tasks.resolve_company_goal(collab, company) if company else None
        if not goal_id:
            raise ValueError("the shared queue needs a company general-engineering goal")
        recommended = decision.get("recommended") or {}
        number = decision["number"]
        ref = f"EDC-{number}"
        title = str(decision.get("title") or "(no subject)")
        acceptance = str(recommended.get("human_line") or "") or title
        task = team_tasks.add_pool_task(
            collab,
            title=f"[EDC] {title}",
            description=_card_excerpt(decision),
            actor=actor,
            goal_id=goal_id,
            ref=ref,
            acceptance_criteria=acceptance,
            due_date=decision.get("deadline_at"),
            priority=_PRIORITY_BY_CLASS.get(str(decision.get("classification") or ""), "normal"),
            source="decision",
        )
        store.update_decision(
            decision["id"],
            owner_employee_id=actor,
            fields={"board_task_id": task.id, "board_task_ref": ref},
        )
        return {
            "kind": "defer",
            "defer_mode": "queue",
            "board_task_id": task.id,
            "board_task_ref": ref,
            "deferred_at": utc_now_iso(),
        }

    def verify(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "verified": False,
            "reason": "work queued; outcome verified on the wq terminal pass / card verify",
        }


def defer(
    store: DecisionStore,
    decision: dict[str, Any],
    *,
    actor: str,
    mode: str | None = None,
    wq_store: Any = None,
) -> dict[str, Any]:
    """Run a defer end-to-end. ``mode`` is ``'queue'`` (default) or ``'machine'``.

    ``decision`` must already be ``in_progress``. A workqueue store may be
    injected for the machine lane; the shared-queue lane needs none.
    """
    if mode is not None:
        execution = dict(decision.get("execution") or {})
        execution["defer_mode"] = mode
        decision = {**decision, "execution": execution}
    executor = _DeferExecutor(wq_store=wq_store) if wq_store is not None else None
    return run_executor(store, decision, actor=actor, kind="defer", executor=executor)


# CLOSED registry: P2 deliberately has no generic shell/tool escape hatch; P3
# adds only the two canonical work-routing kinds (delegate, defer).
EXECUTORS: dict[str, Executor] = {
    "send_email": _SendExecutor(),
    "delegate": _DelegateExecutor(),
    "defer": _DeferExecutor(),
}


#: The audit ``event`` each executor kind stamps on a successful dispatch. Every
#: kind lands ``done_unverified`` (the effect was dispatched, the OUTCOME is not
#: yet proven — the completion sweep promotes it to ``done_verified``); only the
#: event name differs so the trail reads ``send``/``delegate``/``defer`` honestly.
_SUCCESS_EVENT: dict[str, str] = {
    "send_email": "send",
    "delegate": "delegate",
    "defer": "defer",
}


def run_executor(
    store: DecisionStore,
    decision: dict[str, Any],
    *,
    actor: str,
    kind: str = "send_email",
    executor: Executor | None = None,
) -> dict[str, Any]:
    """Run the selected executor and finish in an honest F03 recovery state.

    ``executor`` may be passed to override the registry entry — the P3
    delegate/defer wrappers use it to inject a collaborator-wired instance (a
    workqueue store, say) without mutating the shared registry.
    """
    executor = executor or EXECUTORS.get(kind)
    if executor is None:
        raise ValueError(f"unsupported EDC executor: {kind}")
    if decision.get("status") != "in_progress":
        raise DecisionConflictError(decision)
    execution = dict(decision.get("execution") or {})
    execution.update(executor.preview(decision))
    execution["started_at"] = utc_now_iso()
    try:
        result = executor.execute(decision, store=store, actor=actor)
    except BaseException as exc:
        # Broker transport/finalization errors may have crossed the effect
        # boundary. Never blind-retry those; local/refused failures are safe.
        reason = str(getattr(exc, "reason", ""))
        ambiguous = reason in {"transport_error", "audit_finalization_failed"}
        transient = isinstance(
            exc, (_RetryableEffectError, TimeoutError, ConnectionError)
        ) or reason in {
            "audit_unavailable",
            "grant_store_unavailable",
        }
        target = (
            "reconcile_required" if ambiguous else "failed_retryable" if transient else "failed"
        )
        # F2: never persist a raw exception message — a type+reason label only.
        execution.update({"error": safe_error(exc), "reason": reason, "failed_at": utc_now_iso()})
        store.transition_effect(
            decision["id"],
            owner_employee_id=actor,
            from_status="in_progress",
            to_status=target,
            event="execute",
            execution=execution,
            note=f"{kind} {target}",
        )
        raise
    execution.update(result)
    execution["verification"] = executor.verify(result)
    return store.transition_effect(
        decision["id"],
        owner_employee_id=actor,
        from_status="in_progress",
        to_status="done_unverified",
        event=_SUCCESS_EVENT.get(kind, "execute"),
        execution=execution,
        note=f"{kind} dispatched",
    )


__all__ = [
    "EXECUTORS",
    "defer",
    "delegate",
    "draft_sha256",
    "preview_defer",
    "preview_delegate",
    "preview_send",
    "run_executor",
    "safe_error",
]
