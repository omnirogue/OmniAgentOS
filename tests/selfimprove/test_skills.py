"""capture_skill / capture_skill_from_run_dir (omniagentos/selfimprove/skills.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.contracts import NoteType
from omniagentos.knowledge.memory_store import InMemoryKnowledgeStore
from omniagentos.knowledge.testing import make_fake_embedder
from omniagentos.selfimprove.errors import UnverifiedCaptureError
from omniagentos.selfimprove.models import CapabilityLearning, GateStatus
from omniagentos.selfimprove.paths import PathEscapesRootError, skill_md_path, skill_relpath
from omniagentos.selfimprove.skills import (
    capture_skill,
    capture_skill_from_run_dir,
    render_skill_note,
)
from omniagentos.vault import parse_frontmatter

from .helpers import sample_gate, sample_metadata, write_status_json


def test_render_skill_note_frontmatter_and_relpath() -> None:
    metadata = sample_metadata()
    gate = sample_gate()

    relpath, content = render_skill_note(metadata, gate)

    fm = parse_frontmatter(content)
    # NOTE: not a literal "playbook/skill-add-additive-migration.md" — slugs
    # always carry a digest suffix now (F4, path.safe_slug injectivity fix).
    assert relpath == skill_relpath(metadata.skill_id)
    assert relpath.startswith("playbook/skill-add-additive-migration-")
    assert relpath.endswith(".md")
    assert fm.id == metadata.skill_id
    assert fm.type == NoteType.PLAYBOOK
    assert fm.discipline == "code-changes"
    assert fm.source_run == gate.source_run_id


def test_render_skill_note_body_has_required_sections_and_home_wikilink() -> None:
    _relpath, content = render_skill_note(sample_metadata(), sample_gate())

    for heading in (
        "## Input format",
        "## Steps",
        "## Output structure",
        "## Validation rules",
    ):
        assert heading in content
    assert "[[Home]]" in content
    assert "[[code-changes]]" in content
    assert "Migration must be additive only" in content


def test_capture_skill_writes_note_and_returns_result(vault_dir: Path) -> None:
    metadata = sample_metadata()
    gate = sample_gate()

    result = capture_skill(metadata, gate, str(vault_dir), autocommit=False)

    assert result.skill_id == metadata.skill_id
    note_path = Path(result.note_path)
    assert note_path.is_file()
    assert note_path == (vault_dir / skill_relpath(metadata.skill_id)).resolve()
    assert result.skill_md_path is None


def test_distiller_capture_writes_one_company_note_per_atomic_capability(
    vault_dir: Path,
) -> None:
    metadata = sample_metadata().model_copy(
        update={
            "company_id": "co_initech",
            "brand": "Initech",
            "capabilities": [
                CapabilityLearning(
                    statement="Tool X renders audio and video together",
                    domains=["video", "audio"],
                    kind="tool",
                ),
                CapabilityLearning(
                    statement="Normalize narration before final muxing",
                    domains=["audio"],
                    kind="technique",
                ),
            ],
        }
    )
    store = InMemoryKnowledgeStore(embedder=make_fake_embedder())

    result = capture_skill(
        metadata,
        sample_gate(),
        str(vault_dir),
        capability_store=store,  # type: ignore[arg-type]
        autocommit=False,
    )

    assert len(result.capability_fact_ids) == 2
    assert len(result.capability_note_paths) == 2
    for fact_id, note_path in zip(
        result.capability_fact_ids, result.capability_note_paths, strict=True
    ):
        fact = store.get_fact(fact_id)
        assert fact is not None
        assert fact.capability_scope == "company"
        assert fact.company_id == "co_initech"
        assert fact.last_verified is not None
        assert Path(note_path).is_file()


def test_distiller_rejects_non_atomic_capability_before_any_write(vault_dir: Path) -> None:
    metadata = sample_metadata().model_copy(
        update={
            "company_id": "co_initech",
            "brand": "Initech",
            "capabilities": [
                CapabilityLearning(
                    statement="Tool X renders video\n- customer Alice pays $499",
                    domains=["video"],
                    kind="tool",
                )
            ],
        }
    )
    store = InMemoryKnowledgeStore(embedder=make_fake_embedder())

    with pytest.raises(ValueError, match="exactly one line"):
        capture_skill(
            metadata,
            sample_gate(),
            str(vault_dir),
            capability_store=store,  # type: ignore[arg-type]
            autocommit=False,
        )

    assert not (vault_dir / "playbook").exists()
    assert store._facts == {}


def test_capture_skill_refuses_unverified_gate_and_writes_nothing(vault_dir: Path) -> None:
    metadata = sample_metadata()
    gate = sample_gate(status=GateStatus.FAILED)

    with pytest.raises(UnverifiedCaptureError):
        capture_skill(metadata, gate, str(vault_dir), autocommit=False)

    assert not (vault_dir / "playbook").exists()


def test_capture_skill_refuses_pending_gate(vault_dir: Path) -> None:
    metadata = sample_metadata()
    gate = sample_gate(status=GateStatus.PENDING)

    with pytest.raises(UnverifiedCaptureError):
        capture_skill(metadata, gate, str(vault_dir), autocommit=False)


def test_capture_skill_optionally_mirrors_skill_md(vault_dir: Path, tmp_path: Path) -> None:
    metadata = sample_metadata()
    gate = sample_gate()
    skills_dir = tmp_path / "skills"

    result = capture_skill(
        metadata, gate, str(vault_dir), skills_dir=str(skills_dir), autocommit=False
    )

    assert result.skill_md_path is not None
    skill_md = Path(result.skill_md_path)
    assert skill_md == skill_md_path(metadata.skill_id, skills_dir=str(skills_dir))
    content = skill_md.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "name: add-additive-migration" in content
    assert "## Validation rules" in content
    assert "Captured from a **passed** verification gate" in content


def test_capture_skill_refuses_pre_existing_symlinked_skill_directory(
    vault_dir: Path, tmp_path: Path
) -> None:
    """F3: a pre-existing symlink at <skills_dir>/<skill-id-slug> must not
    let the SKILL.md mirror write escape skills_dir."""
    metadata = sample_metadata()
    gate = sample_gate()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    slug_dir_name = skill_md_path(metadata.skill_id, skills_dir=str(skills_dir)).parent.name
    (skills_dir / slug_dir_name).symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathEscapesRootError):
        capture_skill(metadata, gate, str(vault_dir), skills_dir=str(skills_dir), autocommit=False)

    assert not (outside / "SKILL.md").exists()


def test_capture_skill_refuses_pre_existing_symlinked_skill_md_file(
    vault_dir: Path, tmp_path: Path
) -> None:
    metadata = sample_metadata()
    gate = sample_gate()
    skills_dir = tmp_path / "skills"
    outside_file = tmp_path / "outside-SKILL.md"
    outside_file.write_text("do not touch\n", encoding="utf-8")
    target = skill_md_path(metadata.skill_id, skills_dir=str(skills_dir))
    target.parent.mkdir(parents=True)
    target.symlink_to(outside_file)

    with pytest.raises((PathEscapesRootError, OSError)):
        capture_skill(metadata, gate, str(vault_dir), skills_dir=str(skills_dir), autocommit=False)

    assert outside_file.read_text(encoding="utf-8") == "do not touch\n"


def test_capture_skill_from_run_dir_reads_real_status_json(vault_dir: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "session-1"
    write_status_json(run_dir, state="done")
    metadata = sample_metadata()

    result = capture_skill_from_run_dir(str(run_dir), metadata, str(vault_dir), autocommit=False)

    assert Path(result.note_path).is_file()


def test_capture_skill_from_run_dir_refuses_when_state_is_partial(
    vault_dir: Path, tmp_path: Path
) -> None:
    run_dir = tmp_path / "session-2"
    write_status_json(run_dir, state="partial")
    metadata = sample_metadata()

    with pytest.raises(UnverifiedCaptureError):
        capture_skill_from_run_dir(str(run_dir), metadata, str(vault_dir), autocommit=False)

    assert not (vault_dir / "playbook").exists()


def test_skill_relpath_does_not_collide_with_lab_discipline_playbook_files() -> None:
    # H2 lab writes bare `playbook/<discipline>.md` (omniagentos/lab/vault/paths.py);
    # a skill id equal to a real discipline name must never collide with it.
    assert skill_relpath("code-changes") != "playbook/code-changes.md"
    assert skill_relpath("code-changes").startswith("playbook/skill-code-changes-")
    assert skill_relpath("code-changes").endswith(".md")
