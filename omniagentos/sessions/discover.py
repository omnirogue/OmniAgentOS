"""Best-effort discovery of interactive AI agent CLI processes on this host.

Surfaces terminals that were **not** started by OmniAgentOS (Claude Code,
Codex, Gemini CLI, Grok CLI, Kimi, Qwen Code, Aider, Cursor Agent, …) so the control plane
can mirror them as ``source=external`` sessions and Kanban cards.

This is **observational only**:
- No hooks injected, no stdin attached, no process control.
- Discovery is local process-table + optional cwd via ``lsof``.
- Web/desktop-only apps without a recognizable CLI process are invisible.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

# Basename → provider. First matching token on argv0 wins.
_PROVIDER_BASES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "grok": "grok",
    "kimi": "kimi",
    "kimi-code": "kimi",
    "qwen": "qwen",
    "aider": "aider",
    "cursor-agent": "cursor",
}

# Path/argv markers for node wrappers that invoke a provider binary by path
# (e.g. ``node …/bin/gemini`` or the codex-darwin vendor binary).
_PATH_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("codex", re.compile(r"(?:^|[/\s])codex(?:[\s/]|$)")),
    ("gemini", re.compile(r"(?:^|[/\s])gemini(?:[\s]|$)")),
    ("claude", re.compile(r"(?:^|[/\s])claude(?:[\s]|$)")),
    ("grok", re.compile(r"(?:^|[/\s])grok(?:[\s]|$)")),
    ("kimi", re.compile(r"(?:^|[/\s])kimi(?:-code)?(?:[\s]|$)")),
    ("qwen", re.compile(r"(?:^|[/\s])qwen(?:[\s]|$)")),
    ("aider", re.compile(r"(?:^|[/\s])aider(?:[\s]|$)")),
    ("cursor", re.compile(r"(?:^|[/\s])cursor-agent(?:[\s]|$)")),
]

# Never treat these as interactive agent sessions.
_EXCLUDE_SUBSTRINGS = (
    "uvicorn",
    "pytest",
    "omniagentos",
    "crashpad",
    "google drive",
    "rg -i",
    "rg -n",
    "grep ",
    "tail -f",
    "ssh ",
    "git ",
    "npm ",
    "node_modules/.bin/eslint",
    "node_modules/.bin/next",
    "vitest",
    "typescript",
)

# session_ref prefix for process-discovered external rows (vs hook-ingest).
EXTPROC_REF_PREFIX = "extproc:"


@dataclass(frozen=True)
class DiscoveredProcess:
    """One live interactive agent CLI process."""

    pid: int
    provider: str
    command: str
    tty: str | None
    cwd: str | None

    @property
    def session_ref(self) -> str:
        return f"{EXTPROC_REF_PREFIX}{self.provider}:{self.pid}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "provider": self.provider,
            "command": self.command,
            "tty": self.tty,
            "cwd": self.cwd,
            "session_ref": self.session_ref,
        }


def classify_command(command: str) -> str | None:
    """Return provider slug for a process command line, or None if not an agent CLI."""
    text = (command or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for needle in _EXCLUDE_SUBSTRINGS:
        if needle in lowered:
            return None

    # argv0 basename
    first = text.split(None, 1)[0]
    base = Path(first).name.lower()
    # Strip trailing noise (e.g. "kimi-code" with odd unicode)
    base = base.strip()
    if base in _PROVIDER_BASES:
        return _PROVIDER_BASES[base]

    # node / path wrappers — require a provider marker in the full command
    for provider, pattern in _PATH_MARKERS:
        if pattern.search(lowered):
            # Avoid matching e.g. "agentproacademy.com" ssh hosts
            if provider == "claude" and "agentpro" in lowered and "ssh" in lowered:
                continue
            # Prefer explicit binary path hits over vague "claude" substrings in long cmdlines
            if base in {"node", "nodejs", "python", "python3", "bash", "zsh", "sh"}:
                return provider
            # basename wasn't a provider but path markers matched (rare)
            if f"/{provider}" in lowered or f" {provider} " in f" {lowered} ":
                return provider
    return None


def _parse_ps_line(line: str) -> tuple[int, str | None, str] | None:
    """Parse ``ps -axo pid=,tty=,command=`` output into (pid, tty|None, command)."""
    raw = line.rstrip("\n")
    if not raw.strip():
        return None
    # pid is leading integer; tty is next token; rest is command
    parts = raw.lstrip().split(None, 2)
    if len(parts) < 3:
        return None
    try:
        pid = int(parts[0])
    except ValueError:
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    tty_raw = parts[1]
    tty = None if tty_raw in {"??", "-", "?"} else tty_raw
    command = parts[2]
    return pid, tty, command


def _run_ps(ps_runner: Callable[[], str] | None = None) -> str:
    if ps_runner is not None:
        return ps_runner()
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-axo", "pid=,tty=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.debug("process discovery ps failed: %s", exc)
        return ""
    if completed.returncode not in (0, None) and not completed.stdout:
        return ""
    return completed.stdout or ""


def _cwds_for_pids(
    pids: Iterable[int],
    *,
    lsof_runner: Callable[[list[int]], str] | None = None,
) -> dict[int, str]:
    """Best-effort cwd map via ``lsof -a -d cwd -p …`` (macOS / BSD)."""
    unique = sorted({int(p) for p in pids if int(p) > 0})
    if not unique:
        return {}
    if lsof_runner is not None:
        text = lsof_runner(unique)
    else:
        try:
            # Cap batch size so the argv stays reasonable.
            batch = unique[:80]
            completed = subprocess.run(
                ["lsof", "-a", "-d", "cwd", "-p", ",".join(str(p) for p in batch), "-Fn"],
                check=False,
                capture_output=True,
                text=True,
                timeout=4.0,
            )
            text = completed.stdout or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.debug("process discovery lsof cwd failed: %s", exc)
            return {}

    out: dict[int, str] = {}
    current_pid: int | None = None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current_pid = int(line[1:])
        elif line.startswith("n") and current_pid is not None:
            path = line[1:].strip()
            if path and path not in {"/", "(unknown)"}:
                out[current_pid] = path
    return out


def _score_candidate(command: str, provider: str) -> int:
    """Higher = prefer as the representative process for a (provider, tty) group.

    Prefer real binaries over node shim parents.
    """
    score = 0
    lowered = command.lower()
    base = Path(command.strip().split(None, 1)[0]).name.lower() if command.strip() else ""
    if base in _PROVIDER_BASES:
        score += 50
    if f"/{provider}" in lowered or base == provider or base == f"{provider}-code":
        score += 20
    if base in {"node", "nodejs"}:
        score -= 10
    # Prefer longer paths that look like installed CLIs
    score += min(len(command), 200) // 50
    return score


def list_discovered_processes(
    *,
    ps_runner: Callable[[], str] | None = None,
    lsof_runner: Callable[[list[int]], str] | None = None,
    include_cwd: bool = True,
) -> list[DiscoveredProcess]:
    """Scan the local process table for interactive multi-provider agent CLIs.

    Dedupes parent/child wrappers that share a TTY (e.g. ``node …/bin/codex`` +
    vendor ``codex`` binary) to one representative process per (provider, tty).
    Headless (``??`` tty) processes are still included, keyed by pid alone.
    """
    raw = _run_ps(ps_runner)
    candidates: list[tuple[int, str | None, str, str, int]] = []
    for line in raw.splitlines():
        parsed = _parse_ps_line(line)
        if parsed is None:
            continue
        pid, tty, command = parsed
        provider = classify_command(command)
        if provider is None:
            continue
        candidates.append((pid, tty, command, provider, _score_candidate(command, provider)))

    # Dedupe: for a given (provider, tty) keep the highest-scoring process.
    # For no-tty rows, keep each pid (no grouping).
    best: dict[tuple[str, str], tuple[int, str | None, str, str, int]] = {}
    solo: list[tuple[int, str | None, str, str, int]] = []
    for row in candidates:
        pid, tty, command, provider, score = row
        if tty:
            key = (provider, tty)
            prev = best.get(key)
            if prev is None or score > prev[4]:
                best[key] = row
        else:
            solo.append(row)

    selected = list(best.values()) + solo
    cwds: dict[int, str] = {}
    if include_cwd:
        cwds = _cwds_for_pids((row[0] for row in selected), lsof_runner=lsof_runner)

    results = [
        DiscoveredProcess(
            pid=pid,
            provider=provider,
            command=command,
            tty=tty,
            cwd=cwds.get(pid),
        )
        for pid, tty, command, provider, _score in selected
    ]
    results.sort(key=lambda d: (d.provider, d.pid))
    return results


def discovery_enabled() -> bool:
    """Multi-provider process discovery — ON by default for max production.

    Set ``OMNIAGENTOS_DISCOVER_EXTERNAL=0`` to disable scanning when hermetic
    tests need a quiet board.
    """
    raw = os.environ.get("OMNIAGENTOS_DISCOVER_EXTERNAL", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}
