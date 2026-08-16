"""Tier3 read-only probes against the LIVE api at 127.0.0.1:8485.

Doubly opt-in: file-level ``live`` marker (excluded from the isolated tier3
selection and every default lane) AND ``FH_LIVE_PROBES=1`` in the environment
(the runner's ``--live-probes`` flag). GET-only by contract — these must NEVER
mutate the operator control plane. Ledger records for this file carry
``env:"live"``.

The session token is read from ``var/secrets/sessions-token`` when present and
sent as ``X-Session-Token`` (harmless on public reads, required if a probe path
is ever token-gated). Every probe asserts bounded latency (<10s) — the same
"bounded time, clear error" guarantee the dashboard gives its own fetches.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FH_LIVE_PROBES") != "1",
        reason="read-only live probes are opt-in: set FH_LIVE_PROBES=1 (run.sh --live-probes)",
    ),
]

_BASE_URL = os.environ.get("FH_LIVE_API_BASE", "http://127.0.0.1:8485")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOKEN_PATH = _REPO_ROOT / "var" / "secrets" / "sessions-token"
_MAX_LATENCY_S = 10.0
_MAX_BOARD_BYTES = 5 * 1024 * 1024


def _headers() -> dict[str, str]:
    try:
        token = _TOKEN_PATH.read_text().strip()
    except OSError:
        return {}
    return {"X-Session-Token": token} if token else {}


def _probe(path: str) -> tuple[httpx.Response, float]:
    start = time.perf_counter()
    response = httpx.get(f"{_BASE_URL}{path}", headers=_headers(), timeout=_MAX_LATENCY_S)
    latency = time.perf_counter() - start
    assert latency < _MAX_LATENCY_S, f"GET {path} took {latency:.2f}s (cap {_MAX_LATENCY_S}s)"
    return response, latency


def test_live_health_status_ok_and_db_true() -> None:
    response, _ = _probe("/api/health")
    assert response.status_code == 200, response.text
    body = response.json()
    worker = body.get("worker") or {}
    assert body.get("status") == "ok" and body.get("db") is True, (
        f"live /api/health degraded: status={body.get('status')!r} db={body.get('db')!r} "
        f"worker.alive={worker.get('alive')!r}"
    )


def test_live_dashboard_today_responds() -> None:
    response, _ = _probe("/api/dashboard/today")
    assert response.status_code == 200, response.text


def test_live_board_responds_and_stays_projected() -> None:
    # The board list is GET /api/board (the reconciled one). Its duplicate,
    # GET /api/collab/board, was removed — same table, no reconcile, no caller.
    # Probed with an explicit page bound, which is what a phone actually asks
    # for; the unbounded feed measured 2.66 MB / 13.9 s on the live board.
    response, _ = _probe("/api/board?limit=200")
    assert response.status_code == 200, response.text
    # Projection regression guard: a board list that re-grew the swarm_json
    # envelopes balloons past this bound long before the kanban falls over.
    size = len(response.content)
    assert size < _MAX_BOARD_BYTES, (
        f"live /api/board?limit=200 returned {size} bytes (cap {_MAX_BOARD_BYTES}) — "
        "list projection regression?"
    )


def test_live_goals_responds() -> None:
    response, _ = _probe("/api/goals")
    assert response.status_code == 200, response.text


@pytest.mark.fh_known_issue(id="FH-000")
def test_live_routines_are_not_silently_suppressed() -> None:
    """A routine the tick refuses to fire must SAY so in its own status.

    Detects the I-0 shape observed 2026-07-31: both autonomous routines were
    skipped on every tick with "acceptance rate below the 50% auto-pause floor"
    while ``GET /api/routines`` still reported ``status: active`` and an empty
    ``auto_pause_reason`` — a live outage with a healthy-looking control plane.
    Read-only: parses the routines list and the tick log, mutates nothing.
    """
    response, _ = _probe("/api/routines")
    assert response.status_code == 200, response.text
    body = response.json()
    routines = body if isinstance(body, list) else (body.get("routines") or [])

    log_path = _REPO_ROOT / "var" / "log" / "routines.log"
    if not log_path.exists():
        pytest.skip(f"no tick log at {log_path} — cannot correlate fire behaviour")

    lines = log_path.read_text(errors="replace").splitlines()
    if not lines:
        pytest.skip("tick log is empty — scheduler has not ticked yet")
    # Scan back for the newest COMPLETE record: the tail can be a partially
    # flushed line if a tick is mid-write, and skipping on that would hide the
    # very outage this probe exists to catch.
    last_tick = None
    for line in reversed(lines[-10:]):
        try:
            last_tick = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if last_tick is None:
        pytest.skip("no parseable tick record in the last 10 log lines")

    # Reasons that mean "not due yet" are healthy; anything else is suppression.
    suppressed = {
        entry.get("routine_id"): entry.get("reason", "")
        for entry in last_tick.get("skipped") or []
        if "not due" not in (entry.get("reason") or "")
    }
    if not suppressed:
        return

    by_id = {r.get("id"): r for r in routines}
    lying = [
        f"{rid} skipped with {reason!r} but API reports "
        f"status={by_id.get(rid, {}).get('status')!r} "
        f"auto_pause_reason={by_id.get(rid, {}).get('auto_pause_reason')!r}"
        for rid, reason in suppressed.items()
        if by_id.get(rid, {}).get("status") == "active"
        and not by_id.get(rid, {}).get("auto_pause_reason")
    ]
    assert not lying, (
        "routines are suppressed at fire time but report themselves healthy "
        "(silent autonomous-dispatch outage):\n  " + "\n  ".join(lying)
    )
