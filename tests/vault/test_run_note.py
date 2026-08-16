"""render_run_note (contracts/interfaces.md §p05, D-011 wikilinks, Usage
estimated-flag surfacing)."""

from __future__ import annotations

import re
from pathlib import Path

from omniagentos.contracts import IdempotencyReceipt, NoteType
from omniagentos.vault import parse_frontmatter, render_run_note, write_note
from tests.vault.helpers import sample_run, sample_steps


def test_relpath_is_runs_yyyy_mm_run_id() -> None:
    run = sample_run(id="run_xyz123")
    relpath, _content = render_run_note(run, [], "ledger/x.jsonl", [])
    assert relpath.startswith("runs/")
    assert relpath.endswith("/run_xyz123.md")
    parts = relpath.split("/")
    assert len(parts) == 4  # runs / yyyy / mm / file
    assert len(parts[1]) == 4 and parts[1].isdigit()
    assert len(parts[2]) == 2 and parts[2].isdigit()


def test_run_note_frontmatter_matches_run() -> None:
    run = sample_run(id="run_abc", discipline="code-changes")
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    fm = parse_frontmatter(content)
    assert fm.id == "run_abc"
    assert fm.type == NoteType.RUN
    assert fm.discipline == "code-changes"
    assert fm.source_run == "run_abc"
    assert fm.status == "active"
    assert fm.confidence is None
    assert fm.supersedes is None


def test_run_note_contains_required_wikilinks() -> None:
    run = sample_run(discipline="code-changes")
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    assert "[[code-changes]]" in content
    assert "[[Home]]" in content


def test_run_note_task_title_is_plain_text_not_a_wikilink() -> None:
    run = sample_run(task_title="Fix flaky retry test")
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    assert "Fix flaky retry test" in content
    assert "[[Fix flaky retry test]]" not in content
    assert "# run: Fix flaky retry test" in content


def test_run_note_wikilinks_resolve_after_write(vault_dir: Path) -> None:
    run = sample_run(discipline="code-changes")
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(vault_dir), relpath, content)

    assert "[[code-changes]]" in content
    assert "[[Home]]" in content
    # both wikilink TARGETS must actually exist as files in the vault
    assert (vault_dir / "disciplines" / "code-changes.md").is_file()
    assert (vault_dir / "Home.md").is_file()


def test_discipline_stub_has_valid_frontmatter_and_home_link(vault_dir: Path) -> None:
    run = sample_run(discipline="research-briefs", id="run_stubtest")
    relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    write_note(str(vault_dir), relpath, content)

    stub = (vault_dir / "disciplines" / "research-briefs.md").read_text(encoding="utf-8")
    fm = parse_frontmatter(stub)
    assert fm.id == "research-briefs"
    assert fm.type == NoteType.DISCIPLINE
    assert fm.status == "draft"
    assert fm.source_run == "run_stubtest"
    assert "[[Home]]" in stub


def test_discipline_stub_not_overwritten_once_present(vault_dir: Path) -> None:
    run1 = sample_run(discipline="code-changes", id="run_first")
    relpath1, content1 = render_run_note(run1, [], "ledger/x.jsonl", [])
    write_note(str(vault_dir), relpath1, content1)
    stub_path = vault_dir / "disciplines" / "code-changes.md"
    original = stub_path.read_text(encoding="utf-8")

    # a human edits the stub
    edited = original.replace("## Notes (human)", "## Notes (human)\n\nHuman-authored scope text.")
    stub_path.write_text(edited, encoding="utf-8")

    run2 = sample_run(discipline="code-changes", id="run_second")
    relpath2, content2 = render_run_note(run2, [], "ledger/x.jsonl", [])
    write_note(str(vault_dir), relpath2, content2)

    assert stub_path.read_text(encoding="utf-8") == edited


def test_usage_estimated_flag_shown_true() -> None:
    run = sample_run(usage_estimated=True, usage_source="estimator")
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    usage_section = content.split("## Usage", 1)[1].split("## Steps", 1)[0]
    assert "Estimated:" in usage_section
    assert "**yes**" in usage_section
    assert "estimator" in usage_section


def test_usage_estimated_flag_shown_false_when_provider_reported() -> None:
    run = sample_run(usage_estimated=False, usage_source="cli-report")
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    usage_section = content.split("## Usage", 1)[1].split("## Steps", 1)[0]
    assert "Estimated:" in usage_section
    assert "**no**" in usage_section
    assert "cli-report" in usage_section


def test_usage_shows_tokens_and_cost() -> None:
    run = sample_run(input_tokens=111, output_tokens=222, cost_usd=1.5)
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    assert "input=111" in content
    assert "output=222" in content
    assert "$1.5000 USD" in content


def test_usage_missing_tokens_and_cost_says_not_reported() -> None:
    run = sample_run(input_tokens=None, output_tokens=None, cost_usd=None)
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    usage_section = content.split("## Usage", 1)[1].split("## Steps", 1)[0]
    assert "not reported" in usage_section


def test_steps_table_rendered() -> None:
    run = sample_run()
    _relpath, content = render_run_note(run, sample_steps(), "ledger/x.jsonl", [])
    assert "| 1 | plan | completed" in content
    assert "| 2 | apply_patch | completed" in content


def test_no_steps_says_none_recorded() -> None:
    run = sample_run()
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    steps_section = content.split("## Steps", 1)[1].split("## Artifacts", 1)[0]
    assert "No steps recorded" in steps_section


def test_provenance_has_task_trace_and_manifest() -> None:
    run = sample_run(task_id="tsk_prov", trace_id="trace_prov")
    _relpath, content = render_run_note(run, [], "ledger/runs-202607.jsonl", [])
    provenance = content.split("## Provenance", 1)[1]
    assert "tsk_prov" in provenance
    assert "trace_prov" in provenance
    assert "ledger/runs-202607.jsonl" in provenance


def test_receipts_listed_under_provenance() -> None:
    run = sample_run()
    receipts = [
        IdempotencyReceipt(
            key="idem_1",
            run_id=run["id"],
            step_name="apply_patch",
            result_json='{"ok": true}',
            created_at="2026-07-11T15:21:00Z",
            completed_at="2026-07-11T15:21:05Z",
        )
    ]
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", receipts)
    provenance = content.split("## Provenance", 1)[1]
    assert "idem_1" in provenance
    assert "apply_patch" in provenance
    assert "2026-07-11T15:21:00Z" in provenance


def test_no_receipts_says_none_recorded() -> None:
    run = sample_run()
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    assert "No receipts recorded" in content


def test_manifest_path_pointed_to_not_duplicated_as_json() -> None:
    """Machine-readable duplication is forbidden (contract): the note points to
    the ledger line rather than embedding the manifest JSON."""
    run = sample_run()
    _relpath, content = render_run_note(run, [], "ledger/runs-202607.jsonl", [])
    assert "ledger/runs-202607.jsonl" in content
    assert '"schema_version"' not in content


def test_minimal_run_dict_does_not_crash() -> None:
    """run: dict is untyped by contract — a sparse dict must still render a
    readable note, not raise."""
    relpath, content = render_run_note({"id": "run_bare"}, [], "", [])
    assert re.fullmatch(r"runs/\d{4}/\d{2}/run_bare\.md", relpath)
    assert "[[Home]]" in content
    fm = parse_frontmatter(content)
    assert fm.id == "run_bare"
    assert fm.discipline is None


def test_no_discipline_still_links_home_only() -> None:
    run = sample_run()
    del run["discipline"]
    _relpath, content = render_run_note(run, [], "ledger/x.jsonl", [])
    fm = parse_frontmatter(content)
    assert fm.discipline is None
    assert "[[Home]]" in content
