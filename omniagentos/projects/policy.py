"""Overlay per-project permission grants onto the global action-class policy.

The global policy (``configs/policy.yaml``, loaded as
:class:`omniagentos.policy.PolicyConfig`) is authoritative and fail-closed. A
project may supply grants that *restrict* the decision for a given action class,
subject to one hard invariant: **overlays are monotonic**. A grant can only
*add* restrictions (turn ``requires_approval``/``always_human`` on where the
global policy left them off); it can never relax the global floor. This keeps
deny-by-default intact -- a project can never downgrade a globally
approval-gated (or ``always_human``) action to unattended execution, no matter
what a future per-project config claims -- mirroring the broker's
``HARD_HUMAN_CLASSES`` guard while generalizing it to every class.

This module is pure: it takes a loaded policy plus a list of plain grant dicts
(as returned by :class:`omniagentos.projects.store.ProjectStore`) and never
touches the database.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from omniagentos.contracts import ActionClass, PolicyDecision
from omniagentos.policy import ActionClassPolicy, PolicyConfig, PolicyError

__all__ = [
    "assert_grants_monotonic",
    "evaluate_action_for_project",
    "resolve_project_policy",
]


def resolve_project_policy(
    cfg: PolicyConfig, grants: Iterable[dict[str, Any]]
) -> dict[ActionClass, ActionClassPolicy]:
    """Return the effective per-action-class policy after applying ``grants``.

    Starts from the global ``cfg.action_classes`` and overlays each grant
    *monotonically*: the effective ``requires_approval`` and ``always_human``
    are the logical OR of the global posture and the grant. A grant therefore
    can only tighten -- it can never lower a global ``requires_approval`` or
    ``always_human`` floor -- and any class left ``always_human`` is forced to
    also require approval.
    """
    resolved: dict[ActionClass, ActionClassPolicy] = dict(cfg.action_classes)
    for grant in grants:
        raw_class = grant.get("action_class")
        try:
            action_class = ActionClass(str(raw_class))
        except (TypeError, ValueError) as exc:
            raise PolicyError(f"grant names unknown action class {raw_class!r}") from exc

        base = cfg.action_classes.get(action_class)
        base_requires_approval = bool(base.requires_approval) if base is not None else True
        base_always_human = bool(base.always_human) if base is not None else False

        # Monotonic overlay: OR the global floor with the grant. A grant that
        # asks for a *lower* setting is a no-op here (deny-by-default holds).
        always_human = base_always_human or bool(grant.get("always_human", False))
        requires_approval = (
            base_requires_approval or bool(grant.get("requires_approval", False)) or always_human
        )
        resolved[action_class] = ActionClassPolicy(
            requires_approval=requires_approval,
            always_human=always_human,
        )
    return resolved


def assert_grants_monotonic(cfg: PolicyConfig, grants: Iterable[dict[str, Any]]) -> None:
    """Reject grants that attempt to relax the global floor (fail closed at the edge).

    ``resolve_project_policy`` is already safe-by-construction -- a relaxing
    grant is a no-op there. This is the *API boundary* guard: rather than
    silently ignore an operator's attempt to downgrade a globally
    approval-gated (or ``always_human``) class, we surface it as an error so the
    intent is never mistaken for success. Raises :class:`PolicyError`; callers
    map it to HTTP 422.
    """
    for grant in grants:
        raw_class = grant.get("action_class")
        try:
            action_class = ActionClass(str(raw_class))
        except (TypeError, ValueError) as exc:
            raise PolicyError(f"grant names unknown action class {raw_class!r}") from exc

        base = cfg.action_classes.get(action_class)
        if base is None:
            continue
        if base.requires_approval and not bool(grant.get("requires_approval", True)):
            raise PolicyError(
                f"action class {action_class.value!r} requires approval globally; "
                "a project grant cannot relax it to unattended execution"
            )
        if base.always_human and not bool(grant.get("always_human", True)):
            raise PolicyError(
                f"action class {action_class.value!r} is always-human globally; "
                "a project grant cannot relax it"
            )


def evaluate_action_for_project(
    action_class: ActionClass | str,
    cfg: PolicyConfig,
    grants: Iterable[dict[str, Any]],
) -> PolicyDecision:
    """Evaluate one action class under the project-overlaid policy, failing closed."""
    try:
        normalized = ActionClass(action_class)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"Unknown action class {action_class!r}; action denied") from exc

    resolved = resolve_project_policy(cfg, grants)
    action_policy = resolved.get(normalized)
    if action_policy is None:
        raise PolicyError(f"Action class {normalized.value!r} is not configured; action denied")
    return PolicyDecision(
        requires_approval=action_policy.requires_approval,
        always_human=action_policy.always_human,
        reason=f"Project-scoped policy for action class {normalized.value}",
    )
