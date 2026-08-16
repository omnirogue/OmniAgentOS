"""write_note (contracts/interfaces.md §p05)."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.vault.frontmatter import parse_frontmatter, render_frontmatter
from omniagentos.vault.gitops import autocommit_enabled, commit_note
from omniagentos.vault.paths import discipline_relpath
from omniagentos.vault.sections import merge_human_section
from omniagentos.vault.templating import render_template


def write_note(
    vault_dir: str,
    relpath: str,
    content: str,
    *,
    autocommit: bool | None = None,
) -> str:
    """Write `content` to `<vault_dir>/<relpath>`.

    - Preserves any existing `## Notes (human)` section verbatim (human-edit
      rule, contracts/vault-frontmatter.md).
    - Creates parent directories as needed.
    - When the note is a run note referencing a discipline, ensures
      `disciplines/<slug>.md` exists — auto-stub on first reference (D-011) so
      the note's `[[<discipline-slug>]]` wikilink always resolves.
    - Auto-commits when `autocommit` is True, or (`autocommit is None`) the
      `OMNIAGENTOS_VAULT_AUTOCOMMIT=1` env var is set. Default OFF. See
      omniagentos.vault.gitops for the vault-only hard guard.

    Returns the absolute path written.
    """
    target = _confined_target(vault_dir, relpath)
    old_content = target.read_text(encoding="utf-8") if target.is_file() else None
    final_content = merge_human_section(content, old_content)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(final_content, encoding="utf-8")

    fm = parse_frontmatter(final_content)

    if fm.type == NoteType.RUN and fm.discipline:
        _ensure_discipline_stub(vault_dir, fm.discipline, fm.id, autocommit=autocommit)

    if autocommit_enabled(autocommit):
        commit_note(vault_dir, target.resolve(), fm.type.value, fm.id)

    return str(target.resolve())


def _confined_target(vault_dir: str, relpath: str) -> Path:
    """Return a resolved target only when *relpath* remains inside the vault."""
    candidate = Path(relpath)
    windows_candidate = PureWindowsPath(relpath)
    if (
        candidate.is_absolute()
        or windows_candidate.is_absolute()
        or ".." in candidate.parts
        or ".." in windows_candidate.parts
    ):
        raise ValueError(f"vault note path must be a relative, non-traversing path: {relpath!r}")

    vault_root = Path(vault_dir).resolve()
    target = (vault_root / candidate).resolve()
    if inode_relative_parts_anchored(target, vault_root) is None:
        raise ValueError(f"vault note path escapes vault directory: {relpath!r}")
    return target


def _ensure_discipline_stub(
    vault_dir: str, slug: str, source_run_id: str, *, autocommit: bool | None
) -> None:
    """Create `disciplines/<slug>.md` if it does not already exist. A stub is
    `status: draft` so it visibly invites a human to flesh it out; regenerating
    a run note that references an already-stubbed (or since human-edited)
    discipline note leaves it untouched (write_note is only called here on
    first reference)."""
    stub_path = Path(vault_dir) / discipline_relpath(slug)
    if stub_path.is_file():
        return
    fm = VaultFrontmatter(
        id=slug,
        type=NoteType.DISCIPLINE,
        discipline=None,
        created=utc_now_iso(),
        source_run=source_run_id,
        confidence=None,
        status="draft",
        supersedes=None,
    )
    body = render_template("discipline_stub.md.j2", slug=slug, source_run=source_run_id)
    content = render_frontmatter(fm) + "\n" + body
    write_note(vault_dir, discipline_relpath(slug), content, autocommit=autocommit)
