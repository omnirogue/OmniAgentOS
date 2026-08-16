"""Vault knowledge-graph notes obey the frozen contract: exact frontmatter,
≥1 resolving wikilink per note, escaped pipes inside tables, human sections
preserved across regeneration. Uses the REAL configs/modelintel.yaml and
templates so drift there fails here."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.modelintel import registry as registry_mod
from omniagentos.modelintel import vault_notes
from omniagentos.modelintel.config import load_config
from omniagentos.vault.frontmatter import parse_frontmatter

WIKILINK = re.compile(r"\[\[[^\]]+\]\]")


@pytest.fixture()
def rendered_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry_mod, "FUSION_RANKINGS", tmp_path / "absent.json")
    cfg = load_config()
    registry = registry_mod.build(cfg, {}, None, None)  # priors-only registry
    vault = tmp_path / "vault"
    vault_notes.render_all(cfg, registry, str(vault))
    return vault


def test_every_note_has_valid_frontmatter_and_a_wikilink(rendered_vault: Path) -> None:
    notes = list(rendered_vault.rglob("*.md"))
    assert len(notes) >= 20  # 8 models + 8 domains + 4 leaderboards + MOC
    for note in notes:
        content = note.read_text(encoding="utf-8")
        fm = parse_frontmatter(content)  # raises on any contract violation
        assert fm.status == "active"
        assert WIKILINK.search(content), f"orphan note (no wikilink): {note.name}"


def test_moc_table_escapes_wikilink_pipes(rendered_vault: Path) -> None:
    moc = (rendered_vault / "sources" / "model-intelligence.md").read_text(encoding="utf-8")
    table_rows = [line for line in moc.splitlines() if line.startswith("|") and "[[" in line]
    assert table_rows
    for row in table_rows:
        for link in re.findall(r"\[\[[^\]]+\]\]", row):
            assert "\\|" in link or "|" not in link, f"unescaped pipe in table link: {link}"


def test_human_notes_survive_regeneration(
    rendered_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = rendered_vault / "capabilities" / "speed.md"
    content = target.read_text(encoding="utf-8")
    target.write_text(content + "\nthe operator's hand-written observation.\n", encoding="utf-8")
    cfg = load_config()
    registry = registry_mod.build(cfg, {}, None, None)
    vault_notes.render_all(cfg, registry, str(rendered_vault))
    assert "the operator's hand-written observation." in target.read_text(encoding="utf-8")


# --- MOC benchmark freshness must reflect MEASURED evidence, not a proxy ------


def _moc_status_line(vault: Path, bench_key: str) -> str:
    moc = (vault / "sources" / "model-intelligence.md").read_text(encoding="utf-8")
    for line in moc.splitlines():
        if line.startswith(f"- [[{bench_key}|"):
            return line
    raise AssertionError(f"no MOC line for benchmark {bench_key}")


def _render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, research) -> Path:
    monkeypatch.setattr(registry_mod, "FUSION_RANKINGS", tmp_path / "absent.json")
    cfg = load_config()
    registry = registry_mod.build(cfg, {}, research, None)
    vault = tmp_path / "vault"
    vault_notes.render_all(cfg, registry, str(vault))
    return vault


def test_research_benchmark_with_no_rows_is_not_reported_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Grok sweep is instructed to OMIT any benchmark it could not verify.
    A sweep that returned zero rows for terminal-bench measured nothing about
    terminal-bench, so the MOC must not label that board 'fresh'."""
    from omniagentos.modelintel.research import ResearchResult

    research = ResearchResult(fetched_at=utc_now_iso(), ok=True, rows=[])
    vault = _render(tmp_path, monkeypatch, research)
    line = _moc_status_line(vault, "terminal-bench")
    assert "fresh" not in line, line


def test_research_benchmark_with_rows_is_reported_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omniagentos.modelintel.research import ResearchResult
    from omniagentos.modelintel.sources import BenchmarkRow

    research = ResearchResult(
        fetched_at=utc_now_iso(),
        ok=True,
        rows=[
            BenchmarkRow(
                benchmark="terminal-bench",
                model_name="gpt-5.6-sol",
                score=61.0,
                metric="percent",
                source_url="https://example.invalid",
            )
        ],
    )
    vault = _render(tmp_path, monkeypatch, research)
    line = _moc_status_line(vault, "terminal-bench")
    assert "fresh" in line, line
