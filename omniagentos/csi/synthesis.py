"""PlanSynthesisService — cross-lineage synthesis (PLAN §13)."""

from __future__ import annotations

from collections import Counter, defaultdict

from omniagentos.csi.frozen import reject_frozen_paths
from omniagentos.csi.models import PlannerPlan, PlannerProposal, SynthesisResult


class PlanSynthesisService:
    """Compare independent plans; default is no_change."""

    def __init__(self, *, min_panel_size: int = 2) -> None:
        self.min_panel_size = min_panel_size

    def synthesize(self, plans: list[PlannerPlan]) -> SynthesisResult:
        ok_plans = [p for p in plans if p is not None]
        execution_counts = Counter(
            str(p.execution_id).strip()
            for p in ok_plans
            if p.provenance_verified and str(p.execution_id).strip()
        )
        trusted_plans = [
            p
            for p in ok_plans
            if p.provenance_verified
            and str(p.execution_id).strip()
            and execution_counts[str(p.execution_id).strip()] == 1
            and str(p.lineage).strip()
        ]
        independent_lineages = {str(p.lineage).strip() for p in trusted_plans}
        independent_size = len(independent_lineages)
        if independent_size < self.min_panel_size:
            return SynthesisResult(
                verdict="insufficient_panel",
                reason=(
                    f"independent_lineages={independent_size} "
                    f"< min={self.min_panel_size}; repeated calls through one "
                    "lineage are not an independent panel"
                ),
                panel_size=independent_size,
            )

        proposing = [p for p in trusted_plans if p.verdict == "propose" and p.proposals]
        if not proposing:
            reasons = [p.no_change_reason for p in trusted_plans if p.no_change_reason]
            return SynthesisResult(
                verdict="no_change",
                reason="; ".join(reasons[:3]) or "all_planners_said_no_change",
                panel_size=independent_size,
                human_review_items=[item for p in trusted_plans for item in p.human_review_items],
            )

        validated: list[tuple[PlannerPlan, PlannerProposal]] = []
        rejected: list[dict] = []
        human: list[str] = []
        for plan in proposing:
            for prop in plan.proposals:
                if not prop.affected_paths:
                    rejected.append(
                        {
                            "planner": plan.planner,
                            "reason": "empty_affected_paths",
                            "change": prop.change[:120],
                        }
                    )
                    continue
                hits = reject_frozen_paths(prop.affected_paths)
                if hits:
                    rejected.append(
                        {
                            "planner": plan.planner,
                            "reason": "frozen_surface",
                            "paths": hits,
                        }
                    )
                    human.extend(f"frozen:{h}" for h in hits)
                    continue
                if prop.confidence < 0.3:
                    rejected.append(
                        {
                            "planner": plan.planner,
                            "reason": "below_confidence_floor",
                            "change": prop.change[:120],
                        }
                    )
                    continue
                validated.append((plan, prop))

        if not validated:
            return SynthesisResult(
                verdict="no_change",
                reason="no_proposal_survived_synthesis",
                rejected_items=rejected,
                human_review_items=human,
                panel_size=independent_size,
            )

        supporters: dict[str, set[str]] = defaultdict(set)
        for plan, prop in validated:
            for path in prop.affected_paths:
                supporters[path].add(plan.lineage)
        consensus_paths = {
            path for path, lineages in supporters.items() if len(lineages) >= self.min_panel_size
        }
        if not consensus_paths:
            return SynthesisResult(
                verdict="no_change",
                reason="no_validated_path_consensus_across_independent_lineages",
                rejected_items=rejected + [{"reason": "no_path_consensus"}],
                human_review_items=human,
                panel_size=independent_size,
            )

        accepted: list[PlannerProposal] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for plan, prop in sorted(
            validated,
            key=lambda item: item[1].confidence,
            reverse=True,
        ):
            supported_paths = tuple(
                sorted(path for path in prop.affected_paths if path in consensus_paths)
            )
            unsupported_paths = sorted(set(prop.affected_paths) - set(supported_paths))
            if unsupported_paths:
                rejected.append(
                    {
                        "planner": plan.planner,
                        "reason": "paths_without_independent_consensus",
                        "paths": unsupported_paths,
                    }
                )
            if not supported_paths:
                continue
            key = (prop.change, supported_paths)
            if key in seen:
                continue
            seen.add(key)
            accepted.append(prop.model_copy(update={"affected_paths": list(supported_paths)}))

        if not accepted:
            return SynthesisResult(
                verdict="no_change",
                reason="validated_path_consensus_filtered_all_proposals",
                rejected_items=rejected,
                human_review_items=human,
                panel_size=independent_size,
            )

        return SynthesisResult(
            verdict="propose",
            reason="accepted_validated_cross_lineage_path_consensus",
            accepted_proposals=accepted[:3],
            rejected_items=rejected,
            human_review_items=human,
            panel_size=independent_size,
        )


__all__ = ["PlanSynthesisService"]
