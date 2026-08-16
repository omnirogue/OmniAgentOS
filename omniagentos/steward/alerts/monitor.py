"""Cooldown-aware alert monitor runnable as ``python -m ...monitor --once``."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos import runtime_paths
from omniagentos.contracts import Events, default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.grants.store import GrantsStore
from omniagentos.intake.board_sweep import goal_limbo_candidates
from omniagentos.notifications.service import notify_alert
from omniagentos.steward.alerts.rules import (
    AlertCandidate,
    borderline_urgent,
    goal_limbo,
    payment_failure_burst,
    reliability_deadman,
    revenue_drop,
    roas_floor,
    spend_spike,
    spend_spike_intraday,
    vip_urgent,
)
from omniagentos.steward.alerts.triage import triage_message
from omniagentos.steward.config import StewardConfig, load_steward_config
from omniagentos.steward.notify import NotifyResult, send_piedpiper_email, send_slack
from omniagentos.steward.policy import expired_suggestion_ids, stale_alert_ids
from omniagentos.steward.quoting import quote_untrusted
from omniagentos.steward.store import StewardStore

logger = logging.getLogger(__name__)

# Capability TOKENS that mark a grant as spend-path related. Matched as a
# whole segment after splitting the capability string on ".", "_", "-" —
# NOT a bare substring (a bare-substring match previously caught unrelated
# capabilities like "metadata.read", "system.uploads", and
# "forum.threads.delete", none of which are spend/ads/meta paths).
_SPEND_CAPABILITY_TOKENS = frozenset({"spend", "ads", "meta"})
_CAPABILITY_TOKEN_SPLIT_RE = re.compile(r"[._\-]+")


def _is_spend_capability(capability: str) -> bool:
    """True if any whole "."/"_"/"-"-delimited segment is a spend-path token.

    Whole-segment matching (not substring) so "meta_acmeuni.budget_change" and
    "ads.pause_campaign" match while "metadata.read", "system.uploads", and
    "forum.threads.delete" do not.
    """
    tokens = _CAPABILITY_TOKEN_SPLIT_RE.split(capability.casefold())
    return any(token in _SPEND_CAPABILITY_TOKENS for token in tokens)
_SPEND_BREAKER_RULES = frozenset({"spend_spike", "spend_spike_intraday"})
_SPEND_BREAKER_SEVERITIES = frozenset({"high", "critical"})

# Money-channel split: payment_failures/roas_floor/revenue_drop are customer-
# money conditions and previously shared ONE Slack sink (send_slack(text) with
# no webhook_env) with goal_limbo, an internal loop-integrity rule that is
# 1,713 of 1,806 alert rows (94.85%) in a channel where nothing has ever been
# acked (acked_at IS NULL on all 1,806 rows, verified 2026-08-13). Money
# conditions were firing (roas_floor/payment_failures both carried
# occurrence_count=804 the same day) but arriving buried. MONEY_RULES is a
# fixed set of rule NAMES, deliberately not inferred from severity: goal_limbo
# also emits "high" severity, so a severity-based split would put loop noise
# right back in the money channel.
MONEY_RULES = frozenset({"payment_failures", "roas_floor", "revenue_drop"})
_MONEY_WEBHOOK_ENV = "MONEY_ALERT_SLACK_WEBHOOK_URL"
_DEFAULT_WEBHOOK_ENV = "SLACK_WEBHOOK_URL"


def _webhook_env_for_rule(rule: str) -> str:
    """Select the Slack webhook env var a rule's notifications resolve.

    Every non-money rule (including goal_limbo) keeps the existing shared
    ``SLACK_WEBHOOK_URL`` channel unchanged. A money rule resolves its own
    dedicated ``MONEY_ALERT_SLACK_WEBHOOK_URL`` channel instead -- this is the
    env var name every "unconfigured channel" check below must also read, so
    an unarmed money split (env not yet set by the operator) is visible as a
    warning rather than silently reading as "configured" off the shared var.
    """
    return _MONEY_WEBHOOK_ENV if rule in MONEY_RULES else _DEFAULT_WEBHOOK_ENV

REMEDIATIONS: dict[str, dict[str, str]] = {
    "roas_floor": {
        "title": "Review underperforming ad sets",
        "risk_class": "read_only",
        "proposed_plan": (
            "Analyze yesterday's Meta ad performance and produce a ranked list of ad sets to "
            "pause or rebalance, with reasoning. Do not change anything."
        ),
    },
    "payment_failures": {
        "title": "Investigate payment failures",
        "risk_class": "read_only",
        "proposed_plan": "List yesterday's failed Stripe charges with decline codes and suggest recovery actions.",
    },
}

Summary = dict[str, int]
TriageFunction = Callable[[dict[str, Any], Any], dict[str, bool | str]]


def _message_time(message: dict[str, Any]) -> datetime | None:
    value = str(message.get("sent_at") or "")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _recent_messages(steward: StewardStore, now: datetime) -> list[dict[str, Any]]:
    cutoff = now - timedelta(hours=24)
    return [
        message
        for message in steward.list_comms_messages(limit=500)
        if (sent_at := _message_time(message)) is not None and sent_at >= cutoff
    ]


def _notify(candidate: AlertCandidate, cfg: StewardConfig) -> list[NotifyResult]:
    """Send every configured channel and return EACH channel's outcome.

    M3/PROD-004: the caller previously discarded these results, so an alert
    whose only configured channel silently failed (or where no channel was
    ever configured) reached nobody with no trace of that fact anywhere.
    """
    text = f"[{candidate.severity.upper()}] {candidate.title}\n{candidate.body}"
    webhook_env = _webhook_env_for_rule(candidate.rule)
    send_env = webhook_env
    if webhook_env == _MONEY_WEBHOOK_ENV and not os.environ.get(_MONEY_WEBHOOK_ENV):
        # Fail-visible fallback, never fail-silent: the operator has not yet
        # created the dedicated money webhook, so keep delivering the money
        # alert through the shared channel rather than dropping it. The
        # unconfigured-channel check in _persist_candidate reads the
        # SELECTED (money) env, not this fallback, so the run stays loud
        # about the split being unarmed even while delivery still succeeds.
        send_env = _DEFAULT_WEBHOOK_ENV
    results = [send_slack(text, webhook_env=send_env)]
    briefing = cfg.briefing
    deliver_email = (
        briefing.get("deliver_email") if isinstance(briefing, dict) else briefing.deliver_email
    )
    if candidate.severity == "critical" and deliver_email:
        results.append(
            send_piedpiper_email(
                deliver_email,
                candidate.title,
                f"<pre>{html.escape(text)}</pre>",
            )
        )
    return results


def _breaker_state_path() -> Path:
    """Resolve the spend circuit-breaker state file under the runtime var root.

    Mirrors chokepoint's ``_get_ledger_path`` so monitor (trip) and chokepoint
    (enforce) share one on-disk location that respects
    ``OMNIAGENTOS_VAR_DIR`` / ``OMNIAGENTOS_VAR`` and stays test-isolated.
    """
    return runtime_paths.resolve_var_root(
        env_keys=runtime_paths.TOKEN_VAR_ENV_KEYS,
        leaf=("spend-breaker-state.json",),
    )


def _is_breaker_currently_tripped() -> bool:
    """Best-effort read used ONLY to decide whether a re-trip is redundant.

    F2 re-arm fix: without this check, a spend-breaker candidate whose
    cooldown_key was already suppressed by ``create_alert`` (cooldown_minutes
    still running) never reached the trip call at all -- so an operator (or
    anyone) clearing the breaker while the underlying spike was STILL LIVE
    left it cleared until the cooldown window lapsed. The caller now checks
    this BEFORE the cooldown-gated alert path, and trips unconditionally
    whenever it reads False, which re-arms even mid-cooldown.

    Deliberately loose, NOT the fail-safe reader: any read failure (missing
    var root, corrupt file, race with a concurrent write) returns False here,
    biasing toward one extra (harmless, idempotent) trip rather than toward
    skipping a trip. chokepoint.py's ``_read_breaker_state`` is the separate,
    stricter fail-CLOSED reader that governs actual request enforcement; this
    one only governs whether ``_trip_spend_breaker`` bothers running again.
    """
    try:
        path = _breaker_state_path()
    except runtime_paths.RuntimePathError:
        return False
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("tripped"))


def _write_breaker_state(candidate: AlertCandidate) -> Path | None:
    """Idempotently write the fail-safe breaker state file. Returns path or None."""
    payload = {
        "tripped": True,
        "reason": candidate.title,
        "rule": candidate.rule,
        "tripped_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "evidence": candidate.evidence,
    }
    try:
        path = _breaker_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        # F4: write to a temp file in the SAME directory, then os.replace() onto
        # the final path. os.replace is atomic on POSIX and Windows, so a
        # concurrent chokepoint read never observes a partially-written file
        # (which would otherwise raise JSONDecodeError and, per the fail-closed
        # contract, spuriously 503 unrelated requests mid-write).
        fd, tmp_name = tempfile.mkstemp(
            prefix=".spend-breaker-state.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(tmp_name, path)
        finally:
            # If os.replace already consumed tmp_name this is a no-op miss;
            # only cleans up a leftover temp file on an earlier failure.
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return path
    except Exception:  # noqa: BLE001 - state write must never abort the monitor cycle
        logger.exception(
            "Failed to write spend-breaker state file for rule=%s", candidate.rule
        )
        return None


def _revoke_spend_grants(database: SqliteStore, *, reason: str) -> list[str]:
    """Revoke active grants whose capability looks spend/ads/meta-related.

    Missing or uninitialized grants tables are logged and ignored — the
    breaker state file is the hard fail-safe signal chokepoint reads; grant
    revoke is best-effort enforcement on top. Revoking twice is a no-op
    (``revoke_grant`` re-sets the same columns).
    """
    revoked: list[str] = []
    try:
        grants = GrantsStore(database)
        active = grants.list_active_grants()
    except Exception:  # noqa: BLE001 - grants DB may be absent in some envs
        logger.exception("Spend breaker: could not list active grants; continuing")
        return revoked
    for grant in active:
        capability = str(grant.get("capability") or "")
        if not _is_spend_capability(capability):
            continue
        grant_id = str(grant.get("id") or "")
        if not grant_id:
            continue
        try:
            grants.revoke_grant(grant_id, reason=reason)
            revoked.append(grant_id)
        except Exception:  # noqa: BLE001 - one bad grant must not block others
            logger.exception(
                "Spend breaker: failed to revoke grant %s", grant_id
            )
    return revoked


def _capability_is_provably_read_only(capability: str) -> bool:
    """True only when the connector registry PROVES this capability cannot write.

    "Proves" means: the capability exists, it has a reviewed HTTP spec, that spec
    names at least one method, and EVERY method it names is a read method. Any
    other answer -- unknown capability, no reviewed call path, an empty method
    list, an unreadable registry -- is not a proof, so the caller must treat the
    grant as write-capable and revoke it.
    """
    from omniagentos.connectors import load_registry
    from omniagentos.connectors.broker import READ_METHODS

    spec = load_registry().capability(capability).http
    if spec is None:
        return False
    methods = {str(method).upper() for method in (spec.methods or [])}
    return bool(methods) and methods <= READ_METHODS


def _standing_grant_is_read_only(mode: str, capability: str) -> bool:
    """Whether one ``agent_capabilities`` row is safe to LEAVE in place on a trip.

    Fail-CLOSED: a row is spared only when it declares ``mode='read'`` AND the
    registry proves the capability itself cannot write. An unknown/blank/garbled
    mode, a write mode, an unknown capability, or any failure to consult the
    registry all answer False -- i.e. revoke.

    The mode column alone is deliberately NOT trusted as the whole test.
    ``broker.authorize`` only enforces ``mode``/``expires_at`` when the caller
    names an ``agent_id`` (broker.py's lifecycle block); the standing
    ``grant_holder`` path -- which loads the capability list straight out of
    ``agent_capabilities`` -- performs no mode check at all. So "the row says
    read" is a statement of intent, not an enforced ceiling, and it is only
    trustworthy when the capability's own method allowlist agrees.
    """
    if mode.strip().casefold() != "read":
        return False
    try:
        return _capability_is_provably_read_only(capability)
    except Exception:  # noqa: BLE001 - unreadable registry must not spare a grant
        logger.exception(
            "Spend breaker: could not prove %s is read-only; revoking it", capability
        )
        return False


def _revoke_spend_standing_grants(database: SqliteStore, *, reason: str) -> list[str]:
    """Delete standing ``agent_capabilities`` spend grants. Returns what it took.

    Twin of :func:`_revoke_spend_grants`, which is entirely ``campaign_grants``-
    scoped (``GrantsStore.list_active_grants`` / ``revoke_grant`` only ever read
    and write that one table). Without this, the breaker's revoke is sufficient
    only while an invariant holds -- "every ad-spend capability is
    ``consequential`` and therefore funnels through a revocable campaign grant".
    A standing write-mode grant on an ads capability lives in a DIFFERENT table
    that has no ``revoked_at`` column, so the campaign-grants-only revoke missed
    it silently and a TRIPPED breaker still left that spend authorized.

    The row is DELETED, not expired. ``expires_at`` is only enforced on the
    ``agent_id`` lifecycle path; the standing ``grant_holder`` path reads the
    capability list with no expiry filter (``CapabilityStore.get_grant`` is a
    bare ``SELECT capability_id ... WHERE agent_id = ?``), so expiring the row
    would leave it authorizing calls. Deletion is the only form of this revoke
    that is invariant-independent.

    The removal is recorded in the append-only ``capability_grant_log`` WITH the
    row's lifecycle snapshot (mode/expires_at/issued_by/request_id), so an
    operator can see exactly what the breaker took and reissue it deliberately
    once the spike is resolved. A missing/uninitialized table is logged and
    ignored for the same reason as the campaign-grant twin: the breaker state
    file is the hard fail-safe chokepoint reads, and grant revoke is
    best-effort enforcement layered on top. Revoking twice is a no-op.
    """
    revoked: list[str] = []
    try:
        rows = database._connection.execute(
            "SELECT agent_id, capability_id, mode, expires_at, issued_by, request_id "
            "FROM agent_capabilities"
        ).fetchall()
    except Exception:  # noqa: BLE001 - capabilities table may be absent in some envs
        logger.exception(
            "Spend breaker: could not list standing capability grants; continuing"
        )
        return revoked

    for raw in rows:
        row = dict(raw)
        capability = str(row.get("capability_id") or "")
        agent_id = str(row.get("agent_id") or "")
        if not capability or not agent_id:
            continue
        if not _is_spend_capability(capability):
            continue
        if _standing_grant_is_read_only(str(row.get("mode") or ""), capability):
            continue
        try:
            deleted = database._write_count(
                "DELETE FROM agent_capabilities WHERE agent_id = ? AND capability_id = ?",
                (agent_id, capability),
            )
        except Exception:  # noqa: BLE001 - one bad row must not block the others
            logger.exception(
                "Spend breaker: failed to revoke standing grant %s/%s", agent_id, capability
            )
            continue
        if not deleted:
            continue
        revoked.append(f"{agent_id}::{capability}")
        try:
            database._write(
                "INSERT INTO capability_grant_log "
                "(agent_id, capability_id, action, action_class, actor, note, ts, "
                "mode, expires_at, issued_by, request_id) "
                "VALUES (?, ?, 'revoke', '', 'spend-breaker', ?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    capability,
                    reason,
                    utc_now_iso(),
                    row.get("mode"),
                    row.get("expires_at"),
                    row.get("issued_by"),
                    row.get("request_id"),
                ),
            )
        except Exception:  # noqa: BLE001 - the revoke stands even if the audit fails
            logger.exception(
                "Spend breaker: failed to log standing revoke %s/%s", agent_id, capability
            )
    return revoked


def _trip_spend_breaker(
    candidate: AlertCandidate,
    *,
    database: SqliteStore,
) -> None:
    """Pause spend on a confirmed spike: state file first, then grant revoke.

    Ordering is intentional and non-negotiable for the deadman contract:

    1. Write ``spend-breaker-state.json`` (idempotent overwrite) — this is what
       chokepoint reads to refuse state-changing requests. A notify/Slack
       outage MUST NOT skip this step; callers invoke us before ``_notify``.
    2. Revoke active spend/ads/meta grants (idempotent; missing grants DB is
       non-fatal once the state file is written). BOTH grant carriers are
       revoked: the bounded ``campaign_grants`` rows AND the standing
       ``agent_capabilities`` rows. Revoking only the former made the breaker
       sufficient just while "every ad-spend capability is consequential, so it
       can only be exercised through a campaign grant" happened to hold; a
       standing write-mode grant on an ads capability broke that invariant and
       survived the trip. Neither revoke may skip the other.
    3. Record a durable ``spend_breaker_tripped`` event for audit visibility.

    Safe to call twice for the same incident: rewrites state, re-attempts
    revoke (already-revoked grants stay revoked, already-deleted standing rows
    are simply absent), inserts another event.
    """
    reason = f"spend circuit breaker: {candidate.rule}"
    state_path = _write_breaker_state(candidate)
    revoked = _revoke_spend_grants(database, reason=reason)
    revoked_standing = _revoke_spend_standing_grants(database, reason=reason)
    try:
        database.insert_event(
            "spend_breaker_tripped",
            "steward-alerts",
            "tripped",
            target_type="breaker",
            target_id=candidate.rule,
            payload={
                "rule": candidate.rule,
                "severity": candidate.severity,
                "reason": reason,
                "state_path": str(state_path) if state_path is not None else None,
                "revoked_grant_ids": revoked,
                "revoked_standing_grants": revoked_standing,
                "evidence": candidate.evidence,
            },
        )
    except Exception:  # noqa: BLE001 - audit failure must not undo the trip
        logger.exception(
            "Spend breaker: failed to insert trip event for rule=%s", candidate.rule
        )


def _persist_candidate(
    candidate: AlertCandidate,
    *,
    steward: StewardStore,
    database: SqliteStore,
    cfg: StewardConfig,
) -> bool:
    # RR-PROD-002: whether any critical-capable channel is even CONFIGURED is
    # knowable before the send, so stamp that onto the alert ROW's own evidence
    # (durable + queryable + dashboard-visible) — not just the audit event. This
    # closes the "high/critical alert reached nobody and looks identical to a
    # delivered one" gap for the misconfiguration case (no channel at all).
    alert_evidence = dict(candidate.evidence)
    briefing_cfg = cfg.briefing
    deliver_email_cfg = (
        briefing_cfg.get("deliver_email")
        if isinstance(briefing_cfg, dict)
        else briefing_cfg.deliver_email
    )
    # Email only fires for critical (see _notify), so a HIGH alert with Slack unset
    # + email configured still reaches nobody — count email as a channel only for
    # the severity that can actually use it (RR-PROD-002 residual).
    # Money-channel split: read the SELECTED webhook env for this candidate's
    # rule (MONEY_ALERT_SLACK_WEBHOOK_URL for a money rule, SLACK_WEBHOOK_URL
    # otherwise), not always the default. An unarmed money split (money env
    # unset) must warn here even though _notify still falls back to the
    # shared channel for actual delivery — an absent money webhook must never
    # silently read as "configured" via the shared var.
    webhook_env = _webhook_env_for_rule(candidate.rule)
    has_channel = bool(os.environ.get(webhook_env)) or (
        candidate.severity == "critical" and bool(deliver_email_cfg)
    )
    if candidate.severity in {"high", "critical"} and not has_channel:
        alert_evidence["delivery_warning"] = "no critical-capable channel configured"
    # Spend circuit breaker: evaluate & trip BEFORE the cooldown-gated
    # create_alert call below, and BEFORE notify. Two independent reasons for
    # each ordering choice:
    #
    #  * Before create_alert (F2 re-arm fix): create_alert returns None when
    #    this candidate's cooldown_key is still cooling down from a PRIOR
    #    firing -- that must never silently skip enforcement while the
    #    underlying spike is still live. Gating trip behind "alert is not
    #    None" was the re-arm gap: reset the breaker while a cooldown-
    #    suppressed spike persists, and it stayed cleared until the cooldown
    #    window lapsed on its own.
    #  * Before notify (deadman, unchanged from round 1): a Slack/email
    #    outage must not defeat enforcement either.
    #
    # Idempotency guard: only trip when the breaker is NOT already tripped,
    # so a persisting spike does not rewrite state / re-attempt grant revoke
    # every single cycle. If the breaker gets cleared (via the authenticated
    # reset route) while the spike persists, the NEXT candidate for this rule
    # reads not-tripped and re-arms it -- this is the re-arm path FIX 2 adds.
    if (
        candidate.rule in _SPEND_BREAKER_RULES
        and candidate.severity in _SPEND_BREAKER_SEVERITIES
        and not _is_breaker_currently_tripped()
    ):
        try:
            _trip_spend_breaker(candidate, database=database)
        except Exception:  # noqa: BLE001 - trip defects must not drop the alert
            logger.exception(
                "Spend breaker trip raised for rule=%s; continuing to persist/notify",
                candidate.rule,
            )
    alert = steward.create_alert(
        {
            "rule": candidate.rule,
            "severity": candidate.severity,
            "title": candidate.title,
            "body": candidate.body,
            "evidence": alert_evidence,
            "cooldown_key": candidate.cooldown_key,
            "cooldown_minutes": cfg.alerts.cooldown_minutes,
            "magnitude": candidate.magnitude,
        }
    )
    if alert is None:
        return False
    alert_id = str(alert["id"])
    results = _notify(candidate, cfg)
    delivery = [
        {"channel": result.channel, "ok": result.ok, "detail": result.detail} for result in results
    ]
    undelivered = candidate.severity in {"high", "critical"} and not any(
        result.ok for result in results
    )
    # M3/PROD-004: store.create_alert (Wave A, frozen) has no update path for
    # an already-persisted alert's evidence_json, so the delivery outcome is
    # recorded on the SAME alert-creation audit event instead -- still keyed
    # exactly like the alert's own evidence would be (evidence["delivery"] /
    # evidence["undelivered"]) so a critical alert that reached nobody is
    # visibly recorded (queryable via the events table / SSE feed), never
    # silently dropped. A WARNING is also logged for anyone tailing logs.
    event_evidence = {**candidate.evidence, "delivery": delivery}
    if undelivered:
        event_evidence["undelivered"] = True
        logger.warning(
            "Alert %s (rule=%s, severity=%s) reached no delivery channel: %s",
            alert_id,
            candidate.rule,
            candidate.severity,
            delivery,
        )
    database.insert_event(
        Events.ALERT_CREATED,
        "steward-alerts",
        "created",
        target_type="alert",
        target_id=alert_id,
        payload={
            "rule": candidate.rule,
            "severity": candidate.severity,
            "evidence": event_evidence,
        },
    )
    remediation = REMEDIATIONS.get(candidate.rule)
    if remediation is not None:
        # SUGGESTION DEDUPE: collapse repeat firings of the same remediation
        # into one occurrence-counted open row instead of appending forever
        # (council: 71 suggestions collapsed to ~2 repeated titles).
        steward.create_or_bump_suggestion(
            {
                "title": remediation["title"],
                "rationale": f"Suggested by alert rule {candidate.rule}.",
                "evidence": [candidate.evidence],
                "proposed_plan": {"prompt": remediation["proposed_plan"]},
                "risk_class": remediation["risk_class"],
                "source": "alerts",
                "alert_id": alert["id"],
            }
        )
    # NT-notify: a HIGH/critical alert becomes a durable notification linked to
    # the alert row (ref_type='alert'), written through the SAME connection the
    # alert was persisted on so it stays isolated with the caller's database.
    if candidate.severity in {"high", "critical"}:
        # goal_limbo (the 94.85%-of-rows loop-integrity rule) still gets its
        # notification row persisted durably (dashboard/API visible), but is
        # muted on the shared ntfy/OPS_ALERT_SLACK_WEBHOOK_URL push transport
        # so it stops drowning out money rules there too -- money rules keep
        # push=True unchanged.
        notify_alert(
            alert_id=alert_id,
            title=candidate.title,
            body=candidate.body,
            severity=candidate.severity,
            rule=candidate.rule,
            connection=getattr(database, "_connection", None),
            push=candidate.rule != "goal_limbo",
        )
    return True


def _triage_alert(message: dict[str, Any], reason: str) -> AlertCandidate:
    content = f"Subject: {message.get('subject') or ''}\n\n{message.get('body_text') or ''}"
    return AlertCandidate(
        rule="llm_triage",
        severity="high",
        title="Urgent message identified by triage",
        body=quote_untrusted(
            content,
            source=f"comms:{message.get('source') or 'unknown'}",
            max_chars=1000,
        ),
        evidence={"message_id": message.get("id"), "triage_reason": reason},
        cooldown_key=f"triage:{message.get('id') or message.get('external_id') or 'unknown'}",
    )


def _default_triaged_marker_path() -> Path:
    """Where the "already triaged" message-id marker lives by default.

    H6/PERF-001/SEC-O-005: store.py is RA-owned/frozen and has no column to
    record "this comms message has already been triaged" -- so it is tracked
    entirely outside the database, as a small JSON file living alongside the
    Steward sqlite db (i.e. the SAME directory contracts.default_db_path()
    resolves, so it follows OMNIAGENTOS_DB / the repo-root default exactly
    like the db itself does -- council findings INT-002/INT-003 on
    cwd-relative paths causing silent split-brain).
    """
    return Path(os.path.dirname(default_db_path())) / "steward-triaged.json"


def _load_triaged_ids(path: Path) -> set[str]:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw}


def _save_triaged_ids(path: Path, ids: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    except OSError:
        logger.warning("Failed to persist triaged-message marker file at %s", path)


def _goal_limbo_rows(database: SqliteStore, *, now: datetime) -> list[dict[str, Any]]:
    """Board-task limbo candidates for this cycle, sharing ``database``'s db path.

    Constructed lazily (no module-level CollabStore) so this stays a normal
    SqliteStore-backed connection like every other steward read here, and so
    a database that has never run the collab migrations (an older/minimal
    test db) fails INSIDE the isolated rule evaluation below rather than at
    import time.
    """
    from omniagentos.collab.store import CollabStore
    from omniagentos.intake.service import _sqlite_db_path

    collab = CollabStore(_sqlite_db_path(database))
    return goal_limbo_candidates(collab, now=now)


def _reliability_liveness(
    database: SqliteStore,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Fetch the reliability watch cursor + newest audit row for the dead-man rule.

    Each read is isolated. A missing row remains ``None``; a failed read becomes
    an explicit ``_state=store_error`` sentinel consumed by the dead-man rule.
    """
    try:
        cursor_row = database._connection.execute(
            "SELECT key, value_json, updated_at FROM reliability_state WHERE key = 'watch_cursor'"
        ).fetchone()
        watch_cursor = dict(cursor_row) if cursor_row else None
    except Exception as exc:  # noqa: BLE001 - failure is an explicit dead-man state
        watch_cursor = {
            "_state": "store_error",
            "error_type": type(exc).__name__,
        }

    try:
        audit_row = database._connection.execute(
            "SELECT id, kind, status, started_at FROM reliability_audits"
            " WHERE kind IN ('twice_daily', 'on_demand') ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        latest_audit = dict(audit_row) if audit_row else None
    except Exception as exc:  # noqa: BLE001 - failure is an explicit dead-man state
        latest_audit = {
            "_state": "store_error",
            "error_type": type(exc).__name__,
        }
    return watch_cursor, latest_audit


def _isolated_rule_candidates(
    rules: list[tuple[str, Callable[[], list[AlertCandidate]]]],
) -> tuple[list[AlertCandidate], dict[str, list[str]]]:
    """Evaluate each rule independently so one defect cannot blind the cycle.

    Returns the flattened candidates AND, per rule that completed WITHOUT
    raising, the cooldown_keys it reported this cycle. The second value drives
    AUTO-RESOLVE: a rule's open case whose key it did NOT report this cycle
    has recovered. A rule that raised is left out of the mapping entirely so a
    transient failure can never be misread as "the condition cleared".
    """
    candidates: list[AlertCandidate] = []
    ran_keys: dict[str, list[str]] = {}
    for name, evaluate in rules:
        try:
            result = evaluate()
        except Exception:  # noqa: BLE001 - isolation is the dead-man contract
            logger.exception("Alert rule %s failed; continuing with other rules", name)
            continue
        candidates.extend(result)
        ran_keys[name] = [candidate.cooldown_key for candidate in result]
    return candidates, ran_keys


def _auto_resolve_recovered(
    steward: StewardStore, ran_keys: dict[str, list[str]], *, now: datetime
) -> int:
    """Close any OPEN case whose rule ran this cycle but no longer reports its key.

    Case identity (``store.create_alert``) stops new rows piling up while a
    condition stays active; this is the other half -- once the condition
    itself clears, the still-open row from the last time it fired would
    otherwise sit open forever (the resolved-side twin of the reported
    "82 opens, 3 keys" symptom). Only rules that completed this cycle (present
    in ``ran_keys``) are eligible, so a rule that merely failed to evaluate
    can never look like "recovered".
    """
    resolved_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved = 0
    for row in steward.open_alerts():
        rule_name = row.get("rule")
        if rule_name not in ran_keys:
            continue
        if row.get("cooldown_key") in set(ran_keys[rule_name]):
            continue
        if steward.resolve_alert(row["id"], reason="recovered", resolved_at=resolved_at):
            resolved += 1
    return resolved


def _resolve_disabled_rule_cases(
    steward: StewardStore, configured: set[str], *, now: datetime
) -> int:
    """Close any OPEN case owned by a rule the cycle no longer evaluates at all.

    THE DISTINCTION, and it is the whole point: a rule that is present but
    CRASHED is in ``configured`` (it was attempted) and keeps its open cases --
    a broken evaluator must never look like a cleared condition. A rule that is
    ABSENT -- disabled or deleted from the rule set, or a rule name from an
    older version that still owns rows -- is never going to report its key
    again, so ``_auto_resolve_recovered`` (which requires the rule to have run)
    can never close its cases and they sit open forever, until the 14-day stale
    sweep eventually ages them out as backlog they were never part of.

    Resolved as ``rule_disabled``, distinct from both ``recovered`` (the
    condition cleared) and ``stale_backlog`` (nobody ever looked): the reason a
    case closed is the only record of what actually happened to it.
    """
    resolved_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved = 0
    for row in steward.open_alerts():
        if str(row.get("rule") or "") in configured:
            continue
        if steward.resolve_alert(row["id"], reason="rule_disabled", resolved_at=resolved_at):
            resolved += 1
    return resolved


def _sweep_backlog(steward: StewardStore, *, now: datetime) -> tuple[int, int]:
    """BACKLOG POLICY: age out cases nothing ever resolved or decided.

    An open alert with no new occurrence in > policy.ALERT_STALE_DAYS closes
    as stale (``evidence["_case"]["resolved_reason"] == "stale_backlog"``,
    distinct from ``"recovered"``); an undecided suggestion older than
    policy.SUGGESTION_EXPIRE_DAYS auto-expires into its own terminal state
    (``"expired"``), distinct from a human decision (approved/rejected/
    dismissed/...). Runs every cycle (one-shot ``--once`` and the periodic
    loop share this same entry point) -- cheap when there is nothing to sweep.
    The aging RULE itself is a pure function in ``steward.policy`` with its
    own unit tests; this is just the read+write wiring.
    """
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved = 0
    for alert_id in stale_alert_ids(steward.open_alerts(), now=now):
        if steward.resolve_alert(alert_id, reason="stale_backlog", resolved_at=now_iso):
            resolved += 1
    expired = 0
    for suggestion_id in expired_suggestion_ids(steward.list_suggestions("open"), now=now):
        if steward.decide_suggestion(
            suggestion_id, state="expired", decided_by="system:policy-sweep"
        ):
            expired += 1
    return resolved, expired


def monitor_once(
    database: SqliteStore,
    *,
    cfg: StewardConfig | None = None,
    now: datetime | None = None,
    triage: TriageFunction = triage_message,
    triaged_marker_path: Path | str | None = None,
) -> Summary:
    """Evaluate one monitor cycle and return deterministic creation counts."""
    config = cfg or load_steward_config()
    current = now or datetime.now(UTC)
    steward = StewardStore(database)
    steward.list_goals()  # Load active context even though these global rules do not target one goal.
    roas = steward.latest_snapshot("meta", "roas")
    failures = steward.latest_snapshot("stripe", "payment_failures")
    spend = steward.snapshot_series("spend_usd", source="meta", days=14)
    revenue = steward.snapshot_series(
        "net_revenue_usd", source="stripe", days=config.alerts.revenue_baseline_days + 3
    )
    messages = _recent_messages(steward, current)

    watch_cursor, latest_audit = _reliability_liveness(database)
    rules: list[tuple[str, Callable[[], list[AlertCandidate]]]] = [
        (
            "roas_floor",
            lambda: roas_floor(
                [row for row in [roas] if row is not None],
                config.alerts,
            ),
        ),
        ("spend_spike", lambda: spend_spike(spend, config.alerts)),
        (
            "spend_spike_intraday",
            lambda: spend_spike_intraday(spend, config.alerts),
        ),
        (
            # The label IS the rule name the candidates carry (rules.py:157) --
            # both auto-resolve and the disabled-rule sweep key an alert row's
            # ``rule`` column against these labels, so a label that named the
            # FUNCTION instead ("payment_failure_burst") silently opted this
            # rule out of auto-resolve and, worse, made every open payment alert
            # look like it came from a rule that no longer exists.
            "payment_failures",
            lambda: payment_failure_burst(
                [row for row in [failures] if row is not None],
                config.alerts,
            ),
        ),
        ("revenue_drop", lambda: revenue_drop(revenue, config.alerts)),
        ("vip_urgent", lambda: vip_urgent(messages, config.alerts)),
        (
            "reliability_deadman",
            lambda: reliability_deadman(
                watch_cursor,
                latest_audit,
                current,
            ),
        ),
        (
            "goal_limbo",
            lambda: goal_limbo(_goal_limbo_rows(database, now=current)),
        ),
    ]
    borderline_rules: list[tuple[str, Callable[[], list[AlertCandidate]]]] = [
        (
            "borderline_urgent",
            lambda: borderline_urgent(messages, config.alerts),
        )
    ]
    candidates, ran_keys = _isolated_rule_candidates(rules)
    borderline, _borderline_keys = _isolated_rule_candidates(borderline_rules)
    # Every rule name this cycle can own an open case under -- ATTEMPTED, not
    # completed, so a rule that raised keeps its cases (see
    # _resolve_disabled_rule_cases). Three sources, deliberately:
    #   * the labels above (a rule that evaluated to nothing is still active),
    #   * the rule name of anything actually raised this cycle, so a label that
    #     ever drifts from its candidates' rule name cannot make a LIVE rule
    #     look retired, and
    #   * ``llm_triage``, written further down this function by _triage_alert:
    #     leaving it out would resolve every open triage alert before the triage
    #     pass had a chance to re-report it.
    configured_rules = {name for name, _ in rules} | {name for name, _ in borderline_rules}
    configured_rules |= {candidate.rule for candidate in candidates}
    configured_rules |= {candidate.rule for candidate in borderline}
    configured_rules.add("llm_triage")
    summary: Summary = {
        "evaluated": len(candidates) + len(borderline),
        "created": 0,
        "suppressed": 0,
        "triaged": 0,
    }
    for candidate in candidates:
        if _persist_candidate(candidate, steward=steward, database=database, cfg=config):
            summary["created"] += 1
        else:
            summary["suppressed"] += 1

    # AUTO-RESOLVE + DISABLED-RULE SWEEP + BACKLOG POLICY SWEEP: none of the
    # three changes the four-key Summary contract above (tests assert exact
    # dict equality against it) -- all are queryable afterwards via
    # steward.list_alerts()/list_suggestions(), each with its own
    # resolved_reason (recovered / rule_disabled / stale_backlog).
    _auto_resolve_recovered(steward, ran_keys, now=current)
    _resolve_disabled_rule_cases(steward, configured_rules, now=current)
    _sweep_backlog(steward, now=current)

    # H6/PERF-001/SEC-O-005: a borderline message must be triaged (an LLM
    # subprocess call) AT MOST ONCE, ever -- not once per 15-min cycle for as
    # long as it keeps showing up in the trailing-24h window. Filter already-
    # triaged messages out BEFORE spending any LLM call (deterministic, cheap
    # check first), then cap the remaining first-seen flood so even a burst of
    # brand-new messages can't spawn unbounded subprocesses in one cycle.
    marker_path = (
        Path(triaged_marker_path)
        if triaged_marker_path is not None
        else _default_triaged_marker_path()
    )
    triaged_ids = _load_triaged_ids(marker_path)
    pending: list[AlertCandidate] = []
    for candidate in borderline:
        assert candidate.message is not None
        message_id = candidate.message.get("id")
        if message_id is not None and str(message_id) in triaged_ids:
            continue
        pending.append(candidate)
    cap = config.alerts.triage_max_per_cycle
    to_triage, deferred = pending[:cap], pending[cap:]
    if deferred:
        logger.warning(
            "Triage per-cycle cap hit: %d borderline message(s) exceed cap of %d; %d deferred to a later cycle",
            len(pending),
            cap,
            len(deferred),
        )

    newly_triaged = set(triaged_ids)
    for candidate in to_triage:
        assert candidate.message is not None
        message_id = candidate.message.get("id")
        summary["triaged"] += 1
        if message_id is not None:
            newly_triaged.add(str(message_id))
        result = triage(candidate.message, config.alerts)
        if not result.get("urgent"):
            continue
        triaged = _triage_alert(candidate.message, str(result.get("reason") or ""))
        if _persist_candidate(triaged, steward=steward, database=database, cfg=config):
            summary["created"] += 1
        else:
            summary["suppressed"] += 1
    _save_triaged_ids(marker_path, newly_triaged)
    _remind_expiring_approvals(database, now=current)
    return summary


def _remind_expiring_approvals(database: SqliteStore, *, now: datetime) -> Summary:
    """T-24h approval re-push, hosted on this periodic cycle.

    This monitor is the launchd-driven periodic job that already holds an open
    control-plane store, which is exactly what the reminder sweep needs — and it
    is deliberately NOT the runner's routines tick, which must not grow another
    responsibility.

    The sweep is isolated like every alert rule: a reminder failure logs and
    returns zeroes rather than aborting the cycle that already persisted alerts.
    The counts are logged rather than folded into ``Summary``, whose four keys
    are a stable contract for the ``--once`` JSON consumers.
    """
    from omniagentos.notifications.approval_reminders import remind_from_store

    try:
        result = remind_from_store(database, now=now)
    except Exception:  # noqa: BLE001 - isolation is the dead-man contract
        logger.exception("Approval expiry reminder sweep failed")
        return {"considered": 0, "reminded": 0, "already_reminded": 0, "failed": 0}
    if result["reminded"] or result["failed"]:
        logger.info("Approval expiry reminders: %s", json.dumps(result, sort_keys=True))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate Steward alert rules")
    parser.add_argument("--once", action="store_true", help="run one monitor cycle")
    parser.parse_args(argv)
    try:
        summary = monitor_once(SqliteStore(default_db_path()))
    except Exception:
        logger.exception("Alert monitor cycle failed")
        summary = {"evaluated": 0, "created": 0, "suppressed": 0, "triaged": 0}
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through module command
    raise SystemExit(main())


__all__ = [
    "MONEY_RULES",
    "REMEDIATIONS",
    "_breaker_state_path",
    "_trip_spend_breaker",
    "main",
    "monitor_once",
]
