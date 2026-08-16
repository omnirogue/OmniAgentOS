"""Extract a diff from raw model output, and apply it in a disposable checkout.

Every candidate gets its OWN scratch directory so N candidates can be applied and
tested without ever touching the caller's real working tree — verification must
never risk the repo it's trying to fix (:mod:`omniagentos.agentless.pipeline`
relies on this to run candidates concurrently and to guarantee the original tree
comes back untouched no matter what a generated patch does).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

_DIFF_FENCE_RE = re.compile(r"```diff\s*\n(.*?)```", re.DOTALL)
_GENERIC_FENCE_RE = re.compile(r"```(?:patch)?\s*\n(.*?)```", re.DOTALL)

_IGNORE_COPY = shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__")


def extract_diff(raw_output: str) -> str | None:
    """Pull a unified diff out of ``raw_output``.

    Preference order: a ```diff fence; else a generic fence that looks like a
    diff; else the first 'diff --git' or '--- ' block found bare in the text.
    Returns None if nothing diff-shaped is present. Normalizes to end with
    exactly one trailing newline."""
    match = _DIFF_FENCE_RE.search(raw_output)
    if match is None:
        generic = _GENERIC_FENCE_RE.search(raw_output)
        if generic and ("--- " in generic.group(1) or "diff --git" in generic.group(1)):
            match = generic

    body = match.group(1) if match is not None else _extract_bare_diff(raw_output)
    if body is None:
        return None
    body = body.strip("\n")
    if not body:
        return None
    return body + "\n"


_DIFF_LINE_PREFIXES = ("diff --git ", "index ", "--- ", "+++ ", "@@", " ", "+", "-")


def _looks_like_diff_line(line: str) -> bool:
    return line == "" or line.startswith(_DIFF_LINE_PREFIXES)


def _extract_bare_diff(raw_output: str) -> str | None:
    lines = raw_output.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("diff --git ") or line.startswith("--- "):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if not _looks_like_diff_line(lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


def _is_git_repo(repo_dir: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _prepare_checkout(repo_dir: str, workdir: str) -> None:
    """Create a scratch checkout of ``repo_dir`` at ``workdir``: a real (detached)
    git worktree when ``repo_dir`` is a git repo, else a filtered copytree with
    its own throwaway ``git init`` so ``git apply``/``--3way`` still work."""
    if _is_git_repo(repo_dir):
        subprocess.run(
            ["git", "worktree", "add", "--detach", workdir, "HEAD"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        return
    Path(workdir).mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo_dir, workdir, ignore=_IGNORE_COPY, dirs_exist_ok=True)
    subprocess.run(["git", "init"], cwd=workdir, capture_output=True, text=True)
    subprocess.run(["git", "add", "-A"], cwd=workdir, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "--no-gpg-sign", "-m", "agentless scratch baseline", "--allow-empty"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=_scratch_commit_env(),
    )


def _scratch_commit_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "agentless-scratch")
    env.setdefault("GIT_AUTHOR_EMAIL", "agentless-scratch@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "agentless-scratch")
    env.setdefault("GIT_COMMITTER_EMAIL", "agentless-scratch@localhost")
    return env


def _git_apply(workdir: str, diff: str, extra_args: list[str]) -> bool:
    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", *extra_args, "-"],
        cwd=workdir,
        input=diff,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def apply_candidate(repo_dir: str, diff: str, workdir: str) -> bool:
    """Prepare a scratch checkout at ``workdir`` and apply ``diff`` into it.

    Returns True iff the diff applied cleanly (``git apply``, falling back to
    ``git apply --3way``). The scratch dir is left in place for the caller to
    verify against; the caller MUST clean it up via ``cleanup_checkout``."""
    _prepare_checkout(repo_dir, workdir)
    if _git_apply(workdir, diff, []):
        return True
    return _git_apply(workdir, diff, ["--3way"])


def cleanup_checkout(repo_dir: str, workdir: str) -> None:
    """Always call this in a ``finally``.

    Best-effort ``git worktree remove --force`` + ``git worktree prune`` (a
    no-op, harmlessly, if ``workdir`` was a copytree fallback rather than a real
    worktree), followed by an unconditional ``rmtree`` so the scratch dir is gone
    either way."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", workdir],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "worktree", "prune"], cwd=repo_dir, capture_output=True, text=True)
    shutil.rmtree(workdir, ignore_errors=True)
