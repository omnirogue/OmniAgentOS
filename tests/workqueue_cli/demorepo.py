"""Throwaway git repos for the workqueue tests — the same shape the worker uses.

A real repo (not a mock) because the parts under test are exactly the parts that
talk to git: the pinned fingerprint, the plain local clone, and the owned_paths
observation. Mocking git here would test the mock.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(*args: str, cwd: str | Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def make_repo(root: Path, name: str = "demo") -> tuple[Path, str]:
    """Create a one-commit repo with a ``demo/`` scratch dir. Returns (path, sha)."""
    repo = root / name
    (repo / "demo").mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(repo))
    _git("config", "user.email", "wq-test@localhost", cwd=repo)
    _git("config", "user.name", "wq test", cwd=repo)
    (repo / "README.md").write_text("wq test repo\n")
    (repo / "demo" / "NOTES.md").write_text("scratch\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    return repo, _git("rev-parse", "HEAD", cwd=repo)


def make_mirror(repo: Path, dest: Path) -> Path:
    _git("clone", "--mirror", "-q", str(repo), str(dest))
    return dest


def make_checkout(mirror: Path, dest: Path, base_sha: str, branch: str = "wq/test") -> Path:
    """Exactly what the worker does: plain local clone, then checkout -b at the pin."""
    _git("clone", "--no-checkout", "--no-tags", "-q", str(mirror), str(dest))
    _git("checkout", "-q", "-b", branch, base_sha, cwd=dest)
    return dest


def unit(
    base_sha: str,
    repo: Path,
    *,
    acceptance_cmd: str = "python3 -c 'print(1)'",
    owned_paths: list[str] | None = None,
    profile: str = "script",
    gate: str | None = None,
    idempotency_key: str = "wq-test-unit",
    branch: str = "wq/test",
) -> dict[str, object]:
    """One ``unit_submit`` in the contract.schema.json shape."""
    return {
        "idempotency_key": idempotency_key,
        "repo_url": str(repo),
        "repo_slug": repo.name,
        "base_sha": base_sha,
        "base_ref": "main",
        "branch": branch,
        "owned_paths": owned_paths if owned_paths is not None else ["demo/**"],
        "brief_inline": None,
        "brief_path": None,
        "agent_profile": profile,
        # Generous work-timeout so a gated unit's accurate-gate subprocess is not
        # spuriously killed under CI contention (2-core runner, 16-way xdist): the
        # gate work is trivial but every syscall crawls when 16 workers share 2
        # cores. Tests that assert the timeout path itself override this to 1.
        "timeout_s": 900,
        "labels": [],
        "risk_class": "mechanical",
        "acceptance_cmd": acceptance_cmd,
        "acceptance_gate": gate,
        "priority": 2,
        "max_attempts": 3,
    }
