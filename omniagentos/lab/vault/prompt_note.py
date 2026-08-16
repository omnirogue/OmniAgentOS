"""render_prompt_note (contracts/lab-interfaces.md §L08-labvault) — a
browsable, versioned companion note for a PROMPT-kind Surface.

L03-surfaces writes the RAW prompt text itself to `surface["path"]` (e.g.
`prompts/<role>/vNN.md`) with NO frontmatter — it is fed to a model verbatim
via `load_surface_content`. This function renders a DIFFERENT artifact: a
full vault note (frontmatter + surface metadata + the same `content` quoted
for human review/diffing), filed at `prompts/<discipline>/<surface-id>.md`
(`omniagentos.lab.vault.paths.prompt_note_relpath`) so it can never collide
with L03's own `vNN.md` filenames even inside the same folder.

Wikilinks (contracts/lab-interfaces.md: "Notes wikilink experiments<->
tournaments<->surfaces<->leaderboard<->playbook"): `[[<discipline>]]` and
`[[Home]]`.
"""

from __future__ import annotations

from typing import Any

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.lab.vault.paths import prompt_note_relpath
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.templating import render_template
from omniagentos.vault.util import as_optional_str, as_str, pick

NOT_SET = "_not set_"

# SurfaceStatus values (lab.contracts) that map to VaultFrontmatter's
# "superseded"/"draft" note lifecycle; anything else (champion/challenger) is
# live content -> "active" (contracts/vault-frontmatter.md: active|superseded|draft).
_ARCHIVED_SURFACE_STATUS = "archived"
_DRAFT_SURFACE_STATUS = "draft"


def render_prompt_note(surface: dict[str, Any], content: str) -> tuple[str, str]:
    """Render a prompt surface's companion note. Returns (relpath, full
    content incl. frontmatter). Embeds `content` (the actual prompt text)
    verbatim in a fenced block for human review.
    """
    surface_id = as_str(pick(surface, "id"), default="srf_unknown")
    discipline = as_str(pick(surface, "discipline"), default="unknown")
    note_date = as_str(pick(surface, "created_at"), default=utc_now_iso())
    surface_status = as_str(pick(surface, "status"), default=_DRAFT_SURFACE_STATUS)

    fm = VaultFrontmatter(
        id=surface_id,
        type=NoteType.PROMPT,
        discipline=discipline,
        created=note_date,
        source_run=None,
        confidence=None,
        status=_note_status(surface_status),
        supersedes=None,
    )

    body = render_template(
        "lab/prompt_note.md.j2",
        discipline=discipline,
        label=as_optional_str(pick(surface, "label")),
        kind=as_str(pick(surface, "kind"), default=NOT_SET),
        version=pick(surface, "version", default="?"),
        surface_status=surface_status,
        content_hash=as_str(pick(surface, "content_hash"), default=NOT_SET),
        parent_version=pick(surface, "parent_version"),
        safety_relevant=bool(pick(surface, "safety_relevant", default=False)),
        source_path=as_str(pick(surface, "path"), default=NOT_SET),
        content=content if content else "_empty prompt content_",
    )

    relpath = prompt_note_relpath(discipline, surface_id)
    return relpath, render_frontmatter(fm) + "\n" + body


def _note_status(surface_status: str) -> str:
    """Map SurfaceStatus (champion|challenger|archived|draft) to the note
    lifecycle vocabulary (active|superseded|draft) — contracts/vault-frontmatter.md."""
    if surface_status == _ARCHIVED_SURFACE_STATUS:
        return "superseded"
    if surface_status == _DRAFT_SURFACE_STATUS:
        return "draft"
    return "active"  # champion | challenger
