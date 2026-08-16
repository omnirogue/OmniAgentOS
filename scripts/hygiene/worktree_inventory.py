"""WORKTREE/BRANCH REAPER — Phase A, REPORT-ONLY inventory.

Enumerates every ``git worktree`` registered against the serving repo (any
root — ``.claude/worktrees``, ``/tmp``, ``OmniAgentOS-builders``,
``OmniAgentOS-wt*``, ``Work/worktrees``, home, ``var/swarm/worktrees``,
scratch dirs, ...) plus a global local-branch merged/unmerged count, and
emits a machine-readable JSON report + a human summary. This is Phase A of
the worktree/branch reaper: STRICTLY READ-ONLY.

Phase B (salvage-commit dirty trees, ``git push origin --all``, prune with
``branch -d`` only, ARMED=0 dry-run first, moving basetemps out of ``var/``)
is explicitly OUT OF SCOPE here and requires a separate operator arming
decision (Git-Isolation-Doctrine) once this report makes that decision
possible. Nothing in this module may invoke a mutating git subcommand.

MECHANICAL enforcement, not convention: every git call goes through
``omniagentos.worktrees.git.run_readonly_git``, which raises ``ValueError``
before running anything not on ``READONLY_GIT_SUBCOMMANDS`` (plus the single
``worktree list`` exception) — see that module's docstring. This module
never imports or calls ``SubprocessWorktrees`` (the mutating machinery) at
all.

PERFORMANCE: the real estate has 300+ worktrees and 2000+ branches; a naive
recursive scan over that has previously spiked host load and stalled the
merge-gate daemon. This module is deliberately bounded:

* worktree discovery is ONE ``git worktree list --porcelain`` call, never a
  filesystem walk of candidate root directories;
* branch merged/unmerged counts are TWO ``git for-each-ref`` calls total
  (``--merged``/``--no-merged``), never one ``merge-base`` subprocess per
  branch;
* per-worktree probes (dirty count, ahead/behind, ``.venv`` size) are one
  bounded call each, run only for worktrees whose path still exists on disk,
  each under a short subprocess timeout so one wedged filesystem can't hang
  the whole run;
* ``.venv`` sizing uses ``du -sk <path>`` (a single bounded call per
  worktree, not a Python ``os.walk``) and is best-effort — a timeout or
  failure reports ``venv_bytes: null``, never blocks the rest of the report;
* nothing here recursively globs or walks a whole worktree tree.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.worktrees.git import parse_worktree_porcelain, run_readonly_git
from scripts.hygiene.hygiene import DEFAULT_REPO_ROOT, KNOWN_WORKTREE_ROOTS

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
_SUBPROCESS_TIMEOUT = 10.0
_DU_TIMEOUT = 5.0

VENV_DIRNAME = ".venv"
PYTEST_TMP_HINTS = ("pytest", "basetemp", "tmp")


def utc_now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime(_TS_FMT)


# --------------------------------------------------------------------------
# Discovery — one `worktree list --porcelain` call
# --------------------------------------------------------------------------


def list_worktrees(repo_root: Path) -> list[dict[str, str]]:
    """Every worktree git has registered against ``repo_root`` — one call,
    grouped/parsed with the shared parser. Empty list (never raises) if the
    command fails for any reason."""
    try:
        proc = run_readonly_git(
            ["worktree", "list", "--porcelain"], str(repo_root), timeout=30.0
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return parse_worktree_porcelain(proc.stdout)


def branch_short_name(entry: dict[str, str]) -> str | None:
    """``branch`` field of a porcelain entry is ``refs/heads/<name>`` (or
    absent for a detached/bare worktree)."""
    ref = entry.get("branch")
    if not ref:
        return None
    prefix = "refs/heads/"
    return ref[len(prefix) :] if ref.startswith(prefix) else ref


# --------------------------------------------------------------------------
# Root classification — labeling only, never used to decide discovery
# --------------------------------------------------------------------------


def classify_root(path: Path, repo_root: Path, known_roots: tuple[tuple[str, Path], ...]) -> str:
    """Label a worktree path with the naming-convention root it falls under.

    Purely cosmetic grouping for the report; ``list_worktrees`` above is the
    authoritative discovery mechanism regardless of what this returns."""
    resolved = _safe_resolve(path)
    home = _safe_resolve(Path.home())
    repo = _safe_resolve(repo_root)

    candidates: list[tuple[str, Path]] = [
        (".claude/worktrees", repo / ".claude" / "worktrees"),
        ("var/swarm/worktrees", repo / "var" / "swarm" / "worktrees"),
        ("var/gate", repo / "var" / "gate"),
        *known_roots,
    ]
    for label, base in candidates:
        base_resolved = _safe_resolve(base)
        if _is_under(resolved, base_resolved):
            return label

    name_lower = str(resolved).lower()
    if "scratchpad" in name_lower:
        return "scratchpads"
    parent_name = resolved.parent.name
    if parent_name.startswith("OmniAgentOS-wt"):
        return "OmniAgentOS-wt*"
    if _is_under(resolved, home) and resolved.parent == home:
        return "home"
    return "other"


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Per-worktree probes — bounded, one call each
# --------------------------------------------------------------------------


def worktree_git_file_age_hours(wt_path: Path, *, now: float | None = None) -> float | None:
    """Hours since the worktree's own ``.git`` file (a pointer file, not a
    directory, for a linked worktree) was last written — cheap ``stat``, no
    directory walk."""
    try:
        mtime = (wt_path / ".git").stat().st_mtime
    except OSError:
        return None
    return max(0.0, ((now if now is not None else time.time()) - mtime) / 3600.0)


def worktree_dirty_count(wt_path: Path) -> int | None:
    try:
        proc = run_readonly_git(
            ["status", "--porcelain"], str(wt_path), timeout=_SUBPROCESS_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return len([line for line in proc.stdout.splitlines() if line.strip()])


def worktree_ahead_behind(wt_path: Path, branch: str | None) -> tuple[int | None, int | None]:
    """``(ahead, behind)`` of ``branch`` vs its ``origin/<branch>`` — ``None``
    when there is no branch (detached HEAD), no matching remote ref, or the
    probe fails for any reason (never raises)."""
    if not branch:
        return None, None
    try:
        proc = run_readonly_git(
            ["rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"],
            str(wt_path),
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None
    if proc.returncode != 0:
        return None, None
    parts = proc.stdout.split()
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


def worktree_venv_info(wt_path: Path) -> tuple[bool, int | None]:
    """``(has_venv, size_bytes)`` — size is best-effort via a single bounded
    ``du -sk`` call; ``None`` when ``.venv`` is absent, ``du`` is unavailable,
    times out, or its output doesn't parse. Never a Python-side directory
    walk (see module docstring)."""
    venv = wt_path / VENV_DIRNAME
    try:
        is_dir = venv.is_dir()
    except OSError:
        return False, None
    if not is_dir:
        return False, None
    try:
        proc = subprocess.run(  # noqa: S603,S607 -- fixed argv, never shell
            ["du", "-sk", str(venv)],
            capture_output=True,
            text=True,
            timeout=_DU_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True, None
    if proc.returncode != 0:
        return True, None
    try:
        kb = int(proc.stdout.split()[0])
    except (IndexError, ValueError):
        return True, None
    return True, kb * 1024


def worktree_has_var_tmp(wt_path: Path) -> bool:
    """Whether ``<worktree>/var`` holds a directory that looks like a pytest
    basetemp/tmp dropping (non-recursive top-level scan only — one
    ``iterdir()``, never a walk)."""
    var_dir = wt_path / "var"
    try:
        if not var_dir.is_dir():
            return False
        for entry in var_dir.iterdir():
            name_lower = entry.name.lower()
            if entry.is_dir() and any(hint in name_lower for hint in PYTEST_TMP_HINTS):
                return True
    except OSError:
        return False
    return False


@dataclass(frozen=True)
class WorktreeRecord:
    path: str
    root: str
    branch: str | None
    detached: bool
    exists: bool
    age_hours: float | None
    dirty_count: int | None
    ahead: int | None
    behind: int | None
    has_venv: bool
    venv_bytes: int | None
    has_var_tmp: bool


def build_worktree_record(
    entry: dict[str, str], repo_root: Path, known_roots: tuple[tuple[str, Path], ...]
) -> WorktreeRecord:
    path = Path(entry.get("worktree", ""))
    branch = branch_short_name(entry)
    detached = "detached" in entry
    exists = path.is_dir()
    if not exists:
        return WorktreeRecord(
            path=str(path),
            root=classify_root(path, repo_root, known_roots),
            branch=branch,
            detached=detached,
            exists=False,
            age_hours=None,
            dirty_count=None,
            ahead=None,
            behind=None,
            has_venv=False,
            venv_bytes=None,
            has_var_tmp=False,
        )
    has_venv, venv_bytes = worktree_venv_info(path)
    ahead, behind = worktree_ahead_behind(path, branch)
    return WorktreeRecord(
        path=str(path),
        root=classify_root(path, repo_root, known_roots),
        branch=branch,
        detached=detached,
        exists=True,
        age_hours=worktree_git_file_age_hours(path),
        dirty_count=worktree_dirty_count(path),
        ahead=ahead,
        behind=behind,
        has_venv=has_venv,
        venv_bytes=venv_bytes,
        has_var_tmp=worktree_has_var_tmp(path),
    )


# --------------------------------------------------------------------------
# Global branch merged/unmerged counts — two `for-each-ref` calls, never
# one `merge-base` subprocess per branch
# --------------------------------------------------------------------------


def branch_merge_counts(repo_root: Path, *, base: str = "main") -> dict[str, int | None]:
    def _count(flag: str) -> int | None:
        try:
            proc = run_readonly_git(
                ["for-each-ref", "--format=%(refname:short)", flag, "refs/heads"],
                str(repo_root),
                timeout=30.0,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if proc.returncode != 0:
            return None
        return len([line for line in proc.stdout.splitlines() if line.strip()])

    merged = _count(f"--merged={base}")
    unmerged = _count(f"--no-merged={base}")
    total = None if merged is None or unmerged is None else merged + unmerged
    return {"merged": merged, "unmerged": unmerged, "total": total}


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------


def build_report(
    repo_root: Path = DEFAULT_REPO_ROOT,
    *,
    base_branch: str = "main",
    known_roots: tuple[tuple[str, Path], ...] = KNOWN_WORKTREE_ROOTS,
    now: datetime | None = None,
) -> dict[str, Any]:
    entries = list_worktrees(repo_root)
    records = [build_worktree_record(entry, repo_root, known_roots) for entry in entries]
    branches = branch_merge_counts(repo_root, base=base_branch)

    root_counts = Counter(r.root for r in records)
    dirty = [r for r in records if (r.dirty_count or 0) > 0]
    clean_existing = [r for r in records if r.exists and (r.dirty_count or 0) == 0]
    venvs = [r for r in records if r.has_venv]
    venv_bytes_known = [r.venv_bytes for r in venvs if r.venv_bytes is not None]
    stale = [r for r in records if not r.exists]
    var_tmp = [r for r in records if r.has_var_tmp]

    counts = {
        "worktrees_total": len(records),
        "roots": dict(sorted(root_counts.items())),
        "worktrees_stale_registration": len(stale),
        "worktrees_dirty": len(dirty),
        "worktrees_clean": len(clean_existing),
        "worktrees_with_venv": len(venvs),
        "venv_total_bytes_known": sum(venv_bytes_known) if venv_bytes_known else 0,
        "venv_size_unknown_count": len(venvs) - len(venv_bytes_known),
        "worktrees_with_var_tmp_dropping": len(var_tmp),
        "branches_local_total": branches["total"],
        "branches_merged_to_main": branches["merged"],
        "branches_unmerged_to_main": branches["unmerged"],
    }

    return {
        "generated_at": utc_now_iso(now),
        "repo_root": str(repo_root),
        "base_branch": base_branch,
        "counts": counts,
        "worktrees": [asdict(r) for r in records],
    }


def format_summary(report: dict[str, Any]) -> str:
    c = report["counts"]
    lines = [
        f"worktree/branch inventory — {report['generated_at']} (repo_root={report['repo_root']})",
        f"  worktrees: {c['worktrees_total']} total, "
        f"{c['worktrees_dirty']} dirty, {c['worktrees_clean']} clean, "
        f"{c['worktrees_stale_registration']} stale-registration (path missing on disk)",
        f"  roots: {', '.join(f'{label}={n}' for label, n in c['roots'].items()) or '(none)'}",
        f"  .venv: {c['worktrees_with_venv']} worktree(s) carry their own; "
        f"{c['venv_total_bytes_known'] / 1_000_000:.1f} MB measured "
        f"({c['venv_size_unknown_count']} unmeasured)",
        f"  var/ pytest-tmp droppings inside a worktree: {c['worktrees_with_var_tmp_dropping']}",
        f"  local branches vs {report['base_branch']}: {c['branches_local_total']} total, "
        f"{c['branches_merged_to_main']} merged, {c['branches_unmerged_to_main']} unmerged",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/hygiene/worktree_inventory.py",
        description=(
            "Worktree/branch reaper Phase A — REPORT-ONLY inventory. Strictly "
            "read-only: every git call is routed through "
            "omniagentos.worktrees.git.run_readonly_git's subcommand allowlist. "
            "Phase B (salvage/prune/arming) is a separate, unbuilt lane."
        ),
    )
    parser.add_argument(
        "--repo-root", type=Path, default=DEFAULT_REPO_ROOT, help="Repo to enumerate worktrees for."
    )
    parser.add_argument(
        "--base-branch", default="main", help="Branch merged/unmerged counts are computed against."
    )
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable JSON report.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = build_report(args.repo_root, base_branch=args.base_branch)
    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    else:
        print(format_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
