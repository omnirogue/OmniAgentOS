"""render_tournament_note (contracts/lab-interfaces.md §L08-labvault).

`tnm: dict[str, Any]` is a Tournament (raw LabStore `tournaments` row, or a
`Tournament.model_dump()`); `config_ids_json`/`config_ids` may be a JSON
string or an already-decoded list. `matches` and `elo` are the matching
`matches` / `elo_ratings` rows for this tournament's subject (the blind
judge-panel notes and the standings). Every field is read defensively so a
partial/hand-built dict renders a readable note instead of crashing.

Wikilinks (contracts/lab-interfaces.md: "Notes wikilink experiments<->
tournaments<->surfaces<->leaderboard<->playbook"): one `[[<config_id>]]` per
config in the tournament (each config_id IS a Surface.id of an
ORCHESTRATION_GENOME surface — HD-004), `[[<subject>]]` (the leaderboard note
for the same subject — `render_leaderboard_note` uses the raw subject string
as its note id, same convention as this note's own `[[<discipline>]]` link),
and `[[Home]]`.
"""

from __future__ import annotations

from typing import Any

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.lab.vault.paths import tournament_relpath
from omniagentos.lab.vault.util import fmt_list
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.templating import render_template
from omniagentos.vault.util import as_optional_str, as_str, pick

NOT_SET = "_not set_"


def render_tournament_note(
    tnm: dict[str, Any],
    matches: list[dict[str, Any]],
    elo: list[dict[str, Any]],
) -> tuple[str, str]:
    """Render a tournament note. Returns (relpath, full content incl.
    frontmatter). Shows every match's blind judge notes verbatim ("the notes
    from the judges") and the elo standings, ranked by rating.
    """
    tnm_id = as_str(pick(tnm, "id"), default="tnm_unknown")
    subject = as_str(pick(tnm, "subject"), default=NOT_SET)
    discipline = as_optional_str(pick(tnm, "discipline"))
    note_date = as_str(pick(tnm, "created_at"), default=utc_now_iso())

    fm = VaultFrontmatter(
        id=tnm_id,
        type=NoteType.TOURNAMENT,
        discipline=discipline,
        created=note_date,
        source_run=None,
        confidence=None,
        status="active",
        supersedes=None,
    )

    config_ids = fmt_list(pick(tnm, "config_ids_json", "config_ids", default=[]))
    winner = as_optional_str(pick(tnm, "winner_config_id"))

    match_rows = [
        {
            "config_a": as_str(pick(m, "config_a"), default="?"),
            "config_b": as_str(pick(m, "config_b"), default="?"),
            "winner": as_optional_str(pick(m, "winner")),
            "score_a": pick(m, "score_a", default=0),
            "score_b": pick(m, "score_b", default=0),
            "blind": bool(pick(m, "blind", default=True)),
            "judge_notes": as_str(pick(m, "judge_notes"), default=""),
        }
        for m in matches
    ]
    elo_rows = sorted(
        (_elo_row(e) for e in elo),
        key=lambda row: float(row["rating"]),
        reverse=True,
    )

    body = render_template(
        "lab/tournament_note.md.j2",
        subject=subject,
        discipline=discipline,
        status=as_str(pick(tnm, "status"), default=NOT_SET),
        arena_task_hash=as_optional_str(pick(tnm, "arena_task_hash")),
        config_ids=config_ids,
        winner=winner,
        created_at=note_date,
        matches=match_rows,
        elo=elo_rows,
    )

    relpath = tournament_relpath(tnm_id)
    return relpath, render_frontmatter(fm) + "\n" + body


def _elo_row(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "config_id": as_str(pick(e, "config_id"), default="?"),
        "rating": pick(e, "rating", default=1000.0),
        "matches": pick(e, "matches", default=0),
        "wins": pick(e, "wins", default=0),
        "losses": pick(e, "losses", default=0),
        "draws": pick(e, "draws", default=0),
        "updated_at": as_str(pick(e, "updated_at"), default="?"),
    }
