"""Typed shapes for project provisioning: the plan, the result, the scope request."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# The two kinds of scope a project can hold or ask for.
SCOPE_DIR = "dir"
SCOPE_CONNECTOR = "connector"

# The decision an orchestrator/provisioner records for a request-more-scope call.
DECISION_AUTO_GRANTED = "auto_granted"
DECISION_ESCALATED = "escalated"

# Persisted lifecycle of a scope request.
STATUS_GRANTED = "granted"
STATUS_PENDING = "pending"
STATUS_DENIED = "denied"


class ProvisionPlan(BaseModel):
    """What the (cheap) provisioning LLM decided a goal needs.

    ``existing_project`` names an existing project/workspace the goal references
    (attach to it) or is ``None`` (create a fresh scoped workspace).
    ``connectors`` are the connector/capability ids the goal likely needs, drawn
    from the ``configs/connectors.yaml`` catalogue; anything not in the registry
    is dropped before it can widen scope.
    """

    # Tolerate extra keys a model may emit; we only read what we declare.
    model_config = ConfigDict(extra="ignore")

    existing_project: str | None = None
    connectors: list[str] = Field(default_factory=list)
    reason: str = ""


class ProvisionResult(BaseModel):
    """The scoped project a goal was provisioned into."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    name: str
    root_dirs: list[str]
    # The single dir a dispatched run should work in (the project's primary root).
    working_dir: str
    # Connector capability ids (dotted) the project is scoped to reach.
    allowed_connectors: list[str]
    # True when provisioning created a fresh scoped workspace, False when it
    # attached to an existing project.
    created: bool
    reason: str = ""


class ScopeRequestResult(BaseModel):
    """Outcome of a mid-task request for an additional dir or connector."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    project_id: str
    scope_kind: str  # SCOPE_DIR | SCOPE_CONNECTOR
    scope_value: str
    action_class: str | None = None
    decision: str  # DECISION_AUTO_GRANTED | DECISION_ESCALATED
    granted: bool
    status: str  # STATUS_GRANTED | STATUS_PENDING
    reason: str = ""


__all__ = [
    "DECISION_AUTO_GRANTED",
    "DECISION_ESCALATED",
    "ProvisionPlan",
    "ProvisionResult",
    "SCOPE_CONNECTOR",
    "SCOPE_DIR",
    "STATUS_DENIED",
    "STATUS_GRANTED",
    "STATUS_PENDING",
    "ScopeRequestResult",
]
