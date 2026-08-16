"""Fair, blind pairwise tournaments over orchestration genome configurations."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from itertools import combinations
from typing import Any

from omniagentos.lab.contracts import Elo, MatchResult, Tournament, elo_update
from omniagentos.lab.eval.blind import unlinkable_token

# L04x may be installed later.  Keeping this patch point at module scope lets
# integration tests provide its executor without introducing an eager import.
execute_genome: Callable[..., Any] | None = None


def _arena_task_hash(arena_task: dict[str, Any]) -> str:
    encoded = json.dumps(arena_task, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _elo_from_store(store: Any, subject: str, config_id: str) -> Elo:
    row = store.get_elo(subject, config_id)
    if row is None:
        return Elo(subject=subject, config_id=config_id)
    return Elo.model_validate(row)


def _record_after_match(elo: Elo, rating: float, outcome: float) -> Elo:
    update: dict[str, Any] = {"rating": rating, "matches": elo.matches + 1}
    if outcome == 1.0:
        update["wins"] = elo.wins + 1
    elif outcome == 0.0:
        update["losses"] = elo.losses + 1
    else:
        update["draws"] = elo.draws + 1
    return elo.model_copy(update=update)


def _call_runner(runner: Callable[..., Any], config_id: str, arena_task: dict[str, Any]) -> Any:
    """Adapt the small executor interface expected while L04x evolves separately."""
    try:
        return runner(config_id=config_id, arena_task=arena_task)
    except TypeError:
        return runner(config_id, arena_task)


def _run_config(evaluator: Any, config_id: str, arena_task: dict[str, Any], dry_run: bool) -> Any:
    if dry_run:
        mock_outputs = arena_task.get("mock_outputs", {})
        return mock_outputs.get(config_id, {"arena_task": arena_task, "dry_run": True})

    for name in ("run_genome", "execute_genome", "run", "execute"):
        runner = getattr(evaluator, name, None)
        if callable(runner):
            return _call_runner(runner, config_id, arena_task)
    if callable(evaluator):
        return _call_runner(evaluator, config_id, arena_task)
    if execute_genome is not None:
        return _call_runner(execute_genome, config_id, arena_task)

    # L04x can be delivered independently.  Importing it only at execution time
    # keeps dry runs and unit tests free of that dependency.
    try:
        from omniagentos.lab.executor import (
            execute_genome as l04_execute_genome,  # type: ignore[import-untyped]  # noqa: F401
        )
    except ImportError as exc:
        raise RuntimeError("no genome executor is available") from exc
    return _call_runner(l04_execute_genome, config_id, arena_task)


def _blind_payload(output: Any, token: str) -> dict[str, Any]:
    """The judge receives an opaque token, never a configuration identifier."""
    if isinstance(output, dict):
        output = {key: value for key, value in output.items() if key not in {"config_id", "id"}}
    return {"blind_token": token, "output": output}


def _call_judge(
    evaluator: Any, output_a: Any, output_b: Any, arena_task: dict[str, Any]
) -> tuple[float, float, str]:
    """Judge one pair blind and return ``(score_a, score_b, notes)`` in the
    caller's canonical arm order.

    Tokens are fresh ``unlinkable_token()`` draws — never position labels — and
    presentation order is coin-flipped per call, so neither the token string nor
    the slot a payload arrives in can leak which arm it belongs to."""
    judge = getattr(evaluator, "judge_blind", None)
    if not callable(judge):
        raise RuntimeError("evaluator must provide judge_blind for tournament judging")

    blind_a = _blind_payload(output_a, unlinkable_token())
    blind_b = _blind_payload(output_b, unlinkable_token())
    swapped = bool(secrets.randbits(1))
    first, second = (blind_b, blind_a) if swapped else (blind_a, blind_b)
    try:
        raw = judge(output_a=first, output_b=second, arena_task=arena_task)
    except TypeError:
        try:
            raw = judge(first, second, arena_task)
        except TypeError:
            raw = judge(first, second)
    score_first, score_second, notes = _judge_result(raw)
    if swapped:
        # Free-text notes narrate presentation order; mark the swap so persisted
        # notes cannot silently contradict the canonical score_a/score_b.
        notes = f"{notes} [presentation-order swapped]".strip()
        return score_second, score_first, notes
    return score_first, score_second, notes


def _as_float(value: Any) -> float:
    if isinstance(value, dict):
        for key in ("score", "mean", "value"):
            if key in value:
                return float(value[key])
    return float(value)


def _judge_result(result: Any) -> tuple[float, float, str]:
    """Normalise common blind-panel result shapes without assigning identities."""
    if isinstance(result, tuple | list):
        if len(result) < 2:
            raise ValueError("judge_blind must return scores for both candidates")
        tuple_notes: str = "" if len(result) < 3 else str(result[2])
        return _as_float(result[0]), _as_float(result[1]), tuple_notes
    if not isinstance(result, dict):
        raise ValueError("judge_blind must return a score mapping or score tuple")

    score_a = result.get("score_a", result.get("a", result.get("candidate_a")))
    score_b = result.get("score_b", result.get("b", result.get("candidate_b")))
    if score_a is None or score_b is None:
        scores = result.get("scores")
        if isinstance(scores, tuple | list) and len(scores) >= 2:
            score_a, score_b = scores[0], scores[1]
    if score_a is None or score_b is None:
        raise ValueError("judge_blind result has no score_a/score_b")
    notes_value: Any = result.get("judge_notes", result.get("notes", ""))
    notes: str
    if isinstance(notes_value, list):
        notes = "\n".join(str(note) for note in notes_value)
    else:
        notes = str(notes_value) if notes_value is not None else ""
    return _as_float(score_a), _as_float(score_b), notes


def _outcome(score_a: float, score_b: float) -> float:
    if score_a > score_b:
        return 1.0
    if score_a < score_b:
        return 0.0
    return 0.5


def run_tournament(
    store: Any,
    evaluator: Any,
    subject: str,
    discipline: str,
    config_ids: list[str],
    arena_task: dict[str, Any],
    *,
    dry_run: bool = False,
) -> Tournament:
    """Run every pair on one hash-pinned task and persist blind ELO results."""
    if len(config_ids) < 2:
        raise ValueError("a tournament requires at least two configuration ids")
    if len(set(config_ids)) != len(config_ids):
        raise ValueError("configuration ids must be unique")

    tournament = Tournament(
        subject=subject,
        discipline=discipline,
        arena_task_hash=_arena_task_hash(arena_task),
        config_ids=list(config_ids),
    )
    store.create_tournament(tournament)

    outputs = {
        config_id: _run_config(evaluator, config_id, arena_task, dry_run)
        for config_id in config_ids
    }
    ratings = {config_id: _elo_from_store(store, subject, config_id) for config_id in config_ids}

    for config_a, config_b in combinations(config_ids, 2):
        score_a, score_b, notes = _call_judge(
            evaluator, outputs[config_a], outputs[config_b], arena_task
        )
        outcome_a = _outcome(score_a, score_b)
        outcome_b = 1.0 - outcome_a
        new_a, new_b = elo_update(ratings[config_a].rating, ratings[config_b].rating, outcome_a)
        ratings[config_a] = _record_after_match(ratings[config_a], new_a, outcome_a)
        ratings[config_b] = _record_after_match(ratings[config_b], new_b, outcome_b)
        winner = config_a if outcome_a == 1.0 else config_b if outcome_a == 0.0 else None
        store.record_match(
            MatchResult(
                tournament_id=tournament.id,
                config_a=config_a,
                config_b=config_b,
                winner=winner,
                score_a=score_a,
                score_b=score_b,
                judge_notes=notes,
                blind=True,
            )
        )
        store.upsert_elo(ratings[config_a])
        store.upsert_elo(ratings[config_b])

    winner_config_id = max(config_ids, key=lambda config_id: ratings[config_id].rating)
    tournament = tournament.model_copy(
        update={"winner_config_id": winner_config_id, "status": "done"}
    )
    store.update_tournament(
        tournament.id,
        {"winner_config_id": winner_config_id, "status": "done"},
    )
    return tournament
