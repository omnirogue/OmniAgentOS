"""Validated-trait extraction from decisive tournament outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omniagentos.lab.contracts import Elo, GenomeSpec, GenomeStage, PlaybookEntry

_DECISIVE_MARGIN = 0.10
_HIGH_CONFIDENCE_ELO = 24.0


def _tournament_row(store: Any, tournament_id: str) -> dict[str, Any]:
    row = store.get_tournament(tournament_id)
    if row is None:
        raise ValueError(f"unknown tournament: {tournament_id}")
    return dict(row)


def _matches(store: Any, tournament_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in store.tournament_matches(tournament_id)]


def _genome_from_surface(store: Any, config_id: str) -> dict[str, Any] | None:
    surface = store.get_surface(config_id)
    if surface is None:
        return None
    for key in ("genome", "content", "spec"):
        value = surface.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            return json.loads(value)
    path = Path(surface["path"])
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _trait(winner: GenomeSpec, loser: GenomeSpec) -> str:
    for winning, losing in zip(winner.roles, loser.roles, strict=False):
        if winning.model != losing.model:
            return f"{winning.model or 'default model'} in {winning.name} role"
        if winning.agent != losing.agent:
            return f"{winning.agent} agent in {winning.name} role"
        if winning.effort != losing.effort:
            return f"{winning.effort or 'default'} effort in {winning.name} role"
    if len(winner.roles) != len(loser.roles):
        return (
            "additional orchestration role"
            if len(winner.roles) > len(loser.roles)
            else "simplified role configuration"
        )
    for winning_stage, losing_stage in zip(winner.flow, loser.flow, strict=False):
        w_stage: GenomeStage = winning_stage  # noqa: F841
        l_stage: GenomeStage = losing_stage  # noqa: F841
        if winning_stage.iterations != losing_stage.iterations:
            return f"{winning_stage.iterations}-iteration {winning_stage.stage} stage"
    if winner.judge.get("blind") != loser.judge.get("blind"):
        return "blind judge panel" if winner.judge.get("blind") else "non-blind judge panel"
    if winner.review.get("adversarial") != loser.review.get("adversarial"):
        return (
            "adversarial review" if winner.review.get("adversarial") else "non-adversarial review"
        )
    if winner.judge != loser.judge:
        return "judge panel configuration"
    if winner.review != loser.review:
        return "review configuration"
    if len(winner.flow) != len(loser.flow):
        return (
            "additional orchestration stage"
            if len(winner.flow) > len(loser.flow)
            else "simplified orchestration flow"
        )
    if winner.archetype != loser.archetype:
        return f"{winner.archetype} orchestration archetype"
    return "winning orchestration configuration"


def accumulate_playbook(store: Any, tournament_id: str) -> list[PlaybookEntry]:
    """Persist entries for materially decisive wins in a completed tournament."""
    tournament = _tournament_row(store, tournament_id)
    subject = str(tournament["subject"])
    discipline = str(tournament["discipline"])
    entries: list[PlaybookEntry] = []

    for match in _matches(store, tournament_id):
        if match["winner"] is None:
            continue
        if abs(float(match["score_a"]) - float(match["score_b"])) < _DECISIVE_MARGIN:
            continue
        winner_id = str(match["winner"])
        loser_id = str(match["config_b"] if winner_id == match["config_a"] else match["config_a"])
        winning_genome = _genome_from_surface(store, winner_id)
        losing_genome = _genome_from_surface(store, loser_id)
        if winning_genome is None or losing_genome is None:
            continue
        winner = GenomeSpec.model_validate(winning_genome)
        loser = GenomeSpec.model_validate(losing_genome)
        winner_elo = Elo.model_validate(
            store.get_elo(subject, winner_id) or {"subject": subject, "config_id": winner_id}
        )
        loser_elo = Elo.model_validate(
            store.get_elo(subject, loser_id) or {"subject": subject, "config_id": loser_id}
        )
        support = winner_elo.rating - loser_elo.rating
        entry = PlaybookEntry(
            trait=_trait(winner, loser),
            discipline=discipline,
            evidence_tournaments=[tournament_id],
            scope=f"{subject}: {winner_id} over {loser_id}",
            confidence="high" if support >= _HIGH_CONFIDENCE_ELO else "medium",
            elo_support=support,
        )
        store.add_playbook_entry(entry)
        entries.append(entry)
    return entries
