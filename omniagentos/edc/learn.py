"""Nightly pattern learner for the Executive Decision Center (synthesis §9).

One owner-scoped SQL pass over the owner's RESOLVED decisions, clustered by
``(resolution, counterparty domain)``. When the owner has handled **N ≥ 3**
matching items the SAME way with **zero contrary** decisions in the window, the
learner files (or refreshes) ONE ``proposed`` rule and its ``rule_proposal``
Decision through the shared :func:`omniagentos.edc.rules.file_proposed_rule`
seam — the owner still has to approve it.

Two structural guarantees make this safe (spec §15.7, RESOLUTIONS.md F3, F11):

* **No self-reference (rule inception).** The clustering read excludes
  internal/system-origin decisions in the DAL itself
  (:meth:`DecisionStore.resolved_for_learning` filters ``source NOT IN
  (...)``), so a ``rule_proposal`` decision can never feed the learner.
* **No unattended authority, ever.** The learner only proposes the pre-fill
  kinds in :data:`_LEARNABLE_KINDS`; the automation kinds are neither named nor
  reachable from this module (grep-provable + a unit test), and the shared
  proposal seam refuses them regardless. Unattended execution is a separate,
  explicit per-rule owner promotion — never something the learner can reach.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.db.store import SqliteStore
from omniagentos.edc.rules import file_proposed_rule
from omniagentos.edc.store import INTERNAL_DECISION_SOURCES, DecisionStore

__all__ = ["run_learning"]

#: How far back the learner clusters, and the minimum consistent support.
_WINDOW_DAYS = 14
_MIN_SUPPORT = 3
#: A declined proposal suppresses re-proposal for this long (synthesis §9).
_DECLINE_SUPPRESSION_DAYS = 30

#: Owner resolution → the pre-fill rule kind it justifies proposing. These are
#: the ONLY kinds the learner can emit; none of them is a pre-approved unattended
#: kind, so learning can never itself cause a side effect.
_RESOLUTION_TO_KIND: dict[str, str] = {
    "delegate": "delegate",
    "dismiss": "suppress",
    "snooze": "snooze_default",
}
_LEARNABLE_KINDS = frozenset(_RESOLUTION_TO_KIND.values())


def _domain(counterparty: str) -> str:
    """The lowercased sender domain of a counterparty header, or ''."""
    match = re.search(r"@([\w.\-]+)", counterparty or "")
    return (match.group(1) if match else "").strip().rstrip(".").lower()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rules_for(rules: list[dict[str, Any]], *, kind: str, domain: str) -> list[dict[str, Any]]:
    """Every rule of the owner's matching this (kind, sender_domain)."""
    return [
        rule
        for rule in rules
        if rule.get("kind") == kind
        and str((rule.get("matcher") or {}).get("sender_domain") or "").lower() == domain
    ]


def _active_or_proposed_rule_for(
    rules: list[dict[str, Any]], *, kind: str, domain: str
) -> dict[str, Any] | None:
    """The standing active/proposed twin for the skip/refresh decision.

    STATE PRECEDENCE (STORM fix): ``_existing_rule_for`` used to return the OLDEST
    matching rule regardless of state, so once a ``declined`` rule aged past the
    30-day window it neither skipped (no longer suppressed) nor refreshed (not
    proposed/active), and the learner filed a brand-new proposal every tick →
    unbounded twins. Here the active rule wins over a proposed one, and a
    ``declined`` rule is deliberately IGNORED — it is consulted only by
    :func:`_declined_rule_for` for the separate suppression check.
    """
    matches = _rules_for(rules, kind=kind, domain=domain)
    active = [rule for rule in matches if rule.get("state") == "active"]
    if active:
        return active[0]
    proposed = [rule for rule in matches if rule.get("state") == "proposed"]
    return proposed[0] if proposed else None


def _declined_rule_for(
    rules: list[dict[str, Any]], *, kind: str, domain: str
) -> dict[str, Any] | None:
    """The most recently declined rule for this (kind, domain) — suppression only.

    The learner never refreshes or skips on a declined rule; it consults it purely
    for the 30-day re-proposal suppression window. Newest decline wins so an old,
    long-expired decline never re-arms suppression after a newer one lapsed.
    """
    declined = [
        rule
        for rule in _rules_for(rules, kind=kind, domain=domain)
        if rule.get("state") == "declined"
    ]
    if not declined:
        return None
    return max(
        declined, key=lambda rule: str(rule.get("approved_at") or rule.get("updated_at") or "")
    )


def _suppressed_by_decline(rule: dict[str, Any] | None, *, now: datetime) -> bool:
    """Whether a declined rule still suppresses re-proposal (30-day window)."""
    if rule is None or rule.get("state") != "declined":
        return False
    stamp = str(rule.get("approved_at") or rule.get("updated_at") or "")
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True  # a declined rule with an unparseable stamp stays suppressed
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when >= now - timedelta(days=_DECLINE_SUPPRESSION_DAYS)


def _cluster(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group resolved rows by sender domain, tracking consistency + contrariness.

    Each cluster records the set of resolutions seen (contrary detection), the
    count of the consistent resolution, and — for delegations — the set of
    assignees (a delegation cluster is only consistent when it is ONE assignee).
    """
    clusters: dict[str, dict[str, Any]] = {}
    for row in rows:
        domain = _domain(str(row.get("counterparty") or ""))
        if not domain:
            continue
        resolution = str(row.get("resolution") or "")
        bucket = clusters.setdefault(
            domain,
            {"resolutions": set(), "count": 0, "assignees": set(), "category": ""},
        )
        bucket["resolutions"].add(resolution)
        bucket["count"] += 1
        if resolution == "delegate":
            bucket["assignees"].add(str(row.get("assignee_employee_id") or ""))
        if not bucket["category"]:
            bucket["category"] = str(row.get("company_slug") or "")
    return clusters


def _refresh_proposal_decision(
    store: DecisionStore,
    *,
    owner: str,
    rule: dict[str, Any],
    action: dict[str, Any],
) -> None:
    """Re-render the open ``rule_proposal`` Decision linked to a refreshed rule.

    When the learner refreshes a standing proposal whose action drifted (a new
    assignee, say), the owner-facing ``rule_proposal`` Decision must show the NEW
    behaviour — otherwise the owner approves a stale human line. Finds the open
    proposal Decision whose ``source_ref`` is this rule id and updates its title +
    ``recommended`` (human line + params.action) in place. Owner-scoped; a no-op
    if no open proposal is linked (already decided).
    """
    from omniagentos.edc.rules import render_rule

    rule_id = str(rule.get("id") or "")
    if not rule_id:
        return
    matcher = dict(rule.get("matcher") or {})
    kind = str(rule.get("kind") or "")
    for decision in store.list_decisions(owner_employee_id=owner, status="open"):
        if decision.get("source") != "rule_proposal":
            continue
        if str(decision.get("source_ref") or "") != rule_id:
            continue
        human_line = render_rule(kind, matcher, action)
        recommended = dict(decision.get("recommended") or {})
        recommended["human_line"] = human_line
        recommended["params"] = {
            **(recommended.get("params") or {}),
            "action": dict(action),
        }
        store.update_decision(
            decision["id"],
            owner_employee_id=owner,
            fields={"title": f"Rule proposal: {human_line}", "recommended": recommended},
        )
        return


def run_learning(
    base_store: SqliteStore,
    owner_employee_ids: list[str],
    *,
    now: datetime | None = None,
    window_days: int = _WINDOW_DAYS,
    min_support: int = _MIN_SUPPORT,
) -> dict[str, int]:
    """Cluster each owner's decided history and file/refresh pre-fill proposals.

    Idempotent to re-run (safe on every tick): a proposal REFRESHES the existing
    ``proposed`` rule rather than twinning it, and its ``rule_proposal`` Decision
    dedupes on ``(source, source_ref, owner)``. Returns counts of proposals
    filed / refreshed / suppressed.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    since = _iso(moment - timedelta(days=window_days))
    store = DecisionStore(base_store)
    stats = {"filed": 0, "refreshed": 0, "suppressed": 0}

    for owner in owner_employee_ids:
        rows = store.resolved_for_learning(
            owner_employee_id=owner,
            since_iso=since,
            exclude_sources=INTERNAL_DECISION_SOURCES,
        )
        rules = store.list_rules(owner_employee_id=owner)
        for domain, bucket in _cluster(rows).items():
            resolutions: set[str] = bucket["resolutions"]
            if len(resolutions) != 1:
                continue  # contrary decisions in the window — never propose
            resolution = next(iter(resolutions))
            kind = _RESOLUTION_TO_KIND.get(resolution)
            if kind is None or bucket["count"] < min_support:
                continue
            action: dict[str, Any] = {}
            if kind == "delegate":
                if len(bucket["assignees"]) != 1:
                    continue  # split across assignees — not a single consistent rule
                assignee = next(iter(bucket["assignees"]))
                if not assignee:
                    continue
                action["assignee"] = assignee

            # STATE PRECEDENCE (STORM fix): resolve the active/proposed twin for
            # the skip/refresh decision; a declined rule is consulted ONLY for the
            # 30-day suppression check below. This stops the expired-decline loop
            # that filed a fresh proposal every tick (then found the same expired
            # decline again next tick) — after the window lapses the learner files
            # AT MOST ONE fresh proposal and REFRESHES that one thereafter.
            existing = _active_or_proposed_rule_for(rules, kind=kind, domain=domain)
            if existing is not None and existing.get("state") == "active":
                continue  # already an active rule — nothing to propose
            if existing is not None and existing.get("state") == "proposed":
                # Dedupe: refresh the standing proposal in place, never a twin.
                refreshed_rule = store.update_rule(
                    existing["id"],
                    owner_employee_id=owner,
                    fields={"action": action, "category": bucket["category"]},
                )
                # Re-render the linked rule_proposal Decision so the owner approves
                # what they see when the action drifts (e.g. a new assignee).
                _refresh_proposal_decision(
                    store,
                    owner=owner,
                    rule=refreshed_rule or existing,
                    action=action,
                )
                stats["refreshed"] += 1
                continue

            # No standing active/proposed twin: a recent decline suppresses a fresh
            # proposal for 30 days; once that lapses, exactly one proposal is filed.
            if _suppressed_by_decline(
                _declined_rule_for(rules, kind=kind, domain=domain), now=moment
            ):
                stats["suppressed"] += 1
                continue

            file_proposed_rule(
                store,
                owner_employee_id=owner,
                kind=kind,
                matcher={"sender_domain": domain},
                action=action,
                category=str(bucket["category"] or ""),
                created_from="learned",
            )
            stats["filed"] += 1

    return stats
