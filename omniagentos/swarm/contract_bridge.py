"""Bridge TaskContract adoption onto swarm tasks (M-16).

Builds a TaskContract from swarm task + swarm_json, adopts it into the
persisted store, and stamps identity fields back onto swarm_json.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from omniagentos.taskcontract.models import (
    AcceptanceCriterion,
    Budgets,
    RiskClass,
    TaskContract,
    TaskContractError,
)
from omniagentos.taskcontract.store import TaskContractRecord, TaskContractStore
from omniagentos.taskcontract.transitions import ContractState

LOG = logging.getLogger(__name__)

__all__ = [
    "adopt_swarm_task_contract",
    "build_task_contract_from_swarm",
    "transition_swarm_contract",
]


def _risk(swarm_risk: str) -> RiskClass:
    raw = str(swarm_risk or "none").strip().lower()
    mapping = {
        "none": RiskClass.R0,
        "read_only": RiskClass.R0,
        "reversible_internal": RiskClass.R1,
        "external": RiskClass.R2,
        "bounded_external": RiskClass.R2,
        "deploy": RiskClass.R2,
        "destructive": RiskClass.R3,
        "irreversible": RiskClass.R3,
    }
    return mapping.get(raw, RiskClass.R1)


def build_task_contract_from_swarm(
    *,
    task: Mapping[str, Any],
    swarm_json: Mapping[str, Any],
    task_id: str,
) -> TaskContract:
    """Construct a validated TaskContract from swarm task signals."""
    title = str(task.get("title") or swarm_json.get("task_key") or task_id).strip()
    description = str(task.get("description") or "").strip()
    acceptance = str(swarm_json.get("acceptance") or task.get("acceptance") or "").strip()
    objective = description or title
    if not objective:
        objective = f"swarm task {task_id}"

    criteria: list[AcceptanceCriterion] = []
    if acceptance:
        criteria.append(
            AcceptanceCriterion(id="acceptance", condition=acceptance, evidence_required=True)
        )
    verify = str(swarm_json.get("verify_command") or "").strip()
    if verify:
        criteria.append(
            AcceptanceCriterion(
                id="verify_command",
                condition=f"verify_command exits 0: {verify}",
                evidence_required=True,
            )
        )
    if not criteria:
        criteria.append(
            AcceptanceCriterion(
                id="deliver",
                condition=f"Complete task: {title or task_id}",
                evidence_required=True,
            )
        )

    owned = swarm_json.get("owned_paths") or task.get("owned_paths") or []
    if not isinstance(owned, (list, tuple)):
        owned = []
    write_set = tuple(str(p).strip() for p in owned if str(p).strip())
    read_set = write_set  # swarm tasks typically read their owned surface

    risk = _risk(str(swarm_json.get("risk_class") or task.get("risk_class") or "none"))
    from omniagentos.taskcontract.models import _normalize_path_set, _opt_int, _opt_str

    out_of_scope_paths = _normalize_path_set(
        swarm_json.get("out_of_scope_paths") or task.get("out_of_scope_paths")
    )
    non_goals = _normalize_path_set(swarm_json.get("non_goals") or task.get("non_goals"))
    security_requirements = _normalize_path_set(
        swarm_json.get("security_requirements") or task.get("security_requirements")
    )
    performance_requirements = _normalize_path_set(
        swarm_json.get("performance_requirements") or task.get("performance_requirements")
    )
    retry_limit = _opt_int(
        swarm_json.get("retry_limit") if "retry_limit" in swarm_json else task.get("retry_limit")
    )
    escalation_path = _opt_str(swarm_json.get("escalation_path") or task.get("escalation_path"))
    handoff_role = _opt_str(swarm_json.get("handoff_role") or task.get("handoff_role"))

    return TaskContract(
        objective=objective,
        acceptance_criteria=tuple(criteria),
        read_set=read_set,
        write_set=write_set,
        risk_class=risk,
        budgets=Budgets(),
        task_id=str(task_id),
        lineage_id=str(swarm_json.get("lineage_id") or task_id),
        version=int(swarm_json.get("contract_version") or 1),
        metadata={
            "lane": "swarm",
            "task_key": swarm_json.get("task_key"),
            "title": title,
        },
        out_of_scope_paths=out_of_scope_paths,
        non_goals=non_goals,
        security_requirements=security_requirements,
        performance_requirements=performance_requirements,
        retry_limit=retry_limit,
        escalation_path=escalation_path,
        handoff_role=handoff_role,
    )


def adopt_swarm_task_contract(
    *,
    db_path: str | None,
    task: Mapping[str, Any],
    swarm_json: Mapping[str, Any],
    task_id: str,
    dal: Any | None = None,
) -> tuple[TaskContractRecord | None, dict[str, Any]]:
    """Adopt a TaskContract for the swarm task and stamp swarm_json.

    Returns ``(record_or_none, updated_swarm_json)``. Failures are logged and
    never block spawn — but when adoption succeeds, the ledger hash is
    authoritative for later transitions.
    """
    merged = dict(swarm_json)
    try:
        contract = build_task_contract_from_swarm(task=task, swarm_json=swarm_json, task_id=task_id)
        store = TaskContractStore(database=db_path)
        try:
            rec = store.adopt(
                contract,
                lane="swarm",
                ref_id=str(task_id),
                actor="swarm.spawn",
                metadata={"source": "contract_bridge"},
            )
            # Move to active for an in-flight spawn.
            if rec.state == ContractState.ADOPTED.value:
                rec = store.transition(
                    rec.id,
                    ContractState.ACTIVE,
                    reason="spawn_started",
                    actor="swarm.spawn",
                    expected_hash=rec.contract_hash,
                )
            merged["task_contract_id"] = rec.id
            merged["task_contract_hash"] = rec.contract_hash
            merged["task_contract_state"] = rec.state
            if dal is not None and hasattr(dal, "set_swarm_json"):
                try:
                    dal.set_swarm_json(task_id, merged)
                except Exception:  # noqa: BLE001
                    LOG.warning(
                        "could not persist task contract stamp for %s", task_id, exc_info=True
                    )
            return rec, merged
        finally:
            store.close()
    except TaskContractError as exc:
        LOG.warning("task contract adopt rejected for %s: %s", task_id, exc)
        return None, merged
    except Exception:  # noqa: BLE001
        LOG.warning("task contract adopt failed for %s", task_id, exc_info=True)
        return None, merged


def transition_swarm_contract(
    *,
    db_path: str | None,
    swarm_json: Mapping[str, Any],
    to_state: str | ContractState,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> TaskContractRecord | None:
    """Transition the adopted contract using ledger identity checks."""
    cid = str((swarm_json or {}).get("task_contract_id") or "").strip()
    expected = str((swarm_json or {}).get("task_contract_hash") or "").strip() or None
    if not cid:
        return None
    try:
        store = TaskContractStore(database=db_path)
        try:
            rec = store.get(cid)
            if rec is not None and str(rec.state).lower() in {"closed", "rejected", "superseded"}:
                return rec
            return store.transition(
                cid,
                to_state,
                reason=reason,
                actor="swarm.settle_handoff",
                evidence=evidence,
                expected_hash=expected,
            )
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        LOG.warning("task contract transition failed id=%s → %s", cid, to_state, exc_info=True)
        return None
