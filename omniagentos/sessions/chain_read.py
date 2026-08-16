"""T6.4 chain-read protocol — accumulate relevant file reads for a session.

When Read/Glob/Grep/LS complete, paths are recorded as relevant (or blacklisted
if the server/heuristic marks them noise). Later steps inherit the relevant set
via PostToolUse additionalContext.

State is session-local JSON under ``var/sessions/chain-read/<session_id>.json``
so it survives process restarts without a migration.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.runtime_paths import CANONICAL_VAR_ENV_KEYS, resolve_var_root

_READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "read_file", "search_files", "list_files"})
_NOISE_PATH_RE = re.compile(
    r"("
    r"node_modules/|\.git/|__pycache__/|\.venv/|dist/|build/|\.next/|package-lock\.json$"
    r"|\.jambot-chrome/|[\w.-]*-chrome/|Default/IndexedDB/|Service Worker/CacheStorage/"
    r"|indexeddb\.blob/"
    r")",
    re.I,
)


def _state_path(session_id: str) -> Path:
    # OMNIAGENTOS_VAR stays FIRST: this call site honoured it alone, and the
    # launchers export both variables, so putting VAR_DIR in front would move
    # live production state. VAR_DIR joins only as the second choice — and under
    # a campaign the package-anchored fallback is refused outright, because this
    # function MKDIRS its root (a simulation was creating
    # ``<operator checkout>/var/sessions/chain-read``).
    root = resolve_var_root(
        env_keys=CANONICAL_VAR_ENV_KEYS,
        leaf=("sessions", "chain-read"),
    )
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)[:120]
    return root / f"{safe}.json"


@contextlib.contextmanager
def _state_lock(session_id: str) -> Iterator[None]:
    """Exclusive cross-process lock over one session's chain-read state.

    ``flock`` is scoped to the INODE, so the lock has to live on a STABLE path.
    ``save_state`` used to flock the fresh ``mkstemp`` temp it was about to
    write -- a brand-new inode on every call -- so two PostToolUse hooks locked
    two different files, never contended, and the load->modify->save in
    :func:`record_post_read` silently lost updates. Same idiom as the other
    cross-process locks in the tree (``improvement_chain``, ``lease/record``):
    a dedicated lockfile, held across the whole read-modify-write.

    Best-effort by construction: this runs on the hook path, which must never
    fail a completed tool call. If ``fcntl`` or the lockfile is unavailable we
    proceed unlocked rather than raise -- degraded exactly to the old
    behaviour, never worse.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - POSIX only
        yield
        return
    state = _state_path(session_id)
    lock_path = state.parent / f"{state.name}.lock"
    try:
        handle = lock_path.open("a+")
    except OSError:
        yield
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@dataclass
class ChainReadState:
    relevant: list[str] = field(default_factory=list)
    blacklisted: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"relevant": self.relevant, "blacklisted": self.blacklisted}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ChainReadState:
        return cls(
            relevant=list(raw.get("relevant") or []),
            blacklisted=list(raw.get("blacklisted") or []),
        )


def load_state(session_id: str) -> ChainReadState:
    path = _state_path(session_id)
    if not path.is_file():
        return ChainReadState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return ChainReadState.from_dict(data)
    except (OSError, json.JSONDecodeError):
        pass
    return ChainReadState()


def save_state(session_id: str, state: ChainReadState) -> None:
    """Write the state file atomically via a temp + ``os.replace``.

    ``os.replace`` is what makes a concurrent reader see either the whole old
    file or the whole new one and never a torn one; no lock is involved in that
    and none is needed. What a lock IS needed for is the read-modify-write a
    caller wraps around this -- see :func:`_state_lock`, which callers must hold
    across ``load_state`` .. ``save_state``. This function cannot provide that
    guarantee on its own, and the ``flock`` that used to sit here claimed to:
    it locked the freshly created temp file, a different inode on every call.
    """
    import tempfile

    path = _state_path(session_id)
    payload = json.dumps(state.to_dict(), indent=0)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def extract_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    if tool_name not in _READ_TOOLS and tool_name not in {"Bash"}:
        return []
    paths: list[str] = []
    for key in ("file_path", "path", "pattern", "glob"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip() and not val.startswith("*"):
            # Glob patterns are not concrete paths
            if key == "pattern" and any(ch in val for ch in "*?[]"):
                continue
            paths.append(val.strip())
    return paths


def relevance_verdict(path: str) -> str:
    """Heuristic relevance: noise dirs blacklisted; else relevant.

    Server-side ranking can replace this later; must stay cheap (hook path).
    """
    if _NOISE_PATH_RE.search(path):
        return "blacklist"
    return "relevant"


def record_post_read(session_id: str, tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Update chain-read state after a read tool. Returns additionalContext or None."""
    if not session_id:
        return None
    paths = extract_paths(tool_name, tool_input)
    if not paths and tool_name not in _READ_TOOLS:
        return None
    # Each PostToolUse hook is its own process, so this read-modify-write races
    # against every other hook for the session. The lock spans load..save; a
    # lock taken only inside save_state would still let two readers start from
    # the same state and lose one another's paths.
    with _state_lock(session_id):
        state = load_state(session_id)
        changed = False
        for path in paths:
            verdict = relevance_verdict(path)
            if verdict == "blacklist":
                if path not in state.blacklisted:
                    state.blacklisted.append(path)
                    changed = True
            else:
                if path not in state.relevant and path not in state.blacklisted:
                    state.relevant.append(path)
                    # Cap buffer
                    if len(state.relevant) > 80:
                        state.relevant = state.relevant[-80:]
                    changed = True
        if changed:
            save_state(session_id, state)
            # Emit context only when the relevant set actually changed (not every
            # tool call).
            if not state.relevant:
                return None
            top = state.relevant[-12:]
            return (
                "Chain-read relevant files for this session (reuse; do not re-scan noise):\n"
                + "\n".join(f"- {p}" for p in top)
            )
    return None


def is_blacklisted(session_id: str, path: str) -> bool:
    state = load_state(session_id)
    return path in state.blacklisted
