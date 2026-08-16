"""G1 criterion B7: the vault frontmatter field set is EXACT — 8 fields, frozen
order, YAML via pyyaml, null for None. parse_frontmatter must round-trip
render_frontmatter output (contracts/vault-frontmatter.md)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omniagentos.contracts import NoteType, VaultFrontmatter
from omniagentos.vault import VaultError, parse_frontmatter
from omniagentos.vault.frontmatter import FRONTMATTER_FIELDS, render_frontmatter

FROZEN_ORDER = (
    "id",
    "type",
    "discipline",
    "created",
    "source_run",
    "confidence",
    "status",
    "supersedes",
)


def test_frontmatter_fields_are_frozen_order() -> None:
    assert FRONTMATTER_FIELDS == FROZEN_ORDER


def test_render_frontmatter_exact_field_set_and_order() -> None:
    fm = VaultFrontmatter(
        id="run_ab12cd",
        type=NoteType.RUN,
        discipline="code-changes",
        created="2026-07-11T15:30:00Z",
        source_run="run_ab12cd",
        confidence=None,
        status="active",
        supersedes=None,
    )
    rendered = render_frontmatter(fm)

    assert rendered.startswith("---\n")
    assert rendered.rstrip("\n").endswith("---")

    # exactly one frontmatter fence pair, YAML mapping with exactly the 8 keys
    fence = rendered.split("---\n", 2)
    yaml_block = fence[1]
    parsed = yaml.safe_load(yaml_block)
    assert list(parsed.keys()) == list(FROZEN_ORDER)
    assert set(parsed.keys()) == set(FROZEN_ORDER)

    # null for None fields, exact scalar values otherwise
    assert parsed["confidence"] is None
    assert parsed["supersedes"] is None
    assert parsed["type"] == "run"  # enum serialized to plain value, not a YAML tag
    assert parsed["id"] == "run_ab12cd"


def test_render_frontmatter_matches_contract_example() -> None:
    fm = VaultFrontmatter(
        id="run_ab12cd",
        type=NoteType.RUN,
        discipline="code-changes",
        created="2026-07-11T15:30:00Z",
        source_run="run_ab12cd",
        confidence=None,
        status="active",
        supersedes=None,
    )
    rendered = render_frontmatter(fm)
    expected = (
        "---\n"
        "id: run_ab12cd\n"
        "type: run\n"
        "discipline: code-changes\n"
        "created: '2026-07-11T15:30:00Z'\n"
        "source_run: run_ab12cd\n"
        "confidence: null\n"
        "status: active\n"
        "supersedes: null\n"
        "---\n"
    )
    assert rendered == expected


@pytest.mark.parametrize(
    "fm",
    [
        VaultFrontmatter(
            id="run_1",
            type=NoteType.RUN,
            discipline="code-changes",
            created="2026-07-11T15:30:00Z",
            source_run="run_1",
            confidence="medium",
            status="active",
            supersedes=None,
        ),
        VaultFrontmatter(
            id="disc_1",
            type=NoteType.DISCIPLINE,
            discipline=None,
            created="2026-01-01T00:00:00Z",
            source_run=None,
            confidence=None,
            status="draft",
            supersedes=None,
        ),
        VaultFrontmatter(
            id="learning_2",
            type=NoteType.LEARNING,
            discipline="research-briefs",
            created="2026-02-02T02:02:02Z",
            source_run="run_9",
            confidence="high",
            status="superseded",
            supersedes="learning_1",
        ),
    ],
)
def test_parse_frontmatter_round_trips_render(fm: VaultFrontmatter) -> None:
    content = render_frontmatter(fm) + "\n# a note\n\nbody text\n"
    parsed = parse_frontmatter(content)
    assert parsed == fm


def test_parse_frontmatter_rejects_extra_field() -> None:
    content = (
        "---\n"
        "id: x\n"
        "type: run\n"
        "discipline: null\n"
        "created: '2026-01-01T00:00:00Z'\n"
        "source_run: null\n"
        "confidence: null\n"
        "status: active\n"
        "supersedes: null\n"
        "extra_field: surprise\n"
        "---\n"
        "# body\n"
    )
    with pytest.raises(VaultError, match="extra_field"):
        parse_frontmatter(content)


def test_parse_frontmatter_rejects_missing_field() -> None:
    content = (
        "---\n"
        "id: x\n"
        "type: run\n"
        "discipline: null\n"
        "created: '2026-01-01T00:00:00Z'\n"
        "source_run: null\n"
        "confidence: null\n"
        "status: active\n"
        # supersedes omitted
        "---\n"
        "# body\n"
    )
    with pytest.raises(VaultError, match="supersedes"):
        parse_frontmatter(content)


def test_parse_frontmatter_requires_leading_block() -> None:
    with pytest.raises(VaultError):
        parse_frontmatter("# just a heading, no frontmatter\n")


def test_parse_frontmatter_reads_the_real_home_md() -> None:
    home_md = Path(__file__).resolve().parents[2] / "vault" / "Home.md"
    content = home_md.read_text(encoding="utf-8")
    fm = parse_frontmatter(content)
    assert fm.id == "home"
    assert fm.type == NoteType.SOURCE
    assert fm.status == "active"
