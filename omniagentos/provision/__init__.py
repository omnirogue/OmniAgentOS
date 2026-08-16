"""Project provisioning: scope a goal to exactly the folders + APIs it needs.

Given a refined goal/spec, :func:`provision_project` determines the working
dir(s) and connector APIs the task needs (via a cheap LLM, degrading to a
deny-by-default heuristic) and creates/attaches a scoped W2 project with exactly
that reach. :func:`request_scope` is the mid-task widen-scope flow: a normal dir
or a read/auto-tier connector auto-grants; a hard-stop scope (money/transfer,
delete-capable, or a dir outside the user's home) escalates for human approval.

Beside that project-scope flow lives the AGENT capability spine:
:class:`CapabilityRequestService` transports one typed, immutable
``CapabilityRequest`` from any lane, and :class:`CapabilityPolicy` decides it --
auto-granting the benign read floor in-process, parking everything else for an
operator, and releasing the caller at the 90-second wall with a typed next move.
"""

from __future__ import annotations

from omniagentos.provision.capability_policy import (
    CapabilityPolicy,
    PolicyReceipt,
    PolicyViolation,
)
from omniagentos.provision.capability_requests import (
    CapabilityRequest,
    CapabilityRequestService,
    EnvelopeRejected,
    ExecutionFloorHint,
    ExtraAgentRequest,
    KeyScopeRequest,
    SkillRequest,
    ToolRequest,
)
from omniagentos.provision.contracts import (
    ProvisionPlan,
    ProvisionResult,
    ScopeRequestResult,
)
from omniagentos.provision.drive import grant_drive_dir
from omniagentos.provision.service import (
    HardStopFn,
    ProvisionLLM,
    ProvisionServiceError,
    build_scope_view,
    default_provision_llm,
    determine_scope,
    is_hard_stop_action,
    provision_project,
    request_scope,
)
from omniagentos.provision.store import ProvisionError, ProvisionStore

__all__ = [
    "CapabilityPolicy",
    "CapabilityRequest",
    "CapabilityRequestService",
    "EnvelopeRejected",
    "ExecutionFloorHint",
    "ExtraAgentRequest",
    "HardStopFn",
    "KeyScopeRequest",
    "PolicyReceipt",
    "PolicyViolation",
    "ProvisionError",
    "ProvisionLLM",
    "ProvisionPlan",
    "ProvisionResult",
    "ProvisionServiceError",
    "ProvisionStore",
    "ScopeRequestResult",
    "SkillRequest",
    "ToolRequest",
    "build_scope_view",
    "default_provision_llm",
    "determine_scope",
    "grant_drive_dir",
    "is_hard_stop_action",
    "provision_project",
    "request_scope",
]
