"""TaskContract — bounded attempt intent for OmniAgentOS.

Additive module: does **not** edit frozen ``omniagentos.contracts``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "AcceptanceCriterion",
    "Budgets",
    "Gate",
    "LeaseFields",
    "RiskClass",
    "TaskContract",
    "TaskContractError",
]


class TaskContractError(ValueError):
    """Invalid TaskContract payload."""


class RiskClass(StrEnum):
    R0 = "R0"  # reversible, local, low blast
    R1 = "R1"  # moderate; standard gates
    R2 = "R2"  # high; independent review
    R3 = "R3"  # critical; human / multi-judge


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    id: str
    condition: str
    evidence_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "condition": self.condition,
            "evidence_required": self.evidence_required,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> AcceptanceCriterion:
        cid = str(raw.get("id") or "").strip()
        condition = str(raw.get("condition") or "").strip()
        if not cid:
            raise TaskContractError("acceptance criterion id must be non-empty")
        if not condition:
            raise TaskContractError(f"acceptance criterion {cid!r} condition must be non-empty")
        ev = raw.get("evidence_required", True)
        if isinstance(ev, str):
            # Legacy string form from older drafts → truthy if non-empty.
            evidence_required = bool(ev.strip())
        else:
            evidence_required = bool(ev)
        return cls(id=cid, condition=condition, evidence_required=evidence_required)


@dataclass(frozen=True, slots=True)
class Budgets:
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_seconds: int | None = None
    max_tool_calls: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_wall_seconds": self.max_wall_seconds,
            "max_tool_calls": self.max_tool_calls,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> Budgets:
        if not raw:
            return cls()
        return cls(
            max_tokens=_opt_int(raw.get("max_tokens")),
            max_cost_usd=_opt_float(raw.get("max_cost_usd")),
            max_wall_seconds=_opt_int(raw.get("max_wall_seconds")),
            max_tool_calls=_opt_int(raw.get("max_tool_calls")),
        )


@dataclass(frozen=True, slots=True)
class LeaseFields:
    lease_id: str | None = None
    holder: str | None = None
    expires_at: str | None = None  # ISO-8601
    generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "holder": self.holder,
            "expires_at": self.expires_at,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> LeaseFields:
        if not raw:
            return cls()
        gen = raw.get("generation", 0)
        return cls(
            lease_id=_opt_str(raw.get("lease_id")),
            holder=_opt_str(raw.get("holder")),
            expires_at=_opt_str(raw.get("expires_at")),
            generation=int(gen) if gen is not None else 0,
        )


@dataclass(frozen=True, slots=True)
class Gate:
    command: str
    cwd: str | None = None
    timeout_s: int = 600
    expected_exit: int = 0
    thresholds: dict[str, Any] = field(default_factory=dict)
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "timeout_s": self.timeout_s,
            "expected_exit": self.expected_exit,
            "thresholds": dict(self.thresholds),
            "blocking": self.blocking,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Gate:
        cmd = str(raw.get("command") or "").strip()
        if not cmd:
            raise TaskContractError("gate command must be non-empty")
        cwd = _opt_str(raw.get("cwd"))
        timeout_s = int(raw.get("timeout_s", 600))
        expected_exit = int(raw.get("expected_exit", 0))
        thresholds = dict(raw.get("thresholds") or {})
        blocking = bool(raw.get("blocking", True))
        return cls(
            command=cmd,
            cwd=cwd,
            timeout_s=timeout_s,
            expected_exit=expected_exit,
            thresholds=thresholds,
            blocking=blocking,
        )


@dataclass(frozen=True, slots=True)
class TaskContract:
    """One bounded attempt: objective + criteria + scope + budgets + lease."""

    objective: str
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    read_set: tuple[str, ...]
    write_set: tuple[str, ...]
    risk_class: RiskClass
    budgets: Budgets = field(default_factory=Budgets)
    lease: LeaseFields = field(default_factory=LeaseFields)
    task_id: str | None = None
    lineage_id: str | None = None
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    out_of_scope_paths: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    security_requirements: tuple[str, ...] = ()
    performance_requirements: tuple[str, ...] = ()
    retry_limit: int | None = None
    escalation_path: str | None = None
    handoff_role: str | None = None
    gates: tuple[Gate, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out = {
            "objective": self.objective,
            "acceptance_criteria": [c.to_dict() for c in self.acceptance_criteria],
            "read_set": list(self.read_set),
            "write_set": list(self.write_set),
            "risk_class": self.risk_class.value,
            "budgets": self.budgets.to_dict(),
            "lease": self.lease.to_dict(),
            "task_id": self.task_id,
            "lineage_id": self.lineage_id,
            "version": self.version,
            "metadata": dict(self.metadata),
        }
        if self.out_of_scope_paths:
            out["out_of_scope_paths"] = list(self.out_of_scope_paths)
        if self.non_goals:
            out["non_goals"] = list(self.non_goals)
        if self.security_requirements:
            out["security_requirements"] = list(self.security_requirements)
        if self.performance_requirements:
            out["performance_requirements"] = list(self.performance_requirements)
        if self.retry_limit is not None:
            out["retry_limit"] = self.retry_limit
        if self.escalation_path is not None:
            out["escalation_path"] = self.escalation_path
        if self.handoff_role is not None:
            out["handoff_role"] = self.handoff_role
        if self.gates:
            out["gates"] = [g.to_dict() for g in self.gates]
        return out

    def _hash_payload(self) -> dict[str, Any]:
        """Canonical intent payload excluding volatile lease fields.

        Lease identity (lease_id/holder/expires_at/generation) may change as
        workers renew without changing the contract's objective or scope, so
        the entire ``lease`` object is omitted from ``contract_hash``.
        """
        data = self.to_dict()
        data.pop("lease", None)
        return data

    def contract_hash(self) -> str:
        """SHA-256 of canonical JSON intent (lease excluded)."""
        raw = json.dumps(
            self._hash_payload(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )

    @classmethod
    def from_json(cls, raw: str) -> TaskContract:
        data = json.loads(raw)
        if not isinstance(data, Mapping):
            raise TaskContractError("TaskContract JSON must be an object")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskContract:
        objective = str(data.get("objective") or "").strip()
        if not objective:
            raise TaskContractError("objective must be non-empty")

        criteria_raw = data.get("acceptance_criteria") or []
        if not isinstance(criteria_raw, Sequence) or isinstance(criteria_raw, (str, bytes)):
            raise TaskContractError("acceptance_criteria must be a list")
        criteria = tuple(
            AcceptanceCriterion.from_dict(c) for c in criteria_raw if isinstance(c, Mapping)
        )
        if not criteria:
            raise TaskContractError("TaskContract requires at least one acceptance criterion")
        ids = [c.id for c in criteria]
        if len(ids) != len(set(ids)):
            raise TaskContractError("acceptance criterion ids must be unique")

        try:
            risk = RiskClass(str(data.get("risk_class") or RiskClass.R1))
        except ValueError as exc:
            raise TaskContractError(f"invalid risk_class: {data.get('risk_class')!r}") from exc

        version = int(data.get("version") or 1)
        if version < 1:
            raise TaskContractError("version must be >= 1")

        budgets_raw = data.get("budgets")
        if not isinstance(budgets_raw, Mapping):
            budgets_raw = {
                k: data.get(k)
                for k in ("max_tokens", "max_cost_usd", "max_wall_seconds", "max_tool_calls")
                if k in data
            }

        gates_raw = data.get("gates") or []
        if not isinstance(gates_raw, Sequence) or isinstance(gates_raw, (str, bytes)):
            raise TaskContractError("gates must be a list")
        gates = tuple(Gate.from_dict(g) for g in gates_raw if isinstance(g, Mapping))

        return cls(
            objective=objective,
            acceptance_criteria=criteria,
            read_set=_normalize_path_set(data.get("read_set")),
            write_set=_normalize_path_set(data.get("write_set")),
            risk_class=risk,
            budgets=Budgets.from_dict(budgets_raw if isinstance(budgets_raw, Mapping) else None),
            lease=LeaseFields.from_dict(
                data.get("lease") if isinstance(data.get("lease"), Mapping) else None
            ),
            task_id=_opt_str(data.get("task_id")),
            lineage_id=_opt_str(data.get("lineage_id")),
            version=version,
            metadata=dict(data.get("metadata") or {}),
            out_of_scope_paths=_normalize_path_set(data.get("out_of_scope_paths")),
            non_goals=_normalize_path_set(data.get("non_goals")),
            security_requirements=_normalize_path_set(data.get("security_requirements")),
            performance_requirements=_normalize_path_set(data.get("performance_requirements")),
            retry_limit=_opt_int(data.get("retry_limit")),
            escalation_path=_opt_str(data.get("escalation_path")),
            handoff_role=_opt_str(data.get("handoff_role")),
            gates=gates,
        )


def _normalize_path_set(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise TaskContractError("read_set/write_set must be a list of strings")
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text:
            out.append(text)
    return tuple(out)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
