"""Benchmark provenance collection and verification.

WHY:
A benchmark measurement that cannot be attributed to an exact code state is not evidence.
Therefore, we refuse to capture from a dirty or moving source tree, and record full provenance
details (git state, configurations, lockfiles, CLI versions, and environment).

UNTRACKED-FILE POLICY:
- Tracked modifications, staged changes, deletions, renames, and merge conflicts ALWAYS abort.
- Untracked files: recorded, not fatal by default. A benchmark tree always carries untracked
  scratch (notes, exports); refusing on all of them would make the gate unusable and it would
  be turned off, which is worse than the risk. Gitignored paths (e.g., var/, caches) never appear
  in 'git status --porcelain' at all, so they are already out of scope.
- EXCEPTION: any untracked '*.py' file under CODE_ROOTS aborts. This is because Python can import
  them and silently change the behavior being measured. That is not scratch, it is uncommitted code.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Exceptions
class ProvenanceError(RuntimeError):
    """Base: the capture cannot be attributed to an exact code state."""


class DirtyTreeError(ProvenanceError):
    """Uncommitted changes make the measurement unattributable."""


class MovedTreeError(ProvenanceError):
    """HEAD (or a tracked file) changed mid-capture: the result spans two code states."""


# Constants
REPO_ROOT = Path(__file__).resolve().parents[2]

# Configs whose contents can change what a capture measures. Relative to repo root.
DEFAULT_CONFIG_PATHS: tuple[str, ...] = (
    "configs/modelintel.yaml",
    "configs/llm.yaml",
    "configs/routing.yaml",
    "configs/policy.yaml",
    "tests/benchmarks/frozen_digests.json",
)

DEFAULT_LOCK_PATHS: tuple[str, ...] = ("uv.lock", "poetry.lock")
DEFAULT_PROVIDER_CLIS: tuple[str, ...] = ("claude", "grok", "codex", "gemini")

# Untracked files under these roots that end in .py abort the capture: Python can
# import them, so they can change what is measured.
CODE_ROOTS: tuple[str, ...] = ("omniagentos", "scripts", "tests")
ABSENT = "absent"


@dataclass(frozen=True)
class GitState:
    commit_sha: str
    dirty_paths: tuple[str, ...]  # tracked modifications / staged / deleted / renamed
    untracked_paths: tuple[str, ...]  # every untracked path git reported
    blocking_untracked: tuple[str, ...]  # subset of untracked that abort (see POLICY)

    @property
    def clean(self) -> bool:
        return not self.dirty_paths and not self.blocking_untracked


def _git(repo_root: Path, *args: str) -> str:
    """Run git in repo_root and return stdout stripped. Raise ProvenanceError on failure."""
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except Exception as exc:
        raise ProvenanceError(f"Git command failed to execute: {exc}") from exc

    if proc.returncode != 0:
        raise ProvenanceError(f"Git command failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.rstrip()


def git_state(repo_root: str | Path = REPO_ROOT) -> GitState:
    root = Path(repo_root)
    commit_sha = _git(root, "rev-parse", "HEAD")
    # Use --untracked-files=all because --untracked-files=normal collapses a new
    # untracked directory into a single entry ending in '/', which defeats the blocking rule
    # for uncommitted code.
    status_out = _git(root, "status", "--porcelain=v1", "--untracked-files=all")

    dirty_paths_list: list[str] = []
    untracked_paths_list: list[str] = []
    blocking_untracked_list: list[str] = []

    for line in status_out.splitlines():
        if not line or len(line) < 4:
            continue
        xy = line[:2]
        rest = line[3:]

        # Determine path
        if "R" in xy:
            parts = rest.split(" -> ")
            if len(parts) >= 2:
                path = parts[-1].strip()
            else:
                path = rest.strip()
        else:
            path = rest.strip()

        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]

        if xy == "??":
            untracked_paths_list.append(path)
            p_parts = Path(path).parts
            if p_parts and p_parts[0] in CODE_ROOTS and path.endswith(".py"):
                blocking_untracked_list.append(path)
        else:
            dirty_paths_list.append(path)

    return GitState(
        commit_sha=commit_sha,
        dirty_paths=tuple(sorted(dirty_paths_list)),
        untracked_paths=tuple(sorted(untracked_paths_list)),
        blocking_untracked=tuple(sorted(blocking_untracked_list)),
    )


def assert_clean_tree(repo_root: str | Path = REPO_ROOT) -> GitState:
    """Return the GitState, or raise DirtyTreeError listing the offending paths."""
    state = git_state(repo_root)
    if state.clean:
        return state

    lines = ["refusing to capture from a dirty tree"]

    all_offenders: list[tuple[str, str]] = []
    for p in state.dirty_paths:
        all_offenders.append(("modified", p))
    for p in state.blocking_untracked:
        all_offenders.append(("untracked code", p))

    total_offenders = len(all_offenders)
    limit = 40
    for prefix, path in all_offenders[:limit]:
        lines.append(f"  {prefix}: {path}")

    if total_offenders > limit:
        lines.append(f"  ... {total_offenders - limit} more")

    lines.append("Please commit or stash your changes.")
    raise DirtyTreeError("\n".join(lines))


def verify_unmoved(before: GitState, repo_root: str | Path = REPO_ROOT) -> None:
    """Re-check after the run. Raise MovedTreeError if HEAD moved or a tracked file changed."""
    after = git_state(repo_root)
    if before.commit_sha != after.commit_sha:
        raise MovedTreeError(
            f"refusing the capture: HEAD moved mid-capture ({before.commit_sha[:12]} -> "
            f"{after.commit_sha[:12]}); the result spans two code states"
        )
    if after.dirty_paths:
        paths_str = ", ".join(after.dirty_paths)
        raise MovedTreeError(
            f"refusing the capture: working tree changed mid-capture. Dirty paths: {paths_str}"
        )
    if after.blocking_untracked:
        paths_str = ", ".join(after.blocking_untracked)
        raise MovedTreeError(
            "refusing the capture: importable untracked code appeared mid-capture: "
            f"{paths_str}"
        )


def file_digest(path: str | Path) -> str:
    """sha256 hex of a file's bytes, or ABSENT if it does not exist."""
    target = Path(path)
    if not target.is_file():
        return ABSENT
    try:
        data = target.read_bytes()
        return hashlib.sha256(data).hexdigest()
    except Exception:
        return ABSENT


def config_digests(
    repo_root: str | Path,
    paths: Sequence[str] = DEFAULT_CONFIG_PATHS,
) -> dict[str, str]:
    root = Path(repo_root)
    digests: dict[str, str] = {}
    for p in paths:
        digests[p] = file_digest(root / p)
    return digests


def lock_digest(
    repo_root: str | Path,
    paths: Sequence[str] = DEFAULT_LOCK_PATHS,
) -> tuple[str, str]:
    """Return (relative_path_used, digest). ('absent','absent') when no lock file exists."""
    root = Path(repo_root)
    for p in paths:
        target = root / p
        if target.is_file():
            return p, file_digest(target)
    return ABSENT, ABSENT


def cli_version(name: str) -> str:
    if shutil.which(name) is None:
        return ABSENT
    try:
        proc = subprocess.run(
            [name, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            return ABSENT
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:120]
        return ABSENT
    except (OSError, subprocess.SubprocessError):
        return ABSENT


def provider_cli_versions(names: Sequence[str] = DEFAULT_PROVIDER_CLIS) -> dict[str, str]:
    return {name: cli_version(name) for name in names}


def detect_sandbox_mode() -> str:
    """'seatbelt' when omniagentos.runner.sandbox.sandbox_available() is True, else

    'unavailable'. Import lazily inside the function; on any Exception return 'unknown'.
    """
    try:
        from omniagentos.runner.sandbox import sandbox_available

        if sandbox_available():
            return "seatbelt"
        return "unavailable"
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class CaptureProvenance:
    capture_id: str
    commit_sha: str
    dirty: bool
    dirty_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    config_digests: dict[str, str]
    lock_path: str
    lock_digest: str
    model_id: str | None
    cli_versions: dict[str, str]
    sandbox_mode: str
    python_version: str
    platform: str
    env_hash: str
    captured_at: str
    head_verified_at: str | None = None
    valid: bool = True
    abort_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Stable, JSON-serializable. Tuples -> sorted lists, dicts sorted by key."""
        return {
            "capture_id": self.capture_id,
            "commit_sha": self.commit_sha,
            "dirty": self.dirty,
            "dirty_paths": sorted(list(self.dirty_paths)),
            "untracked_paths": sorted(list(self.untracked_paths)),
            "config_digests": dict(sorted(self.config_digests.items())),
            "lock_path": self.lock_path,
            "lock_digest": self.lock_digest,
            "model_id": self.model_id,
            "cli_versions": dict(sorted(self.cli_versions.items())),
            "sandbox_mode": self.sandbox_mode,
            "python_version": self.python_version,
            "platform": self.platform,
            "env_hash": self.env_hash,
            "captured_at": self.captured_at,
            "head_verified_at": self.head_verified_at,
            "valid": self.valid,
            "abort_reason": self.abort_reason,
        }

    def environment_hash(self) -> str:
        """sha256 (first 16 hex chars) over ONLY the fields that define environment

        equality: commit_sha, config_digests, lock_path, lock_digest, model_id,
        cli_versions, sandbox_mode, python_version, platform, env_hash.
        Deliberately EXCLUDES capture_id, captured_at, head_verified_at, valid,
        abort_reason, and the path lists -- two captures of the same code in the same
        environment must hash equal so lane 5 can compare them, and must hash different
        if any of those environment fields differ.
        Implementation: json.dumps(payload, sort_keys=True, separators=(',',':')) then
        omniagentos.contracts.digest(...)[:16].
        """
        payload = {
            "commit_sha": self.commit_sha,
            "config_digests": self.config_digests,
            "lock_path": self.lock_path,
            "lock_digest": self.lock_digest,
            "model_id": self.model_id,
            "cli_versions": self.cli_versions,
            "sandbox_mode": self.sandbox_mode,
            "python_version": self.python_version,
            "platform": self.platform,
            "env_hash": self.env_hash,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        from omniagentos.contracts import digest

        return digest(serialized)[:16]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaptureProvenance:
        """Round-trip from to_dict output (lists -> tuples)."""
        return cls(
            capture_id=data["capture_id"],
            commit_sha=data["commit_sha"],
            dirty=data["dirty"],
            dirty_paths=tuple(data["dirty_paths"]),
            untracked_paths=tuple(data["untracked_paths"]),
            config_digests=dict(data["config_digests"]),
            lock_path=data["lock_path"],
            lock_digest=data["lock_digest"],
            model_id=data.get("model_id"),
            cli_versions=dict(data["cli_versions"]),
            sandbox_mode=data["sandbox_mode"],
            python_version=data["python_version"],
            platform=data["platform"],
            env_hash=data["env_hash"],
            captured_at=data["captured_at"],
            head_verified_at=data.get("head_verified_at"),
            valid=data.get("valid", True),
            abort_reason=data.get("abort_reason"),
        )


def collect(
    *,
    capture_id: str,
    repo_root: str | Path = REPO_ROOT,
    model_id: str | None = None,
    config_paths: Sequence[str] = DEFAULT_CONFIG_PATHS,
    cli_names: Sequence[str] = DEFAULT_PROVIDER_CLIS,
    sandbox_mode: str | None = None,
) -> tuple[CaptureProvenance, GitState]:
    """Gate + record. Calls assert_clean_tree FIRST (so a dirty tree costs nothing else),

    then gathers digests. sandbox_mode=None means call detect_sandbox_mode().
    """
    git_st = assert_clean_tree(repo_root)

    cf_digests = config_digests(repo_root, config_paths)
    l_path, l_digest = lock_digest(repo_root)
    cli_vers = provider_cli_versions(cli_names)

    sb_mode = sandbox_mode if sandbox_mode is not None else detect_sandbox_mode()
    py_ver = sys.version.split()[0]
    plat_str = f"{platform.system()}-{platform.machine()}"

    from omniagentos.harnesses.envhash import env_hash as get_env_hash

    e_hash = get_env_hash()

    from omniagentos.contracts import utc_now_iso

    captured_at = utc_now_iso()

    provenance = CaptureProvenance(
        capture_id=capture_id,
        commit_sha=git_st.commit_sha,
        dirty=not git_st.clean,
        dirty_paths=git_st.dirty_paths,
        untracked_paths=git_st.untracked_paths,
        config_digests=cf_digests,
        lock_path=l_path,
        lock_digest=l_digest,
        model_id=model_id,
        cli_versions=cli_vers,
        sandbox_mode=sb_mode,
        python_version=py_ver,
        platform=plat_str,
        env_hash=e_hash,
        captured_at=captured_at,
    )
    return provenance, git_st


def write_sidecar(record: CaptureProvenance, out_dir: str | Path) -> Path:
    """Write <out_dir>/provenance/<capture_id>.json (mkdir parents), sorted keys, indent=2,

    with an extra top-level "environment_hash" key. Return the path."""
    out_path = Path(out_dir) / "provenance" / f"{record.capture_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    data = record.to_dict()
    data["environment_hash"] = record.environment_hash()

    serialized = json.dumps(data, sort_keys=True, indent=2)
    out_path.write_text(serialized, encoding="utf-8")
    return out_path


__all__ = [
    "ProvenanceError",
    "DirtyTreeError",
    "MovedTreeError",
    "REPO_ROOT",
    "DEFAULT_CONFIG_PATHS",
    "DEFAULT_LOCK_PATHS",
    "DEFAULT_PROVIDER_CLIS",
    "CODE_ROOTS",
    "ABSENT",
    "GitState",
    "git_state",
    "assert_clean_tree",
    "verify_unmoved",
    "file_digest",
    "config_digests",
    "lock_digest",
    "cli_version",
    "provider_cli_versions",
    "detect_sandbox_mode",
    "CaptureProvenance",
    "collect",
    "write_sidecar",
]
