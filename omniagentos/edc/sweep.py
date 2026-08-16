"""EDC P3 — completion + OUTCOME-verification sweep (synthesis §7, F10).

Runs inside the same ~5-minute tick as triage/reconcile. It reads ONLY strong,
outcome-verified signals and promotes a *dispatched* decision
(``done_unverified``) to ``done_verified``:

* **Delegate / Defer-to-queue** — the linked board card is ``status='done'``
  **AND** ``verified_at IS NOT NULL``. Verification is written by
  ``TeamStore.verify_task`` alone (mechanical merged-``pr``/``test_run`` at a
  passing gate, or a counter-signed human — the owner cannot self-verify). The
  ``emp_owner`` operator self-verify exemption is documented and LOGGED explicitly
  when the sweep leans on it (F10).
* **Defer-to-machine** — the workqueue unit reached the terminal ``pass``
  (``state='done'`` with ``terminal_reason='accepted'``). A ``sensitive`` unit
  stops at ``review`` and never auto-lands, so it is NOT completed here.
* **Execute / Reply** — an optional declarative probe
  (``verification_json = {capability, method, path, expect}``, read-only broker
  calls only) is run through an INJECTED ``probe_runner``. A pass ⇒
  ``done_verified`` + a concise owner DM; no feasible probe ⇒ the decision stays
  ``done_unverified`` and the DM says exactly that.

WEAK signals — a branch exists, a commit exists, a Slack claim, an open PR, mere
session activity — have NO code path here: the sweep consults only
``board_tasks.verified_at``, the wq terminal state, and the declared probe. The
broker is never imported (the GREP INVARIANT that only ``actions.execute_send``
imports it holds across ``edc/*.py`` — the probe is a caller-supplied callable).

The same pass DMs the owner about a BLOCKED delegated card and an APPROACHING
deadline, each at most once (a one-shot flag in ``execution_json`` prevents the
5-minute tick from re-pinging).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.edc.store import DecisionStore

__all__ = ["run_completion_sweep"]

#: The operator — the one identity allowed to verify a card they own (F10).
_OPERATOR_EMPLOYEE_ID = "emp_owner"


class _Notifier(Protocol):
    def post_dm(
        self,
        slack_user_id: str,
        text: str,
        *,
        blocks: list | None = None,
        color: str | None = None,
    ) -> bool: ...


class _ProbeRunner(Protocol):
    def __call__(self, decision: dict[str, Any]) -> dict[str, Any]: ...


def _collab_on(base_store: SqliteStore) -> Any:
    """A ``CollabStore`` bound to the shared ``SqliteStore`` (one lock, one conn)."""
    from omniagentos.collab.store import CollabStore

    collab = CollabStore.__new__(CollabStore)
    collab._store = base_store
    return collab


def _dm(
    notifier: _Notifier | None, slack_reverse: dict[str, str] | None, owner: str, text: str
) -> bool:
    if notifier is None or not slack_reverse:
        return False
    slack_id = slack_reverse.get(owner)
    if not slack_id:
        return False
    try:
        return bool(notifier.post_dm(slack_id, text))
    except Exception:  # noqa: BLE001 -- a DM must never sink the sweep
        return False


def _flagged(decision: dict[str, Any], key: str) -> bool:
    """True if the one-shot DM ``key`` was already stamped on this decision."""
    return bool((decision.get("execution") or {}).get(key))


def _stamp_flag(decisions: DecisionStore, decision: dict[str, Any], owner: str, key: str) -> None:
    """Stamp the one-shot ``key`` in ``execution_json`` (review F1/E1).

    Called ONLY after the DM it guards was actually delivered — stamping before
    a DM attempt froze the flag even when the notifier was ``None`` (a real prod
    state with no ``SLACK_BOT_TOKEN``) or ``post_dm`` failed, permanently losing
    the message. Stamp-on-success means a failed tick leaves the flag unset so a
    later healthy tick re-sends. The in-memory decision is updated too so a
    second consult inside the same tick sees the stamp.
    """
    execution = dict(decision.get("execution") or {})
    execution[key] = utc_now_iso()
    decisions.update_decision(
        decision["id"], owner_employee_id=owner, fields={"execution": execution}
    )
    decision["execution"] = execution


def _complete_verified(
    decisions: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    verification: dict[str, Any],
    *,
    notifier: _Notifier | None,
    slack_reverse: dict[str, str] | None,
    note: str,
) -> dict[str, Any] | None:
    updated = decisions.complete_outcome(
        decision["id"],
        owner_employee_id=owner,
        from_status="done_unverified",
        to_status="done_verified",
        verification=verification,
        note=note,
    )
    if updated is None:
        return None
    ref = str(decision.get("board_task_ref") or f"EDC-{decision.get('number')}")
    title = str(decision.get("title") or "").strip()
    _dm(notifier, slack_reverse, owner, f"✅ {ref} verified complete: {title}")
    return updated


def _sweep_delegated(
    decisions: DecisionStore,
    collab: Any,
    decision: dict[str, Any],
    owner: str,
    *,
    now: datetime,
    notifier: _Notifier | None,
    slack_reverse: dict[str, str] | None,
    deadline_horizon_hours: int,
    stats: dict[str, int],
) -> None:
    """A delegate / defer-to-queue decision: complete on the card's verified_at."""
    card = collab.get_board_task(str(decision["board_task_id"]))
    if card is None:
        return
    status = str(card.get("status") or "")
    verified_at = card.get("verified_at")
    if status == "done" and verified_at:
        verified_by = str(card.get("verified_by") or "")
        card_owner = str(card.get("owner_employee_id") or "")
        operator_self_verify = verified_by == card_owner == _OPERATOR_EMPLOYEE_ID and bool(
            card_owner
        )
        if operator_self_verify:
            # F10: the operator is the ONE identity allowed to verify their own
            # card (nobody to counter-sign with). Log the exemption explicitly so
            # a completion that rests on it is never a silent self-attestation.
            print(
                f"edc.sweep: EDC-{decision.get('number')} completes on the emp_owner "
                f"operator self-verify exemption (card {card.get('id')})",
                flush=True,
            )
        verification = {
            "path": "board_card",
            "board_task_id": card.get("id"),
            "verified_at": verified_at,
            "verified_by": verified_by,
            "operator_self_verify": operator_self_verify,
        }
        note = f"delegated card {card.get('ref') or card.get('id')} verified by {verified_by}"
        if _complete_verified(
            decisions,
            decision,
            owner,
            verification,
            notifier=notifier,
            slack_reverse=slack_reverse,
            note=note,
        ):
            stats["verified"] += 1
            stats["completed"] += 1
        return
    # Not yet complete: nudge on a blocked card or an approaching deadline (once).
    if status == "blocked" or str(card.get("blocked_reason") or "").strip():
        if not _flagged(decision, "blocked_dm_at"):
            ref = str(decision.get("board_task_ref") or f"EDC-{decision.get('number')}")
            reason = str(card.get("blocked_reason") or "blocked").strip()
            if _dm(notifier, slack_reverse, owner, f"⛔ {ref} is blocked: {reason}"):
                _stamp_flag(decisions, decision, owner, "blocked_dm_at")
                stats["blocked_dm"] += 1
            else:
                # F1/E1: leave the flag unset so a later healthy tick re-sends;
                # bump a failed counter so the drop leaves a receipt.
                stats["blocked_dm_failed"] += 1
        return
    _maybe_deadline_dm(
        decisions,
        decision,
        owner,
        now=now,
        horizon_hours=deadline_horizon_hours,
        notifier=notifier,
        slack_reverse=slack_reverse,
        stats=stats,
    )


def _maybe_deadline_dm(
    decisions: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    *,
    now: datetime,
    horizon_hours: int,
    notifier: _Notifier | None,
    slack_reverse: dict[str, str] | None,
    stats: dict[str, int],
) -> None:
    deadline = decision.get("deadline_at")
    if not deadline:
        return
    try:
        due = datetime.fromisoformat(str(deadline))
    except ValueError:
        return
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    if now <= due <= now + timedelta(hours=horizon_hours):
        if not _flagged(decision, "deadline_dm_at"):
            ref = str(decision.get("board_task_ref") or f"EDC-{decision.get('number')}")
            if _dm(
                notifier,
                slack_reverse,
                owner,
                f"⏰ {ref} is still open and due {str(deadline)[:16].replace('T', ' ')}",
            ):
                _stamp_flag(decisions, decision, owner, "deadline_dm_at")
                stats["deadline_dm"] += 1
            else:
                # F1/E1: stamp only on delivery — a missed tick re-sends later.
                stats["deadline_dm_failed"] += 1


def _sweep_machine(
    decisions: DecisionStore,
    wq_store: Any,
    decision: dict[str, Any],
    owner: str,
    *,
    notifier: _Notifier | None,
    slack_reverse: dict[str, str] | None,
    stats: dict[str, int],
) -> None:
    """A defer-to-machine decision: complete on the wq unit's terminal pass."""
    if wq_store is None:
        return
    unit = wq_store.get_unit(str(decision["wq_unit_id"]))
    if unit is None:
        return
    # Terminal PASS only: state='done' with terminal_reason='accepted'. A
    # sensitive unit stops at 'review' (never auto-lands) and is not completed.
    if str(unit.get("state")) == "done" and str(unit.get("terminal_reason")) == "accepted":
        verification = {
            "path": "workqueue",
            "wq_unit_id": unit.get("id"),
            "state": unit.get("state"),
            "terminal_reason": unit.get("terminal_reason"),
            "result_sha": unit.get("result_sha"),
        }
        if _complete_verified(
            decisions,
            decision,
            owner,
            verification,
            notifier=notifier,
            slack_reverse=slack_reverse,
            note=f"wq unit {unit.get('id')} accepted",
        ):
            stats["verified"] += 1
            stats["completed"] += 1


def _sweep_probe(
    decisions: DecisionStore,
    decision: dict[str, Any],
    owner: str,
    *,
    probe_runner: _ProbeRunner | None,
    notifier: _Notifier | None,
    slack_reverse: dict[str, str] | None,
    stats: dict[str, int],
) -> None:
    """An execute/reply decision: an OPTIONAL declarative probe, else honest DM.

    Three cases, kept distinct (review E2):

    (a) NO probe declared → the effect was dispatched but there is no feasible
        outcome check; tell the owner exactly that, once.
    (b) probe declared but PENDING (runner absent this tick, or ``verified=False``)
        → do nothing and wait: a feasible probe simply hasn't passed yet, so the
        "no feasible probe" DM would be a false claim. Re-checked next tick.
    (c) probe PASSED → complete to done_verified + the verified-outcome DM.
    """
    probe = decision.get("verification") or {}
    declared = isinstance(probe, dict) and bool(probe.get("capability"))
    if declared:
        # (b)/(c): a feasible probe exists. Only a PASS completes; anything else
        # (no runner this tick, verified=False, or a probe error) is PENDING and
        # must NOT fall through to the "no feasible probe" DM.
        if probe_runner is None:
            return
        try:
            outcome = probe_runner(decision)
        except Exception:  # noqa: BLE001 -- a probe failure is not a completion
            outcome = {"verified": False}
        if outcome.get("verified"):
            verification = {"path": "probe", **{k: v for k, v in outcome.items()}}
            if _complete_verified(
                decisions,
                decision,
                owner,
                verification,
                notifier=notifier,
                slack_reverse=slack_reverse,
                note="declarative outcome probe passed",
            ):
                stats["verified"] += 1
                stats["completed"] += 1
        # Pending (verified=False): nothing this tick, no DM — wait and re-check.
        return
    # (a) No probe declared: the effect was dispatched but the OUTCOME is
    # unprovable here. Tell the owner exactly that, once (stamp on delivery).
    if not _flagged(decision, "unverified_dm_at"):
        ref = str(decision.get("board_task_ref") or f"EDC-{decision.get('number')}")
        title = str(decision.get("title") or "").strip()
        if _dm(
            notifier,
            slack_reverse,
            owner,
            f"☑️ {ref} dispatched — outcome not verified (no feasible probe): {title}",
        ):
            _stamp_flag(decisions, decision, owner, "unverified_dm_at")
            stats["unverified_dm"] += 1
        else:
            # F1/E1: unstamped on failure so a healthy tick re-sends the notice.
            stats["unverified_dm_failed"] += 1


def run_completion_sweep(
    base_store: SqliteStore,
    owner_employee_ids: list[str],
    *,
    now: datetime | None = None,
    notifier: _Notifier | None = None,
    slack_reverse: dict[str, str] | None = None,
    wq_store: Any = None,
    probe_runner: _ProbeRunner | None = None,
    deadline_horizon_hours: int = 24,
) -> dict[str, int]:
    """Promote dispatched decisions to ``done_verified`` on strong outcomes.

    Owner-scoped throughout (every read/write names the owner). Returns per-class
    counts. ``wq_store`` is required only for defer-to-machine outcomes;
    ``probe_runner`` only for execute/reply probes — both may be ``None``.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    decisions = DecisionStore(base_store)
    collab = _collab_on(base_store)
    stats = {
        "seen": 0,
        "completed": 0,
        "verified": 0,
        "blocked_dm": 0,
        "deadline_dm": 0,
        "unverified_dm": 0,
        # F1/E1: a DM that failed to deliver (notifier None, unmapped owner, or
        # post_dm False/raised) bumps a *_failed counter and leaves its one-shot
        # flag UNSET so a later healthy tick re-sends — the drop leaves a receipt.
        "blocked_dm_failed": 0,
        "deadline_dm_failed": 0,
        "unverified_dm_failed": 0,
    }
    for owner in owner_employee_ids:
        for decision in decisions.list_decisions(owner_employee_id=owner, status="done_unverified"):
            stats["seen"] += 1
            if decision.get("board_task_id"):
                _sweep_delegated(
                    decisions,
                    collab,
                    decision,
                    owner,
                    now=moment,
                    notifier=notifier,
                    slack_reverse=slack_reverse,
                    deadline_horizon_hours=deadline_horizon_hours,
                    stats=stats,
                )
            elif decision.get("wq_unit_id"):
                _sweep_machine(
                    decisions,
                    wq_store,
                    decision,
                    owner,
                    notifier=notifier,
                    slack_reverse=slack_reverse,
                    stats=stats,
                )
            else:
                _sweep_probe(
                    decisions,
                    decision,
                    owner,
                    probe_runner=probe_runner,
                    notifier=notifier,
                    slack_reverse=slack_reverse,
                    stats=stats,
                )
    return stats
