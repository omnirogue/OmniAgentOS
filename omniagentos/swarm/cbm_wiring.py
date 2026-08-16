"""L16 CBM/gate wiring helpers (M-38).

Populate real allocation inputs from task/swarm signals, close allocations on
terminal outcomes, and block false autonomous replans. Does **not** edit L18
CBM storage — only calls ``CognitiveBudgetService`` public methods.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger(__name__)

__all__ = [
    "CbmInputEnvelope",
    "close_task_allocation",
    "derive_cbm_inputs",
    "guard_control_action",
    "persist_allocation_stamp",
]

_IRREVERSIBLE = frozenset({"irreversible", "destructive", "deploy", "external", "bounded_external"})
_HIGH_DIFFICULTY_HINTS = re.compile(
    r"\b(architect|refactor|migrate|multi[- ]?file|distributed|security|"
    r"concurrency|performance|schema|protocol)\b",
    re.I,
)
_NOVELTY_HINTS = re.compile(
    r"\b(novel|greenfield|prototype|spike|research|unknown|exploratory|"
    r"first[- ]time|from[- ]scratch)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class CbmInputEnvelope:
    """Real inputs for ``CognitiveBudgetService.allocate``."""

    risk_class: str
    novelty: str
    difficulty: str
    stage: str
    required_quality: float
    fingerprint: dict[str, Any]

    def to_allocate_kwargs(self) -> dict[str, Any]:
        return {
            "risk_class": self.risk_class,
            "novelty": self.novelty,
            "difficulty": self.difficulty,
            "stage": self.stage,
            "required_quality": self.required_quality,
            "fingerprint": dict(self.fingerprint),
        }


def derive_cbm_inputs(
    *,
    task: Mapping[str, Any] | None = None,
    swarm_json: Mapping[str, Any] | None = None,
    stage: str = "execution",
) -> CbmInputEnvelope:
    """Derive non-decorative CBM rung inputs from task + plan signals.

    Prefer explicit swarm_json fields when present; otherwise infer from
    risk_class, acceptance/verify coverage, title/description, and est minutes.
    Never invent a constant novelty/difficulty that ignores task shape.
    """
    task = dict(task or {})
    sj = dict(swarm_json or {})

    raw_risk = (
        str(sj.get("risk_class") or task.get("risk_class") or "reversible_internal").strip().lower()
    )
    if raw_risk in {"none", "", "read_only", "readonly"}:
        risk_class = "read_only" if raw_risk in {"read_only", "readonly"} else "reversible_internal"
    elif raw_risk in _IRREVERSIBLE or raw_risk in {"external", "deploy", "destructive"}:
        risk_class = "irreversible" if raw_risk in {"destructive", "deploy"} else "bounded_external"
        if raw_risk == "irreversible":
            risk_class = "irreversible"
    else:
        risk_class = (
            raw_risk
            if raw_risk
            in {
                "reversible_internal",
                "irreversible",
                "bounded_external",
                "read_only",
            }
            else "reversible_internal"
        )

    text = " ".join(
        str(x or "")
        for x in (
            task.get("title"),
            task.get("description"),
            sj.get("acceptance"),
            task.get("acceptance"),
        )
    )

    # Novelty
    explicit_novelty = str(sj.get("novelty") or "").strip().lower()
    if explicit_novelty in {"low", "medium", "high"}:
        novelty = explicit_novelty
    elif _NOVELTY_HINTS.search(text):
        novelty = "high"
    elif not str(sj.get("acceptance") or task.get("acceptance") or "").strip():
        novelty = "medium"  # weak acceptance → more uncertainty
    else:
        novelty = "low"

    # Difficulty
    explicit_diff = str(sj.get("difficulty") or "").strip().lower()
    if explicit_diff in {"low", "medium", "high"}:
        difficulty = explicit_diff
    else:
        est = 0
        for key in ("est_agent_minutes", "est_manual_minutes"):
            try:
                est = max(est, int(sj.get(key) or task.get(key) or 0))
            except (TypeError, ValueError):
                pass
        complexity = str(sj.get("complexity") or task.get("complexity") or "").lower()
        if complexity == "complex" or est >= 45 or _HIGH_DIFFICULTY_HINTS.search(text):
            difficulty = "high"
        elif complexity == "simple" and est and est < 10:
            difficulty = "low"
        else:
            difficulty = "medium"

    acceptance = str(sj.get("acceptance") or task.get("acceptance") or "").strip()
    verify_command = str(sj.get("verify_command") or task.get("verify_command") or "").strip()
    owned = sj.get("owned_paths") or task.get("owned_paths") or []
    if not isinstance(owned, (list, tuple)):
        owned = []

    est_minutes = 0
    for key in ("est_agent_minutes", "est_manual_minutes"):
        try:
            est_minutes = max(est_minutes, int(sj.get(key) or task.get(key) or 0))
        except (TypeError, ValueError):
            pass

    # Quality bar rises with risk and weak verification surface.
    required_quality = 0.9
    if risk_class in {"irreversible", "bounded_external"}:
        required_quality = 0.95
    if not verify_command and not acceptance:
        required_quality = min(0.99, required_quality + 0.03)

    fingerprint = {
        "risk": risk_class,
        "novelty": novelty,
        "difficulty": difficulty,
        "stage": stage,
        "has_acceptance": bool(acceptance),
        "has_verify_command": bool(verify_command),
        "owned_path_count": len([p for p in owned if str(p).strip()]),
        "est_agent_minutes": est_minutes,
        "title_fp": (str(task.get("title") or "")[:80]),
        "source": "l16.derive_cbm_inputs",
    }

    return CbmInputEnvelope(
        risk_class=risk_class,
        novelty=novelty,
        difficulty=difficulty,
        stage=stage,
        required_quality=required_quality,
        fingerprint=fingerprint,
    )


def persist_allocation_stamp(
    dal: Any,
    task_id: str,
    swarm_json: Mapping[str, Any],
    *,
    allocation: Mapping[str, Any] | None,
    effort_source: str | None = None,
    applied_effort: str | None = None,
    cbm_inputs: CbmInputEnvelope | None = None,
) -> dict[str, Any]:
    """Merge CBM stamps into swarm_json and persist when dal supports it."""
    merged = dict(swarm_json)
    if allocation:
        merged["cbm_allocation_id"] = allocation.get("id")
        merged["cbm_rung"] = allocation.get("rung")
        merged["cbm_effort"] = str(allocation.get("reasoning_effort") or "").strip() or None
        merged["cbm_model_role"] = allocation.get("model_role")
        merged["cbm_status"] = allocation.get("status") or "active"
    if effort_source:
        merged["effort_source"] = effort_source
    if applied_effort is not None:
        merged["applied_effort"] = applied_effort
    if cbm_inputs is not None:
        merged["cbm_inputs"] = {
            "risk_class": cbm_inputs.risk_class,
            "novelty": cbm_inputs.novelty,
            "difficulty": cbm_inputs.difficulty,
            "required_quality": cbm_inputs.required_quality,
            "fingerprint": dict(cbm_inputs.fingerprint),
        }
    setter = getattr(dal, "set_swarm_json", None)
    if callable(setter):
        try:
            setter(task_id, merged)
        except Exception:  # noqa: BLE001
            LOG.warning("could not persist cbm stamp on task %s", task_id, exc_info=True)
    return merged


def close_task_allocation(
    *,
    db_path: str | None,
    swarm_json: Mapping[str, Any] | None,
    first_pass_accepted: bool,
    wall_seconds: float | None = None,
    repair_count: int = 0,
    verifier_score: float | None = None,
    production_outcome: str = "healthy",
) -> dict[str, Any] | None:
    """Close the active CBM allocation stamped on swarm_json, if any."""
    sj = dict(swarm_json or {})
    alloc_id = str(sj.get("cbm_allocation_id") or "").strip()
    if not alloc_id:
        return None
    if str(sj.get("cbm_status") or "").lower() == "closed":
        return {"allocation_id": alloc_id, "closed": True, "already_closed": True}
    if db_path:
        try:
            from omniagentos.db.store import _connect

            with _connect(db_path) as conn:
                row = conn.execute(
                    "SELECT status FROM cbm_allocations WHERE id = ?", (alloc_id,)
                ).fetchone()
                if row and str(row[0] or "").lower() == "closed":
                    return {"allocation_id": alloc_id, "closed": True, "already_closed": True}
        except Exception:  # noqa: BLE001
            pass
    try:
        from omniagentos.cbm.service import CognitiveBudgetService

        cbm = CognitiveBudgetService(database=db_path)
        try:
            result = cbm.close_allocation(
                alloc_id,
                first_pass_accepted=first_pass_accepted,
                wall_seconds=wall_seconds,
                repair_count=repair_count,
                verifier_score=verifier_score,
                production_outcome=production_outcome,
            )
            return result
        finally:
            cbm.close()
    except KeyError:
        LOG.warning("cbm allocation %s not found for close", alloc_id)
        return None
    except Exception:  # noqa: BLE001
        LOG.warning("cbm close_allocation failed for %s", alloc_id, exc_info=True)
        return None


def guard_control_action(
    action: str,
    *,
    reason_codes: Sequence[str] | None = None,
    evidence_delta: float | None = None,
    stall_count: int = 0,
    progress: float | None = None,
    previous_actions: list[str] | None = None,
    max_replans: int = 1,
    replan_budget_used: int = 0,
) -> tuple[str, str]:
    """Block decorative/false autonomous replans (M-38).

    Returns ``(action, guard_reason)``. A ``replan`` is only allowed when there
    is real stall/no-progress evidence, remaining replan budget, and the action
    is not a repeated false replan loop.

    Production callers (L16-owned):

    * ``omniagentos.fanin.pipeline.run_fanin_pipeline`` — rewrites false
      replan exits after verify.
    * ``omniagentos.swarm.summary.write_summary`` — records guarded control
      stamps when a run terminalizes with open replan pressure.

    L18 interface (metacog storage is L18-exclusive): call this on the
    evaluate path before persisting ``next_control_action == \"replan\"``::

        from omniagentos.swarm.cbm_wiring import guard_control_action
        action, reason = guard_control_action(
            state.next_control_action,
            reason_codes=state.reason_codes,
            evidence_delta=state.new_evidence_delta,
            stall_count=state.stall_count,
            progress=state.objective_progress,
            previous_actions=previous_actions,
        )
    """
    act = str(action or "continue").strip().lower() or "continue"
    reasons = [str(r) for r in (reason_codes or []) if str(r).strip()]
    prev = [str(a).strip().lower() for a in (previous_actions or []) if str(a).strip()]

    if act != "replan":
        return act, "passthrough"

    # Budget exhausted → stop, do not thrash replan.
    if replan_budget_used >= max(0, int(max_replans)):
        return "stop", "replan_budget_exhausted"

    # False replan: progress improving or no stall signal.
    if evidence_delta is not None and float(evidence_delta) > 0.05:
        return "continue", "false_replan_progressing"

    if progress is not None and float(progress) >= 1.0:
        return "stop", "false_replan_already_complete"

    has_stall_signal = (
        int(stall_count) > 0
        or "no_progress_cycles" in reasons
        or "forced_no_continue" in reasons
        or "repetition" in reasons
    )
    if not has_stall_signal:
        return "continue", "false_replan_no_stall_evidence"

    # Repeated replan without new reasons → stop (anti thrash).
    if prev.count("replan") >= max(1, int(max_replans)):
        return "stop", "false_replan_repeated"

    return "replan", "allowed"
