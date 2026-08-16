"""write_note (contracts/interfaces.md §p05): human-section preservation,
parent-dir creation, absolute-path return. Git behavior is covered separately
in test_gitops.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.vault import render_run_note, write_note
from omniagentos.vault.sections import NOTES_HUMAN_HEADING
from tests.vault.helpers import sample_run


def test_write_note_creates_parent_dirs_and_returns_abs_path(vault_dir: Path) -> None:
    relpath = "runs/2026/07/run_new.md"
    run = sample_run(id="run_new")
    _rp, content = render_run_note(run, [], "ledger/x.jsonl", [])
    assert not (vault_dir / "runs").exists()

    result = write_note(str(vault_dir), relpath, content, autocommit=False)

    written = Path(result)
    assert written.is_absolute()
    assert written == (vault_dir / relpath).resolve()
    assert written.is_file()


def test_write_note_preserves_human_section_verbatim_on_regeneration(vault_dir: Path) -> None:
    run = sample_run(id="run_human_test")
    relpath, content_v1 = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(vault_dir), relpath, content_v1, autocommit=False)

    note_path = vault_dir / relpath
    on_disk = note_path.read_text(encoding="utf-8")
    human_note = "Operator: re-ran after a flaky network blip, second attempt succeeded."
    edited = on_disk + f"\n{human_note}\n"
    note_path.write_text(edited, encoding="utf-8")

    # regenerate with different data (simulates a later re-render of the same run)
    run_v2 = sample_run(id="run_human_test", task_title="Fix flaky retry test (updated)")
    relpath_v2, content_v2 = render_run_note(run_v2, [], "ledger/x.jsonl", [])
    assert relpath_v2 == relpath
    write_note(str(vault_dir), relpath_v2, content_v2, autocommit=False)

    final = note_path.read_text(encoding="utf-8")
    assert human_note in final
    assert "Fix flaky retry test (updated)" in final
    # exactly one Notes (human) heading — not duplicated across regenerations
    assert final.count(NOTES_HUMAN_HEADING) == 1


def test_write_note_preserves_human_section_when_regenerated_content_is_shorter(
    vault_dir: Path,
) -> None:
    relpath = "learnings/example.md"
    v1 = (
        "---\nid: x\ntype: learning\ndiscipline: null\ncreated: '2026-01-01T00:00:00Z'\n"
        "source_run: null\nconfidence: null\nstatus: active\nsupersedes: null\n---\n\n"
        "# learning: example\n\nSome system text.\n\n## Notes (human)\n"
    )
    write_note(str(vault_dir), relpath, v1, autocommit=False)
    note_path = vault_dir / relpath
    note_path.write_text(note_path.read_text(encoding="utf-8") + "\nHand-written insight.\n", encoding="utf-8")

    v2 = (
        "---\nid: x\ntype: learning\ndiscipline: null\ncreated: '2026-01-02T00:00:00Z'\n"
        "source_run: null\nconfidence: null\nstatus: active\nsupersedes: null\n---\n\n"
        "# learning: example (regenerated)\n\n## Notes (human)\n"
    )
    write_note(str(vault_dir), relpath, v2, autocommit=False)

    final = note_path.read_text(encoding="utf-8")
    assert "Hand-written insight." in final
    assert "example (regenerated)" in final


def test_write_note_appends_human_section_if_new_template_lacks_one(vault_dir: Path) -> None:
    relpath = "learnings/no-human-section.md"
    v1 = (
        "---\nid: y\ntype: learning\ndiscipline: null\ncreated: '2026-01-01T00:00:00Z'\n"
        "source_run: null\nconfidence: null\nstatus: active\nsupersedes: null\n---\n\n"
        "# learning: y\n\nbody\n\n## Notes (human)\nKeep this.\n"
    )
    write_note(str(vault_dir), relpath, v1, autocommit=False)

    v2_without_section = (
        "---\nid: y\ntype: learning\ndiscipline: null\ncreated: '2026-01-02T00:00:00Z'\n"
        "source_run: null\nconfidence: null\nstatus: active\nsupersedes: null\n---\n\n"
        "# learning: y (v2)\n\nbody v2\n"
    )
    write_note(str(vault_dir), relpath, v2_without_section, autocommit=False)

    final = (vault_dir / relpath).read_text(encoding="utf-8")
    assert "Keep this." in final
    assert "y (v2)" in final


def test_write_note_first_write_has_no_prior_content_to_merge(vault_dir: Path) -> None:
    relpath = "runs/2026/07/run_fresh.md"
    run = sample_run(id="run_fresh")
    _rp, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(vault_dir), relpath, content, autocommit=False)
    final = (vault_dir / relpath).read_text(encoding="utf-8")
    assert final == content


def test_malicious_run_id_cannot_escape_vault(vault_dir: Path) -> None:
    run_id = "improve:x/../../ESCAPE/pwned"
    relpath, content = render_run_note(
        sample_run(id=run_id), [], "ledger/x.jsonl", []
    )

    written = Path(write_note(str(vault_dir), relpath, content, autocommit=False))

    assert written.is_relative_to(vault_dir.resolve())
    assert written.name != "pwned.md"
    assert not (vault_dir.parent / "ESCAPE" / "pwned.md").exists()
    with pytest.raises(ValueError, match="non-traversing"):
        write_note(str(vault_dir), "../ESCAPE/pwned.md", content, autocommit=False)
    assert not (vault_dir.parent / "ESCAPE" / "pwned.md").exists()
