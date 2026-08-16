#!/usr/bin/env python3
"""Shared helpers for the LiveSim diagnostic suite.

LiveSim is an OBSERVATIONAL, non-gate live-simulation suite for OmniAgentOS.
It exercises the *running* system — the live API on :8485, the live runtime DB,
the process table, the reaper stack, the filesystem sandbox — plus cheap-LLM
probes, and records full telemetry (model, provider, cost, tokens, latency,
inputs, outputs, environment, commit, config, timestamps) for every test run.

Nothing here gates a merge or a deploy. See docs/testing/LIVESIM.md.

This module is import-safe with or without the omniagentos package on the path;
it deliberately depends only on the stdlib so the runner and the pytest plugin
share one definition of the run context and the ledger record.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "livesim.v1"

# Repo root = two levels up from scripts/livesim/livesim_common.py
REPO = Path(__file__).resolve().parents[2]

DEFAULT_API_BASE = "http://127.0.0.1:8485"


def _resolve_live_db() -> Path:
    """The LIVE runtime DB the API+runner actually use.

    Per var/memories: state.sqlite3 is the live DB (var/omniagentos.db is a stale
    fallback that lies about migration state). It lives in the SERVING checkout,
    not in a feature worktree — so resolve the canonical main-checkout path, not
    this worktree's (which has no runtime state). Overridable via LIVESIM_LIVE_DB.
    LiveSim reads it read-only and only ever writes rows tagged with the run
    namespace, which its own cleanup removes.
    """
    override = os.environ.get("LIVESIM_LIVE_DB")
    if override:
        return Path(override)
    # Prefer this worktree's own runtime DB if present (rare); else the canonical
    # serving checkout.
    local = REPO / "var" / "runtime" / "state.sqlite3"
    if local.exists():
        return local
    return Path("/Users/youruser/OmniAgentOS/var/runtime/state.sqlite3")


LIVE_DB = _resolve_live_db()


def var_dir() -> Path:
    return Path(os.environ.get("LIVESIM_VAR_DIR") or (REPO / "var" / "livesim"))


def ledger_path() -> Path:
    return var_dir() / "ledger.jsonl"


def run_dir(run_id: str) -> Path:
    return var_dir() / "runs" / run_id


def evidence_dir(run_id: str) -> Path:
    return var_dir() / "evidence" / run_id


def _cmd(args: list[str], cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(
            args,
            cwd=str(cwd or REPO),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def git_sha() -> str:
    return _cmd(["git", "rev-parse", "HEAD"]) or "unknown"


def git_dirty() -> bool:
    return bool(_cmd(["git", "status", "--porcelain", "--untracked-files=no"]))


def host_load_1m() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def digest(value: Any) -> str:
    """Stable short sha256 of any JSON-able value (for input/output identity)."""
    try:
        blob = json.dumps(value, sort_keys=True, default=str).encode("utf-8", "replace")
    except (TypeError, ValueError):
        blob = repr(value).encode("utf-8", "replace")
    return hashlib.sha256(blob).hexdigest()[:16]


def preview(value: Any, cap: int = 400) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    text = text.replace("\n", " ")
    return text[:cap]


@dataclass
class RunContext:
    """Everything about the environment a run happened in — captured once."""

    run_id: str
    env_label: str
    git_sha: str
    git_dirty: bool
    python: str
    host: str
    platform: str
    api_base: str
    live_db: str
    started_at: str
    host_load_1m: float | None
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def capture(cls, run_id: str, *, config: dict[str, Any] | None = None) -> RunContext:
        return cls(
            run_id=run_id,
            env_label=os.environ.get("LIVESIM_ENV_LABEL", "live-local"),
            git_sha=git_sha(),
            git_dirty=git_dirty(),
            python=platform.python_version(),
            host=socket.gethostname(),
            platform=platform.platform(),
            api_base=os.environ.get("LIVESIM_API_BASE", DEFAULT_API_BASE),
            live_db=str(LIVE_DB),
            started_at=iso_now(),
            host_load_1m=host_load_1m(),
            config=dict(config or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TestTelemetry:
    """Per-test telemetry a test attaches via the `livesim` fixture.

    All fields are optional: a $0 API/DB/proc test simply leaves model/provider
    None and cost 0.0, which the ledger records faithfully (an absent model is
    recorded as null, never invented).
    """

    model: str | None = None
    provider: str | None = None
    cost_usd: float = 0.0
    cost_quality: str = "exact"  # exact | approximate | unreported | n/a
    tokens_in: int | None = None
    tokens_out: int | None = None
    inputs_digest: str | None = None
    outputs_digest: str | None = None
    inputs_preview: str | None = None
    outputs_preview: str | None = None
    live_target: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cleanup_ok: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON record with flock + fsync (mirrors the fleet ledgers)."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
