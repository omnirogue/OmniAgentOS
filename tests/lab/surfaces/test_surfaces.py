from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.lab import surfaces
from omniagentos.lab.contracts import (
    Disposition,
    Experiment,
    ExperimentStatus,
    Scorecard,
    Surface,
    SurfaceKind,
    SurfaceStatus,
)
from omniagentos.lab.db import LabStore


@pytest.fixture
def store() -> LabStore:
    return LabStore(":memory:")


@pytest.fixture
def surface_root(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(surfaces, "_repository_root", lambda: tmp_path)
    return tmp_path


def test_surface_file_refuses_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_surface_file`` must refuse a relative path that resolves outside the repo.

    Failing-on-revert: if the inode containment check is removed and the helper
    becomes ``return root / relative``, this raises nothing and the assertion fails.
    """
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("leaked", encoding="utf-8")
    escape = root / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(surfaces, "_repository_root", lambda: root)

    with pytest.raises(ValueError, match="escapes repository"):
        surfaces._surface_file("escape/secret.txt")

    # Positive control: a safe relative path under the repo is admitted.
    admitted = surfaces._surface_file("vault/prompts/coder/v01.md")
    assert admitted == (root / "vault" / "prompts" / "coder" / "v01.md").resolve()


def test_versions_prompt_and_genome_with_surface_id_binding(store: LabStore, surface_root) -> None:
    first = surfaces.version_prompt(store, "coding", "coder", "be precise")
    second = surfaces.version_prompt(store, "coding", "coder", "be concise", parent_version=1)

    assert first.path == "vault/prompts/coder/v01.md"
    assert second.path == "vault/prompts/coder/v02.md"
    assert surfaces.load_surface_content(store.get_surface(first.id)) == "be precise"  # type: ignore[arg-type]

    genome = {
        "archetype": "pipeline",
        "roles": [{"name": "writer", "agent": "mock", "model": "mock-v1"}],
        "flow": [{"stage": "draft", "kind": "generate", "role": "writer"}],
        "judge": {"blind": True},
        "review": {"adversarial": False},
        "budget": {"tokens": 200},
    }
    surface = surfaces.version_genome(store, "coding", genome)
    loaded = surfaces.load_surface_content(store.get_surface(surface.id))  # type: ignore[arg-type]

    assert surface.path == f"configs/genomes/{surface.id}.json"
    assert loaded == {"id": surface.id, **genome}


def _experiment(champion_id: str, challenger_id: str, kind: SurfaceKind) -> Experiment:
    return Experiment(
        id="exp_promote",
        hypothesis="improves the surface",
        discipline="coding",
        mutable_surface_kind=kind,
        champion_surface_id=champion_id,
        challenger_surface_id=challenger_id,
        eval_suite_id="evs_unused",
        status=ExperimentStatus.DECIDED,
        disposition=Disposition.PROMOTE,
    )


def test_promote_archives_outgoing_and_rollback_keeps_it_immutable(
    store: LabStore, surface_root
) -> None:
    seed = surfaces.version_prompt(store, "coding", "coder", "first")
    challenger = surfaces.version_prompt(store, "coding", "coder", "better", parent_version=1)
    surfaces.seed_champion(store, "coding", SurfaceKind.PROMPT, seed.id)
    store.create_experiment(_experiment(seed.id, challenger.id, SurfaceKind.PROMPT))

    promoted = surfaces.promote(store, "exp_promote", challenger.id)

    assert promoted.rollback_to_surface_id == seed.id
    assert promoted.cas_version == 2
    assert store.get_surface(seed.id)["status"] == SurfaceStatus.ARCHIVED  # type: ignore[index]
    assert store.get_champion("coding", "prompt")["surface_id"] == challenger.id  # type: ignore[index]

    rolled_back = surfaces.rollback(store, "coding", SurfaceKind.PROMPT)

    assert rolled_back.surface_id == seed.id
    assert rolled_back.rollback_to_surface_id == challenger.id
    assert store.get_champion("coding", "prompt")["surface_id"] == seed.id  # type: ignore[index]
    assert store.get_surface(seed.id)["status"] == SurfaceStatus.CHAMPION  # type: ignore[index]
    assert store.get_surface(challenger.id)["status"] == SurfaceStatus.ARCHIVED  # type: ignore[index]

    second = surfaces.version_prompt(store, "coding", "coder", "best", parent_version=2)
    second_experiment = _experiment(seed.id, second.id, SurfaceKind.PROMPT).model_copy(
        update={"id": "exp_promote_after_rollback"}
    )
    store.create_experiment(second_experiment)

    promoted_again = surfaces.promote(store, second_experiment.id, second.id)

    assert promoted_again.rollback_to_surface_id == seed.id
    assert store.get_champion("coding", "prompt")["surface_id"] == second.id  # type: ignore[index]
    assert store.get_surface(seed.id)["status"] == SurfaceStatus.ARCHIVED  # type: ignore[index]
    assert store.get_surface(second.id)["status"] == SurfaceStatus.CHAMPION  # type: ignore[index]


def test_promote_refuses_unresolved_audit_flags(store: LabStore, surface_root) -> None:
    seed = surfaces.version_prompt(store, "coding", "coder", "first")
    challenger = surfaces.version_prompt(store, "coding", "coder", "suspicious")
    surfaces.seed_champion(store, "coding", SurfaceKind.PROMPT, seed.id)
    experiment = _experiment(seed.id, challenger.id, SurfaceKind.PROMPT).model_copy(
        update={"scorecard": Scorecard(audit_flags=["suspicious_perfect:accuracy"])}
    )
    store.create_experiment(experiment)

    with pytest.raises(ValueError, match="unresolved audit flags"):
        surfaces.promote(store, experiment.id, challenger.id)

    assert store.get_champion("coding", "prompt")["surface_id"] == seed.id  # type: ignore[index]


def test_safety_surface_routes_experiment_to_human_review(
    store: LabStore, surface_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = Surface(
        id="srf_seed",
        kind=SurfaceKind.REVIEW_RUBRIC,
        discipline="coding",
        path="vault/prompts/reviewer/v01.md",
        content_hash="seed",
    )
    challenger = seed.model_copy(
        update={
            "id": "srf_challenger",
            "version": 2,
            "path": "vault/prompts/reviewer/v02.md",
            "content_hash": "challenger",
        }
    )
    store.create_surface(seed)
    store.create_surface(challenger)
    surfaces.seed_champion(store, "coding", SurfaceKind.REVIEW_RUBRIC, seed.id)
    store.create_experiment(_experiment(seed.id, challenger.id, SurfaceKind.REVIEW_RUBRIC))

    with pytest.raises(ValueError, match="human_decided_by"):
        surfaces.promote(store, "exp_promote", challenger.id)

    assert store.get_experiment("exp_promote")["disposition"] == Disposition.HUMAN_REVIEW  # type: ignore[index]

    monkeypatch.delenv(surfaces.OPERATOR_TOKEN_ENV, raising=False)
    with pytest.raises(ValueError, match="OMNIAGENTOS_OPERATOR_TOKEN"):
        surfaces.promote(store, "exp_promote", challenger.id, human_decided_by="operator")

    monkeypatch.setenv(surfaces.OPERATOR_TOKEN_ENV, "correct-token")
    with pytest.raises(ValueError, match="operator token"):
        surfaces.promote(store, "exp_promote", challenger.id, human_decided_by="operator")
    with pytest.raises(ValueError, match="operator token"):
        surfaces.promote(
            store,
            "exp_promote",
            challenger.id,
            human_decided_by="operator",
            operator_token="wrong",
        )

    surfaces.promote(
        store,
        "exp_promote",
        challenger.id,
        human_decided_by="operator",
        operator_token="correct-token",
    )
    assert store.get_champion("coding", "review_rubric")["surface_id"] == challenger.id  # type: ignore[index]


def test_safety_surface_rollback_requires_operator_token(
    store: LabStore, surface_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = Surface(
        id="srf_seed",
        kind=SurfaceKind.REVIEW_RUBRIC,
        discipline="coding",
        path="vault/prompts/reviewer/v01.md",
        content_hash="seed",
    )
    challenger = seed.model_copy(
        update={
            "id": "srf_challenger",
            "version": 2,
            "path": "vault/prompts/reviewer/v02.md",
            "content_hash": "challenger",
        }
    )
    store.create_surface(seed)
    store.create_surface(challenger)
    surfaces.seed_champion(store, "coding", SurfaceKind.REVIEW_RUBRIC, seed.id)
    store.create_experiment(_experiment(seed.id, challenger.id, SurfaceKind.REVIEW_RUBRIC))
    monkeypatch.setenv(surfaces.OPERATOR_TOKEN_ENV, "correct-token")
    surfaces.promote(
        store,
        "exp_promote",
        challenger.id,
        human_decided_by="operator",
        operator_token="correct-token",
    )

    monkeypatch.delenv(surfaces.OPERATOR_TOKEN_ENV)
    with pytest.raises(ValueError, match="OMNIAGENTOS_OPERATOR_TOKEN"):
        surfaces.rollback(store, "coding", SurfaceKind.REVIEW_RUBRIC)

    monkeypatch.setenv(surfaces.OPERATOR_TOKEN_ENV, "correct-token")
    with pytest.raises(ValueError, match="operator token"):
        surfaces.rollback(store, "coding", SurfaceKind.REVIEW_RUBRIC)
    with pytest.raises(ValueError, match="operator token"):
        surfaces.rollback(
            store,
            "coding",
            SurfaceKind.REVIEW_RUBRIC,
            operator_token="wrong-token",
        )

    rolled_back = surfaces.rollback(
        store,
        "coding",
        SurfaceKind.REVIEW_RUBRIC,
        operator_token="correct-token",
    )
    assert rolled_back.surface_id == seed.id
    assert store.get_champion("coding", "review_rubric")["surface_id"] == seed.id  # type: ignore[index]
