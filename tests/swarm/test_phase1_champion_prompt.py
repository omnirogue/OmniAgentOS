from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import digest
from omniagentos.lab import surfaces
from omniagentos.lab.contracts import SurfaceKind
from omniagentos.lab.db import LabStore
from omniagentos.lab.runtime import (
    CHAMPION_PROMPT_MODE_ENV,
    ChampionPromptError,
    champion_prompt_mode,
    get_champion_prompt,
    parse_champion_prompt_mode,
    select_champion_prompt,
)


class ExplodingStore:
    def get_champion(self, discipline: str, kind: str) -> dict[str, Any] | None:
        raise AssertionError("off mode must not read the champion store")

    def get_surface(self, surface_id: str) -> dict[str, Any] | None:
        raise AssertionError("off mode must not read a surface")


@pytest.fixture
def promoted_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[LabStore, Path]:
    store = LabStore(":memory:")
    monkeypatch.setattr(surfaces, "_repository_root", lambda: tmp_path)
    prompt = surfaces.version_prompt(
        store,
        "swarm",
        "fast_implementer",
        "Use the promoted champion instructions.",
    )
    surfaces.seed_champion(store, "swarm", SurfaceKind.PROMPT, prompt.id)
    return store, tmp_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "off"),
        ("", "off"),
        ("invalid", "off"),
        ("1", "off"),
        (" OFF ", "off"),
        ("Shadow", "shadow"),
        (" enforce ", "enforce"),
    ],
)
def test_mode_parser_is_explicit_and_defaults_off(raw: object, expected: str) -> None:
    assert parse_champion_prompt_mode(raw) == expected


def test_environment_mode_defaults_off_and_parses_named_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CHAMPION_PROMPT_MODE_ENV, raising=False)
    assert champion_prompt_mode() == "off"

    monkeypatch.setenv(CHAMPION_PROMPT_MODE_ENV, " shadow ")
    assert champion_prompt_mode() == "shadow"
    assert champion_prompt_mode({CHAMPION_PROMPT_MODE_ENV: "enforce"}) == "enforce"


def test_off_mode_preserves_fallback_without_touching_store() -> None:
    selected = select_champion_prompt(
        "scheduler brief",
        role="not-even-validated/in-off-mode",
        discipline="swarm",
        store=ExplodingStore(),
        mode="off",
    )

    assert selected.prompt == "scheduler brief"
    assert selected.selected_prompt == "scheduler brief"
    assert selected.source == "fallback"
    assert selected.champion is None
    assert selected.shadow_diff is None
    assert selected.reason == "feature_off"


def test_accessor_reads_role_aware_promoted_champion_without_mutating_store(
    promoted_prompt: tuple[LabStore, Path],
) -> None:
    store, root = promoted_prompt
    champion_before = store.get_champion("swarm", "prompt")
    history_before = store.champion_history("swarm", "prompt")
    surfaces_before = store.list_surfaces("swarm", "prompt")

    prompt = get_champion_prompt(
        store,
        "fast_implementer",
        discipline="swarm",
        repository_root=root,
    )

    assert prompt is not None
    assert prompt.role == "fast_implementer"
    assert prompt.discipline == "swarm"
    assert prompt.content == "Use the promoted champion instructions."
    assert prompt.surface_version == 1
    assert prompt.cas_version == 1
    assert store.get_champion("swarm", "prompt") == champion_before
    assert store.champion_history("swarm", "prompt") == history_before
    assert store.list_surfaces("swarm", "prompt") == surfaces_before


def test_accessor_defaults_discipline_to_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LabStore(":memory:")
    monkeypatch.setattr(surfaces, "_repository_root", lambda: tmp_path)
    surface = surfaces.version_prompt(store, "reviewer", "reviewer", "Review carefully.")
    surfaces.seed_champion(store, "reviewer", SurfaceKind.PROMPT, surface.id)

    prompt = get_champion_prompt(store, "reviewer", repository_root=tmp_path)

    assert prompt is not None
    assert prompt.discipline == "reviewer"
    assert prompt.content == "Review carefully."


def test_shadow_mode_preserves_fallback_and_emits_structured_selection_diff(
    promoted_prompt: tuple[LabStore, Path],
) -> None:
    store, root = promoted_prompt

    selected = select_champion_prompt(
        "scheduler brief",
        role="fast_implementer",
        discipline="swarm",
        store=store,
        mode="shadow",
        repository_root=root,
    )

    assert selected.prompt == "scheduler brief"
    assert selected.source == "fallback"
    assert selected.champion is not None
    assert selected.shadow_diff is not None
    assert asdict(selected.shadow_diff) == {
        "role": "fast_implementer",
        "discipline": "swarm",
        "current_source": "fallback",
        "would_select": "champion",
        "changed": True,
        "fallback_hash": digest("scheduler brief"),
        "champion_hash": selected.champion.content_hash,
        "fallback_length": len("scheduler brief"),
        "champion_length": len("Use the promoted champion instructions."),
        "champion_surface_id": selected.champion.surface_id,
        "champion_surface_version": 1,
        "reason": "champion_differs",
    }


def test_enforce_mode_selects_valid_champion(
    promoted_prompt: tuple[LabStore, Path],
) -> None:
    store, root = promoted_prompt

    selected = select_champion_prompt(
        "scheduler brief",
        role="fast_implementer",
        discipline="swarm",
        store=store,
        mode="enforce",
        repository_root=root,
    )

    assert selected.prompt == "Use the promoted champion instructions."
    assert selected.source == "champion"
    assert selected.champion is not None
    assert selected.shadow_diff is None
    assert selected.reason == "champion_selected"


def test_missing_champion_is_structured_in_shadow_and_falls_back_in_enforce() -> None:
    store = LabStore(":memory:")

    shadow = select_champion_prompt(
        "scheduler brief",
        role="fast_implementer",
        discipline="swarm",
        store=store,
        mode="shadow",
    )
    enforced = select_champion_prompt(
        "scheduler brief",
        role="fast_implementer",
        discipline="swarm",
        store=store,
        mode="enforce",
    )

    assert shadow.shadow_diff is not None
    assert shadow.shadow_diff.would_select == "fallback"
    assert shadow.shadow_diff.changed is False
    assert shadow.shadow_diff.reason == "no_champion"
    assert enforced.prompt == "scheduler brief"
    assert enforced.source == "fallback"
    assert enforced.reason == "no_champion"


def test_wrong_role_is_rejected_and_reported_without_breaking_selection(
    promoted_prompt: tuple[LabStore, Path],
) -> None:
    store, root = promoted_prompt

    with pytest.raises(ChampionPromptError, match="wrong role"):
        get_champion_prompt(
            store,
            "reviewer",
            discipline="swarm",
            repository_root=root,
        )

    selected = select_champion_prompt(
        "scheduler brief",
        role="reviewer",
        discipline="swarm",
        store=store,
        mode="shadow",
        repository_root=root,
    )
    assert selected.shadow_diff is not None
    assert selected.shadow_diff.would_select == "fallback"
    assert selected.shadow_diff.reason.startswith("invalid_champion:")


def test_tampered_prompt_content_never_reaches_enforce(
    promoted_prompt: tuple[LabStore, Path],
) -> None:
    store, root = promoted_prompt
    prompt_path = root / "vault" / "prompts" / "fast_implementer" / "v01.md"
    prompt_path.write_text("tampered", encoding="utf-8")

    selected = select_champion_prompt(
        "scheduler brief",
        role="fast_implementer",
        discipline="swarm",
        store=store,
        mode="enforce",
        repository_root=root,
    )

    assert selected.prompt == "scheduler brief"
    assert selected.source == "fallback"
    assert selected.champion is None
    assert selected.reason.startswith("invalid_champion:")


def test_prompt_symlink_cannot_escape_its_canonical_role_directory(
    promoted_prompt: tuple[LabStore, Path],
) -> None:
    store, root = promoted_prompt
    prompt_path = root / "vault" / "prompts" / "fast_implementer" / "v01.md"
    other_role_path = root / "vault" / "prompts" / "reviewer" / "v01.md"
    other_role_path.parent.mkdir(parents=True)
    other_role_path.write_text("Use the promoted champion instructions.", encoding="utf-8")
    prompt_path.unlink()
    prompt_path.symlink_to(other_role_path)

    selected = select_champion_prompt(
        "scheduler brief",
        role="fast_implementer",
        discipline="swarm",
        store=store,
        mode="enforce",
        repository_root=root,
    )

    assert selected.prompt == "scheduler brief"
    assert selected.source == "fallback"
    assert selected.reason == (
        "invalid_champion:prompt surface path escapes its canonical role directory"
    )
