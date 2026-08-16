from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.lab import surfaces
from omniagentos.lab.contracts import GenomeSpec, Surface, SurfaceKind
from omniagentos.lab.db import LabStore
from omniagentos.lab.tournament import (
    accumulate_playbook,
    mutate_single_trait,
    run_tournament,
)
from omniagentos.lab.tournament.driver import TournamentDriver


def _genome(config_id: str, *, model: str, iterations: int = 1) -> dict[str, Any]:
    return {
        "id": config_id,
        "roles": [
            {"name": "writer", "agent": "mock", "model": model, "effort": "medium"},
            {"name": "judge", "agent": "mock", "model": "judge-model", "effort": "high"},
        ],
        "flow": [
            {"stage": "draft", "kind": "generate", "role": "writer", "iterations": iterations},
            {"stage": "judge", "kind": "judge", "role": "judge", "inputs_from": ["draft"]},
        ],
        "judge": {"panel": ["mock-judge"], "blind": True, "dimensions": ["quality"]},
        "review": {"adversarial": False, "categories": ["correctness"]},
        "budget": {"wall_min": 1, "tokens": 1000, "cost_usd": 0.01},
    }


def _store_genome(store: LabStore, root: Path, genome: dict[str, Any]) -> None:
    path = root / f"{genome['id']}.json"
    path.write_text(json.dumps(genome), encoding="utf-8")
    store.create_surface(
        Surface(
            id=genome["id"],
            kind=SurfaceKind.ORCHESTRATION_GENOME,
            discipline="coding",
            path=str(path),
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    )


class MockBlindJudge:
    def judge_blind(
        self, *, output_a: dict[str, Any], output_b: dict[str, Any], arena_task: dict[str, Any]
    ) -> dict[str, Any]:
        # Only the opaque payload is received.  Scores are properties of the
        # output, rather than a configuration identifier.
        assert set(output_a) == {"blind_token", "output"}
        assert set(output_b) == {"blind_token", "output"}
        quality_a = output_a["output"]["quality"]
        quality_b = output_b["output"]["quality"]
        return {
            "score_a": quality_a,
            "score_b": quality_b,
            "notes": f"blind panel compared {quality_a:.1f} to {quality_b:.1f}",
        }


def test_run_tournament_dry_run(tmp_path: Path) -> None:
    store = LabStore(":memory:")
    config_ids = ["srf_alpha", "srf_beta", "srf_gamma"]
    tournament = run_tournament(
        store,
        MockBlindJudge(),
        subject="python",
        discipline="coding",
        config_ids=config_ids,
        arena_task={
            "prompt": "implement fizzbuzz",
            "mock_outputs": {
                "srf_alpha": {"quality": 0.9},
                "srf_beta": {"quality": 0.7},
                "srf_gamma": {"quality": 0.4},
            },
        },
        dry_run=True,
    )

    assert tournament.status == "done"
    assert tournament.winner_config_id == "srf_alpha"
    matches = store._connection.execute(
        "SELECT * FROM matches WHERE tournament_id = ? ORDER BY id", (tournament.id,)
    ).fetchall()
    assert len(matches) == 3
    assert all(row["judge_notes"].startswith("blind panel") for row in matches)
    ratings = {config_id: store.get_elo("python", config_id)["rating"] for config_id in config_ids}
    assert ratings["srf_alpha"] > ratings["srf_beta"] > ratings["srf_gamma"]


def test_production_driver_resolves_surface_before_real_genome_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.lab.executor as executor

    store = LabStore(":memory:")
    monkeypatch.setattr(surfaces, "_repository_root", lambda: tmp_path)
    alpha = surfaces.version_genome(store, "coding", _genome("ignored-a", model="alpha"))
    beta = surfaces.version_genome(store, "coding", _genome("ignored-b", model="beta"))
    seen: list[str] = []

    def fake_execute(
        genome: GenomeSpec,
        case_input: dict[str, Any],
        budgets: Any,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        del case_input, budgets
        assert dry_run is False
        seen.append(genome.id)
        return {"quality": 1.0 if genome.id == alpha.id else 0.0}

    monkeypatch.setattr(executor, "execute_genome", fake_execute)
    tournament = run_tournament(
        store,
        TournamentDriver(store),
        subject="python",
        discipline="coding",
        config_ids=[alpha.id, beta.id],
        arena_task={"prompt": "implement fizzbuzz", "deterministic_metric": "quality"},
    )

    assert tournament.status == "done"
    assert tournament.winner_config_id == alpha.id
    assert seen == [alpha.id, beta.id]
    assert len(store.tournament_matches(tournament.id)) == 1


def _leaf_differences(left: Any, right: Any) -> int:
    if isinstance(left, dict) and isinstance(right, dict):
        return sum(
            _leaf_differences(left.get(key), right.get(key)) for key in left.keys() | right.keys()
        )
    if isinstance(left, list) and isinstance(right, list):
        return abs(len(left) - len(right)) + sum(
            _leaf_differences(a, b) for a, b in zip(left, right, strict=False)
        )
    return int(left != right)


def test_mutate_single_trait() -> None:
    original = _genome("srf_original", model="claude-opus")
    variants = mutate_single_trait(original)

    assert 3 <= len(variants) <= 5
    for variant in variants:
        GenomeSpec.model_validate(variant)
        assert _leaf_differences(original, variant) == 1


def test_accumulate_playbook_from_decisive_wins(tmp_path: Path) -> None:
    store = LabStore(":memory:")
    winner = _genome("srf_opus", model="claude-opus")
    loser = _genome("srf_sonnet", model="claude-sonnet")
    _store_genome(store, tmp_path, winner)
    _store_genome(store, tmp_path, loser)
    tournament = run_tournament(
        store,
        MockBlindJudge(),
        subject="python",
        discipline="coding",
        config_ids=[winner["id"], loser["id"]],
        arena_task={
            "mock_outputs": {
                winner["id"]: {"quality": 1.0},
                loser["id"]: {"quality": 0.0},
            }
        },
        dry_run=True,
    )

    entries = accumulate_playbook(store, tournament.id)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.evidence_experiments == []
    assert entry.evidence_tournaments == [tournament.id]
    assert "claude-opus" in entry.trait
    assert entry.elo_support is not None and entry.elo_support > 0


def test_blind_pairing_fresh_tokens_and_forced_swap_unswaps_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tokens are never position labels, and canonical score order survives both
    presentation orders; swapped runs mark their notes."""
    from omniagentos.lab.tournament import core

    seen_tokens: list[str] = []

    class TokenCheckingJudge(MockBlindJudge):
        def judge_blind(self, *, output_a, output_b, arena_task):
            for payload in (output_a, output_b):
                token = payload["blind_token"]
                assert token not in {"candidate-a", "candidate-b"}
                assert len(token) >= 24
                seen_tokens.append(token)
            return super().judge_blind(output_a=output_a, output_b=output_b, arena_task=arena_task)

    for forced in (0, 1):
        monkeypatch.setattr(core.secrets, "randbits", lambda _n, _f=forced: _f)
        score_a, score_b, notes = core._call_judge(
            TokenCheckingJudge(),
            {"quality": 0.9},
            {"quality": 0.4},
            {"prompt": "x"},
        )
        assert (score_a, score_b) == (0.9, 0.4)
        assert ("[presentation-order swapped]" in notes) == bool(forced)
    assert len(seen_tokens) == len(set(seen_tokens))
