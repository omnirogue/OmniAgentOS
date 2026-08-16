"""Fixture builders for the estate-compute readers and route.

Every source is injected — a fake ``fetch`` for the pool HTTP call, a fake
``runner`` for the ``gh`` subprocess, a tmp ``connections.env`` for the token
parser — so no test ever makes a live network call or shells out for real.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from omniagentos.compute import readers


@pytest.fixture
def auth_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """The X-Session-Token header the gated ``/api/compute/estate`` read requires.

    TOKEN_PATH is redirected into tmp so the real var/secrets token is never
    read or created (same pattern as tests/testobs/conftest.py).
    """
    from omniagentos.sessions import token

    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return {"X-Session-Token": token.load_or_create_token()}


@pytest.fixture
def connections_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[str], None]:
    """Point the pool-token parser at a tmp connections.env, and clear WQ_TOKEN.

    Returns a ``write(text)`` helper so a test can seed the file's contents
    (or leave it absent, for the missing-token case).
    """
    monkeypatch.delenv("WQ_TOKEN", raising=False)
    env_path = tmp_path / "connections.env"
    monkeypatch.setattr(readers, "CONNECTIONS_ENV", env_path)

    def write(text: str) -> None:
        env_path.write_text(text, encoding="utf-8")

    return write


def fake_fetch(payload: bytes | Exception) -> readers.FetchFn:
    """A ``FetchFn`` that returns ``payload`` verbatim, or raises it."""

    def _fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        if isinstance(payload, Exception):
            raise payload
        return payload

    return _fetch


def fake_runner(
    *, returncode: int = 0, stdout: str = "[]", raises: Exception | None = None
) -> readers.RunnerFn:
    """A ``RunnerFn`` that returns a fixed CompletedProcess, or raises."""

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return _runner


# A realistic /v1/status sample, trimmed from a live pool response (field
# names/types verified live 2026-08-14) to two machines — enough to exercise
# every field the reader relays, without embedding the whole fleet.
POOL_STATUS_SAMPLE: dict = {
    "now": "2026-08-14T20:07:16Z",
    "machines": [
        {
            "machine_id": "macstudio-b",
            "hostname": "macstudio-b.local",
            "os": "darwin",
            "labels": ["darwin", "build", "pytest", "script"],
            "max_concurrent": 3,
            "drain": 0,
            "in_flight": 1,
            "last_seen_at": "2026-08-14T20:07:09Z",
            "last_seen_age_s": 0.0,
            "ncpu": 16,
            "perf_cores": 12,
            "mem_gb": 128.0,
            "load1": 4.58,
            "load5": 5.18,
            "mem_free_gb": 94.44,
            "ceiling_fraction": 0.75,
            "done_1h": 0,
            "done_6h": 2,
            "attempts_per_completion": 1.0,
        },
        {
            "machine_id": "initech-dev",
            "hostname": "initech-dev.vps.ovh.us",
            "os": "linux",
            "labels": ["linux", "linux-ci", "script", "pytest-linux"],
            "max_concurrent": 2,
            "drain": 0,
            "in_flight": 0,
            "last_seen_at": "2026-08-14T20:06:57Z",
            "last_seen_age_s": 12.0,
            "ncpu": 12,
            "perf_cores": 12,
            "mem_gb": 45.9,
            "load1": 5.85,
            "load5": 4.56,
            "mem_free_gb": 32.48,
            "ceiling_fraction": 0.4,
            "done_1h": 0,
            "done_6h": 0,
            "attempts_per_completion": None,
        },
    ],
    "workers": 36,
    "depth": {"queued": 0, "claimed": 0, "running": 0, "review": 0, "done": 27, "parked": 12, "cancelled": 37},
    "oldest_unclaimed_s": None,
    "unclaimable": [],
    "refusals_24h": {
        "candidate-defect": 8, "environment": 4, "instrument-error": 28,
        "unchanged-retry": 3, "total": 43, "instrument_share": 0.744,
    },
    "capacity": {"total_cores": 106, "total_perf_cores": 88, "total_slots": 16, "free_slots": 16, "in_flight": 0},
}

POOL_STATUS_SAMPLE_BYTES = json.dumps(POOL_STATUS_SAMPLE).encode("utf-8")

RUNNERS_SAMPLE = [
    {"name": "acmeuni-claude-estate", "labels": ["self-hosted", "estate", "Linux", "X64"],
     "status": "online", "busy": False},
    {"name": "mw0001-estate", "labels": ["self-hosted", "macOS", "ARM64", "estate"],
     "status": "online", "busy": True},
]
RUNNERS_SAMPLE_STDOUT = json.dumps(RUNNERS_SAMPLE)
