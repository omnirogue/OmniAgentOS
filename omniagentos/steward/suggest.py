"""Deterministic goal-rule suggestion engine (autonomy rung 1).

Design note (see docs/adr/ADR-007-steward-autonomy.md): every rule in
``GOAL_RULES`` is a pure function over already-stored metric snapshots — no LLM
call is involved in *generating* a suggestion. A suggestion's ``proposed_plan``
is a prompt an operator can choose to run through an agent (which the approve
flow in ``omniagentos.api.routes.suggestions`` turns into a real task+run), but
the decision to create the suggestion itself never depends on model output.
This is deliberate: an LLM-authored suggestion could launder untrusted content
(e.g. a comms message) into what looks like a system-generated recommendation,
quietly forging the "the Steward decided this, not an attacker" trust signal.
Keeping suggestion GENERATION deterministic keeps that signal honest; only the
approve step (a human action) crosses into agent execution, and it does so
through the exact same validated create_task_service/create_run_service path
the HTTP API uses (D-001) — never a bespoke write.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from omniagentos.steward.config import StewardConfig
from omniagentos.steward.store import StewardStore, suggestion_occurrences

# A rule receives one goal row (already JSON-decoded by StewardStore) and the
# store to read metric series from, and returns either a suggestion candidate
# (ready to pass to StewardStore.create_suggestion, minus its id/state/timestamps)
# or None when its condition is not met.
GoalRule = Callable[[dict[str, Any], StewardStore], "dict[str, Any] | None"]


def _metric_values(
    sst: StewardStore, source: str, metric: str, goal_id: str, *, days: int = 14
) -> list[float]:
    """Chronologically ascending (oldest first) metric values for one goal."""
    rows = sst.snapshot_series(metric, goal_id=goal_id, source=source, days=days)
    return [float(row["value"]) for row in rows]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def revenue_downtrend_rule(goal: dict[str, Any], sst: StewardStore) -> dict[str, Any] | None:
    """Flag a >20% drop in the trailing 3-day mean vs. the prior 4-day mean.

    Applies only to goals whose north star tracks ``net_revenue_usd``
    (the seeded ``increase-revenue`` goal). Read-only by design: the proposed
    plan asks an agent to analyze Stripe data, never to act on it.
    """
    north_star = goal.get("north_star") or {}
    if not isinstance(north_star, dict) or north_star.get("metric") != "net_revenue_usd":
        return None
    source = str(north_star.get("source") or "stripe")
    goal_id = str(goal["id"])

    values = _metric_values(sst, source, "net_revenue_usd", goal_id)
    if len(values) < 7:
        return None  # not enough history to compare a 3-day mean to a prior 4-day mean

    recent_mean = _mean(values[-3:])
    prior_mean = _mean(values[-7:-3])
    if prior_mean <= 0:
        return None
    drop_pct = (prior_mean - recent_mean) / prior_mean
    if drop_pct <= 0.20:
        return None

    return {
        "title": "Investigate revenue dip",
        "rationale": (
            f"net_revenue_usd's 3-day mean ({recent_mean:.2f}) is {drop_pct:.0%} below "
            f"the prior 4-day mean ({prior_mean:.2f})."
        ),
        "evidence": [
            {
                "metric": "net_revenue_usd",
                "source": source,
                "recent_3d_mean": recent_mean,
                "prior_4d_mean": prior_mean,
                "drop_pct": drop_pct,
            }
        ],
        "proposed_plan": {
            "prompt": (
                "Analyze recent Stripe revenue data READ-ONLY to identify the likely cause "
                f"of the net_revenue_usd decline (3-day mean {recent_mean:.2f} vs. prior "
                f"4-day mean {prior_mean:.2f}). Do not issue refunds, change prices, or take "
                "any other action -- produce findings and a hypothesis only."
            ),
            "harness": "cli-claude",
            "action_class": "read_only",
        },
        "risk_class": "read_only",
        "source": "steward.suggest.revenue_downtrend",
        "goal_id": goal_id,
    }


def roas_opportunity_rule(goal: dict[str, Any], sst: StewardStore) -> dict[str, Any] | None:
    """Flag a scaling opportunity when ROAS is strong and spend is flat.

    Applies only to goals whose north star tracks ``roas`` (the seeded
    ``improve-ad-roi`` goal). Analysis-only by design: the proposed plan asks
    for a scaling PROPOSAL, and explicitly changes nothing.
    """
    north_star = goal.get("north_star") or {}
    if not isinstance(north_star, dict) or north_star.get("metric") != "roas":
        return None
    source = str(north_star.get("source") or "meta")
    goal_id = str(goal["id"])

    roas_values = _metric_values(sst, source, "roas", goal_id)
    if not roas_values:
        return None
    latest_roas = roas_values[-1]
    if latest_roas <= 2.0:
        return None

    spend_values = _metric_values(sst, source, "spend_usd", goal_id)
    if len(spend_values) < 4:
        return None  # not enough history to judge "flat"
    window = spend_values[-4:]
    spend_mean = _mean(window)
    if spend_mean <= 0:
        return None
    # "Flat" = every value in the trailing window sits within 10% of the mean.
    max_deviation = max(abs(value - spend_mean) for value in window) / spend_mean
    if max_deviation > 0.10:
        return None

    return {
        "title": "Consider scaling winning campaigns",
        "rationale": (
            f"roas is {latest_roas:.2f} (> 2.0) while spend has stayed flat around "
            f"{spend_mean:.2f} over the last {len(window)} snapshots."
        ),
        "evidence": [
            {"metric": "roas", "source": source, "latest": latest_roas},
            {"metric": "spend_usd", "source": source, "mean": spend_mean, "window": window},
        ],
        "proposed_plan": {
            "prompt": (
                f"Produce a scaling PROPOSAL only for the currently winning ad campaigns "
                f"(roas={latest_roas:.2f}, spend flat at ~{spend_mean:.2f}). Recommend budget "
                "increases and expected impact. Analysis only -- change nothing (no budget "
                "edits, no campaign changes)."
            ),
            "harness": "cli-claude",
            "action_class": "read_only",
        },
        "risk_class": "read_only",
        "source": "steward.suggest.roas_opportunity",
        "goal_id": goal_id,
    }


GOAL_RULES: tuple[GoalRule, ...] = (revenue_downtrend_rule, roas_opportunity_rule)


# --- Risk labeling (H13) --------------------------------------------------
#
# ``action_class`` is the ground truth of what a suggestion actually EXECUTES:
# it flows into the run's plan step and the runner/broker gates off exactly that
# field. ``risk_class`` is the label an operator sees on the card. They use the
# same vocabulary (``contracts.ActionClass``), and a suggestion where the two
# disagree -- e.g. RD's alert-remediation suggestions, created without an
# explicit ``action_class`` while the card claims ``read_only`` -- is a trust
# hazard: the card can under-state what approval would set running. The helpers
# below reconcile both to a single *effective* action_class and derive the
# coarse task-risk bucket honestly, so the label always matches execution.

# contracts.ActionClass ladder, lowest -> highest risk.
_ACTION_CLASSES: frozenset[str] = frozenset(
    {
        "read_only",
        "sandboxed_creation",
        "internal_reversible",
        "external_reversible",
        "consequential",
        "irreversible",
    }
)
# Default when neither a plan action_class nor a risk_class is present -- the
# same floor create_run_service applies.
_DEFAULT_ACTION_CLASS = "sandboxed_creation"

# action_class -> coarse Task.risk bucket (create_task_service `risk` vocabulary).
_TASK_RISK_BY_ACTION_CLASS: dict[str, str] = {
    "read_only": "low",
    "sandboxed_creation": "low",
    "internal_reversible": "medium",
    "external_reversible": "medium",
    "consequential": "high",
    "irreversible": "high",
}


def effective_action_class(suggestion: Mapping[str, Any]) -> str:
    """The action_class a suggestion actually executes.

    Precedence: an explicit, valid ``proposed_plan.action_class`` (what the run
    step will carry) wins; otherwise fall back to a valid ``risk_class`` (so an
    alert-remediation suggestion whose only risk signal is its class still runs
    at that class, not the silent default); otherwise the sandboxed default.
    """
    proposed = suggestion.get("proposed_plan")
    if isinstance(proposed, dict):
        candidate = proposed.get("action_class")
        if isinstance(candidate, str) and candidate in _ACTION_CLASSES:
            return candidate
    risk_class = suggestion.get("risk_class")
    if isinstance(risk_class, str) and risk_class in _ACTION_CLASSES:
        return risk_class
    return _DEFAULT_ACTION_CLASS


def task_risk_for(suggestion: Mapping[str, Any]) -> str:
    """Coarse Task.risk (low/medium/high) for a suggestion's effective class."""
    return _TASK_RISK_BY_ACTION_CLASS[effective_action_class(suggestion)]


def reconcile_suggestion(suggestion: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy whose ``risk_class`` and ``proposed_plan.action_class`` both
    equal the effective action_class -- so the card an operator sees can never
    under-state what approval would run. Frozen shape for the dashboard (RF):
    ``risk_class`` (accurate), ``proposed_plan`` (dict with ``prompt``,
    ``harness``, ``action_class``) and ``evidence`` are always present/consistent.

    Also includes ``occurrence_count`` (the true firing count) which the evidence
    array may not reflect when capped at _SUGGESTION_EVIDENCE_MAX.
    """
    action_class = effective_action_class(suggestion)
    reconciled = dict(suggestion)
    proposed = suggestion.get("proposed_plan")
    plan = dict(proposed) if isinstance(proposed, dict) else {}
    plan["action_class"] = action_class
    reconciled["proposed_plan"] = plan
    reconciled["risk_class"] = action_class
    reconciled["occurrence_count"] = suggestion_occurrences(dict(suggestion))
    return reconciled


def _has_open_duplicate(sst: StewardStore, goal_id: str, title: str) -> bool:
    return any(
        row.get("goal_id") == goal_id and row.get("title") == title
        for row in sst.list_suggestions(state="open")
    )


def generate_goal_suggestions(sst: StewardStore, cfg: StewardConfig) -> list[dict[str, Any]]:
    """Run every deterministic goal rule and persist newly-created suggestions.

    Dedupe: a rule that would produce the same title for a goal that already
    has an OPEN suggestion with that title is skipped (rung 1 does not pile up
    duplicate open suggestions while an operator has yet to decide the first).
    """
    if cfg.autonomy.rung < 1:
        # Rung 0 would mean "steward proposes nothing" -- not reachable via
        # today's config default (rung: 1) but kept as an explicit floor rather
        # than an assumed one, per ADR-007's autonomy-ladder documentation.
        return []

    created: list[dict[str, Any]] = []
    for goal in sst.list_goals(status="active"):
        goal_id = str(goal["id"])
        for rule in GOAL_RULES:
            candidate = rule(goal, sst)
            if candidate is None:
                continue
            if _has_open_duplicate(sst, goal_id, str(candidate["title"])):
                continue
            created.append(sst.create_suggestion(candidate))
    return created


def build_plan(suggestion: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Turn a suggestion's ``proposed_plan`` into a runner plan + prompt.

    The step's ``action_class`` is the suggestion's *effective* class
    (:func:`effective_action_class`): an explicit ``proposed_plan.action_class``
    if present, else the declared ``risk_class``, else ``sandboxed_creation``
    (the same default ``create_run_service`` uses). It flows unchanged into the
    run's plan_json, and the runner/broker approval gates key off exactly this
    field -- so an elevated class on a suggestion still requires human approval
    at run time precisely as it would for any other task. Approving the
    SUGGESTION is not approving the underlying action, and this function must
    never special-case that away; deriving from ``risk_class`` when the plan
    omits an action_class only makes execution MATCH the risk shown on the card
    (H13), it never lowers it.
    """
    proposed = suggestion.get("proposed_plan")
    proposed_plan: dict[str, Any] = proposed if isinstance(proposed, dict) else {}
    prompt = str(proposed_plan.get("prompt", ""))
    action_class = effective_action_class(suggestion)
    step: dict[str, Any] = {
        "name": "agent",
        "kind": "agent",
        "action_class": action_class,
        "params": {"prompt": prompt},
    }
    return [step], prompt


__all__ = [
    "GOAL_RULES",
    "GoalRule",
    "build_plan",
    "effective_action_class",
    "generate_goal_suggestions",
    "reconcile_suggestion",
    "revenue_downtrend_rule",
    "roas_opportunity_rule",
    "task_risk_for",
]
