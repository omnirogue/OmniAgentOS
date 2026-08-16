"""Production tournament execution and fail-closed pairwise judging."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from omniagentos.lab import surfaces
from omniagentos.lab.contracts import Budgets, GenomeSpec, SurfaceKind
from omniagentos.lab.tournament.judges import (
    TournamentJudge,
    blinded_panel_scores,
    deterministic_task_scores,
)


class TournamentDriver:
    """Real-but-simple default driver, swappable for a model-backed judge.

    The judge sees only the tournament core's blinded payloads. Non-dry genome
    runs resolve the persisted Surface into a validated GenomeSpec before using
    the real lab executor, avoiding the legacy string-config fallback entirely.
    """

    def __init__(
        self,
        store: Any,
        *,
        judge_panel: Sequence[TournamentJudge] = (),
    ) -> None:
        self._store = store
        self._judge_panel = tuple(judge_panel)

    def run_genome(self, config_id: str, arena_task: dict[str, Any]) -> dict[str, Any]:
        from omniagentos.lab.executor import execute_genome

        surface = self._store.get_surface(config_id)
        if surface is None:
            raise ValueError(f"unknown tournament configuration: {config_id}")
        if str(surface.get("kind")) != SurfaceKind.ORCHESTRATION_GENOME.value:
            raise ValueError(f"tournament configuration is not a genome: {config_id}")
        content = surfaces.load_surface_content(surface)
        genome = GenomeSpec.model_validate(content)
        budget = Budgets()
        if {"wall_min", "tokens", "cost_usd"} <= genome.budget.keys():
            budget = Budgets(
                wall_minutes=float(genome.budget["wall_min"]),
                tokens=int(genome.budget["tokens"]),
                cost_usd=float(genome.budget["cost_usd"]),
                replicates=1,
            )
        return execute_genome(
            genome,
            arena_task,
            budget,
            dry_run=False,
        )

    def judge_blind(
        self,
        *,
        output_a: dict[str, Any],
        output_b: dict[str, Any],
        arena_task: dict[str, Any],
    ) -> dict[str, Any]:
        judgment = deterministic_task_scores(output_a, output_b, arena_task)
        raw_a = output_a.get("output")
        raw_b = output_b.get("output")
        if (
            judgment is None
            and isinstance(raw_a, dict)
            and isinstance(raw_b, dict)
            and raw_a.get("dry_run") is True
            and raw_b.get("dry_run") is True
        ):
            return {
                "score_a": 0.0,
                "score_b": 0.0,
                "notes": "dry-run no-op judge; no live rating evidence",
            }
        if judgment is None:
            judgment = blinded_panel_scores(
                self._judge_panel,
                output_a,
                output_b,
                arena_task,
            )
        return {
            "score_a": judgment.score_a,
            "score_b": judgment.score_b,
            "notes": judgment.notes,
        }


__all__ = ["TournamentDriver"]
