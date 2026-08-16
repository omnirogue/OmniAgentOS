"""W2: projects, per-project scoped permissions, and live project activity."""

from __future__ import annotations

from omniagentos.projects.activity import build_project_activity, project_pending_activity
from omniagentos.projects.policy import (
    assert_grants_monotonic,
    evaluate_action_for_project,
    resolve_project_policy,
)
from omniagentos.projects.store import ProjectError, ProjectStore

__all__ = [
    "ProjectError",
    "ProjectStore",
    "assert_grants_monotonic",
    "build_project_activity",
    "evaluate_action_for_project",
    "project_pending_activity",
    "resolve_project_policy",
]
