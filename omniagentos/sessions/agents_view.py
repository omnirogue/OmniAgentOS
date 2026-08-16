"""Best-effort enrollment data from Claude Code's per-profile Agent View."""

from __future__ import annotations

import concurrent.futures
import glob
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from omniagentos.adapters.common import _scrubbed_env

_CACHE_TTL_SECONDS = 30.0
_CACHE_LOCK = threading.Lock()
_cache_at = 0.0
_cache_value: dict[int, dict[str, Any]] | None = None


def list_profiles() -> list[Path]:
    """Return existing Claude profile directories, excluding the empty suffix trap."""
    home = Path.home()
    candidates = [home / ".claude"] + [
        Path(item) for item in glob.glob(str(home / ".claude-account-*"))
    ]
    return sorted(
        (
            profile
            for profile in candidates
            if profile.is_dir() and not profile.name.endswith("account-")
        ),
        key=str,
    )


def fetch_profile(profile: str | Path) -> list[dict[str, Any]]:
    """Read one profile's Agent View without allowing CLI trouble to escape."""
    profile_path = Path(profile)
    # AC-policy money boundary (#499 F1): the spawned `claude` subscription CLI
    # must NOT inherit the parent's payment/bank/infra credentials, model-provider
    # API keys (incl. ANTHROPIC_API_KEY), or the OPERATOR_TOKEN. Hand it only the
    # canonical allowlisted env plus the profile pointer that selects the account.
    try:
        completed = subprocess.run(
            ["claude", "agents", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8.0,
            env={**_scrubbed_env(), "CLAUDE_CONFIG_DIR": str(profile_path)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode not in (0, None):
        return []
    try:
        payload = json.loads(completed.stdout or "")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    profile_name = profile_path.name
    return [dict(item, profile=profile_name) for item in payload if isinstance(item, dict)]


def collect_all() -> dict[int, dict[str, Any]]:
    """Merge profile Agent Views by PID, caching results for a short read interval.

    Profiles are traversed in sorted order. A PID present in multiple profiles is
    intentionally last-write-wins, which is the least surprising representation
    of an active process when Claude's profile registries temporarily overlap.
    """
    global _cache_at, _cache_value
    # The whole rebuild runs under the lock (single-flight): on a cold or
    # expired cache, concurrent callers queue behind one rebuild instead of
    # each spawning their own 6-worker pool of `claude agents` subprocesses.
    with _CACHE_LOCK:
        now = time.monotonic()
        if _cache_value is not None and now - _cache_at < _CACHE_TTL_SECONDS:
            return {pid: dict(item) for pid, item in _cache_value.items()}

        merged: dict[int, dict[str, Any]] = {}
        profiles = list_profiles()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            for entries in executor.map(fetch_profile, profiles):
                for entry in entries:
                    raw_pid = entry.get("pid")
                    if raw_pid is None:
                        continue
                    try:
                        pid = int(raw_pid)
                    except (TypeError, ValueError):
                        continue
                    if pid > 0:
                        merged[pid] = entry

        _cache_at = time.monotonic()
        _cache_value = {pid: dict(item) for pid, item in merged.items()}
        return {pid: dict(item) for pid, item in merged.items()}


