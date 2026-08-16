"""RoutinePlanner — dispatch packet to N independent planners (PLAN §13)."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from omniagentos.csi.frozen import reject_frozen_paths
from omniagentos.csi.models import EvidencePacket, PlannerPlan, PlannerProposal


@dataclass(frozen=True)
class PlannerExecution:
    """Trusted adapter envelope describing the backend call that actually ran."""

    plan: PlannerPlan
    provider: str
    lineage: str
    execution_id: str


PlannerFn = Callable[[EvidencePacket, str], PlannerPlan | PlannerExecution]

_LIVE_LINEAGE = {
    "grok": "xai",
    "sol": "openai",
    "kimi": "moonshot",
    "terra": "openai",
    "opus": "anthropic",
}
_OFFLINE_LINEAGE = "offline-deterministic:v1"

# Per-routine offline proposal target (always under vault/ for safe implement).
_ROUTINE_NOTE_PATH = {
    "design": "vault/skills/design/SKILL.md",
    "self_learning": "vault/skills/self-learning/SKILL.md",
    "routing": "vault/skills/routing/SKILL.md",
    "skills": "vault/skills/skills-catalog/SKILL.md",
    "tool_calls": "vault/skills/tool-calls/SKILL.md",
    "speed": "vault/skills/speed/SKILL.md",
    "quality": "vault/skills/quality/SKILL.md",
    "tech_debt": "vault/skills/tech-debt/SKILL.md",
}

_ROUTINE_BLURB = {
    "design": "UI/UX: reduce TODOs/empty-state friction; clarify hierarchy and workflows",
    "self_learning": "memory/skills: document recovery for repeating failure signals",
    "routing": "routing: reduce low-confidence formation / improve ETAR priors",
    "skills": "skills: merge, split, version, or retire catalog entries",
    "tool_calls": "tools: improve schemas, retries, allowlists, error recovery",
    "speed": "speed: cut wall-clock without lowering accepted quality",
    "quality": "quality: reduce regressions and assertion failures",
    "tech_debt": "tech debt: consolidate TODOs / simplify without new capability",
}


def offline_planner(packet: EvidencePacket, planner_name: str) -> PlannerPlan:
    """Deterministic offline planner — no live model calls.

    Default verdict is no_change. Proposes vault skill notes when the packet
    has clear signals for that routine.
    """
    # Re-running this deterministic function under several display names is
    # one execution lineage, not an independent panel.
    lineage = _OFFLINE_LINEAGE
    routine = packet.routine
    if packet.thin_evidence or not packet.repeat_failures:
        return PlannerPlan(
            routine=routine,
            verdict="no_change",
            no_change_reason=(
                packet.evidence_gaps[0] if packet.evidence_gaps else "thin_evidence_or_no_signals"
            ),
            evidence_gaps=list(packet.evidence_gaps),
            planner=planner_name,
            lineage=lineage,
            execution_id=f"{lineage}:{planner_name}",
            provenance_verified=True,
        )

    top = packet.repeat_failures[0]
    note_path = _ROUTINE_NOTE_PATH.get(routine, f"vault/skills/{routine}/SKILL.md")
    blurb = _ROUTINE_BLURB.get(routine, routine)
    proposal = PlannerProposal(
        addresses_problem_rank=1,
        change=(
            f"[{routine}] {blurb}. "
            f"Primary signal '{top.get('signal')}' (count={top.get('count')}). "
            f"Capture a durable skill note for operators and future planners."
        ),
        affected_paths=[note_path],
        baseline=f"{top.get('signal')}={top.get('count')}",
        target="signal reduced after observation window",
        measured_by=f"{routine}_signal_scan",
        minimum_effect_size="count drop of >=1 in next window",
        tests_required=[f"tests/csi/test_routines_{routine}.py"],
        migration_required=False,
        risks=["skill bloat if signal is noise"],
        rollback="delete or revert the skill note",
        estimated_scope="small",
        confidence=0.45,
    )
    frozen_hits = reject_frozen_paths(proposal.affected_paths)
    if frozen_hits:
        return PlannerPlan(
            routine=routine,
            verdict="no_change",
            no_change_reason="only_candidate_touched_frozen_surface",
            human_review_items=[f"frozen: {p}" for p in frozen_hits],
            planner=planner_name,
            lineage=lineage,
            execution_id=f"{lineage}:{planner_name}",
            provenance_verified=True,
        )

    return PlannerPlan(
        routine=routine,
        verdict="propose",
        problems=[],
        proposals=[proposal],
        planner=planner_name,
        lineage=lineage,
        execution_id=f"{lineage}:{planner_name}",
        provenance_verified=True,
    )


class RoutinePlanner:
    """Dispatch one packet to N planners; collect schema-valid plans."""

    def __init__(
        self,
        *,
        timeout_s: float = 900,
        min_panel_size: int = 2,
        use_live: bool = False,
        planner_fn: PlannerFn | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.min_panel_size = min_panel_size
        self.use_live = use_live
        self.planner_fn = planner_fn
        if use_live and planner_fn is None:
            raise RuntimeError(
                "use_live_planners=true requires an injected planner_fn adapter; "
                "refusing silent offline fallback"
            )
        if self.planner_fn is None:
            self.planner_fn = offline_planner

    def run_panel(
        self,
        packet: EvidencePacket,
        planners: list[str] | tuple[str, ...],
    ) -> list[PlannerPlan]:
        """Run planners in parallel. Timeouts drop that planner (PLAN §10a)."""
        results: list[PlannerPlan] = []
        if not planners:
            return results
        assert self.planner_fn is not None
        planner_fn = self.planner_fn

        def _one(name: str) -> PlannerPlan:
            try:
                result = planner_fn(packet, name)
            except Exception as exc:  # noqa: BLE001
                return PlannerPlan(
                    routine=packet.routine,
                    verdict="no_change",
                    no_change_reason=f"planner_error:{exc}",
                    planner=name,
                )
            if self.use_live:
                expected_provider = _LIVE_LINEAGE.get(name)
                if not isinstance(result, PlannerExecution):
                    return PlannerPlan(
                        routine=packet.routine,
                        verdict="no_change",
                        no_change_reason="planner_provenance_missing",
                        planner=name,
                    )
                provider = result.provider.strip().casefold()
                lineage = result.lineage.strip().casefold()
                execution_id = result.execution_id.strip()
                if (
                    expected_provider is None
                    or provider != expected_provider
                    or lineage != expected_provider
                    or not execution_id
                ):
                    return PlannerPlan(
                        routine=packet.routine,
                        verdict="no_change",
                        no_change_reason="planner_provenance_invalid",
                        planner=name,
                    )
                plan = result.plan
                execution_lineage = lineage
                provenance_verified = True
            else:
                if isinstance(result, PlannerExecution):
                    plan = result.plan
                else:
                    plan = result
                execution_lineage = _OFFLINE_LINEAGE
                execution_id = f"{_OFFLINE_LINEAGE}:{name}"
                provenance_verified = True
            if not plan.planner:
                plan = plan.model_copy(update={"planner": name})
            plan = plan.model_copy(
                update={
                    "lineage": execution_lineage,
                    "execution_id": execution_id,
                    "provenance_verified": provenance_verified,
                }
            )
            safe_props = []
            human: list[str] = list(plan.human_review_items)
            for prop in plan.proposals:
                hits = reject_frozen_paths(prop.affected_paths)
                if hits:
                    human.extend(f"frozen_path_removed:{p}" for p in hits)
                    continue
                safe_props.append(prop)
            if plan.verdict == "propose" and not safe_props:
                return plan.model_copy(
                    update={
                        "verdict": "no_change",
                        "no_change_reason": "all_proposals_hit_frozen_surfaces",
                        "proposals": [],
                        "human_review_items": human,
                    }
                )
            return plan.model_copy(update={"proposals": safe_props, "human_review_items": human})

        with ThreadPoolExecutor(max_workers=min(4, len(planners))) as pool:
            futs = {pool.submit(_one, name): name for name in planners}
            try:
                for fut in as_completed(futs, timeout=self.timeout_s):
                    try:
                        results.append(fut.result(timeout=1))
                    except Exception as exc:  # noqa: BLE001
                        name = futs[fut]
                        results.append(
                            PlannerPlan(
                                routine=packet.routine,
                                verdict="no_change",
                                no_change_reason=f"timeout_or_error:{exc}",
                                planner=name,
                            )
                        )
            except TimeoutError:
                pass
        return results


__all__ = ["PlannerExecution", "RoutinePlanner", "offline_planner"]
