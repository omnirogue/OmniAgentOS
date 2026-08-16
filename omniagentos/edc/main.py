"""EDC triage entry point — poll → classify → store → surface (synthesis §11).

The adapter-agnostic pipeline the launchd triage plist invokes every ~300s:

1. ``run_triage`` — pull every undecisioned event from the source adapters,
   classify each deterministically, write a Decision row for EVERY event
   (including IGNORE/suppressed, D3), advance the F1 watermark only AFTER the
   row is durable, and DM the owner for URGENT items (one ping per decision).
2. ``run_reclassify`` — CODEX F06: re-run classify on stale MAYBE /
   ``classifier='llm_unavailable'`` rows via ``DecisionStore.reclassify_decision``
   so a decision fail-closed during an LLM outage (P2) is re-evaluated later,
   and a MAYBE never freezes permanently.

3. ``run_session_sweep`` — the flag-gated (``OMNIAGENTOS_EDC_SESSIONS``, default
   OFF) counterpart to the session source: it EXPIRES session suggestions whose
   condition cleared, so a card never outlives the state that justified it. A
   no-op — not even a read — while the flag is down.

The pipeline is source-agnostic: it iterates ``SourceAdapter`` instances and
never imports a concrete adapter's internals beyond constructing the defaults in
:func:`default_adapters`. A ``SourceEvent(source='agent', ...)`` enters
``_ingest_event`` with zero core change (Future Sources); an adapter over a
STRUCTURED source may additionally carry its own deterministic ``classify_event``
so it never spends an LLM call re-deriving what its rows already state.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.edc.accounts import accounts_map
from omniagentos.edc.adapters.base import SourceAdapter, SourceEvent
from omniagentos.edc.adapters.email import EmailAdapter, event_from_comms_row
from omniagentos.edc.adapters.sessions import (
    SessionAdapter,
    sessions_source_enabled,
    sweep_cleared_session_decisions,
)
from omniagentos.edc.classify import classify
from omniagentos.edc.snooze import sweep_snoozes
from omniagentos.edc.store import DecisionStore
from omniagentos.edc.sweep import run_completion_sweep
from omniagentos.steward.config import StewardConfig, load_steward_config

__all__ = [
    "default_adapters",
    "main",
    "run_completion_sweep",
    "run_reclassify",
    "run_reconcile_sweep",
    "run_session_sweep",
    "run_triage",
]


class _Notifier(Protocol):
    def post_dm(
        self,
        slack_user_id: str,
        text: str,
        *,
        blocks: list | None = None,
        color: str | None = None,
    ) -> bool: ...


class _EventClassifier(Protocol):
    """An adapter-supplied deterministic verdict (see ``_ingest_event``)."""

    def __call__(self, event: SourceEvent, *, now: datetime) -> dict[str, Any]: ...


class _CycleJsonClient:
    """One shared logical-call budget across triage and reclassification."""

    def __init__(self, limit: int) -> None:
        self.limit = max(0, limit)
        self.used = 0
        self._client: Any = None

    def complete_json(
        self,
        messages: list[dict[str, str]],
        required_keys: list[str],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "default",
    ) -> dict[str, Any]:
        if self.used >= self.limit:
            raise RuntimeError("EDC LLM cycle budget exhausted")
        self.used += 1
        if self._client is None:
            from omniagentos.llm.client import ShortCallClient

            self._client = ShortCallClient()
        return self._client.complete_json(
            messages,
            required_keys,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            purpose=purpose,
        )


def _stat_key(classification: str) -> str:
    return {
        "urgent": "urgent",
        "needs_owner": "needs_owner",
        "maybe": "maybe",
        "ignore": "ignored",
    }.get(classification, "other")


def _decision_payload(event: SourceEvent, verdict: dict[str, Any]) -> dict[str, Any]:
    """Merge a SourceEvent and a classify verdict into a create_decision payload."""
    return {
        "owner_employee_id": event["owner_employee_id"],
        "company_slug": event.get("company_slug", ""),
        "source": event["source"],
        "source_ref": event["source_ref"],
        "source_account": event.get("source_account", ""),
        "occurred_at": event.get("occurred_at") or None,
        "title": event.get("title") or "(no subject)",
        "counterparty": event.get("counterparty", ""),
        "classification": verdict["classification"],
        "consequence": verdict["consequence"],
        "deadline_at": verdict["deadline_at"],
        "likelihood": verdict["likelihood"],
        "confidence": verdict["confidence"],
        "reason": verdict["reason"],
        "classifier": verdict["classifier"],
        "rule_matches": verdict["rule_matches"],
        "recommended": verdict["recommended"],
        "available_actions": verdict["available_actions"],
        "status": verdict["status"],
        "surfaced": verdict["surfaced"],
    }


def _urgent_dm_text(decision: dict[str, Any]) -> str:
    recommended = decision.get("recommended") or {}
    action = str(recommended.get("human_line") or "review now").strip()
    title = str(decision.get("title") or "(no subject)")
    deadline = decision.get("deadline_at")
    due = f" (due {deadline})" if deadline else ""
    # post_dm applies _safe_title on egress; the untrusted subject is carried as
    # data, and the recommended line is a server-generated template.
    return f"🚨 EDC-{decision.get('number')}: {title} — {action}.{due}"


def _surface_urgent(
    decision: dict[str, Any],
    decisions: DecisionStore,
    owner: str,
    notifier: _Notifier | None,
    slack_reverse: dict[str, str] | None,
) -> bool:
    """DM the owner about an URGENT decision; set surfaced=1 only on a sent DM."""
    if notifier is None or not slack_reverse:
        return False
    slack_id = slack_reverse.get(owner)
    if not slack_id:
        print(f"edc.triage: no Slack mapping for owner {owner!r}; not surfaced", file=sys.stderr)
        return False
    if not notifier.post_dm(slack_id, _urgent_dm_text(decision)):
        return False
    decisions.update_decision(decision["id"], owner_employee_id=owner, fields={"surfaced": 1})
    decisions.append_event(
        decision["id"],
        owner_employee_id=owner,
        actor="system",
        event="surface",
        note="urgent dm",
    )
    return True


def _ingest_event(
    event: SourceEvent,
    decisions: DecisionStore,
    *,
    owner_rules: list[dict[str, Any]],
    cfg: StewardConfig,
    now: datetime,
    notifier: _Notifier | None,
    slack_reverse: dict[str, str] | None,
    stats: dict[str, int],
    llm_client: _CycleJsonClient | None = None,
    classify_event: _EventClassifier | None = None,
) -> None:
    """Classify one event, persist a row, advance the watermark, surface if URGENT.

    The core, adapter-agnostic step (proves Future Sources: any SourceEvent —
    email today, ``source='agent'`` tomorrow — travels this exact path).

    ``classify_event`` is an adapter's OWN deterministic verdict function, used
    in place of the shared classifier when the adapter provides one. The shared
    ``classify`` is built for untrusted prose (harm heuristics, then an LLM
    ambiguity estimate); a structured source that already STATES its condition —
    a session row saying ``attention_state='needs_input'`` — needs neither, and
    routing it through the ambiguity branch would spend an LLM call to re-derive
    a fact the row asserts. Everything downstream of the verdict is unchanged, so
    both kinds of source share one ingest path, one dedupe and one audit trail.
    """
    stats["seen"] += 1
    owner = event["owner_employee_id"]
    verdict = (
        classify_event(event, now=now)
        if classify_event is not None
        else classify(
            event,
            owner_rules=owner_rules,
            cfg=cfg.alerts,
            now=now,
            llm_client=llm_client,
            credible_domains=cfg.edc.credible_sender_domains,
        )
    )
    decision, created = decisions.create_decision(_decision_payload(event, verdict))

    # F1: advance the watermark ONLY after the row is durably written. Ordered
    # ascending per owner by the adapter, so this is monotonic. Idempotent even
    # if a crash lands between the write and this advance (D3 UNIQUE backstop).
    if event["source"] == decisions_email_source():
        decisions.advance_source_cursor(
            event["source"],
            owner,
            last_message_id=str(event["source_ref"]),
            last_triaged_at=utc_now_iso(),
        )

    if not created:
        stats["duplicate"] += 1
        return
    stats["created"] += 1
    stats[_stat_key(verdict["classification"])] += 1

    if verdict["classification"] == "urgent" and not decision.get("surfaced"):
        if _surface_urgent(decision, decisions, owner, notifier, slack_reverse):
            stats["dm_sent"] += 1

    _maybe_auto_delegate(decision, decisions, cfg, notifier, slack_reverse, stats)


def _maybe_auto_delegate(
    decision: dict[str, Any],
    decisions: DecisionStore,
    cfg: StewardConfig,
    notifier: _Notifier | None,
    slack_reverse: dict[str, str] | None,
    stats: dict[str, int],
) -> None:
    """Fire an auto_delegate rule's pre-filled delegation, IF its live gate is on.

    Two gates must BOTH be open (F11 / spec §15.12): the rule's own PER-RULE
    ``action.live`` (surfaced by classify into ``recommended.params.live``) AND
    the estate master ``edc.auto_delegate_live`` (default OFF). Enabling the
    master alone arms nothing — every rule opts in individually. When it does
    fire, the delegation runs as the owner (the rule is the owner's advance
    approval) but the assign event records actor ``system:rule:<id>`` so the
    trail shows it was rule-driven, not a manual hand-off.

    A1 crash-safety: ``resolve`` consumes the owner's authority (open →
    ``in_progress``) BEFORE the external delegate I/O. If that I/O throws (a Slack
    timeout, the board write, anything), the decision must not vanish into a
    stranded ``in_progress`` nor a terminal ``failed`` where the owner never sees
    it again. Auto-delegate is pure convenience: on ANY failure it degrades to the
    normal manual pre-fill by re-surfacing the row to ``open`` so the owner can
    delegate/approve by hand (:func:`_degrade_failed_auto_delegate`).
    """
    recommended = decision.get("recommended") or {}
    params = recommended.get("params") or {}
    if not params.get("auto_delegate"):
        return
    rule_id = str(params.get("rule_id") or "")
    assignee = str(params.get("assignee") or "")
    if not (params.get("live") and cfg.edc.auto_delegate_live and assignee and rule_id):
        return  # pre-fill only — the owner approves with one tap
    owner = decision["owner_employee_id"]
    decision_id = decision["id"]
    try:
        from omniagentos.edc import actions as edc_actions

        claimed = decisions.resolve(
            decision_id,
            actor=owner,
            resolution="delegate",
            params={"execution": {"assignee": assignee}},
        )
        edc_actions.delegate(
            decisions, claimed, actor=owner, notifier=notifier, reverse_map=slack_reverse
        )
        decisions.append_event(
            decision_id,
            owner_employee_id=owner,
            actor=f"system:rule:{rule_id}",
            event="delegate",
            note=f"auto-delegated to {assignee} by promoted rule {rule_id}",
        )
        stats["auto_delegated"] += 1
    except Exception as exc:  # noqa: BLE001 — never let a rule effect break triage
        _degrade_failed_auto_delegate(decision_id, decisions, owner, rule_id, exc, stats)


def _degrade_failed_auto_delegate(
    decision_id: str,
    decisions: DecisionStore,
    owner: str,
    rule_id: str,
    exc: BaseException,
    stats: dict[str, int],
) -> None:
    """Re-surface a failed auto-delegate as the owner's manual pre-fill (A1).

    ``edc_actions.delegate`` runs through ``run_executor``, so a genuine effect
    failure is already CAS'd to a recovery state (``failed_retryable`` / ``failed``
    / ``reconcile_required``) and a crash before that CAS can leave the row
    ``in_progress``. Either way, if the delegation did NOT complete, reopen the row
    to ``open`` so the owner sees the same pre-filled NEEDS_OWNER item and can
    delegate/approve by hand — the whole point of auto-delegate is convenience, so
    a failure degrades to the normal manual path and NEVER loses the decision. If
    the effect DID complete and only a post-effect notify (the DM) failed, keep the
    successful delegation rather than double-filing it.
    """
    label = type(exc).__name__
    current = decisions.get_decision(decision_id, owner_employee_id=owner)
    status = str((current or {}).get("status") or "")
    if status in {"done_unverified", "done_verified"}:
        # The card was created; only the post-effect notify failed. Keep the win.
        stats["auto_delegated"] += 1
        print(
            f"edc.triage: auto-delegate for {decision_id} delegated; post-effect "
            f"notify failed ({label}) — delegation kept",
            file=sys.stderr,
        )
        return
    reopened = (
        decisions.reopen(
            decision_id,
            owner_employee_id=owner,
            from_status=status,
            event="surface",
            note=(
                f"auto-delegate via rule {rule_id} failed ({label}); "
                "re-surfaced for manual handling"
            ),
        )
        if status and status != "open"
        else None
    )
    stats["auto_delegate_reverted"] = stats.get("auto_delegate_reverted", 0) + 1
    if reopened is None and status not in {"open", ""}:
        # The row moved underneath us (a concurrent sweep already recovered it) —
        # never re-drive; the reconcile sweep owns any stuck in_progress remainder.
        print(
            f"edc.triage: auto-delegate for {decision_id} failed ({label}); "
            f"status {status!r} left for the reconcile sweep",
            file=sys.stderr,
        )
    else:
        print(
            f"edc.triage: auto-delegate for {decision_id} failed ({label}); "
            "re-surfaced to owner for manual handling",
            file=sys.stderr,
        )


def decisions_email_source() -> str:
    """The email adapter's source name (kept in one place for the watermark test)."""
    return EmailAdapter.name


def default_adapters(
    cfg: StewardConfig | None = None, *, now: datetime | None = None
) -> list[SourceAdapter]:
    """The source adapters a production tick runs, honouring the rollout flags.

    Email is unconditional. The session source is constructed ONLY when
    ``OMNIAGENTOS_EDC_SESSIONS`` is truthy (default OFF) — with the flag down the
    adapter object never exists, so the sessions table is not even read and
    triage behaves exactly as it did before this source landed.
    """
    adapters: list[SourceAdapter] = [EmailAdapter()]
    if sessions_source_enabled():
        adapters.append(SessionAdapter(config=cfg, now=now))
    return adapters


def run_triage(
    base_store: SqliteStore,
    *,
    cfg: StewardConfig | None = None,
    now: datetime | None = None,
    notifier: _Notifier | None = None,
    slack_reverse: dict[str, str] | None = None,
    adapters: list[SourceAdapter] | None = None,
    llm_client: _CycleJsonClient | None = None,
) -> dict[str, int]:
    """One triage sweep across all source adapters. Returns per-class counts."""
    cfg = cfg or load_steward_config()
    now = now or datetime.now(UTC)
    decisions = DecisionStore(base_store)
    adapters = adapters if adapters is not None else default_adapters(cfg, now=now)
    cycle_client = llm_client or _CycleJsonClient(cfg.edc.llm_max_per_cycle)
    stats = {
        "seen": 0,
        "created": 0,
        "duplicate": 0,
        "urgent": 0,
        "needs_owner": 0,
        "maybe": 0,
        "ignored": 0,
        "other": 0,
        "dm_sent": 0,
        "auto_delegated": 0,
        "auto_delegate_reverted": 0,
    }
    rules_cache: dict[str, list[dict[str, Any]]] = {}
    for adapter in adapters:
        # An adapter may carry its own deterministic verdict; absent one, the
        # shared classify (harm rules → bounded LLM ambiguity) runs as before.
        adapter_classifier = getattr(adapter, "classify_event", None)
        for event in adapter.pending_events(decisions):
            owner = event["owner_employee_id"]
            if owner not in rules_cache:
                rules_cache[owner] = decisions.list_rules(owner_employee_id=owner, state="active")
            _ingest_event(
                event,
                decisions,
                owner_rules=rules_cache[owner],
                cfg=cfg,
                now=now,
                notifier=notifier,
                slack_reverse=slack_reverse,
                stats=stats,
                llm_client=cycle_client,
                classify_event=adapter_classifier,
            )
    stats["llm_calls"] = cycle_client.used
    return stats


def run_reclassify(
    base_store: SqliteStore,
    *,
    cfg: StewardConfig | None = None,
    now: datetime | None = None,
    notifier: _Notifier | None = None,
    slack_reverse: dict[str, str] | None = None,
    llm_client: _CycleJsonClient | None = None,
) -> dict[str, int]:
    """Re-run classify on stale MAYBE / ``llm_unavailable`` rows (CODEX F06).

    A MAYBE (especially one fail-closed to ``classifier='llm_unavailable'``
    during an LLM outage) is not frozen: this pass rebuilds the source event and
    re-classifies in place via ``reclassify_decision``. A row promoted to URGENT
    here is surfaced like any other. Deterministic and side-effect-free to run:
    an unchanged verdict is a no-op.
    """
    from omniagentos.steward.store import StewardStore

    cfg = cfg or load_steward_config()
    now = now or datetime.now(UTC)
    decisions = DecisionStore(base_store)
    steward = StewardStore(base_store)
    accounts = accounts_map(cfg.edc)
    owners = sorted({owner.owner_employee_id for owner in accounts.values()})
    cycle_client = llm_client or _CycleJsonClient(cfg.edc.llm_max_per_cycle)

    stats = {"seen": 0, "reclassified": 0, "promoted": 0, "dm_sent": 0}
    for owner in owners:
        rules = decisions.list_rules(owner_employee_id=owner, state="active")
        stale = [
            decision
            for decision in decisions.list_decisions(owner_employee_id=owner, status="open")
            if decision.get("classification") == "maybe"
            or decision.get("classifier") == "llm_unavailable"
        ]
        for decision in stale:
            stats["seen"] += 1
            if decision.get("source") != EmailAdapter.name:
                continue
            source_ref = str(decision.get("source_ref") or "")
            if not source_ref.isdigit():
                continue
            row = steward.get_comms_message(int(source_ref))
            if row is None:
                continue
            event = event_from_comms_row(row, accounts)
            if event is None:
                continue
            verdict = classify(
                event,
                owner_rules=rules,
                cfg=cfg.alerts,
                now=now,
                llm_client=cycle_client,
                credible_domains=cfg.edc.credible_sender_domains,
            )
            changed = (
                verdict["classification"] != decision.get("classification")
                or decision.get("classifier") == "llm_unavailable"
            )
            if not changed:
                continue
            updated = decisions.reclassify_decision(
                decision["id"],
                owner_employee_id=owner,
                fields={
                    "classification": verdict["classification"],
                    "recommended": verdict["recommended"],
                    "classifier": verdict["classifier"],
                    "confidence": verdict["confidence"],
                    "reason": verdict["reason"],
                    "consequence": verdict["consequence"],
                    "deadline_at": verdict["deadline_at"],
                    "likelihood": verdict["likelihood"],
                    "available_actions": verdict["available_actions"],
                    "rule_matches": verdict["rule_matches"],
                    "surfaced": 0,
                },
                actor="system",
                note=f"reclassify {decision.get('classification')} -> {verdict['classification']}",
            )
            if updated is None:
                continue
            stats["reclassified"] += 1
            if verdict["classification"] == "urgent":
                stats["promoted"] += 1
                if _surface_urgent(updated, decisions, owner, notifier, slack_reverse):
                    stats["dm_sent"] += 1
    stats["llm_calls"] = cycle_client.used
    return stats


#: A decision stuck ``in_progress`` past this many minutes is presumed to have
#: crashed mid-dispatch (neither success nor a persisted recovery state) and is
#: routed to ``reconcile_required`` — a human decides; it is NEVER auto-resent.
_STALE_IN_PROGRESS_MINUTES = 15


def run_reconcile_sweep(
    base_store: SqliteStore,
    owner_employee_ids: list[str],
    *,
    now: datetime | None = None,
    threshold_minutes: int = _STALE_IN_PROGRESS_MINUTES,
) -> dict[str, int]:
    """Route decisions stuck ``in_progress`` past the threshold to reconcile.

    Closes the F03 gap where a crash between the CAS and the effect's recovery
    write would strand a decision in ``in_progress`` forever. The sweep NEVER
    re-drives the effect — an ambiguous in-flight action is human-reconciled.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = (moment - timedelta(minutes=threshold_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    decisions = DecisionStore(base_store)
    routed = 0
    for owner in owner_employee_ids:
        routed += len(decisions.route_stale_in_progress(owner_employee_id=owner, cutoff=cutoff))
    return {"reconcile_routed": routed}


def run_session_sweep(
    base_store: SqliteStore,
    *,
    adapter: SessionAdapter | None = None,
    cfg: StewardConfig | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Expire session suggestions whose condition cleared (no-op when flag-off).

    Runs in the same tick as triage, right after it, so a session that answered
    between two ticks loses its card instead of leaving the operator a suggestion about a
    state that no longer exists. Purely a status transition on ``decisions`` — it
    writes nothing to the session and sends nothing anywhere.
    """
    if adapter is None:
        if not sessions_source_enabled():
            return {
                "session_expired": 0,
                "session_revived": 0,
                "session_refreshed": 0,
                "session_expiry_skipped": 0,
            }
        adapter = SessionAdapter(config=cfg)
    decisions = DecisionStore(base_store)
    return sweep_cleared_session_decisions(
        decisions,
        snapshot=adapter.live_snapshot(decisions, now=now),
        owner_employee_id=adapter.owner_employee_id,
    )


def _build_notifier(dry_run: bool) -> tuple[_Notifier | None, dict[str, str]]:
    """A SlackNotifier + reverse (employee → slack) map, or a dry-run sink."""
    from omniagentos.team.decisions import load_slack_env, load_slack_map
    from omniagentos.team.notify import _DryRunNotifier, _reverse_slack_map

    reverse = _reverse_slack_map(load_slack_map())
    if dry_run:
        return _DryRunNotifier(), reverse
    load_slack_env()
    import os

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("edc.triage: no SLACK_BOT_TOKEN — URGENT DMs will not be sent", file=sys.stderr)
        return None, reverse
    from omniagentos.team.notify import SlackNotifier

    return SlackNotifier(token), reverse


def _completion_wq_store() -> Any:
    """A ``WorkQueueStore`` for defer-to-machine outcomes, or ``None``.

    The workqueue is its OWN sqlite file (``WQ_DB``); the completion sweep only
    needs it when a decision was deferred to a machine unit. Absent config is not
    fatal — those decisions simply wait for the next tick that has it.
    """
    import os

    db = os.environ.get("WQ_DB")
    if not db:
        return None
    from omniagentos.workqueue.store import WorkQueueStore

    return WorkQueueStore(db)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Executive Decision Center triage tick.")
    parser.add_argument("--db", default=None, help="control-plane database path")
    parser.add_argument(
        "--dry-run", action="store_true", help="print DMs instead of posting to Slack"
    )
    parser.add_argument(
        "--reclassify-only",
        action="store_true",
        help="run only the stale-MAYBE reclassify pass (F06)",
    )
    args = parser.parse_args(argv)

    base_store = SqliteStore(args.db or default_db_path())
    cfg = load_steward_config()
    notifier, slack_reverse = _build_notifier(args.dry_run)
    llm_client = _CycleJsonClient(cfg.edc.llm_max_per_cycle)

    # One-shot, idempotent: seed emp_owner's classify_hint rules from steward.yaml
    # (VIP senders / urgent patterns) before the first classify consults them.
    from omniagentos.edc.rules import seed_bootstrap_rules

    seed_bootstrap_rules(DecisionStore(base_store), cfg)

    triage_stats = (
        {}
        if args.reclassify_only
        else run_triage(
            base_store,
            cfg=cfg,
            notifier=notifier,
            slack_reverse=slack_reverse,
            llm_client=llm_client,
        )
    )
    reclassify_stats = run_reclassify(
        base_store,
        cfg=cfg,
        notifier=notifier,
        slack_reverse=slack_reverse,
        llm_client=llm_client,
    )
    owner_ids = sorted({binding.owner_employee_id for binding in accounts_map(cfg.edc).values()})
    snooze_stats = sweep_snoozes(
        DecisionStore(base_store),
        owner_ids,
        notifier=notifier,
        slack_reverse=slack_reverse,
    )
    reconcile_stats = run_reconcile_sweep(base_store, owner_ids)
    # Flag-gated (default OFF) and a pure no-op when down: retires session
    # suggestions whose condition cleared since the last tick.
    session_stats = run_session_sweep(base_store, cfg=cfg)
    completion_stats = run_completion_sweep(
        base_store,
        owner_ids,
        notifier=notifier,
        slack_reverse=slack_reverse,
        wq_store=_completion_wq_store(),
    )
    # Nightly learner (idempotent per tick): cluster each owner's decided history
    # and file/refresh pre-fill rule PROPOSALS for approval (never automation).
    from omniagentos.edc.learn import run_learning

    learn_stats = run_learning(base_store, owner_ids)
    print(
        json.dumps(
            {
                "triage": triage_stats,
                "reclassify": reclassify_stats,
                "snooze": snooze_stats,
                "reconcile": reconcile_stats,
                "sessions": session_stats,
                "completion": completion_stats,
                "learn": learn_stats,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
