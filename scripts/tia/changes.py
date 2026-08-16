"""Changed-file detection for the selector.

The one rule worth stating: **untracked files count**. A new module or a new test that
has not been `git add`ed yet is invisible to `git diff --name-only`, and a selector that
cannot see it will happily decide the change affects nothing. That is not a hypothetical:
"add the file, run the fast lane, watch it pass without ever collecting the new test" is
the normal shape of a dev loop.

Every helper here returns repo-relative POSIX paths and raises :class:`GitError` rather
than returning an empty list when git fails — an empty change set means FULL in the
selector, but only if the caller actually gets to see the failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """A git command the change detector depends on failed."""


def _git(repo_root: str | Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        # Missing directory / missing git binary. Surfacing this as an empty result
        # would read as "nothing changed", which is a wrong answer that looks safe.
        raise GitError(f"git {' '.join(args)} could not run in {repo_root}: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _lines(raw: str) -> list[str]:
    return [line.strip().replace("\\", "/") for line in raw.splitlines() if line.strip()]


def merge_base(repo_root: str | Path, ref: str) -> str | None:
    """Merge base of HEAD and ``ref``, or ``None`` when ``ref`` is unreachable."""
    try:
        out = _git(repo_root, "merge-base", "HEAD", ref).strip()
    except GitError:
        return None
    return out or None


def resolve_base(repo_root: str | Path, explicit: str | None = None) -> str:
    """Ref to diff against: the merge base with main, else the previous commit."""
    if explicit:
        return explicit
    for ref in ("main", "origin/main"):
        base = merge_base(repo_root, ref)
        if base:
            return base
    return "HEAD~1"


def changed_files(
    repo_root: str | Path,
    base: str | None = None,
    *,
    include_untracked: bool = True,
) -> tuple[str, ...]:
    """Union of committed-since-base, staged, unstaged, and untracked-but-not-ignored.

    ``include_untracked=False`` exists only so a test can demonstrate what is lost
    without it; production callers must not pass it.
    """
    root = Path(repo_root)
    out: set[str] = set()
    resolved = resolve_base(root, base)
    try:
        out.update(_lines(_git(root, "diff", "--name-only", f"{resolved}...HEAD")))
    except GitError:
        # Unrelated histories / missing base: fall back to the two-dot form rather than
        # silently reporting "nothing changed since a base I could not resolve".
        out.update(_lines(_git(root, "diff", "--name-only", resolved)))
    out.update(_lines(_git(root, "diff", "--name-only", "HEAD")))
    if include_untracked:
        out.update(_lines(_git(root, "ls-files", "--others", "--exclude-standard")))
    return tuple(sorted(out))


def changed_files_for_commit(repo_root: str | Path, sha: str) -> tuple[str, ...]:
    """Paths a historical commit touched, relative to its first parent.

    First-parent is deliberate: for a merge it reports everything the merge brought onto
    this branch, which is what a gate on that commit would have had to cover.
    """
    root = Path(repo_root)
    try:
        _git(root, "rev-parse", "--verify", f"{sha}^")
    except GitError:
        # Root commit: everything in it is new.
        return tuple(sorted(set(_lines(_git(root, "show", "--pretty=", "--name-only", sha)))))
    return tuple(sorted(set(_lines(_git(root, "diff", "--name-only", f"{sha}^", sha)))))


def recent_commits(repo_root: str | Path, count: int, ref: str = "HEAD") -> tuple[str, ...]:
    """Newest-first commit shas, for replay."""
    if count <= 0:
        return ()
    raw = _git(repo_root, "log", f"-{count}", "--format=%H", ref)
    return tuple(_lines(raw))
