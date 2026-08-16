"""omniagentos.lab.vault.paths — relpath builders stay confined (contracts/
lab-interfaces.md §L08-labvault "confinement honored") even under adversarial
input, and are a no-op for the already-clean ids/slugs this system actually
uses (contracts.new_id output, hand-picked discipline slugs)."""

from __future__ import annotations

from omniagentos.lab.vault.paths import (
    experiment_relpath,
    leaderboard_relpath,
    playbook_relpath,
    prompt_note_relpath,
    safe_slug,
    tournament_relpath,
)


def test_safe_slug_is_noop_for_clean_ids() -> None:
    assert safe_slug("exp_ab12cd34ef") == "exp_ab12cd34ef"
    assert safe_slug("code-changes") == "code-changes"
    assert safe_slug("coding-orchestration") == "coding-orchestration"


def test_safe_slug_neutralizes_path_traversal() -> None:
    slug = safe_slug("../../etc/passwd")
    assert ".." not in slug
    assert "/" not in slug
    assert slug  # never empty


def test_safe_slug_neutralizes_absolute_and_whitespace() -> None:
    assert "/" not in safe_slug("/etc/passwd")
    assert " " not in safe_slug("my subject with spaces")
    assert safe_slug("") == "note"


def test_safe_slug_is_collision_resistant_when_sanitized() -> None:
    # Two different unsafe inputs that sanitize to the same visible prefix
    # must not collide on disk - the hash suffix (keyed on the ORIGINAL
    # value) disambiguates them.
    a = safe_slug("../evil")
    b = safe_slug("..\\evil")
    assert a.startswith("evil-")
    assert b.startswith("evil-")
    assert a != b


def test_relpath_builders_stay_relative_and_inside_their_folder() -> None:
    assert experiment_relpath("exp_1") == "experiments/exp_1.md"
    assert tournament_relpath("tnm_1") == "tournaments/tnm_1.md"
    assert leaderboard_relpath("coding-orchestration") == "leaderboard/coding-orchestration.md"
    assert playbook_relpath("coding") == "playbook/coding.md"
    assert prompt_note_relpath("coding", "srf_1") == "prompts/coding/srf_1.md"


def test_relpath_builders_never_escape_even_with_malicious_ids() -> None:
    for builder in (experiment_relpath, tournament_relpath, leaderboard_relpath, playbook_relpath):
        relpath = builder("../../../etc/passwd")
        assert not relpath.startswith("/")
        assert ".." not in relpath.split("/")

    relpath = prompt_note_relpath("../escape", "../../also-escape")
    assert not relpath.startswith("/")
    assert ".." not in relpath.split("/")


def test_prompt_note_relpath_never_collides_with_l03_version_filenames() -> None:
    # L03-surfaces writes the raw prompt content at prompts/<role>/vNN.md
    # (contracts/lab-interfaces.md §L03-surfaces); our companion note must
    # never land on that exact filename even when role == discipline.
    note_path = prompt_note_relpath("coder", "srf_x")
    assert note_path != "prompts/coder/v1.md"
    assert note_path == "prompts/coder/srf_x.md"
