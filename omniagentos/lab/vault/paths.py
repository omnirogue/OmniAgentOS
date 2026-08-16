"""Vault relpath builders for H2 lab notes (contracts/lab-interfaces.md
§L08-labvault, contracts/vault-frontmatter.md layout).

Self-contained: does NOT import `omniagentos.vault.paths` (H1/p05-owned; its
`_safe_filename_id` is a private module helper, not part of the frozen public
vault API this package reuses). `safe_slug` below mirrors the same
sanitize-then-hash-on-collision approach so lab note filenames are confined
and collision-resistant the same way H1's run/benchmark notes are.
"""

from __future__ import annotations

import hashlib
import re

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def safe_slug(value: str) -> str:
    """Return a filesystem- and wikilink-safe, collision-resistant slug for a
    note id/subject/discipline. A no-op for already-clean values (the common
    case: ids from `contracts.new_id`, hand-picked slugs like "code-changes"),
    so the returned slug still matches raw `[[wikilink]]` text built from the
    same source string. Falls back to a content hash suffix only when
    sanitization actually changes the value, to stay collision-resistant."""
    original = value if value else "note"
    sanitized = original.replace("..", "-")
    sanitized = re.sub(r"[\\/:\s]+", "-", sanitized)
    sanitized = _UNSAFE_FILENAME_CHARS.sub("-", sanitized)
    sanitized = sanitized.strip(".-") or "note"
    if sanitized == original:
        return sanitized
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    return f"{sanitized}-{digest}"


def experiment_relpath(exp_id: str) -> str:
    """`experiments/<exp-id>.md` (flat; contracts/vault-frontmatter.md already
    reserves the `experiments/` folder)."""
    return f"experiments/{safe_slug(exp_id)}.md"


def tournament_relpath(tournament_id: str) -> str:
    """`tournaments/<tournament-id>.md` (flat)."""
    return f"tournaments/{safe_slug(tournament_id)}.md"


def leaderboard_relpath(subject: str) -> str:
    """`leaderboard/<subject>.md` — one curated log-book note per subject."""
    return f"leaderboard/{safe_slug(subject)}.md"


def playbook_relpath(discipline: str) -> str:
    """`playbook/<discipline>.md` — one validated-traits note per discipline
    (contracts/vault-frontmatter.md already reserves the `playbook/` folder)."""
    return f"playbook/{safe_slug(discipline)}.md"


def prompt_note_relpath(discipline: str, surface_id: str) -> str:
    """`prompts/<discipline>/<surface-id>.md` — a companion NOTE (frontmatter +
    metadata + quoted content) about a PROMPT surface, distinct from the RAW
    prompt content file L03-surfaces writes at `Surface.path` (versioned
    `<role>/vNN.md`, fed verbatim to a model — no frontmatter). Keying this
    note's filename by the surface id (not a version number) guarantees it
    never collides with L03's own `vNN.md` files even when they share a
    subfolder."""
    return f"prompts/{safe_slug(discipline)}/{safe_slug(surface_id)}.md"
