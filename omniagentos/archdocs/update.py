"""Agent-facing safe write path for the living architecture docs (§8).

TIER S (§6): `omniagentos/archdocs/**`, `ARCHI.md`, and `docs/architecture/**` are
governance-narrative/analyzer-context paths — a docs change is ALWAYS forced >= L3
(always human) and is NEVER self-applied by the reliability pipeline. This module does
not decide whether a docs change should happen; it is the mechanical write step the
pipeline calls ONLY after an `improvements` row of kind='docs' has been through
judging and reached `approved` (human sign-off, per §5/§6). Calling this function is
not itself an approval — callers are responsible for that gate.

Safety properties, independent of the caller:
  - Refuses to touch any path other than `ARCHI.md` or `docs/architecture/*.md`.
  - The on-disk `## Notes (human)` section is ALWAYS preserved verbatim — whatever the
    caller supplies for that region (even if it tries to overwrite it) is discarded in
    favor of what's actually on disk. Human edits can never be clobbered by an
    agent-authored docs improvement.
  - Vault-style autocommit: OFF unless `autocommit=True` or
    `OMNIAGENTOS_ARCHDOCS_AUTOCOMMIT=1` (own flag — deliberately distinct from the
    vault's `OMNIAGENTOS_VAULT_AUTOCOMMIT`, since architecture docs live outside
    `vault/` and an operator may want one enabled without the other). Commits as
    `archdocs-bot <4580856+omniagentos-bot[bot]@users.noreply.github.com>`, never pushes, and — mirroring
    `omniagentos.vault.gitops.commit_note` — refuses to commit (raising, without
    reverting the file write) if the staged set contains anything outside the owned
    doc set (a guard against committing an unrelated dirty index alongside ours).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from omniagentos.path_containment import inode_relative_parts

_HUMAN_HEADING_RE = re.compile(r"^##\s+Notes\s+\(human\)\s*$", re.MULTILINE)
_DEFAULT_HUMAN_SECTION = "## Notes (human)\n"

AUTOCOMMIT_ENV = "OMNIAGENTOS_ARCHDOCS_AUTOCOMMIT"
BOT_NAME = "archdocs-bot"
BOT_EMAIL = "4580856+omniagentos-bot[bot]@users.noreply.github.com"


class ArchdocsGuardError(Exception):
    """Raised when a write/commit would touch something outside the owned doc set."""


def is_owned_doc(repo_root: str | Path, target_path: str | Path) -> bool:
    """True iff `target_path` is `ARCHI.md` (repo root) or `docs/architecture/*.md`.

    Containment is decided by inode ancestry via
    ``omniagentos.path_containment.inode_relative_parts`` — never by
    ``Path.resolve()`` identity or string-prefix checks.  A symlinked
    ``ARCHI.md`` (or architecture doc) that lands outside the repo is
    refused, as is any path whose containment cannot be proved.
    """
    root = Path(repo_root)
    if not root.is_absolute():
        root = root.resolve()
    else:
        root = Path(os.path.abspath(os.fspath(root)))

    target = Path(target_path)
    if not target.is_absolute():
        target = root / target

    parts = inode_relative_parts(os.fspath(target), os.fspath(root))
    if parts is None:
        return False
    if parts == ("ARCHI.md",):
        return True
    if (
        len(parts) >= 3
        and parts[0] == "docs"
        and parts[1] == "architecture"
        and parts[-1].endswith(".md")
    ):
        return True
    return False


def _split_human_section(content: str) -> tuple[str, str]:
    """Split into (before_human, human_section_incl_heading_to_eof)."""
    match = _HUMAN_HEADING_RE.search(content)
    if not match:
        return content, ""
    return content[: match.start()], content[match.start() :]


def _strip_caller_human_section(new_content: str) -> str:
    """Drop anything from `## Notes (human)` onward in caller-supplied content —
    the on-disk human section always wins, never the pipeline's."""
    before, _ = _split_human_section(new_content)
    return before


def autocommit_enabled(explicit: bool | None) -> bool:
    """Resolve the effective autocommit flag: `explicit` wins when given; otherwise
    `OMNIAGENTOS_ARCHDOCS_AUTOCOMMIT=1` (default OFF)."""
    if explicit is not None:
        return explicit
    return os.environ.get(AUTOCOMMIT_ENV) == "1"


def apply_update(
    repo_root: str | Path,
    target_path: str | Path,
    new_content: str,
    actor: str = "pipeline",
    autocommit: bool | None = None,
) -> dict[str, Any]:
    """Write `new_content` to `target_path`, preserving the on-disk `## Notes (human)`
    section verbatim. Returns `{"path", "changed", "human_preserved", "committed"}`.

    Raises `ArchdocsGuardError` if `target_path` is outside the owned doc set, or if
    autocommit is enabled and the staged set includes anything outside it.
    """
    repo_root = Path(repo_root).resolve()
    target = Path(target_path)
    if not target.is_absolute():
        target = repo_root / target

    if not is_owned_doc(repo_root, target):
        raise ArchdocsGuardError(
            f"refusing to write {target}: not ARCHI.md or docs/architecture/*.md"
        )

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    _, human_section = _split_human_section(existing)
    if not human_section:
        human_section = _DEFAULT_HUMAN_SECTION

    caller_narrative = _strip_caller_human_section(new_content).rstrip("\n")
    final_content = f"{caller_narrative}\n\n{human_section}" if caller_narrative else human_section

    changed = final_content != existing
    if changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(final_content, encoding="utf-8")

    committed = False
    if changed and autocommit_enabled(autocommit):
        _commit_doc_change(repo_root, target, actor)
        committed = True

    return {
        "path": str(target),
        "changed": changed,
        "human_preserved": True,
        "committed": committed,
    }


def _commit_doc_change(repo_root: Path, abs_path: Path, actor: str) -> None:
    cwd = str(repo_root)
    _run_git(["add", "--", str(abs_path)], cwd=cwd)

    staged = _staged_paths(cwd)
    offenders = [p for p in staged if not is_owned_doc(repo_root, repo_root / p)]
    if offenders:
        raise ArchdocsGuardError(
            f"refusing to commit: staged path(s) outside owned doc set: {offenders}"
        )
    if not staged:
        return  # regenerated identical content — no-op, not an error

    message = f"archdocs: update {abs_path.name} (actor={actor})"
    _run_git(["commit", "--message", message, "--author", f"{BOT_NAME} <{BOT_EMAIL}>"], cwd=cwd)


def _staged_paths(cwd: str) -> list[str]:
    result = _run_git(["diff", "--cached", "--name-only"], cwd=cwd)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = BOT_NAME
    env["GIT_AUTHOR_EMAIL"] = BOT_EMAIL
    env["GIT_COMMITTER_NAME"] = BOT_NAME
    env["GIT_COMMITTER_EMAIL"] = BOT_EMAIL
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result
