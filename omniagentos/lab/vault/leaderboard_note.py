"""render_leaderboard_note (contracts/lab-interfaces.md §L08-labvault) — the
curated, human-readable log-book of top orchestrations per subject.

`rows: list[dict[str, Any]]` are `leaderboard` table rows (or
`LeaderboardEntry.model_dump()`s) for the SAME `subject`. `LabStore.
leaderboard()` already orders them by rank, but this generator re-sorts
defensively rather than assuming that of every caller — contracts/lab-
interfaces.md §L08-labvault only promises "returns (relpath, content)".

Wikilinks (contracts/lab-interfaces.md: "Notes wikilink experiments<->
tournaments<->surfaces<->leaderboard<->playbook"): `[[<config_id>]]` (a
Surface.id — HD-004) and `[[<source_experiment_id>]]` per row, plus
`[[Home]]`.
"""

from __future__ import annotations

from typing import Any

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.lab.vault.paths import leaderboard_relpath
from omniagentos.lab.vault.util import common_value, fmt_list, latest_timestamp
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.templating import render_template
from omniagentos.vault.util import as_optional_str, as_str, pick


def render_leaderboard_note(subject: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Render the leaderboard/log-book note for `subject`. Returns (relpath,
    full content incl. frontmatter). Shows the ranked orchestrations (elo,
    config, summary) AND a distilled judge-notes digest per config — the two
    things contracts/lab-interfaces.md names explicitly for this note.
    """
    subject_id = as_str(subject, default="unknown-subject")
    discipline = common_value(as_optional_str(pick(r, "discipline")) for r in rows)
    note_date = latest_timestamp(pick(r, "updated_at") for r in rows) or utc_now_iso()

    fm = VaultFrontmatter(
        id=subject_id,
        type=NoteType.LEADERBOARD,
        discipline=discipline,
        created=note_date,
        source_run=None,
        confidence=None,
        status="active",
        supersedes=None,
    )

    ranked = sorted(rows, key=lambda r: pick(r, "rank", default=10**9))
    entries = [_entry(r) for r in ranked]

    body = render_template(
        "lab/leaderboard_note.md.j2",
        subject=subject_id,
        entries=entries,
    )

    relpath = leaderboard_relpath(subject_id)
    return relpath, render_frontmatter(fm) + "\n" + body


def _entry(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": pick(r, "rank", default="?"),
        "config_id": as_str(pick(r, "config_id"), default="?"),
        "discipline": as_str(pick(r, "discipline"), default="?"),
        "elo": pick(r, "elo", default=1000.0),
        "summary": as_str(pick(r, "summary"), default="_no summary_"),
        "judge_notes": as_str(pick(r, "judge_notes"), default=""),
        "source_experiments": fmt_list(
            pick(r, "source_experiments_json", "source_experiments", default=[])
        ),
        "updated_by": as_str(pick(r, "updated_by"), default="curator"),
        "updated_at": as_str(pick(r, "updated_at"), default="?"),
    }
