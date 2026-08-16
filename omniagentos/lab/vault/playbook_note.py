"""render_playbook_note (contracts/lab-interfaces.md §L08-labvault) — the
validated-traits playbook for one discipline.

`entries: list[dict[str, Any]]` are `playbook` table rows (or
`PlaybookEntry.model_dump()`s) for the SAME `discipline`. Validated traits
sort first, then provisional, then retired; ties broken by confidence (high
first) — a human scanning the note sees the strongest, most-current guidance
up top.

Wikilinks (contracts/lab-interfaces.md: "Notes wikilink experiments<->
tournaments<->surfaces<->leaderboard<->playbook"): `[[<evidence_experiment_id>]]`
and `[[<evidence_tournament_id>]]` per entry, plus `[[Home]]`.
"""

from __future__ import annotations

from typing import Any

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.lab.vault.paths import playbook_relpath
from omniagentos.lab.vault.util import fmt_list, latest_timestamp
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.templating import render_template
from omniagentos.vault.util import as_str, pick

_STATUS_ORDER = {"validated": 0, "provisional": 1, "retired": 2}
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def render_playbook_note(discipline: str, entries: list[dict[str, Any]]) -> tuple[str, str]:
    """Render the playbook note for `discipline`. Returns (relpath, full
    content incl. frontmatter)."""
    discipline_id = as_str(discipline, default="unknown-discipline")
    note_date = latest_timestamp(pick(e, "created_at") for e in entries) or utc_now_iso()

    fm = VaultFrontmatter(
        id=discipline_id,
        type=NoteType.PLAYBOOK,
        discipline=discipline_id,
        created=note_date,
        source_run=None,
        confidence=None,
        status="active",
        supersedes=None,
    )

    ordered = sorted(entries, key=_entry_sort_key)
    rows = [_entry(e) for e in ordered]

    body = render_template(
        "lab/playbook_note.md.j2",
        discipline=discipline_id,
        entries=rows,
    )

    relpath = playbook_relpath(discipline_id)
    return relpath, render_frontmatter(fm) + "\n" + body


def _entry(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "trait": as_str(pick(e, "trait"), default="?"),
        "status": as_str(pick(e, "status"), default="validated"),
        "confidence": as_str(pick(e, "confidence"), default="medium"),
        "elo_support": pick(e, "elo_support"),
        "scope": as_str(pick(e, "scope"), default=""),
        "evidence_experiments": fmt_list(
            pick(e, "evidence_experiments_json", "evidence_experiments", default=[])
        ),
        "evidence_tournaments": fmt_list(
            pick(e, "evidence_tournaments_json", "evidence_tournaments", default=[])
        ),
    }


def _entry_sort_key(e: dict[str, Any]) -> tuple[int, int]:
    status = as_str(pick(e, "status"), default="validated")
    confidence = as_str(pick(e, "confidence"), default="medium")
    return (
        _STATUS_ORDER.get(status, 1),
        -_CONFIDENCE_RANK.get(confidence, 1),
    )
