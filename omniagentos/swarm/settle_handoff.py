"""Plain-data settle handoff for TaskContract + CBM close (M-16 / M-38).

L10 owns ``omniagentos/swarm/scheduler.py``. L16 **must not** edit that file.
This module is the plain-data interface L10 (or any terminal hook) can call
with only:

* ``db_path`` — database path string (or None for default)
* ``swarm_json`` — plain dict of stamps written at spawn
* ``accepted`` / ``production_outcome`` / optional wall clock

Required ``swarm_json`` stamps (written by L16 spawn / ``contract_bridge`` /
``cbm_wiring``; missing stamps → no-op, never raise into settle critical path):

* ``cbm_allocation_id`` — active CBM allocation to close
* ``task_contract_id`` + ``task_contract_hash`` — ledger identity for transitions

L10 commit ``454280a`` already documents the reserved settle seam in
``_settle_terminal`` and can receive a one-call wire-up after parent L16
lands, without importing decorative scheduler-local helpers:

```python
from omniagentos.swarm.settle_handoff import settle_task_from_swarm_json

settle_task_from_swarm_json(
    db_path=getattr(self._dal, "db_path", None),
    swarm_json=swarm_json,  # from dal.get_swarm_json(task_id)
    accepted=(outcome.verdict == "confirm"),
    production_outcome="healthy" if accepted else "failed",
)
```

Also used by L16-owned ``summary.write_summary`` as a real production closer
for any allocations/contracts still open at run terminalization.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

LOG = logging.getLogger(__name__)

__all__ = [
    "REQUIRED_SWARM_JSON_STAMPS",
    "settle_run_tasks",
    "settle_task_from_swarm_json",
]

# Documented plain-data contract for L10 / operators.
REQUIRED_SWARM_JSON_STAMPS: tuple[str, ...] = (
    "cbm_allocation_id",
    "cbm_status",
    "task_contract_id",
    "task_contract_hash",
    "task_contract_state",
)


def settle_task_from_swarm_json(
    *,
    db_path: str | None,
    swarm_json: Mapping[str, Any] | None,
    accepted: bool,
    production_outcome: str = "healthy",
    wall_seconds: float | None = None,
    repair_count: int = 0,
    verifier_score: float | None = None,
    dal: Any | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Close CBM allocation and transition TaskContract from plain stamps.

    Returns a structured result (never raises). Truthful failure: each sub-step
    reports ``ok`` / ``skipped`` / ``error`` without claiming success on failure.
    """
    sj = dict(swarm_json or {})
    result: dict[str, Any] = {
        "cbm_close": None,
        "contract": None,
        "accepted": bool(accepted),
        "production_outcome": str(production_outcome or ""),
    }

    # --- CBM close (M-38) -------------------------------------------------
    try:
        from omniagentos.swarm.cbm_wiring import close_task_allocation

        closed = close_task_allocation(
            db_path=db_path,
            swarm_json=sj,
            first_pass_accepted=accepted,
            wall_seconds=wall_seconds,
            repair_count=repair_count,
            verifier_score=verifier_score,
            production_outcome=production_outcome,
        )
        if closed is None:
            if not str(sj.get("cbm_allocation_id") or "").strip():
                result["cbm_close"] = {"status": "skipped", "reason": "no_allocation_id"}
            else:
                result["cbm_close"] = {
                    "status": "error",
                    "reason": "close_returned_none",
                    "allocation_id": sj.get("cbm_allocation_id"),
                }
        else:
            result["cbm_close"] = {
                "status": "ok",
                "closed": bool(closed.get("closed", True)),
                "already_closed": bool(closed.get("already_closed")),
                "allocation_id": closed.get("allocation_id") or sj.get("cbm_allocation_id"),
            }
            sj["cbm_status"] = "closed"
    except Exception as exc:  # noqa: BLE001 — settle must not raise
        LOG.warning("settle_handoff cbm close failed", exc_info=True)
        result["cbm_close"] = {
            "status": "error",
            "reason": f"{type(exc).__name__}:{exc}",
        }

    # --- TaskContract transitions (M-16) ----------------------------------
    try:
        from omniagentos.swarm.contract_bridge import transition_swarm_contract
        from omniagentos.taskcontract.transitions import ContractState

        cid = str(sj.get("task_contract_id") or "").strip()
        if not cid:
            result["contract"] = {"status": "skipped", "reason": "no_task_contract_id"}
            if dal is not None and task_id and hasattr(dal, "set_swarm_json"):
                try:
                    dal.set_swarm_json(task_id, sj)
                except Exception:  # noqa: BLE001
                    pass
            return result

        current_contract_state = str(sj.get("task_contract_state") or "").lower()
        if current_contract_state in {"closed", "rejected", "superseded"}:
            result["contract"] = {
                "status": "ok",
                "contract_id": cid,
                "state": current_contract_state,
                "contract_hash": str(sj.get("task_contract_hash") or ""),
                "already_terminal": True,
            }
            if dal is not None and task_id and hasattr(dal, "set_swarm_json"):
                try:
                    dal.set_swarm_json(task_id, sj)
                except Exception:  # noqa: BLE001
                    pass
            return result

        if accepted:
            # active → verifying → verified → closed (idempotent where already past)
            steps: list[tuple[Any, str]] = [
                (ContractState.VERIFYING, "settle_verify"),
                (ContractState.VERIFIED, "settle_confirmed"),
                (ContractState.CLOSED, "settle_closed"),
            ]
            last_rec = None
            for to_state, reason in steps:
                last_rec = transition_swarm_contract(
                    db_path=db_path,
                    swarm_json={
                        **sj,
                        **(
                            {
                                "task_contract_id": last_rec.id,
                                "task_contract_hash": last_rec.contract_hash,
                            }
                            if last_rec is not None
                            else {}
                        ),
                    },
                    to_state=to_state,
                    reason=reason,
                    evidence={
                        "production_outcome": production_outcome,
                        "accepted": True,
                    },
                )
                if last_rec is None:
                    result["contract"] = {
                        "status": "error",
                        "reason": f"transition_failed_at_{reason}",
                        "contract_id": cid,
                    }
                    if dal is not None and task_id and hasattr(dal, "set_swarm_json"):
                        try:
                            dal.set_swarm_json(task_id, sj)
                        except Exception:  # noqa: BLE001
                            pass
                    return result
            if last_rec is not None:
                result["contract"] = {
                    "status": "ok",
                    "contract_id": last_rec.id,
                    "state": last_rec.state,
                    "contract_hash": last_rec.contract_hash,
                }
                sj["task_contract_state"] = last_rec.state
                sj["task_contract_hash"] = last_rec.contract_hash
        else:
            rec = transition_swarm_contract(
                db_path=db_path,
                swarm_json=sj,
                to_state=ContractState.REJECTED,
                reason="settle_rejected",
                evidence={"production_outcome": production_outcome, "accepted": False},
            )
            if rec is None:
                result["contract"] = {
                    "status": "error",
                    "reason": "reject_transition_failed",
                    "contract_id": cid,
                }
            else:
                result["contract"] = {
                    "status": "ok",
                    "contract_id": rec.id,
                    "state": rec.state,
                    "contract_hash": rec.contract_hash,
                }
                sj["task_contract_state"] = rec.state
                sj["task_contract_hash"] = rec.contract_hash
    except Exception as exc:  # noqa: BLE001
        LOG.warning("settle_handoff contract transition failed", exc_info=True)
        result["contract"] = {
            "status": "error",
            "reason": f"{type(exc).__name__}:{exc}",
        }

    if dal is not None and task_id and hasattr(dal, "set_swarm_json"):
        try:
            dal.set_swarm_json(task_id, sj)
        except Exception:  # noqa: BLE001
            LOG.warning("could not persist updated swarm_json on task %s", task_id, exc_info=True)

    return result


def settle_run_tasks(
    *,
    db_path: str | None,
    dal: Any,
    run_id: str,
    accepted: bool,
    production_outcome: str = "healthy",
) -> list[dict[str, Any]]:
    """Production closer: settle every member task that still has open stamps.

    Called from L16-owned ``summary.write_summary`` so close/transition is not
    decorative — it runs on the real run-terminal path already invoked by the
    scheduler (without editing scheduler.py).
    """
    out: list[dict[str, Any]] = []
    try:
        run = dal.get_run(run_id) if hasattr(dal, "get_run") else None
        if run is None:
            return out
        root_id = str(run.get("board_task_id") or "")
        tasks = list(dal.tasks_for_run(run_id) or [])
    except Exception:  # noqa: BLE001
        LOG.warning("settle_run_tasks could not list tasks for %s", run_id, exc_info=True)
        return out

    for task in tasks:
        tid = str(task.get("id") or "")
        if not tid or tid == root_id:
            continue
        try:
            if hasattr(dal, "get_swarm_json"):
                sj = dal.get_swarm_json(tid) or {}
            else:
                raw = task.get("swarm_json")
                if isinstance(raw, dict):
                    sj = raw
                else:
                    import json

                    sj = json.loads(raw or "{}") if raw else {}
        except Exception:  # noqa: BLE001
            LOG.warning("settle_run_tasks get_swarm_json failed for %s", tid, exc_info=True)
            continue

        if not (
            str(sj.get("cbm_allocation_id") or "").strip()
            or str(sj.get("task_contract_id") or "").strip()
        ):
            continue

        step = settle_task_from_swarm_json(
            db_path=db_path,
            swarm_json=sj,
            accepted=accepted,
            production_outcome=production_outcome,
            dal=dal,
            task_id=tid,
        )
        step["task_id"] = tid
        out.append(step)
    return out
