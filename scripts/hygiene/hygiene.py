"""ESTATE HYGIENE — nightly cleanup sweep for the OmniAgentOS machine estate.

Run as ``python scripts/hygiene/hygiene.py`` (a launchd template installs it
daily at 04:15 — see ``scripts/hygiene/install-hygiene.sh``); the plain
``python scripts/hygiene/hygiene.py`` form (not ``-m``) is deliberate:
``scripts/*`` are not installed packages (mirrors every sibling
``scripts/<name>/<name>.py`` in this repo), so ``hygiene.sh`` execs this file
by path with the pinned venv interpreter.

ONE PRINCIPLE, STAMPED EVERYWHERE: ARCHIVE, NEVER DELETE. Anything this
sweep removes from its working location lands in an archive first (moved +
gzipped transcripts, tar.gz'd ledger sessions, rotated-not-dropped logs up to
3 generations). The two things that look like "delete" are narrowly scoped
and both are safety-gated:

* merged git branches — only ever a `branch -d` (git's own safe/fast-forward
  guard; a branch git itself refuses to call merged is never touched), and
  the branch's history stays in the reflog + any remote that already has it.
* worktree removal — only when the worktree's branch is fully merged to
  main AND its working tree is completely clean (`git status --porcelain`
  empty); anything dirty or unmerged is left in place and logged as skipped,
  never forced.

Execution order deliberately does NOT match this module's section order
below. A merged ``swarm/<run>/<task>`` branch that is still checked out in
one of ``~/OmniAgentOS-worktrees/*`` cannot be `branch -d`'d until that
worktree is gone (git refuses — "used by worktree at ..."). So
``main()`` runs the worktree sweep BEFORE the merged-branch sweep even
though branches are section 1 and worktrees are section 2 in the spec this
module implements; by the time the branch sweep runs, every worktree that
was eligible for removal is already gone and its branch is free to delete.

Policy (retention days, rotation size, and the two "actually touch git/fs"
kill switches) is read from ``scripts/hygiene/prompt.md``'s fenced ``yaml``
``policy:`` block at the top of every run (see ``load_policy``) — editing
that file changes what the next sweep does. A missing file, missing block,
or malformed value falls back to the code defaults below and logs which
happened; the sweep never crashes on a bad policy file.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

_HERE = Path(__file__).resolve()
SCRIPT_DIR = _HERE.parent
DEFAULT_REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_PROMPT_PATH = SCRIPT_DIR / "prompt.md"

DEFAULT_WORKTREES_ROOT = Path.home() / "OmniAgentOS-worktrees"
FUSION_WT_ROOT = Path.home() / ".fusion-wt"  # different subsystem — never ours to prune

# Known worktree-root naming conventions across the estate (Estate-Bottleneck-
# Audit-2026-08-07 + 2026-08-12 replan probe: 343 worktrees / 2264 branches
# spread across 10+ roots — this sweep's own ``sweep_worktrees`` still only
# ever touches ``DEFAULT_WORKTREES_ROOT`` above; NO behavior change here).
# This is a shared LABELING constant for the read-only Phase-A inventory
# (``scripts/hygiene/worktree_inventory.py``), imported from here so the two
# tools never drift on what "a worktree root" is named. Each worktree's
# actual location is still discovered authoritatively via
# ``git worktree list --porcelain`` — this tuple is only used to classify a
# discovered path into a human-readable root label.
KNOWN_WORKTREE_ROOTS: tuple[tuple[str, Path], ...] = (
    ("OmniAgentOS-worktrees", DEFAULT_WORKTREES_ROOT),
    ("OmniAgentOS-builders", Path.home() / "OmniAgentOS-builders"),
    ("Work/worktrees", Path.home() / "Work" / "worktrees"),
    (".claude/worktrees", DEFAULT_REPO_ROOT / ".claude" / "worktrees"),
    ("var/swarm/worktrees", DEFAULT_REPO_ROOT / "var" / "swarm" / "worktrees"),
    ("var/gate", DEFAULT_REPO_ROOT / "var" / "gate"),
    ("/private/tmp", Path("/private/tmp")),
    ("/tmp", Path("/tmp")),
)

DEFAULT_TRANSCRIPT_SOURCES: tuple[tuple[Path, str], ...] = (
    (Path.home() / ".claude" / "projects", "claude"),
    (Path.home() / ".claude-account-1" / "projects", "claude-account-1"),
)
DEFAULT_TRANSCRIPT_ARCHIVE_ROOT = Path.home() / ".claude" / "archive" / "transcripts"

# Files this run's own log redirect is actively appending to — never a
# rotation candidate for the SAME invocation (rotating the file a running
# shell has open via `exec >>file` would sever the redirect mid-run; the
# next run's fresh `exec` picks up wherever this run left off).
LOG_ROTATE_EXCLUDE = frozenset({"hygiene.log"})

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
_BOT_IDENTITY = ("-c", "user.name=hygiene", "-c", "user.email=4580856+omniagentos-bot[bot]@users.noreply.github.com")


# --------------------------------------------------------------------------
# Policy (scripts/hygiene/prompt.md's fenced `policy:` yaml block)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    transcript_archive_days: int = 14
    ledger_archive_days: int = 30
    log_rotate_mb: int = 50
    worktree_prune_enabled: bool = True
    worktree_min_age_hours: float = 24.0
    branch_prune_enabled: bool = True
    filesearch_reindex: bool = True


_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)


def load_policy(prompt_path: Path, log: Callable[[str], None] | None = None) -> Policy:
    """Parse the fenced ``policy:`` yaml block from ``prompt_path``.

    Falls back to :class:`Policy`'s code defaults — logged, never raised —
    when the file is missing, has no fenced yaml block, the block fails to
    parse, it has no top-level ``policy:`` mapping, or any individual value
    fails to coerce to its expected type.
    """
    emit = log or (lambda _msg: None)
    defaults = Policy()
    if not prompt_path.is_file():
        emit(f"policy: {prompt_path} not found; using code defaults {defaults}")
        return defaults
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        emit(f"policy: could not read {prompt_path} ({exc}); using code defaults {defaults}")
        return defaults
    match = _YAML_BLOCK_RE.search(text)
    if not match:
        emit(f"policy: no fenced yaml block in {prompt_path}; using code defaults {defaults}")
        return defaults
    try:
        import yaml

        raw = yaml.safe_load(match.group(1))
    except Exception as exc:  # noqa: BLE001 -- any parse failure is "malformed"
        emit(
            f"policy: failed to parse yaml block in {prompt_path} ({exc}); using code defaults {defaults}"
        )
        return defaults
    if not isinstance(raw, dict) or not isinstance(raw.get("policy"), dict):
        emit(
            f"policy: yaml block in {prompt_path} has no top-level 'policy:' mapping; "
            f"using code defaults {defaults}"
        )
        return defaults
    block = raw["policy"]
    try:
        merged = Policy(
            transcript_archive_days=int(
                block.get("transcript_archive_days", defaults.transcript_archive_days)
            ),
            ledger_archive_days=int(block.get("ledger_archive_days", defaults.ledger_archive_days)),
            log_rotate_mb=int(block.get("log_rotate_mb", defaults.log_rotate_mb)),
            worktree_min_age_hours=_coerce_float(
                block.get("worktree_min_age_hours"), defaults.worktree_min_age_hours
            ),
            worktree_prune_enabled=bool(
                block.get("worktree_prune_enabled", defaults.worktree_prune_enabled)
            ),
            branch_prune_enabled=bool(
                block.get("branch_prune_enabled", defaults.branch_prune_enabled)
            ),
            filesearch_reindex=bool(block.get("filesearch_reindex", defaults.filesearch_reindex)),
        )
    except (TypeError, ValueError) as exc:
        emit(
            f"policy: invalid value(s) in {prompt_path}'s policy block ({exc}); "
            f"using code defaults {defaults}"
        )
        return defaults
    emit(f"policy: loaded from {prompt_path}: {merged}")
    return merged


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------


def utc_now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime(_TS_FMT)


def cutoff_epoch(days: int, *, now: datetime | None = None) -> float:
    """Epoch seconds `days` in the past — the age-filter math shared by the
    transcript (14d) and ledger (30d) sweeps."""
    moment = (now or datetime.now(UTC)) - timedelta(days=days)
    return moment.timestamp()


def is_older_than(mtime_epoch: float, days: int, *, now: datetime | None = None) -> bool:
    return mtime_epoch < cutoff_epoch(days, now=now)


class Reporter:
    """Collects timestamped log lines and prints each as it is logged (the
    shell wrapper's `exec >>hygiene.log 2>&1` captures stdout to the log
    file; tests pass their own `log` callable instead of using this)."""

    def __init__(self, stream: Any = None) -> None:
        self.lines: list[str] = []
        self._stream = stream if stream is not None else sys.stdout

    def log(self, msg: str) -> None:
        line = f"{utc_now_iso()} hygiene: {msg}"
        self.lines.append(line)
        print(line, file=self._stream, flush=True)


def _run_git(
    args: Sequence[str], cwd: Path, *, identity: bool = False, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    prefix = list(_BOT_IDENTITY) if identity else []
    return subprocess.run(  # noqa: S603 -- fixed argv, never shell
        ["git", "-C", str(cwd), *prefix, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# --------------------------------------------------------------------------
# 1. Merged swarm branches
# --------------------------------------------------------------------------


def merged_swarm_branches(
    repo_root: Path, *, pattern: str = "swarm/*", base: str = "main"
) -> list[str]:
    """``swarm/*`` branch names already merged into ``base`` (safe to delete:
    git itself would refuse a `branch -d` on anything not merged)."""
    proc = _run_git(["branch", "--merged", base, "--format=%(refname:short)"], repo_root)
    if proc.returncode != 0:
        return []
    names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return [name for name in names if name != base and fnmatch.fnmatch(name, pattern)]


def delete_merged_branch(repo_root: Path, branch: str) -> tuple[bool, str]:
    """`branch -d` (fast-forward-safe delete) — NEVER `-D`. Returns
    (deleted, detail) so a "still checked out in a worktree" failure is
    reported, not silently swallowed."""
    proc = _run_git(["branch", "-d", branch], repo_root)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def sweep_merged_branches(
    repo_root: Path, log: Callable[[str], None], *, pattern: str = "swarm/*", base: str = "main"
) -> dict[str, Any]:
    branches = merged_swarm_branches(repo_root, pattern=pattern, base=base)
    deleted: list[str] = []
    skipped: list[tuple[str, str]] = []
    for branch in branches:
        ok, detail = delete_merged_branch(repo_root, branch)
        if ok:
            deleted.append(branch)
            log(f"branch: deleted merged {branch}")
        else:
            skipped.append((branch, detail))
            log(f"branch: SKIPPED {branch} (delete failed, never forced): {detail}")
    if not branches:
        log(f"branch: no merged {pattern} branches found")
    return {
        "checked": len(branches),
        "deleted": deleted,
        "skipped": skipped,
    }


# --------------------------------------------------------------------------
# 2. Worktrees — ~/OmniAgentOS-worktrees/* (dev convention, outside var/)
# --------------------------------------------------------------------------


def worktree_dirs(worktrees_root: Path) -> list[Path]:
    if not worktrees_root.is_dir():
        return []
    return sorted(p for p in worktrees_root.iterdir() if p.is_dir())


def worktree_current_branch(worktree_path: Path) -> str | None:
    proc = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    return branch or None


def worktree_is_clean(worktree_path: Path) -> bool:
    proc = _run_git(["status", "--porcelain"], worktree_path)
    return proc.returncode == 0 and not proc.stdout.strip()


def branch_is_merged(repo_root: Path, branch: str, *, base: str = "main") -> bool:
    proc = _run_git(["merge-base", "--is-ancestor", branch, base], repo_root)
    return proc.returncode == 0


def remove_worktree(repo_root: Path, worktree_path: Path) -> tuple[bool, str]:
    """`git worktree remove` (NEVER --force) + prune. Only ever called after
    the caller has confirmed the worktree is clean+merged."""
    proc = _run_git(["worktree", "remove", str(worktree_path)], repo_root)
    ok = proc.returncode == 0
    detail = (proc.stdout + proc.stderr).strip()
    if ok:
        _run_git(["worktree", "prune"], repo_root)
    return ok, detail


def _coerce_float(value: Any, fallback: float) -> float:
    try:
        v = float(value)
        return v if v >= 0 else fallback
    except (TypeError, ValueError):
        return fallback


def worktree_age_hours(wt: Path) -> float:
    """Hours since the worktree was last touched (newest mtime of the dir
    itself or its top-level entries — cheap, no full walk).

    Guards the zero-commit trap: a freshly-created branch with no commits is
    reported "merged" by `git branch --merged` (it points at HEAD), so a
    just-started agent's worktree looked prunable. First live run pruned an
    in-flight builder's worktree this way — never again."""
    import time

    try:
        newest = wt.stat().st_mtime
        for entry in wt.iterdir():
            try:
                newest = max(newest, entry.stat().st_mtime)
            except OSError:
                continue
        return max(0.0, (time.time() - newest) / 3600.0)
    except OSError:
        return 0.0  # unreadable → treat as brand new → skipped (fail safe)


def sweep_worktrees(
    repo_root: Path,
    worktrees_root: Path,
    log: Callable[[str], None],
    *,
    base: str = "main",
    min_age_hours: float = 24.0,
) -> dict[str, Any]:
    resolved_root = worktrees_root.resolve() if worktrees_root.exists() else worktrees_root
    fusion_wt = FUSION_WT_ROOT.resolve() if FUSION_WT_ROOT.exists() else FUSION_WT_ROOT
    if resolved_root == fusion_wt or fusion_wt in resolved_root.parents:
        log(
            f"worktree: REFUSING to touch {worktrees_root} — resolves under ~/.fusion-wt "
            "(a different subsystem, not ours to prune)"
        )
        return {"checked": 0, "removed": [], "skipped": []}

    dirs = worktree_dirs(worktrees_root)
    removed: list[str] = []
    skipped: list[tuple[str, str]] = []
    for wt in dirs:
        branch = worktree_current_branch(wt)
        if branch is None:
            reason = "could not resolve current branch"
            skipped.append((str(wt), reason))
            log(f"worktree: SKIPPED {wt} ({reason})")
            continue
        clean = worktree_is_clean(wt)
        merged = branch_is_merged(repo_root, branch, base=base)
        age_h = worktree_age_hours(wt)
        if age_h < min_age_hours:
            reason = f"younger than min_age_hours ({age_h:.1f}h < {min_age_hours}h) — possibly an in-flight agent"
            skipped.append((str(wt), reason))
            log(f"worktree: SKIPPED {wt} ({reason})")
            continue
        if merged and clean:
            ok, detail = remove_worktree(repo_root, wt)
            if ok:
                removed.append(str(wt))
                log(f"worktree: removed {wt} (branch={branch}, merged+clean)")
            else:
                skipped.append((str(wt), detail))
                log(f"worktree: SKIPPED {wt} (remove failed, never forced): {detail}")
        else:
            reasons = []
            if not merged:
                reasons.append(f"branch {branch} not merged into {base}")
            if not clean:
                reasons.append("working tree dirty")
            reason = "; ".join(reasons)
            skipped.append((str(wt), reason))
            log(f"worktree: SKIPPED {wt} (branch={branch}): {reason} — never forced")
    if not dirs:
        log(f"worktree: {worktrees_root} has no worktree directories")
    return {"checked": len(dirs), "removed": removed, "skipped": skipped}


# --------------------------------------------------------------------------
# 2b. P2 swarm worktree machinery — var/swarm/worktrees/* (migrations 051/052)
# --------------------------------------------------------------------------


def sweep_swarm_var_worktrees(
    repo_root: Path, db_path: Path, log: Callable[[str], None]
) -> dict[str, Any]:
    """Honor the P2 swarm worktree machinery's own salvage/kept-marker rules
    for ``var/swarm/worktrees/*`` INSTEAD of reimplementing them here.

    Absent entirely on this host today (confirmed at build time) — logged
    and skipped either way. When present: import the real helpers
    (``omniagentos.swarm.worktrees.SubprocessSwarmWorktrees`` +
    ``omniagentos.swarm.dal.SwarmDal`` + ``TERMINAL_RUN_STATUSES`` from
    migrations 051/052's module); if the import fails, skip the whole
    directory and log why rather than hand-rolling a competing GC path.
    Only ever acts on a run whose DB status is confirmed TERMINAL — a run
    still in flight is the live coordinator's worktree to manage, never
    hygiene's; ``prune_orphans`` itself is salvage-first (kept-marker
    semantics: unsalvageable dirty state is left on disk, never rmtree'd).
    """
    path = repo_root / "var" / "swarm" / "worktrees"
    if not path.is_dir():
        log(
            "swarm-var-worktrees: var/swarm/worktrees absent — P2 swarm worktree "
            "machinery not in use on this host, nothing to prune"
        )
        return {"present": False, "runs_checked": 0, "removed": 0, "kept": 0, "skipped_runs": []}

    try:
        from omniagentos.swarm.contracts import TERMINAL_RUN_STATUSES
        from omniagentos.swarm.dal import SwarmDal
        from omniagentos.swarm.worktrees import SubprocessSwarmWorktrees
    except Exception as exc:  # noqa: BLE001 -- an import failure means "skip, don't guess"
        log(
            f"swarm-var-worktrees: present at {path} but swarm worktree helpers not "
            f"importable ({exc}); skipping this directory entirely"
        )
        return {"present": True, "runs_checked": 0, "removed": 0, "kept": 0, "skipped_runs": []}

    terminal = {str(s) for s in TERMINAL_RUN_STATUSES}
    dal = SwarmDal(str(db_path))
    worktrees = SubprocessSwarmWorktrees(var_root=repo_root / "var" / "swarm")
    run_ids = sorted(p.name for p in path.iterdir() if p.is_dir())
    removed_total = 0
    kept_total = 0
    skipped_runs: list[tuple[str, str]] = []
    for run_id in run_ids:
        try:
            run = dal.get_run(run_id)
        except Exception as exc:  # noqa: BLE001 -- DB trouble: never guess, skip
            skipped_runs.append((run_id, f"run status lookup failed: {exc}"))
            log(f"swarm-var-worktrees: SKIPPED run {run_id} (status lookup failed: {exc})")
            continue
        status = str((run or {}).get("status") or "")
        if run is None or status not in terminal:
            reason = "unknown run (not in DB)" if run is None else f"status={status} (not terminal)"
            skipped_runs.append((run_id, reason))
            log(
                f"swarm-var-worktrees: SKIPPED run {run_id} ({reason}) — worktrees left for "
                "its own coordinator"
            )
            continue
        before = worktrees.list_run_worktrees(str(repo_root), run_id)
        removed_paths = worktrees.prune_orphans(str(repo_root), run_id, live_task_keys=())
        removed_total += len(removed_paths)
        kept = len(before) - len(removed_paths)
        kept_total += kept
        log(
            f"swarm-var-worktrees: run {run_id} (status={status}) — removed "
            f"{len(removed_paths)}, kept {kept} (salvage-failed / still dirty)"
        )
    return {
        "present": True,
        "runs_checked": len(run_ids),
        "removed": removed_total,
        "kept": kept_total,
        "skipped_runs": skipped_runs,
    }


# --------------------------------------------------------------------------
# 3a. Transcripts — session JSONLs under ~/.claude*/projects
# --------------------------------------------------------------------------


def iter_old_jsonl(root: Path, cutoff: float) -> Iterator[Path]:
    if not root.is_dir():
        return
    for path in root.rglob("*.jsonl"):
        if not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            yield path


def archive_transcript(path: Path, source_root: Path, archive_root: Path, label: str) -> Path:
    """Move `path` into `archive_root/<YYYY-MM>/<label>/<relative-path>`
    (preserving its position under `source_root`) then gzip it in place.
    Returns the final `.jsonl.gz` path. Move-then-compress means the file
    always exists somewhere on disk end to end — never a window where it is
    neither at the source nor in the archive."""
    rel = path.relative_to(source_root)
    month = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y-%m")
    dest_dir = archive_root / month / label / rel.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / rel.name
    shutil.move(str(path), str(dest))
    gz_dest = dest.parent / (dest.name + ".gz")
    with open(dest, "rb") as f_in, gzip.open(gz_dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    dest.unlink()
    return gz_dest


def sweep_transcripts(
    sources: Sequence[tuple[Path, str]],
    archive_root: Path,
    days: int,
    log: Callable[[str], None],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    cutoff = cutoff_epoch(days, now=now)
    archived = 0
    bytes_reclaimed = 0
    for source_root, label in sources:
        if not source_root.is_dir():
            log(f"transcripts: {source_root} absent, skipping ({label})")
            continue
        count_this_source = 0
        for path in list(iter_old_jsonl(source_root, cutoff)):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            try:
                archive_transcript(path, source_root, archive_root, label)
            except OSError as exc:
                log(f"transcripts: FAILED to archive {path} ({exc}); left in place")
                continue
            archived += 1
            count_this_source += 1
            bytes_reclaimed += size
        log(f"transcripts: {label} ({source_root}) — archived {count_this_source} file(s)")
    if archived:
        log(
            f"transcripts: archived {archived} file(s) total into "
            f"{archive_root} ({bytes_reclaimed / 1_000_000:.2f} MB)"
        )
    else:
        log(f"transcripts: nothing older than {days}d found")
    return {"archived": archived, "bytes_reclaimed": bytes_reclaimed}


# --------------------------------------------------------------------------
# 3b. Ledger — ledger/sessions/*.jsonl -> ledger/archive/<YYYY-MM>.tar.gz
# --------------------------------------------------------------------------


def old_ledger_sessions(sessions_dir: Path, cutoff: float) -> list[Path]:
    if not sessions_dir.is_dir():
        return []
    out = []
    for path in sessions_dir.glob("*.jsonl"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                out.append(path)
        except OSError:
            continue
    return sorted(out)


def git_tracked_files(repo_root: Path, pathspec: str) -> set[Path]:
    proc = _run_git(["ls-files", "--", pathspec], repo_root)
    if proc.returncode != 0:
        return set()
    return {
        (repo_root / line.strip()).resolve() for line in proc.stdout.splitlines() if line.strip()
    }


def _git_commit(repo_root: Path, message: str) -> tuple[bool, str]:
    proc = _run_git(["commit", "-m", message], repo_root, identity=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _append_to_month_archive(archive_path: Path, new_files: Sequence[Path]) -> None:
    """Add `new_files` to `archive_path` (a monthly ledger tar.gz), merging
    with whatever that archive already holds. tarfile has no append mode for
    gzip-compressed archives, so an existing archive is read fully, combined
    with the new members in memory, and rewritten atomically (temp file +
    os.replace) — the archive is never left half-written."""
    existing: list[tuple[tarfile.TarInfo, bytes]] = []
    if archive_path.exists():
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile():
                    extracted = tf.extractfile(member)
                    if extracted is not None:
                        existing.append((member, extracted.read()))
    tmp_path = archive_path.with_name(archive_path.name + ".tmp")
    with tarfile.open(tmp_path, "w:gz") as tf:
        for member, data in existing:
            tf.addfile(member, io.BytesIO(data))
        for file_path in new_files:
            tf.add(file_path, arcname=file_path.name)
    os.replace(tmp_path, archive_path)


def sweep_ledger(
    repo_root: Path,
    sessions_dir: Path,
    archive_dir: Path,
    days: int,
    log: Callable[[str], None],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    cutoff = cutoff_epoch(days, now=now)
    old_files = old_ledger_sessions(sessions_dir, cutoff)
    if not old_files:
        log(f"ledger: nothing in {sessions_dir} older than {days}d")
        return {"archived": 0, "committed_first": 0, "months": []}

    tracked = git_tracked_files(repo_root, "ledger/sessions")
    untracked = [f for f in old_files if f.resolve() not in tracked]
    if untracked:
        rel_paths = [str(f.relative_to(repo_root)) for f in untracked]
        _run_git(["add", *rel_paths], repo_root)
        ok, detail = _git_commit(repo_root, "hygiene: track ledger sessions before archive sweep")
        log(
            f"ledger: pre-committed {len(untracked)} previously-untracked session file(s) "
            f"so the archive sweep only ever removes already-committed copies "
            f"(commit {'ok' if ok else 'FAILED: ' + detail})"
        )

    by_month: dict[str, list[Path]] = defaultdict(list)
    for f in old_files:
        month = datetime.fromtimestamp(f.stat().st_mtime, tz=UTC).strftime("%Y-%m")
        by_month[month].append(f)

    archived = 0
    bytes_reclaimed = 0
    archive_dir.mkdir(parents=True, exist_ok=True)
    for month, files in sorted(by_month.items()):
        archive_path = archive_dir / f"{month}.tar.gz"
        sizes = [f.stat().st_size for f in files]
        _append_to_month_archive(archive_path, files)
        for f in files:
            f.unlink()
        archived += len(files)
        bytes_reclaimed += sum(sizes)
        log(f"ledger: archived {len(files)} session file(s) into {archive_path}")

    return {
        "archived": archived,
        "committed_first": len(untracked),
        "months": sorted(by_month),
        "bytes_reclaimed": bytes_reclaimed,
    }


# --------------------------------------------------------------------------
# 4. DB checkpoint
# --------------------------------------------------------------------------


def checkpoint_db(db_path: Path, log: Callable[[str], None]) -> dict[str, Any] | None:
    """`PRAGMA wal_checkpoint(TRUNCATE)` — nothing else touches the DB."""
    if not db_path.exists():
        log(f"db: {db_path} does not exist, skipping checkpoint")
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log(f"db: wal_checkpoint(TRUNCATE) on {db_path} FAILED: {exc}")
        return None
    busy, log_pages, checkpointed = row or (None, None, None)
    log(
        f"db: wal_checkpoint(TRUNCATE) on {db_path} -> busy={busy} "
        f"log_pages={log_pages} checkpointed={checkpointed}"
    )
    return {"busy": busy, "log_pages": log_pages, "checkpointed": checkpointed}


# --------------------------------------------------------------------------
# 5. Log rotation
# --------------------------------------------------------------------------


def logs_over_limit(
    log_dir: Path, limit_bytes: int, *, exclude: frozenset[str] = LOG_ROTATE_EXCLUDE
) -> list[Path]:
    if not log_dir.is_dir():
        return []
    out = []
    for path in sorted(log_dir.glob("*.log")):
        if path.name in exclude:
            continue
        try:
            if path.is_file() and path.stat().st_size > limit_bytes:
                out.append(path)
        except OSError:
            continue
    return out


def rotated_path(path: Path, generation: int) -> Path:
    """`<name>.log` -> `<name>.log.<generation>.gz` (logrotate's own
    numbered-suffix convention)."""
    return path.parent / f"{path.name}.{generation}.gz"


def rotate_log(path: Path, *, keep: int = 3) -> int:
    """Shift .1.gz..<keep-1>.gz up by one generation (dropping whatever was
    at `keep`), gzip the current log to .1.gz, then truncate the live file
    back to empty so the next append starts fresh. Returns bytes reclaimed
    (the pre-rotation size of `path`)."""
    size = path.stat().st_size
    oldest = rotated_path(path, keep)
    if oldest.exists():
        oldest.unlink()
    for generation in range(keep - 1, 0, -1):
        src = rotated_path(path, generation)
        if src.exists():
            src.rename(rotated_path(path, generation + 1))
    gen1 = rotated_path(path, 1)
    with open(path, "rb") as f_in, gzip.open(gen1, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    path.write_bytes(b"")
    return size


def sweep_logs(
    log_dir: Path, limit_mb: int, log: Callable[[str], None], *, keep: int = 3
) -> dict[str, Any]:
    limit_bytes = limit_mb * 1_000_000
    candidates = logs_over_limit(log_dir, limit_bytes)
    rotated: list[str] = []
    bytes_reclaimed = 0
    for path in candidates:
        size = rotate_log(path, keep=keep)
        bytes_reclaimed += size
        rotated.append(str(path))
        log(f"logs: rotated {path} ({size / 1_000_000:.1f} MB) — kept {keep} generations")
    if not candidates:
        log(f"logs: nothing over {limit_mb}MB in {log_dir}")
    return {"rotated": rotated, "bytes_reclaimed": bytes_reclaimed}


# --------------------------------------------------------------------------
# 6. improvement-log + sweep commit
# --------------------------------------------------------------------------


def append_improvement_log(
    improvement_log_path: Path, notes: str, *, now: datetime | None = None
) -> None:
    improvement_log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": utc_now_iso(now),
        "improver": "hygiene",
        "changes": [],
        "notes": notes,
    }
    with open(improvement_log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def commit_ledger_changes(repo_root: Path, log: Callable[[str], None]) -> bool:
    """Diff-guarded commit of any ledger/ working-tree changes (new monthly
    archives, removed session files) — mirrors archi-morning.sh's own
    diff-guard so a run with nothing to commit never creates an empty
    commit, and a run that finds the index already staged skips rather than
    sweeping unrelated work in."""
    if not (repo_root / "ledger").is_dir():
        return False
    proc = _run_git(["diff", "--cached", "--quiet"], repo_root)
    if proc.returncode != 0:
        log("commit: skip — index already has staged changes (refusing to sweep them in)")
        return False
    _run_git(["add", "--", "ledger"], repo_root)
    proc = _run_git(["diff", "--cached", "--quiet"], repo_root)
    if proc.returncode == 0:
        log("commit: no ledger/archive changes to commit")
        return False
    ok, detail = _git_commit(repo_root, "hygiene: nightly sweep")
    if ok:
        rev = _run_git(["rev-parse", "--short", "HEAD"], repo_root).stdout.strip()
        log(f"commit: hygiene: nightly sweep ({rev})")
    else:
        log(f"commit: FAILED: {detail}")
    return ok


# --------------------------------------------------------------------------
# 7. Filesearch reindex — best-effort, so the index reflects archive moves
# --------------------------------------------------------------------------


def sweep_filesearch_reindex(log: Callable[[str], None]) -> dict[str, Any] | None:
    """Best-effort trigger of ``omniagentos.filesearch.catalog.reindex()`` as
    the sweep's final step, so files this run moved (transcripts/ledger
    archives landing at new paths, worktree dirs disappearing) are reflected
    in the search catalog right away instead of waiting for the next
    ``com.omniagentos.filesearch-index`` tick (every 7200s). Never allowed to
    fail the sweep: any exception (missing embeddings backend, DB lock,
    macOS `mdls`/`textutil` hiccups) is caught and logged, not raised."""
    try:
        from omniagentos.filesearch.catalog import reindex

        result = reindex()
    except Exception as exc:  # noqa: BLE001 -- best-effort, never fails the sweep
        log(f"filesearch: reindex FAILED (best-effort, sweep continues): {exc}")
        return None
    log(
        f"filesearch: reindex ok — scanned={result.get('scanned')} "
        f"indexed={result.get('indexed')} deleted={result.get('deleted')} "
        f"total={result.get('total')} embedded={result.get('embedded')}"
    )
    return result


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_sweep(
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
    worktrees_root: Path = DEFAULT_WORKTREES_ROOT,
    transcript_sources: Sequence[tuple[Path, str]] = DEFAULT_TRANSCRIPT_SOURCES,
    transcript_archive_root: Path = DEFAULT_TRANSCRIPT_ARCHIVE_ROOT,
    prompt_path: Path = DEFAULT_PROMPT_PATH,
    log: Callable[[str], None] | None = None,
    policy: Policy | None = None,
) -> dict[str, Any]:
    reporter = Reporter() if log is None else None
    emit = log or reporter.log  # type: ignore[union-attr]

    active_policy = policy if policy is not None else load_policy(prompt_path, emit)
    emit(f"start (repo_root={repo_root})")

    ledger_sessions_dir = repo_root / "ledger" / "sessions"
    ledger_archive_dir = repo_root / "ledger" / "archive"
    db_path = repo_root / "var" / "omniagentos.db"
    log_dir = repo_root / "var" / "log"
    improvement_log_path = repo_root / "var" / "improvement-log.jsonl"

    # Worktree sweep runs BEFORE branch sweep — see module docstring.
    if active_policy.worktree_prune_enabled:
        # active_policy, NOT the raw ``policy`` param — the launchd path passes
        # policy=None, and the raw read crashed every scheduled sweep (rc=1).
        worktree_report = sweep_worktrees(
            repo_root, worktrees_root, emit, min_age_hours=active_policy.worktree_min_age_hours
        )
        swarm_worktree_report = sweep_swarm_var_worktrees(repo_root, db_path, emit)
    else:
        emit("worktree: prune disabled by policy; skipping worktree + swarm-var-worktree sweeps")
        worktree_report = {"checked": 0, "removed": [], "skipped": []}
        swarm_worktree_report = {
            "present": False,
            "runs_checked": 0,
            "removed": 0,
            "kept": 0,
            "skipped_runs": [],
        }

    if active_policy.branch_prune_enabled:
        branch_report = sweep_merged_branches(repo_root, emit)
    else:
        emit("branch: prune disabled by policy; skipping merged-branch sweep")
        branch_report = {"checked": 0, "deleted": [], "skipped": []}

    transcript_report = sweep_transcripts(
        transcript_sources, transcript_archive_root, active_policy.transcript_archive_days, emit
    )
    ledger_report = sweep_ledger(
        repo_root, ledger_sessions_dir, ledger_archive_dir, active_policy.ledger_archive_days, emit
    )
    db_report = checkpoint_db(db_path, emit)
    logs_report = sweep_logs(log_dir, active_policy.log_rotate_mb, emit)

    mb_reclaimed = (
        transcript_report.get("bytes_reclaimed", 0)
        + ledger_report.get("bytes_reclaimed", 0)
        + logs_report.get("bytes_reclaimed", 0)
    ) / 1_000_000

    notes = (
        f"branches removed={len(branch_report['deleted'])} "
        f"(skipped={len(branch_report['skipped'])}); "
        f"worktrees removed={len(worktree_report['removed'])} "
        f"(skipped={len(worktree_report['skipped'])}); "
        f"swarm-var-worktrees removed={swarm_worktree_report['removed']} "
        f"(runs_checked={swarm_worktree_report['runs_checked']}); "
        f"transcripts archived={transcript_report['archived']}; "
        f"ledger sessions archived={ledger_report['archived']}; "
        f"logs rotated={len(logs_report['rotated'])}; "
        f"MB reclaimed={mb_reclaimed:.2f}"
    )
    append_improvement_log(improvement_log_path, notes)
    emit(f"improvement-log: appended ({notes})")

    committed = commit_ledger_changes(repo_root, emit)

    # Final step: best-effort filesearch reindex so the index reflects this
    # run's archive moves immediately (see sweep_filesearch_reindex).
    if active_policy.filesearch_reindex:
        filesearch_report = sweep_filesearch_reindex(emit)
    else:
        emit("filesearch: reindex disabled by policy; skipping")
        filesearch_report = None

    emit("done")

    return {
        "policy": active_policy,
        "branches": branch_report,
        "worktrees": worktree_report,
        "swarm_var_worktrees": swarm_worktree_report,
        "transcripts": transcript_report,
        "ledger": ledger_report,
        "db": db_report,
        "logs": logs_report,
        "mb_reclaimed": mb_reclaimed,
        "notes": notes,
        "committed": committed,
        "filesearch": filesearch_report,
        "log_lines": reporter.lines if reporter is not None else None,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/hygiene/hygiene.py",
        description="ESTATE HYGIENE nightly sweep — archive, never delete.",
    )
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--worktrees-root", type=Path, default=DEFAULT_WORKTREES_ROOT)
    parser.add_argument("--prompt-path", type=Path, default=DEFAULT_PROMPT_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_sweep(
        repo_root=args.repo_root,
        worktrees_root=args.worktrees_root,
        prompt_path=args.prompt_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
