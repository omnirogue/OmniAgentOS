"""Validate the deterministic mission-v1 corpus against production authorities.

This is deliberately a corpus validator, not a set of label assertions.  The
same validator accepts the committed corpus and rejects every hostile overlay.
OpenAPI mission schemas and a product mission transport client remain blocked
P2 seams and are never reported as passed or skipped here.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from omniagentos.contracts import (
    CostObservation,
    CostQuality,
    EffectiveRoute,
    ExecutionRef,
)
from omniagentos.orgdims.contracts import OrganizationContext
from omniagentos.swarm.contracts import (
    READY_PLAN_DISPOSITION,
    FormationBinding,
    SwarmPlanDecision,
)
from omniagentos.taskcontract.models import (
    AcceptanceCriterion,
    Budgets,
    Gate,
    LeaseFields,
    TaskContract,
)
from omniagentos.verify.report import Verdict

FIXTURE_DIR = Path("contracts/fixtures/mission/v1")
OPENAPI_PATH = Path("contracts/openapi.json")
PARITY_PATH = FIXTURE_DIR / "fixture-parity.json"

EXPECTED_FAMILIES = (
    "active-context.json",
    "companies-projects.json",
    "context-manifests.json",
    "execution-contracts.json",
    "formations.json",
    "gate-matrix.json",
    "interventions.json",
    "memory-updates.json",
    "receipts.json",
    "skills-tools.json",
    "stream-frames.ndjson",
    "timeline.json",
)

EXPECTED_VARIANTS = {
    "active-context.json": (
        "confirmed",
        "suggested",
        "ambiguous",
        "corrected",
        "stale-revision",
        "reclassification-pending",
        "high-risk-needs-confirmation",
    ),
    "execution-contracts.json": (
        "complete",
        "incomplete",
        "refused",
        "clarification-required",
        "approval-required",
        "risk-high",
        "changed-after-preview",
    ),
    "context-manifests.json": (
        "forecast",
        "requested",
        "resolved",
        "loaded",
        "denied",
        "redacted",
        "stale",
        "failed",
    ),
    "skills-tools.json": (
        "full-skill-loaded",
        "label-only-skill-rejected",
        "tool-allowed",
        "tool-unavailable",
        "adaptive-access-requested",
        "permission-denied",
    ),
    "gate-matrix.json": (
        "pending",
        "running",
        "passed-with-evidence",
        "failed",
        "blocked",
        "skipped-approved",
        "insufficient-evidence",
        "stale-evidence",
    ),
    "formations.json": (
        "single-agent",
        "planner-implementer",
        "parallel-team",
        "sequential-fallback",
        "requested-effective-model-mismatch",
        "retry",
        "escalation",
        "partial-formation",
    ),
    "timeline.json": (
        "ordered-stages",
        "dependency-fan-out-fan-in",
        "blocker",
        "preview",
        "retry",
        "reconnect-replay",
        "cancellation",
        "partial",
        "terminal-accepted",
        "terminal-rejected",
    ),
    "interventions.json": (
        "clarification",
        "approval",
        "access-request",
        "cost-spend-gate",
        "expired-approval",
        "already-decided-conflict",
        "irreversible-action",
        "rollback-unavailable",
    ),
    "receipts.json": (
        "accepted",
        "rejected",
        "partial",
        "cancelled",
        "timed-out",
        "missing-evidence",
        "signed-not-recorded",
        "recorded-not-promoted",
        "unknown-exact-cost",
    ),
    "memory-updates.json": (
        "proposed",
        "pending-review",
        "accepted",
        "rejected",
        "superseded",
        "promoted",
        "failed",
        "rolled-back",
    ),
    "stream-frames.ndjson": (
        "monotonic sequence",
        "duplicate",
        "gap",
        "out-of-order",
        "reconnect replay",
        "cursor-too-old/resync",
        "terminal event",
        "cross-execution noise",
    ),
}

EXPECTED_COST_TYPES = {
    "null/unknown",
    "estimated zero",
    "exact zero",
    "estimated nonzero",
    "exact nonzero",
    "incomplete aggregation",
}
EXPECTED_MISSION_COMPONENTS = (
    "ExecutionRef",
    "SwarmPlanDecision",
    "EffectiveRoute",
    "CostObservation",
)
# Stable order is part of the digest contract.  Values prove the file is the
# intended authority rather than a same-named placeholder.
CONTRACT_AUTHORITIES = (
    ("omniagentos/contracts.py", "class ExecutionRef"),
    ("omniagentos/swarm/contracts.py", "class SwarmPlanDecision"),
    ("omniagentos/orgdims/contracts.py", "class OrganizationContext"),
    ("omniagentos/taskcontract/models.py", "class TaskContract"),
    ("omniagentos/verify/report.py", "INSUFFICIENT_EVIDENCE"),
    ("omniagentos/chats/classify.py", "project_suggestion"),
)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _load_family(family_name: str) -> list[dict[str, Any]]:
    raw = (FIXTURE_DIR / family_name).read_text()
    parsed = (
        [json.loads(line) for line in raw.splitlines() if line.strip()]
        if family_name.endswith(".ndjson")
        else json.loads(raw)
    )
    assert isinstance(parsed, list), f"{family_name} must contain a list"
    assert all(isinstance(item, dict) for item in parsed), f"{family_name} entries must be objects"
    return parsed


def _load_corpus() -> dict[str, list[dict[str, Any]]]:
    return {name: _load_family(name) for name in EXPECTED_FAMILIES}


def _shape(
    value: Mapping[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    where: str,
) -> None:
    """Exact family-local shape for records with no production DTO."""
    missing = required - value.keys()
    unknown = value.keys() - allowed
    assert not missing, f"{where}: missing required fields {sorted(missing)}"
    assert not unknown, f"{where}: unknown fields {sorted(unknown)}"


def _resolve_schema(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    assert ref.startswith("#/"), f"unsupported external schema ref {ref!r}"
    resolved: Any = root
    for part in ref[2:].split("/"):
        resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
    assert isinstance(resolved, Mapping)
    return resolved


def _schema_branch_for(
    value: Any, schema: Mapping[str, Any], root: Mapping[str, Any]
) -> Mapping[str, Any]:
    resolved = _resolve_schema(schema, root)
    branches = resolved.get("anyOf")
    if not isinstance(branches, list):
        return resolved
    for branch in branches:
        candidate = _resolve_schema(branch, root)
        kind = candidate.get("type")
        if isinstance(value, Mapping) and (kind == "object" or "properties" in candidate):
            return candidate
        if isinstance(value, list) and kind == "array":
            return candidate
        if value is None and kind == "null":
            return candidate
        if not isinstance(value, (Mapping, list)) and kind not in {"object", "array", "null"}:
            return candidate
    return resolved


def _assert_schema_shape(
    value: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    *,
    where: str,
) -> None:
    """Reject extras recursively using a production model's generated schema.

    Pydantic's default extra policy is permissive.  Fixture validation must be
    stricter without inventing a second field authority, so object fields come
    directly from ``model_json_schema()``.  Explicit free-form mappings such as
    ``PlanIssue.detail`` retain their schema-declared additional-properties.
    """
    selected = _schema_branch_for(value, schema, root)
    if isinstance(value, Mapping):
        properties = selected.get("properties")
        additional = selected.get("additionalProperties")
        if isinstance(properties, Mapping):
            unknown = value.keys() - properties.keys()
            assert additional is True or not unknown, (
                f"{where}: production schema rejects unknown fields {sorted(unknown)}"
            )
            for key, child in value.items():
                if key in properties:
                    _assert_schema_shape(
                        child,
                        properties[key],
                        root,
                        where=f"{where}.{key}",
                    )
                elif isinstance(additional, Mapping):
                    _assert_schema_shape(
                        child,
                        additional,
                        root,
                        where=f"{where}.{key}",
                    )
        return
    if isinstance(value, list):
        item_schema = selected.get("items")
        if isinstance(item_schema, Mapping):
            for index, child in enumerate(value):
                _assert_schema_shape(
                    child,
                    item_schema,
                    root,
                    where=f"{where}[{index}]",
                )


def _validate_model(
    model_cls: type[BaseModel], payload: Mapping[str, Any], *, where: str
) -> BaseModel:
    schema = model_cls.model_json_schema()
    _assert_schema_shape(payload, schema, schema, where=where)
    try:
        return model_cls.model_validate(dict(payload))
    except ValidationError as exc:
        raise AssertionError(f"{where}: {model_cls.__name__} rejected payload: {exc}") from exc


def _assert_task_contract(payload: Mapping[str, Any], *, where: str) -> TaskContract:
    """Apply the production parser plus field sets derived from its dataclasses."""
    _shape(
        payload,
        required={"objective", "acceptance_criteria", "read_set", "write_set", "risk_class"},
        allowed={field.name for field in fields(TaskContract)},
        where=where,
    )
    for index, criterion in enumerate(payload.get("acceptance_criteria") or []):
        assert isinstance(criterion, Mapping)
        _shape(
            criterion,
            required={"id", "condition"},
            allowed={field.name for field in fields(AcceptanceCriterion)},
            where=f"{where}.acceptance_criteria[{index}]",
        )
    for key, authority in (("budgets", Budgets), ("lease", LeaseFields)):
        nested = payload.get(key)
        if nested is not None:
            assert isinstance(nested, Mapping)
            _shape(
                nested,
                required=set(),
                allowed={field.name for field in fields(authority)},
                where=f"{where}.{key}",
            )
    for index, gate in enumerate(payload.get("gates") or []):
        assert isinstance(gate, Mapping)
        _shape(
            gate,
            required={"command"},
            allowed={field.name for field in fields(Gate)},
            where=f"{where}.gates[{index}]",
        )
    try:
        return TaskContract.from_dict(payload)
    except Exception as exc:  # noqa: BLE001 - convert parser failure to assertion
        raise AssertionError(f"{where}: TaskContract rejected payload: {exc}") from exc


def _assert_evidence(evidence: Any, *, where: str) -> None:
    assert isinstance(evidence, Mapping), f"{where}: evidence must be an object"
    _shape(
        evidence,
        required={"digest", "artifact_ref"},
        allowed={
            "digest",
            "artifact_ref",
            "verdict",
            "artifact_hash_at_judgment",
            "current_artifact_hash",
            "signed",
            "recorded",
            "promoted",
        },
        where=where,
    )
    assert isinstance(evidence["digest"], str) and evidence["digest"]
    assert isinstance(evidence["artifact_ref"], str) and evidence["artifact_ref"]
    if evidence.get("verdict") is not None:
        assert evidence["verdict"] in {verdict.value for verdict in Verdict}


def _assert_companies_and_projects(data: list[dict[str, Any]]) -> None:
    companies = [item for item in data if item.get("type") == "company"]
    projects = [item for item in data if item.get("type") == "project"]
    assert len(companies) == 2
    assert projects
    company_ids = {company["id"] for company in companies}
    counts: Counter[str] = Counter()
    for item in data:
        if item.get("type") == "company":
            _shape(
                item,
                required={
                    "type",
                    "id",
                    "name",
                    "slug",
                    "shared_infrastructure",
                    "archived",
                    "status",
                },
                allowed={
                    "type",
                    "id",
                    "name",
                    "slug",
                    "shared_infrastructure",
                    "archived",
                    "status",
                },
                where=f"company {item.get('id')}",
            )
            assert isinstance(item["shared_infrastructure"], bool)
            assert isinstance(item["archived"], bool)
            continue
        assert item.get("type") == "project"
        _shape(
            item,
            required={"type", "id", "name", "kind", "archived", "organization_context"},
            allowed={
                "type",
                "id",
                "name",
                "kind",
                "archived",
                "company_id",
                "inherited_company_id",
                "organization_context",
            },
            where=f"project {item.get('id')}",
        )
        owner = item.get("company_id") or item.get("inherited_company_id")
        assert owner in company_ids
        assert bool(item.get("company_id")) != bool(item.get("inherited_company_id"))
        counts[str(owner)] += 1
        org = item["organization_context"]
        assert isinstance(org, Mapping)
        _validate_model(OrganizationContext, org, where=f"project {item['id']}.organization")
        assert org.get("company_id") == owner
    assert any(count >= 2 for count in counts.values())
    assert any(company["archived"] is True for company in companies)
    assert any(company["shared_infrastructure"] is True for company in companies)
    assert any(project["archived"] is True for project in projects)
    assert any(project.get("inherited_company_id") for project in projects)


def _assert_execution_contract(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={"id", "variant", "execution_ref", "plan_decision", "task_contract"},
        allowed={"id", "variant", "execution_ref", "plan_decision", "task_contract"},
        where=f"execution-contract {item.get('id')}",
    )
    ref = _validate_model(
        ExecutionRef,
        item["execution_ref"],
        where=f"execution-contract {item['id']}.execution_ref",
    )
    decision = _validate_model(
        SwarmPlanDecision,
        item["plan_decision"],
        where=f"execution-contract {item['id']}.plan_decision",
    )
    assert ref.execution_id and ref.company_id and ref.project_id
    if decision.disposition == READY_PLAN_DISPOSITION:
        assert decision.plans
        assert isinstance(item["task_contract"], Mapping)
        _assert_task_contract(
            item["task_contract"],
            where=f"execution-contract {item['id']}.task_contract",
        )
    else:
        assert decision.plans == []
        assert item["task_contract"] is None


def _assert_active_context(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={
            "id",
            "variant",
            "execution_ref",
            "revision",
            "sequence",
            "status",
            "timestamps",
            "organization_context",
            "project_suggestion",
        },
        allowed={
            "id",
            "variant",
            "execution_ref",
            "revision",
            "sequence",
            "status",
            "timestamps",
            "organization_context",
            "project_suggestion",
            "candidates",
            "previous_project_id",
            "current_revision",
            "risk_class",
        },
        where=f"active-context {item.get('id')}",
    )
    expected_status = {
        "confirmed": "confirmed",
        "suggested": "suggested",
        "ambiguous": "ambiguous",
        "corrected": "corrected",
        "stale-revision": "stale",
        "reclassification-pending": "reclassification_pending",
        "high-risk-needs-confirmation": "needs_confirmation",
    }
    assert item["status"] == expected_status[item["variant"]]
    assert isinstance(item["revision"], int) and item["revision"] > 0
    assert isinstance(item["sequence"], int) and item["sequence"] > 0
    _validate_model(
        ExecutionRef,
        item["execution_ref"],
        where=f"active-context {item['id']}.execution_ref",
    )
    _validate_model(
        OrganizationContext,
        item["organization_context"],
        where=f"active-context {item['id']}.organization_context",
    )
    _shape(
        item["timestamps"],
        required={"created_at", "updated_at"},
        allowed={"created_at", "updated_at", "confirmed_at", "corrected_at", "stale_at"},
        where=f"active-context {item['id']}.timestamps",
    )
    suggestion = item["project_suggestion"]
    assert isinstance(suggestion, Mapping)
    _shape(
        suggestion,
        required={"project_id", "confidence", "rationale"},
        allowed={"project_id", "confidence", "rationale"},
        where=f"active-context {item['id']}.project_suggestion",
    )
    assert isinstance(suggestion["confidence"], int | float)
    assert 0 <= suggestion["confidence"] <= 1


def _assert_route_identity(route: EffectiveRoute, *, where: str) -> None:
    effective_lineage = route.effective_model.split("/", 1)[0]
    assert route.model_lineage == effective_lineage, (
        f"{where}: model_lineage does not match effective_model"
    )
    assert route.billing_provider == effective_lineage, (
        f"{where}: billing_provider does not match effective_model"
    )
    adapter = {
        ("xai", "cli"): "cli-grok",
        ("anthropic", "cli"): "cli-claude",
        ("google", "cli"): "cli-gemini",
        ("openai", "cli"): "cli-codex",
    }.get((route.model_lineage, route.transport))
    assert adapter == route.adapter_key, f"{where}: transport/adapter mismatch"


def _assert_formation(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "formation_binding",
            "effective_route",
        },
        allowed={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "formation_binding",
            "effective_route",
            "attempt_index",
        },
        where=f"formation {item.get('id')}",
    )
    binding = _validate_model(
        FormationBinding,
        item["formation_binding"],
        where=f"formation {item['id']}.binding",
    )
    route = _validate_model(
        EffectiveRoute,
        item["effective_route"],
        where=f"formation {item['id']}.route",
    )
    assert isinstance(binding, FormationBinding)
    assert isinstance(route, EffectiveRoute)
    _assert_route_identity(route, where=f"formation {item['id']}.route")
    effective_actor = {
        "xai": "grok",
        "anthropic": "opus",
        "google": "gemini",
        "openai": "codex",
    }[route.model_lineage]
    if route.role == "planner":
        assert binding.planner == effective_actor
    else:
        assert effective_actor in binding.implementers, (
            f"formation {item['id']}: effective route is not bound to the executing role"
        )
    if item["variant"] == "requested-effective-model-mismatch":
        assert route.requested_model != route.effective_model
    elif item["variant"] not in {"sequential-fallback", "escalation"}:
        assert route.requested_model == route.effective_model


def _assert_gate(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "gate_id",
            "status",
            "evidence",
            "command",
            "blocking",
        },
        allowed={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "gate_id",
            "status",
            "evidence",
            "command",
            "blocking",
            "reason",
            "verdict",
        },
        where=f"gate {item.get('id')}",
    )
    status = item["status"]
    assert status in {
        "passed",
        "failed",
        "pending",
        "running",
        "skipped",
        "insufficient_evidence",
        "not_required",
        "unavailable",
    }
    evidence = item["evidence"]
    if evidence is not None:
        _assert_evidence(evidence, where=f"gate {item['id']}.evidence")
    if status in {"pending", "running", "insufficient_evidence", "unavailable"}:
        assert evidence is None
    if status == "passed":
        assert evidence is not None and evidence.get("verdict") == Verdict.PASS.value
    if status == "failed" and item["variant"] != "stale-evidence":
        assert evidence is not None and evidence.get("verdict") == Verdict.FAIL.value
    if status == "skipped":
        assert evidence is not None and evidence.get("verdict") == Verdict.PASS.value
        assert item["blocking"] is False
    if status == "insufficient_evidence":
        assert item.get("verdict") == Verdict.INSUFFICIENT_EVIDENCE.value
    if item["variant"] == "stale-evidence":
        assert evidence is not None
        assert evidence.get("artifact_hash_at_judgment")
        assert evidence.get("current_artifact_hash")
        assert evidence["artifact_hash_at_judgment"] != evidence["current_artifact_hash"]


def _assert_cost_identity(observation: CostObservation, *, where: str) -> None:
    effective_lineage = observation.effective_model.split("/", 1)[0]
    assert observation.provider == effective_lineage, (
        f"{where}: provider does not match effective_model"
    )
    assert observation.model_lineage == effective_lineage
    assert observation.billing_provider == effective_lineage
    adapter = {
        ("xai", "cli"): "cli-grok",
        ("anthropic", "cli"): "cli-claude",
        ("google", "cli"): "cli-gemini",
        ("openai", "cli"): "cli-codex",
    }.get((observation.model_lineage, observation.transport))
    assert adapter == observation.adapter_key, f"{where}: transport/adapter mismatch"


def _assert_receipt(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "status",
            "gate_ids",
            "evidence",
            "cost_observation",
        },
        allowed={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "status",
            "gate_ids",
            "evidence",
            "cost_observation",
            "cost_example",
        },
        where=f"receipt {item.get('id')}",
    )
    assert isinstance(item["gate_ids"], list)
    if item["evidence"] is not None:
        _assert_evidence(item["evidence"], where=f"receipt {item['id']}.evidence")
    observation = _validate_model(
        CostObservation,
        item["cost_observation"],
        where=f"receipt {item['id']}.cost_observation",
    )
    assert isinstance(observation, CostObservation)
    assert observation.execution_id == item["execution_id"]
    _assert_cost_identity(observation, where=f"receipt {item['id']}.cost_observation")
    if item["variant"] == "missing-evidence":
        assert item["status"] == "insufficient_evidence"
        assert item["evidence"] is None
    if item["variant"] == "unknown-exact-cost":
        assert observation.cost_quality is CostQuality.UNKNOWN
        assert observation.cost_usd_decimal is None
        assert observation.cost_usd_nanos is None

    example = item.get("cost_example")
    if example is None:
        return
    assert isinstance(example, Mapping)
    _shape(
        example,
        required={"type", "cost_quality"},
        allowed={
            "type",
            "cost_quality",
            "cost_usd_decimal",
            "cost_usd_nanos",
            "cost_upper_bound_usd_nanos",
        },
        where=f"receipt {item['id']}.cost_example",
    )
    cost_type = example["type"]
    assert cost_type in EXPECTED_COST_TYPES
    if cost_type == "null/unknown":
        assert example.get("cost_quality") == "unknown"
        assert example.get("cost_usd_decimal") is None
        assert example.get("cost_usd_nanos") is None
    elif cost_type == "estimated zero":
        assert example.get("cost_quality") == "estimated"
        assert example.get("cost_upper_bound_usd_nanos") == 0
    elif cost_type == "exact zero":
        assert example.get("cost_quality") == "exact"
        assert example.get("cost_usd_decimal") == "0"
        assert example.get("cost_usd_nanos") == 0
    elif cost_type == "estimated nonzero":
        assert example.get("cost_quality") == "estimated"
        assert int(example.get("cost_upper_bound_usd_nanos") or 0) > 0
    elif cost_type == "exact nonzero":
        assert example.get("cost_quality") == "exact"
        assert int(example.get("cost_usd_nanos") or 0) > 0
    else:
        assert example.get("cost_quality") == "estimated"
        assert example.get("cost_usd_decimal") is None


def _assert_context_manifest(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "source",
            "reason",
            "package_id",
            "items",
            "status",
        },
        allowed={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "source",
            "reason",
            "package_id",
            "items",
            "status",
        },
        where=f"context-manifest {item.get('id')}",
    )
    assert item["source"] and item["reason"] and isinstance(item["items"], list)
    for index, entry in enumerate(item["items"]):
        _shape(
            entry,
            required={"path", "bytes", "included", "omit_reason"},
            allowed={"path", "bytes", "included", "omit_reason"},
            where=f"context-manifest {item['id']}.items[{index}]",
        )


def _assert_skills_tools(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "kind",
            "status",
        },
        allowed={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "kind",
            "status",
            "skill_id",
            "name",
            "loaded",
            "label_only",
            "body_digest",
            "tools_allowed",
            "tool_name",
            "allowed",
            "available",
            "reason",
        },
        where=f"skills-tools {item.get('id')}",
    )
    if item["variant"] == "label-only-skill-rejected":
        assert item.get("label_only") is True
        assert item.get("loaded") is False
        assert item.get("body_digest") is None
    if item["variant"] == "full-skill-loaded":
        assert item.get("loaded") is True and item.get("body_digest")


def _assert_timeline(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "stages",
            "status",
            "reconnect_advertised",
            "replay_required",
        },
        allowed={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "stages",
            "status",
            "reconnect_advertised",
            "replay_required",
            "dependencies",
            "blocker",
            "attempt_index",
            "replay_from_cursor",
            "terminal",
        },
        where=f"timeline {item.get('id')}",
    )
    assert isinstance(item["stages"], list) and item["stages"]
    if item["reconnect_advertised"]:
        assert item["replay_required"] is True
        assert item.get("replay_from_cursor")


def _assert_intervention(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "kind",
            "status",
            "decided",
        },
        allowed={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "kind",
            "status",
            "decided",
            "question",
            "approval_class",
            "tool_name",
            "threshold_usd_nanos",
            "expires_at",
            "expiry_events",
            "prior_decision",
            "conflicting_decision",
            "reason",
            "action",
            "reversible",
            "rollback_available",
        },
        where=f"intervention {item.get('id')}",
    )
    if item["variant"] == "expired-approval":
        events = item.get("expiry_events")
        assert isinstance(events, list)
        for index, event in enumerate(events):
            _shape(
                event,
                required={"time", "type"},
                allowed={"time", "type"},
                where=f"intervention {item['id']}.expiry_events[{index}]",
            )
        assert {(event["time"], event["type"]) for event in events} == {
            ("2026-07-30T11:59:59.999Z", "1ms_before"),
            ("2026-07-30T12:00:00.000Z", "exact"),
            ("2026-07-30T12:00:00.001Z", "1ms_after"),
        }


def _assert_memory(item: dict[str, Any]) -> None:
    _shape(
        item,
        required={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "status",
            "candidate_id",
            "content",
            "provenance",
        },
        allowed={
            "id",
            "variant",
            "execution_id",
            "project_id",
            "company_id",
            "status",
            "candidate_id",
            "content",
            "provenance",
            "reason",
            "superseded_by",
            "promoted_to",
        },
        where=f"memory-update {item.get('id')}",
    )
    provenance = item["provenance"]
    assert isinstance(provenance, Mapping)
    _shape(
        provenance,
        required={"source", "execution_id", "evidence_refs"},
        allowed={"source", "execution_id", "evidence_refs", "rollback_of"},
        where=f"memory-update {item['id']}.provenance",
    )
    assert provenance["execution_id"] == item["execution_id"]


def _assert_stream_frames(data: list[dict[str, Any]]) -> None:
    for item in data:
        _shape(
            item,
            required={"seq", "variant", "execution_id", "frame_type", "cursor", "payload"},
            allowed={
                "seq",
                "variant",
                "execution_id",
                "frame_type",
                "cursor",
                "payload",
                "duplicate_of_seq",
                "gap_from_seq",
                "reconnect",
                "replay_supported",
                "terminal",
            },
            where=f"stream-frame {item.get('variant')}",
        )
        assert isinstance(item["seq"], int) and item["seq"] >= 0
        assert isinstance(item["payload"], Mapping)
        payload_required, payload_allowed = {
            "monotonic sequence": ({"stage", "message"}, {"stage", "message"}),
            "duplicate": ({"stage", "message"}, {"stage", "message"}),
            "gap": ({"stage", "message"}, {"stage", "message"}),
            "out-of-order": ({"stage", "message"}, {"stage", "message"}),
            "reconnect replay": (
                {"replay_from_cursor", "replayed_seqs"},
                {"replay_from_cursor", "replayed_seqs"},
            ),
            "cursor-too-old/resync": (
                {"reason", "oldest_retained_seq"},
                {"reason", "oldest_retained_seq"},
            ),
            "terminal event": ({"status"}, {"status"}),
            "cross-execution noise": ({"stage", "message"}, {"stage", "message"}),
        }[item["variant"]]
        _shape(
            item["payload"],
            required=payload_required,
            allowed=payload_allowed,
            where=f"stream-frame {item['variant']}.payload",
        )

    by_variant = {item["variant"]: item for item in data}
    first = by_variant["monotonic sequence"]
    duplicate = by_variant["duplicate"]
    gap = by_variant["gap"]
    late = by_variant["out-of-order"]
    replay = by_variant["reconnect replay"]
    resync = by_variant["cursor-too-old/resync"]
    terminal = by_variant["terminal event"]

    assert duplicate["seq"] == first["seq"]
    assert duplicate.get("duplicate_of_seq") == first["seq"]
    assert duplicate["cursor"] == first["cursor"]
    assert duplicate["payload"] == first["payload"]
    assert gap.get("gap_from_seq") == first["seq"]
    assert gap["seq"] == first["seq"] + 2
    assert late["seq"] == first["seq"] + 1
    assert first["seq"] < late["seq"] < gap["seq"]

    replayed = replay["payload"]["replayed_seqs"]
    assert isinstance(replayed, list) and replayed
    assert replayed == [late["seq"], gap["seq"]]
    assert all(seq > first["seq"] for seq in replayed)
    assert replay["reconnect"] is True
    assert replay["replay_supported"] is True
    assert replay["frame_type"] == "replay"
    assert replay["cursor"] == replay["payload"]["replay_from_cursor"] == first["cursor"]
    assert replay["seq"] == gap["seq"] + 1

    assert resync["frame_type"] == "resync"
    assert resync["reconnect"] is True
    assert resync["replay_supported"] is False
    assert resync["payload"]["reason"] == "cursor_too_old"
    assert resync["payload"]["oldest_retained_seq"] == first["seq"]
    assert terminal["terminal"] is True
    assert terminal["frame_type"] == "terminal"
    assert terminal["seq"] == replay["seq"] + 1
    for frame in (duplicate, gap, late, replay, resync, terminal):
        assert frame["execution_id"] == first["execution_id"]
    assert by_variant["cross-execution noise"]["execution_id"] != first["execution_id"]


def _validate_family(family_name: str, data: list[dict[str, Any]]) -> None:
    assert data, f"{family_name} must not be empty"
    expected = EXPECTED_VARIANTS.get(family_name)
    if expected is not None:
        assert Counter(item.get("variant") for item in data) == Counter(expected), (
            f"Variants mismatch for {family_name}"
        )
    if family_name == "companies-projects.json":
        _assert_companies_and_projects(data)
        return
    if family_name == "stream-frames.ndjson":
        _assert_stream_frames(data)
        return
    validators = {
        "active-context.json": _assert_active_context,
        "context-manifests.json": _assert_context_manifest,
        "execution-contracts.json": _assert_execution_contract,
        "formations.json": _assert_formation,
        "gate-matrix.json": _assert_gate,
        "interventions.json": _assert_intervention,
        "memory-updates.json": _assert_memory,
        "receipts.json": _assert_receipt,
        "skills-tools.json": _assert_skills_tools,
        "timeline.json": _assert_timeline,
    }
    assert family_name in validators, f"unknown fixture family {family_name}"
    for item in data:
        validators[family_name](item)


def _execution_id_of(item: Mapping[str, Any]) -> str | None:
    if item.get("execution_id") is not None:
        return str(item["execution_id"])
    ref = item.get("execution_ref")
    if isinstance(ref, Mapping) and ref.get("execution_id") is not None:
        return str(ref["execution_id"])
    return None


def _project_id_of(item: Mapping[str, Any]) -> str | None:
    if item.get("project_id") is not None:
        return str(item["project_id"])
    ref = item.get("execution_ref")
    if isinstance(ref, Mapping) and ref.get("project_id") is not None:
        return str(ref["project_id"])
    return None


def _company_id_of(item: Mapping[str, Any]) -> str | None:
    if item.get("company_id") is not None:
        return str(item["company_id"])
    ref = item.get("execution_ref")
    if isinstance(ref, Mapping) and ref.get("company_id") is not None:
        return str(ref["company_id"])
    return None


def _assert_cross_family_invariants(
    corpus: Mapping[str, list[dict[str, Any]]],
) -> None:
    entities = corpus["companies-projects.json"]
    companies = {item["id"]: item for item in entities if item["type"] == "company"}
    projects = {item["id"]: item for item in entities if item["type"] == "project"}
    project_owners = {
        project_id: project.get("company_id") or project.get("inherited_company_id")
        for project_id, project in projects.items()
    }
    contracts = corpus["execution-contracts.json"]
    contracts_by_exec = {
        contract["execution_ref"]["execution_id"]: contract for contract in contracts
    }
    assert len(contracts_by_exec) == len(contracts)

    linked_families = tuple(
        name
        for name in EXPECTED_FAMILIES
        if name not in {"companies-projects.json", "stream-frames.ndjson"}
    )
    for family_name in linked_families:
        for item in corpus[family_name]:
            execution_id = _execution_id_of(item)
            assert execution_id in contracts_by_exec, (
                f"{family_name}: unknown execution_id {execution_id!r}"
            )
            contract_ref = contracts_by_exec[execution_id]["execution_ref"]
            project_id = _project_id_of(item) or contract_ref["project_id"]
            company_id = _company_id_of(item) or contract_ref["company_id"]
            assert project_id in projects, f"{family_name}: unknown project_id {project_id!r}"
            assert company_id in companies, f"{family_name}: unknown company_id {company_id!r}"
            assert project_owners[project_id] == company_id, (
                f"{family_name}: company {company_id!r} does not own project {project_id!r}"
            )
            assert company_id == contract_ref["company_id"], (
                f"{family_name}: company disagrees with execution contract"
            )
    for frame in corpus["stream-frames.ndjson"]:
        execution_id = frame["execution_id"]
        assert execution_id in contracts_by_exec, (
            f"stream-frames.ndjson: unknown execution_id {execution_id!r}"
        )
        contract_ref = contracts_by_exec[execution_id]["execution_ref"]
        assert project_owners[contract_ref["project_id"]] == contract_ref["company_id"]

    for item in corpus["active-context.json"]:
        ref = item["execution_ref"]
        org = item["organization_context"]
        assert ref["company_id"] == org["company_id"]
        suggestion = item["project_suggestion"]["project_id"]
        if suggestion is not None:
            assert suggestion in projects
            assert project_owners[suggestion] == ref["company_id"]

    ready_execs = {
        contract["execution_ref"]["execution_id"]
        for contract in contracts
        if contract["plan_decision"]["disposition"] == READY_PLAN_DISPOSITION
    }
    for formation in corpus["formations.json"]:
        if formation["variant"] not in {"partial-formation", "sequential-fallback"}:
            assert formation["execution_id"] in ready_execs, (
                f"formation {formation['id']} is bound to a non-ready execution"
            )

    routes_by_exec: dict[str, set[tuple[str, ...]]] = {}
    for formation in corpus["formations.json"]:
        route = formation["effective_route"]
        routes_by_exec.setdefault(formation["execution_id"], set()).add(
            (
                route["effective_model"],
                route["model_lineage"],
                route["billing_provider"],
                route["transport"],
                route["adapter_key"],
            )
        )

    gates = {gate["id"]: gate for gate in corpus["gate-matrix.json"]}
    assert len(gates) == len(corpus["gate-matrix.json"])
    compatible_gate_statuses = {
        "accepted": {"passed"},
        "signed_not_recorded": {"passed"},
        "recorded_not_promoted": {"passed"},
        "rejected": {"failed", "unavailable"},
        "partial": {"insufficient_evidence"},
        "insufficient_evidence": {"insufficient_evidence"},
        "timed_out": {"running"},
    }
    for receipt in corpus["receipts.json"]:
        contract_ref = contracts_by_exec[receipt["execution_id"]]["execution_ref"]
        observation = receipt["cost_observation"]
        assert observation["request_id"] == contract_ref["request_id"]
        route_identity = (
            observation["effective_model"],
            observation["model_lineage"],
            observation["billing_provider"],
            observation["transport"],
            observation["adapter_key"],
        )
        if receipt["execution_id"] in routes_by_exec and observation["request_state"] != "not_sent":
            assert route_identity in routes_by_exec[receipt["execution_id"]], (
                f"receipt {receipt['id']}: cost observation has no compatible effective route"
            )

        gate_ids = receipt["gate_ids"]
        if receipt["status"] in {
            "accepted",
            "signed_not_recorded",
            "recorded_not_promoted",
        }:
            assert gate_ids, f"receipt {receipt['id']}: accepted outcome requires gate linkage"
        for gate_id in gate_ids:
            assert gate_id in gates, f"receipt {receipt['id']}: unknown gate {gate_id!r}"
            gate = gates[gate_id]
            assert gate["execution_id"] == receipt["execution_id"], (
                f"receipt {receipt['id']}: gate belongs to another execution"
            )
            assert gate["project_id"] == receipt["project_id"]
            assert gate["company_id"] == receipt["company_id"]
            expected_statuses = compatible_gate_statuses.get(receipt["status"])
            if expected_statuses is not None:
                assert gate["status"] in expected_statuses, (
                    f"receipt {receipt['id']}: incompatible linked gate {gate['status']!r}"
                )

    reconnects = [
        timeline for timeline in corpus["timeline.json"] if timeline["reconnect_advertised"] is True
    ]
    replay = next(
        frame for frame in corpus["stream-frames.ndjson"] if frame["variant"] == "reconnect replay"
    )
    assert reconnects
    for timeline in reconnects:
        assert timeline["replay_required"] is True
        assert timeline["replay_from_cursor"] == replay["payload"]["replay_from_cursor"]
        assert timeline["execution_id"] == replay["execution_id"]


def validate_mission_corpus(
    corpus: Mapping[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Validate the complete corpus; hostile tests call this exact entry point."""
    actual = _load_corpus() if corpus is None else corpus
    assert set(actual) == set(EXPECTED_FAMILIES)
    for family_name in EXPECTED_FAMILIES:
        _validate_family(family_name, actual[family_name])
    _assert_cross_family_invariants(actual)


def _fixture_set_digest() -> str:
    hashes: list[str] = []
    for family_name in EXPECTED_FAMILIES:
        data = _load_family(family_name)
        canonical = (
            "\n".join(json.dumps(item, sort_keys=True) for item in data)
            if family_name.endswith(".ndjson")
            else json.dumps(data, sort_keys=True)
        )
        hashes.append(_digest(canonical))
    return _digest("".join(hashes))


def _contract_source_digest() -> str:
    digest = hashlib.sha256()
    for path_text, _ in CONTRACT_AUTHORITIES:
        encoded_path = path_text.encode()
        content = Path(path_text).read_bytes()
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _mission_components_present_in_python() -> bool:
    contracts_source = Path(CONTRACT_AUTHORITIES[0][0]).read_text()
    swarm_source = Path(CONTRACT_AUTHORITIES[1][0]).read_text()
    return (
        "class ExecutionRef" in contracts_source
        and "class EffectiveRoute" in contracts_source
        and "class CostObservation" in contracts_source
        and "class SwarmPlanDecision" in swarm_source
    )


def _openapi_mission_component_schemas(openapi: Mapping[str, Any]) -> list[str]:
    schemas = openapi.get("components", {}).get("schemas", {})
    if not isinstance(schemas, Mapping):
        return []
    return [name for name in EXPECTED_MISSION_COMPONENTS if name in schemas]


def _openapi_mission_routes_present(openapi: Mapping[str, Any]) -> bool:
    paths = openapi.get("paths", {})
    if not isinstance(paths, Mapping):
        return False
    markers = (
        "/api/receipts",
        "/api/task-contracts",
        "/api/intake/formations",
        "/api/context/packages/",
    )
    return all(any(marker in str(path) for path in paths) for marker in markers)


def test_mission_fixture_semantics() -> None:
    actual_families = sorted(
        path.name
        for path in FIXTURE_DIR.iterdir()
        if path.is_file() and path.name != PARITY_PATH.name
    )
    assert actual_families == list(EXPECTED_FAMILIES)
    validate_mission_corpus()
    receipts = _load_family("receipts.json")
    assert {
        item["cost_example"]["type"] for item in receipts if "cost_example" in item
    } == EXPECTED_COST_TYPES


def test_production_authorities_accept_embedded_payloads_strictly() -> None:
    """Exercise every embedded production authority through strict schema validation."""
    validate_mission_corpus()


# REWORK-2's eleven blockers are permanent overlays.  Each starts from a fresh
# corpus and invokes the same validator used by the positive test.
def _unknown_top_level(corpus: dict[str, list[dict[str, Any]]]) -> None:
    corpus["active-context.json"][0]["surprise"] = "must fail"


def _missing_active_status(corpus: dict[str, list[dict[str, Any]]]) -> None:
    del corpus["active-context.json"][0]["status"]


def _nested_execution_ref_extra(corpus: dict[str, list[dict[str, Any]]]) -> None:
    corpus["active-context.json"][0]["execution_ref"]["invented"] = True


def _project_company_mismatch(corpus: dict[str, list[dict[str, Any]]]) -> None:
    corpus["formations.json"][0]["company_id"] = "comp_beta"


def _route_lineage_mismatch(corpus: dict[str, list[dict[str, Any]]]) -> None:
    corpus["formations.json"][0]["effective_route"]["model_lineage"] = "anthropic"


def _cost_transport_adapter_mismatch(corpus: dict[str, list[dict[str, Any]]]) -> None:
    corpus["receipts.json"][0]["cost_observation"]["transport"] = "openrouter"


def _passed_gate_with_failure(corpus: dict[str, list[dict[str, Any]]]) -> None:
    gate = next(item for item in corpus["gate-matrix.json"] if item["status"] == "passed")
    gate["evidence"]["verdict"] = "fail"


def _accepted_receipt_failed_gate(corpus: dict[str, list[dict[str, Any]]]) -> None:
    receipt = next(item for item in corpus["receipts.json"] if item["variant"] == "accepted")
    receipt["gate_ids"] = ["gate_failed"]


def _accepted_receipt_without_gate(corpus: dict[str, list[dict[str, Any]]]) -> None:
    receipt = next(item for item in corpus["receipts.json"] if item["variant"] == "accepted")
    receipt["gate_ids"] = []


def _empty_reconnect_replay(corpus: dict[str, list[dict[str, Any]]]) -> None:
    replay = next(
        item for item in corpus["stream-frames.ndjson"] if item["variant"] == "reconnect replay"
    )
    replay["payload"]["replayed_seqs"] = []


def _corrupt_duplicate_gap_metadata(corpus: dict[str, list[dict[str, Any]]]) -> None:
    duplicate = next(
        item for item in corpus["stream-frames.ndjson"] if item["variant"] == "duplicate"
    )
    duplicate["duplicate_of_seq"] = 999


@pytest.mark.parametrize(
    ("mutation_id", "mutator"),
    [
        ("arbitrary_top_level_unknown", _unknown_top_level),
        ("active_context_missing_status", _missing_active_status),
        ("nested_execution_ref_extra", _nested_execution_ref_extra),
        ("project_company_ownership_mismatch", _project_company_mismatch),
        ("effective_route_lineage_mismatch", _route_lineage_mismatch),
        ("cost_transport_adapter_mismatch", _cost_transport_adapter_mismatch),
        ("passed_gate_with_failure_evidence", _passed_gate_with_failure),
        ("accepted_receipt_linked_to_failed_gate", _accepted_receipt_failed_gate),
        ("accepted_receipt_without_gate", _accepted_receipt_without_gate),
        ("reconnect_empty_replay", _empty_reconnect_replay),
        ("corrupt_duplicate_gap_metadata", _corrupt_duplicate_gap_metadata),
    ],
)
def test_rework_2_hostile_overlays_turn_red(
    mutation_id: str,
    mutator: Callable[[dict[str, list[dict[str, Any]]]], None],
) -> None:
    corpus = copy.deepcopy(_load_corpus())
    mutator(corpus)
    with pytest.raises((AssertionError, ValidationError), match=r".+"):
        validate_mission_corpus(corpus)


# Preserve the earlier correction's independent false-green cases too.
def _missing_unfavorable_variant(corpus: dict[str, list[dict[str, Any]]]) -> None:
    corpus["receipts.json"] = [
        item for item in corpus["receipts.json"] if item["variant"] != "rejected"
    ]


def _collapse_requested_effective_mismatch(corpus: dict[str, list[dict[str, Any]]]) -> None:
    route = next(
        item
        for item in corpus["formations.json"]
        if item["variant"] == "requested-effective-model-mismatch"
    )["effective_route"]
    route["effective_model"] = route["requested_model"]


def _unknown_cost_to_zero(corpus: dict[str, list[dict[str, Any]]]) -> None:
    observation = next(
        item for item in corpus["receipts.json"] if item["variant"] == "unknown-exact-cost"
    )["cost_observation"]
    observation["cost_usd_decimal"] = "0"
    observation["cost_usd_nanos"] = 0


def _non_ready_plan_content(corpus: dict[str, list[dict[str, Any]]]) -> None:
    refused = next(
        item for item in corpus["execution-contracts.json"] if item["variant"] == "refused"
    )
    refused["plan_decision"]["plans"] = [
        {
            "goal": "illegal",
            "tasks": [{"id": "illegal", "owned_paths": ["x/"]}],
        }
    ]


def _unknown_execution_link(corpus: dict[str, list[dict[str, Any]]]) -> None:
    receipt = corpus["receipts.json"][0]
    receipt["execution_id"] = "exec_missing"
    receipt["cost_observation"]["execution_id"] = "exec_missing"


def _gate_field_on_receipt(corpus: dict[str, list[dict[str, Any]]]) -> None:
    corpus["receipts.json"][0]["gate_id"] = "G3"


def _reconnect_not_required(corpus: dict[str, list[dict[str, Any]]]) -> None:
    timeline = next(
        item for item in corpus["timeline.json"] if item["variant"] == "reconnect-replay"
    )
    timeline["replay_required"] = False


@pytest.mark.parametrize(
    ("mutation_id", "mutator"),
    [
        ("cross_family_field_injection", _gate_field_on_receipt),
        ("missing_unfavorable_variant", _missing_unfavorable_variant),
        ("hidden_requested_effective_mismatch", _collapse_requested_effective_mismatch),
        ("unknown_cost_coerced_to_zero", _unknown_cost_to_zero),
        ("invalid_non_ready_plan", _non_ready_plan_content),
        ("missing_execution_contract_link", _unknown_execution_link),
        ("reconnect_advertised_without_replay", _reconnect_not_required),
    ],
)
def test_rework_1_hostile_overlays_stay_red(
    mutation_id: str,
    mutator: Callable[[dict[str, list[dict[str, Any]]]], None],
) -> None:
    corpus = copy.deepcopy(_load_corpus())
    mutator(corpus)
    with pytest.raises((AssertionError, ValidationError), match=r".+"):
        validate_mission_corpus(corpus)


def test_openapi_and_transport_parity_are_explicitly_blocked() -> None:
    openapi_content = OPENAPI_PATH.read_text()
    openapi = json.loads(openapi_content)
    parity = json.loads(PARITY_PATH.read_text())

    assert _mission_components_present_in_python() is True
    assert parity["typed_mission_components_present"] is True
    assert _openapi_mission_component_schemas(openapi) == []
    assert parity["observed_mission_components"] == []
    assert parity["expected_mission_components"] == list(EXPECTED_MISSION_COMPONENTS)
    assert _openapi_mission_routes_present(openapi) is False

    assert parity["dto_parity_claimed"] is False
    assert parity["schema_parity"]["status"] == "blocked"
    assert parity["openapi_parity"]["status"] == "blocked"
    assert parity["transport_parity"]["status"] == "blocked_pending_mission_client"
    for seam in ("schema_parity", "openapi_parity", "transport_parity"):
        claim = str(parity[seam]).lower()
        assert "passed" not in claim
        assert "skipped" not in claim

    assert parity["openapi_artifact_verified"] is True
    assert parity["fixture_semantics_status"] == "passed"
    declared_authorities = parity["contract_authority"]
    expected_authorities = [path for path, _ in CONTRACT_AUTHORITIES]
    assert declared_authorities == expected_authorities
    for path_text, authority_token in CONTRACT_AUTHORITIES:
        authority_path = Path(path_text)
        assert authority_path.is_file()
        assert authority_token in authority_path.read_text()

    assert parity["openapi_sha"] == _digest(openapi_content)
    assert parity["fixture_set_sha"] == _fixture_set_digest()
    assert parity["contract_source_sha"] == _contract_source_digest()
    assert parity.get("openapi_parity_status") not in {"passed", "skipped"}
    assert parity.get("transport_parity_status") not in {"passed", "skipped"}
