"""Flag-gated git auto-commit for vault writes (contracts/vault-frontmatter.md
"Git behavior (p05)"). Default OFF, and OFF in tests except one dedicated
tmp-git-repo test for ON (task brief). Never pushes; never touches non-vault
paths — enforced by a hard guard that raises rather than silently committing
something unexpected.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.vault.errors import VaultGitGuardError

AUTOCOMMIT_ENV = "OMNIAGENTOS_VAULT_AUTOCOMMIT"
BOT_NAME = "omniagentos-bot"
BOT_EMAIL = "4580856+omniagentos-bot[bot]@users.noreply.github.com"


def autocommit_enabled(explicit: bool | None) -> bool:
    """Resolve the effective autocommit flag: `explicit` (True/False) wins when
    given; otherwise falls back to `OMNIAGENTOS_VAULT_AUTOCOMMIT=1` (default,
    and always in tests unless a test opts in explicitly, OFF)."""
    if explicit is not None:
        return explicit
    return os.environ.get(AUTOCOMMIT_ENV) == "1"


def commit_note(vault_dir: str, abs_path: Path, note_type: str, note_id: str) -> None:
    """`git add <abs_path> && git commit`, author/committer `omniagentos-bot
    <4580856+omniagentos-bot[bot]@users.noreply.github.com>`, message `vault: <note_type> <note_id>`. Never
    pushes.

    HARD GUARD: after staging, inspects the FULL staged set (not just the file
    we just added) and raises VaultGitGuardError — refusing to commit anything
    — if any staged path resolves outside `vault_dir`. This catches both a bug
    in our own path handling and a dirty index left over from something else.
    """
    vault_root = Path(vault_dir).resolve()
    cwd = str(abs_path.parent)

    _run_git(["add", "--", str(abs_path)], cwd=cwd)

    repo_root = _repo_root(cwd)
    staged = _staged_paths(cwd)
    offenders = [
        p
        for p in staged
        if inode_relative_parts_anchored((repo_root / p).resolve(), vault_root) is None
    ]
    if offenders:
        raise VaultGitGuardError(
            f"refusing to commit: staged path(s) outside vault dir {vault_root}: {offenders}"
        )

    if not staged:
        return  # nothing changed (e.g. regenerated identical content) — no-op, not an error

    message = f"vault: {note_type} {note_id}"
    _run_git(["commit", "--message", message, "--author", f"{BOT_NAME} <{BOT_EMAIL}>"], cwd=cwd)


def _repo_root(cwd: str) -> Path:
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(result.stdout.strip()).resolve()


def _staged_paths(cwd: str) -> list[str]:
    """The staged set, in a spelling the containment check can actually match.

    `core.quotePath` defaults to TRUE, which C-quotes every path byte >= 0x80 and
    wraps the path in literal double quotes. `commit_note` joins each of these onto
    the repo root, so `vault/café.md` arrived as `"vault/caf\\303\\251.md"` — first
    component `"vault`, not `vault` — and an in-vault note was judged OUTSIDE the
    vault, refusing an unrelated commit and saying so in a message that named an
    in-vault path as outside it.

    Pinning the option to `false` makes the verdict a property of the repository
    rather than of whoever's git config the writer runs under. It does NOT open the
    guard: git still C-quotes a genuine control character (newline, embedded quote)
    with quotePath off, so such a path still fails containment and is still refused.
    Same fix, same reason, as `scripts/gates/forbidden-paths.sh`.
    """
    result = _run_git(["-c", "core.quotePath=false", "diff", "--cached", "--name-only"], cwd=cwd)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _run_git(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = BOT_NAME
    env["GIT_AUTHOR_EMAIL"] = BOT_EMAIL
    env["GIT_COMMITTER_NAME"] = BOT_NAME
    env["GIT_COMMITTER_EMAIL"] = BOT_EMAIL
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result
