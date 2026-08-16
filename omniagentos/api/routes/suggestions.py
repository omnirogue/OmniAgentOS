"""Suggestion engine HTTP surface: list/generate suggestions, approve/dismiss.

The approve endpoint is the autonomy-rung-1 boundary (see
docs/adr/ADR-007-steward-autonomy.md): it turns a suggestion into a REAL
task+run through :func:`create_task_service`/:func:`create_run_service` --
the exact same validated path ``/api/tasks``/``/api/tasks/{id}/runs`` use --
and NOTHING else. There is no direct ``store.create_task``/``enqueue_run``
here, and no auto-approval identity: ``approved_by`` is a required, recorded,
non-reserved human identifier every time.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from omniagentos.api.deps import PolicyDep, StoreDep
from omniagentos.api.services import ApiError, create_run_service, create_task_service, fail
from omniagentos.contracts import Events
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import StewardConfig, load_steward_config
from omniagentos.steward.store import StewardStore
from omniagentos.steward.suggest import (
    build_plan,
    generate_goal_suggestions,
    reconcile_suggestion,
    task_risk_for,
)

router = APIRouter(prefix="/api/suggestions", tags=["suggestions"])

__all__ = ["router"]

# Reserved identity strings a hypothetical future auto-executor would present
# as `approved_by`. NOTHING in this build ever calls approve with one of these
# -- there is no auto-approve path at rung 1 -- but the check is defense in
# depth so that boundary cannot silently erode if a scheduler/bot is ever
# wired up to call this endpoint without first, deliberately, raising the
# autonomy rung (ADR-007). Comparison is case-insensitive and whitespace-
# trimmed.
RESERVED_AUTO_IDENTITIES: frozenset[str] = frozenset(
    {"auto", "system", "autopilot", "scheduler", "bot", "steward", "steward-auto"}
)


@lru_cache(maxsize=1)
def get_steward_config() -> StewardConfig:
    """Load and cache the process-wide Steward config (overridable in tests)."""
    return load_steward_config()


StewardConfigDep = Annotated[StewardConfig, Depends(get_steward_config)]


def _steward(store: Any) -> StewardStore:
    return StewardStore(cast(SqliteStore, store))


def _emit(
    store: Any,
    action: str,
    suggestion_id: str,
    payload: dict[str, Any],
) -> None:
    store.insert_event(
        Events.SUGGESTION_UPDATED,
        "steward",
        action,
        target_type="suggestion",
        target_id=suggestion_id,
        payload=payload,
    )


class ApproveRequest(BaseModel):
    approved_by: str = Field(min_length=1)


class DismissRequest(BaseModel):
    decided_by: str = Field(min_length=1)
    reason: str | None = None


@router.get("")
def list_suggestions(
    store: StoreDep,
    state: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    # H13: reconcile every card so risk_class and proposed_plan.action_class
    # agree with what approval would actually execute (frozen shape for RF).
    return [
        reconcile_suggestion(row)
        for row in _steward(store).list_suggestions(state=state, limit=limit)
    ]


@router.post("/generate")
def generate(store: StoreDep, cfg: StewardConfigDep) -> list[dict[str, Any]]:
    steward = _steward(store)
    created = generate_goal_suggestions(steward, cfg)
    for row in created:
        _emit(
            store,
            "suggestion.created",
            str(row["id"]),
            {"suggestion_id": row["id"], "title": row.get("title"), "goal_id": row.get("goal_id")},
        )
    return created


@router.post("/{suggestion_id}/approve")
def approve(
    suggestion_id: str,
    body: ApproveRequest,
    store: StoreDep,
    policy_cfg: PolicyDep,
) -> dict[str, Any]:
    approved_by = body.approved_by.strip()
    if not approved_by:
        fail(422, "validation", "approved_by is required")
    if approved_by.lower() in RESERVED_AUTO_IDENTITIES:
        fail(
            409,
            "autonomy",
            "auto-approval identities are not permitted at the current autonomy rung",
            {"approved_by": body.approved_by},
        )

    steward = _steward(store)
    suggestion = steward.get_suggestion(suggestion_id)
    if suggestion is None:
        fail(404, "not_found", "suggestion not found", {"id": suggestion_id})

    # H9: CLAIM ATOMICALLY FIRST. claim_suggestion is a single guarded
    # open->approving transition; only one caller can win it. Everything below
    # (create_task_service/create_run_service, which mint a REAL task+run)
    # happens strictly AFTER we hold the claim, so two concurrent approvals can
    # never both create runs -- the loser is turned away here before any side
    # effect. A None return means the suggestion is no longer open (already
    # claimed by a concurrent approver, or already decided) -> 409. Existence
    # was checked above so this cannot be a "missing" case.
    claimed = steward.claim_suggestion(suggestion_id)
    if claimed is None:
        fail(409, "invalid_state", "suggestion is not open", {"state": suggestion.get("state")})
    suggestion = claimed

    discipline_id: str | None = None
    goal_id = suggestion.get("goal_id")
    if goal_id:
        goal = steward.get_goal(str(goal_id))
        if goal is not None:
            resolved = goal.get("discipline_id")
            discipline_id = str(resolved) if resolved else None

    plan, prompt = build_plan(suggestion)
    proposed_plan = suggestion.get("proposed_plan")
    harness = (
        str(proposed_plan.get("harness"))
        if isinstance(proposed_plan, dict) and proposed_plan.get("harness")
        else "cli-claude"
    )

    # H13: label the task with the suggestion's ACTUAL risk (derived from its
    # effective action_class), not a hardcoded "low". The plan step's
    # action_class already carries the real class into the run (build_plan), so
    # the runner/broker gate is unaffected -- this makes the Task row honest too.
    task_risk = task_risk_for(suggestion)

    # create_task_service/create_run_service are the ONLY sanctioned mutation
    # path for turning a suggestion into a task+run (D-001) -- never a direct
    # store.create_task/enqueue_run call. We hold the claim (state 'approving')
    # for the duration; ANY failure below rolls the claim back to 'open' via
    # release_suggestion so the suggestion can be retried, then surfaces the
    # error.
    task: dict[str, Any] | None = None
    try:
        task = create_task_service(
            store,
            policy_cfg,
            title=f"[steward] {suggestion['title']}",
            discipline_id=discipline_id,
            input={"suggestion_id": suggestion_id},
            risk=task_risk,
        )
        run = create_run_service(
            store,
            policy_cfg,
            task_id=task["id"],
            harness=harness,
            plan=plan,
            prompt=prompt,
        )
    except ApiError as exc:
        # Roll the claim back to 'open' first so a retry is possible.
        steward.release_suggestion(suggestion_id)
        if task is None:
            # create_task_service failed before any task row was produced (e.g.
            # an unresolvable discipline_id): surface the original validation
            # error unchanged. The suggestion is back to 'open'.
            raise
        # PARTIAL-TASK CAVEAT (see ADR-007): the task above was already created
        # and is sitting in state 'ready' (or 'queued' if a race won).
        # create_task_service/create_run_service expose no task-delete/void
        # primitive (by design: they are additive-only), so the orphaned task
        # remains visible via GET /api/tasks for manual cleanup. We re-surface
        # as a distinct 502-style error (rather than the original 4xx) so the
        # caller can tell "the suggestion itself was fine, but turning it into a
        # run failed after we already created a task" apart from an ordinary
        # validation error on the approve request itself. The claim has been
        # released above, so the suggestion is 'open' for retry.
        fail(
            502,
            "run_creation_failed",
            "task was created but the run could not be enqueued; the suggestion "
            "remains open for retry",
            {
                "suggestion_id": suggestion_id,
                "task_id": task["id"],
                "original": {"code": exc.code, "message": exc.message, "detail": exc.detail},
            },
        )

    decided = steward.decide_suggestion(
        suggestion_id,
        state="approved",
        decided_by=approved_by,
        task_id=task["id"],
        run_id=run["id"],
    )
    if decided is None:  # pragma: no cover - defensive; we hold the 'approving' claim
        steward.release_suggestion(suggestion_id)
        fail(409, "invalid_state", "suggestion was decided concurrently", {"id": suggestion_id})

    _emit(
        store,
        "suggestion.approved",
        suggestion_id,
        {
            "suggestion_id": suggestion_id,
            "task_id": task["id"],
            "run_id": run["id"],
            "decided_by": approved_by,
        },
    )
    # H13: reconcile the returned card so risk_class/proposed_plan.action_class
    # reflect what actually executed (frozen shape for RF).
    return {
        "suggestion": reconcile_suggestion(decided),
        "task_id": task["id"],
        "run_id": run["id"],
    }


@router.post("/{suggestion_id}/dismiss")
def dismiss(suggestion_id: str, body: DismissRequest, store: StoreDep) -> dict[str, Any]:
    decided_by = body.decided_by.strip()
    if not decided_by:
        fail(422, "validation", "decided_by is required")

    steward = _steward(store)
    suggestion = steward.get_suggestion(suggestion_id)
    if suggestion is None:
        fail(404, "not_found", "suggestion not found", {"id": suggestion_id})
    if suggestion.get("state") != "open":
        fail(409, "invalid_state", "suggestion is not open", {"state": suggestion.get("state")})

    decided = steward.decide_suggestion(suggestion_id, state="dismissed", decided_by=decided_by)
    if decided is None:  # pragma: no cover - defensive; rung 1 has no concurrent decider
        fail(409, "invalid_state", "suggestion was decided concurrently", {"id": suggestion_id})

    steward.record_suggestion_outcome(suggestion_id, {"dismiss_reason": body.reason})
    result = steward.get_suggestion(suggestion_id)
    assert result is not None

    _emit(
        store,
        "suggestion.dismissed",
        suggestion_id,
        {"suggestion_id": suggestion_id, "decided_by": decided_by, "reason": body.reason},
    )
    return result
