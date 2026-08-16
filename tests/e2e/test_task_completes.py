"""Live user-visible outcome probe.

The ONE test bound to the product's actual job: submit a trivial goal through
the public API and require that the deployed system carries **the board card it
created for that goal** to ``status='done'``. 11,000+ unit tests can be green
while this fails; when it passes, the outcome loop demonstrably works.

Run explicitly (deselected from the default suite the same way ``live`` is)::

    .venv/bin/pytest -m e2e tests/e2e/test_task_completes.py

WHAT COUNTS AS THE OUTCOME — the board task id returned by
``POST /api/intake/quick``, reaching ``done``. The first version of this probe
demanded a completed ``swarm_attempts`` row instead, and failed against a
healthy system: for a trivial goal the router legitimately chose the single-
session path, completed it, and the probe was watching the wrong table. The
card is the router-agnostic, user-visible unit — it is what the operator sees on
/board — so the card's terminal state is the honest assertion. Attempt/session
evidence is still collected, but for DIAGNOSIS of which path executed, not as
the requirement.

SAFETY CONTRACT — this test must be INCAPABLE of mutating production:

* Every direct database access opens ``file:...?mode=ro`` (URI read-only). A
  read-only connection cannot write and cannot run migrations, so a branch
  whose migration files are ahead of the live schema cannot alter the live
  database from here (that near-miss actually happened: an earlier version
  opened read-write through the product store, which auto-migrates on connect,
  and only the checksum guard stopped a test run from rewriting ~1,850
  production rows).
* The only writes travel through the live HTTP API — the same surface a person
  uses. What the deployed system does with them is precisely what is measured.
* No scheduler is instantiated in-process: if the deployed system is not
  executing work, the honest result is a FAILURE naming which stage stalled,
  not a test that quietly brings its own engine.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests

LIVE_DB = Path(
    os.environ.get(
        "OMNIAGENTOS_E2E_DB", "/Users/youruser/OmniAgentOS/var/runtime/state.sqlite3"
    )
)
API_BASE = os.environ.get("OMNIAGENTOS_E2E_API", "http://127.0.0.1:8485")
# Budgeted under common shell/tool caps (10 min): a trivial goal has completed
# in ~4 minutes on this system; override with OMNIAGENTOS_E2E_TIMEOUT for
# patience runs.
TIMEOUT_SECONDS = int(os.environ.get("OMNIAGENTOS_E2E_TIMEOUT", "480"))
POLL_SECONDS = 10
TERMINAL_FAIL_STATUSES = {"cancelled", "blocked"}


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _live_session_token() -> str:
    """Read the running API's token without using pytest's isolated var root."""
    configured = os.environ.get("OMNIAGENTOS_E2E_TOKEN_PATH")
    path = Path(configured) if configured else LIVE_DB.parent / "secrets" / "sessions-token"
    return path.read_text(encoding="utf-8").strip()


def _readonly_connection() -> sqlite3.Connection:
    """Open the live DB so that writing is impossible, not merely avoided."""
    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _board_task(task_id: str) -> dict[str, Any] | None:
    with _readonly_connection() as conn:
        row = conn.execute(
            "SELECT id, status, claimed_by, swarm_run_id FROM board_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    return dict(row) if row else None


def _execution_evidence(task_id: str, since: str) -> str:
    """Which path executed (or stalled) — diagnosis text, not the assertion."""
    with _readonly_connection() as conn:
        attempts = conn.execute(
            "SELECT a.provider, a.end_reason, a.detail FROM swarm_attempts a "
            "JOIN swarm_runs r ON r.id = a.swarm_run_id "
            "WHERE r.board_task_id = ? ORDER BY a.started_at DESC LIMIT 5",
            (task_id,),
        ).fetchall()
        sessions = conn.execute(
            "SELECT provider, state, title FROM sessions "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT 5",
            (since,),
        ).fetchall()
    parts = []
    if attempts:
        parts.append(
            "attempts: "
            + ", ".join(f"{a['provider']}:{a['end_reason'] or 'running'}" for a in attempts)
        )
    if sessions:
        parts.append(
            "recent sessions: "
            + ", ".join(f"{s['provider']}:{s['state']}({s['title'][:30]})" for s in sessions)
        )
    return "; ".join(parts) if parts else "no attempts and no sessions observed"


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.timeout(540)
def test_trivial_goal_card_reaches_done() -> None:
    """Submit a trivial goal via the public API; its board card must reach done."""
    if not LIVE_DB.exists():
        pytest.fail(f"live database not found at {LIVE_DB} — is the stack deployed?")

    client = requests.Session()
    client.headers["X-Session-Token"] = _live_session_token()

    try:
        health = client.get(f"{API_BASE}/api/health", timeout=5)
    except requests.RequestException as exc:
        pytest.fail(f"live API not reachable at {API_BASE}: {exc}")
    assert health.status_code == 200, f"/api/health returned {health.status_code}"

    since = _iso_now()
    nonce = uuid.uuid4().hex[:8]
    goal = f"echo hello e2e-{nonce}"

    intake = client.post(
        f"{API_BASE}/api/intake/quick",
        json={"goal": goal, "speed": "fast"},
        timeout=60,
    )
    assert intake.status_code == 201, intake.text
    body = intake.json()
    task_id = body.get("board_task_id")
    assert task_id, f"intake response carried no board_task_id: {body}"

    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_status = "<never read>"
    while time.monotonic() < deadline:
        card = _board_task(task_id)
        if card is not None:
            last_status = str(card["status"])
            if last_status == "done":
                return
            if last_status in TERMINAL_FAIL_STATUSES:
                pytest.fail(
                    f"card {task_id} reached terminal '{last_status}' instead of done — "
                    f"{_execution_evidence(task_id, since)}"
                )
        time.sleep(POLL_SECONDS)

    pytest.fail(
        f"Timeout after {TIMEOUT_SECONDS}s: card {task_id} ended at "
        f"'{last_status}', not 'done' — {_execution_evidence(task_id, since)}"
    )
