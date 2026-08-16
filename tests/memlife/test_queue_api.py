"""L6 decisive acceptance: queue endpoint distinguishes three states.

Governing rule: unknown/absent/unparseable must never render as good.
A missing store directory returning ``200 {pending: 0}`` is the exact defect
fixed twice today (broken question identical to a true empty answer).

Decisive assertions (one test per state):
  - pending N  → 200 ``{pending: N}`` with N > 0
  - pending 0  → 200 ``{pending: 0}`` only when the store root exists
  - unavailable → non-200 with ``{error: store_unavailable}``

Named counterfeit: point the store at a nonexistent path — a 200 with
``pending: 0`` MUST fail this module.

Revert-check: collapse the error branch into the empty branch → the
unavailable test fails (run manually by flipping the guard in routes).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "memlife.db")
    version = migrate(path)
    assert version >= 90, f"expected migration 090 applied, got {version}"
    return path


@pytest.fixture
def store(db_path: str) -> SqliteStore:
    return SqliteStore(db_path=db_path)


@pytest.fixture
def store_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An existing memlife store directory — empty answer is only valid here."""
    root = tmp_path / "memlife-store"
    root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_MEMLIFE_STORE", str(root))
    # Clear any cached resolver if the routes module caches path resolution.
    import omniagentos.api.routes.memlife as memlife_routes

    if hasattr(memlife_routes, "_clear_store_root_cache"):
        memlife_routes._clear_store_root_cache()
    return root


@pytest.fixture
def client(store: SqliteStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: store
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


def _insert_candidate(
    store: SqliteStore,
    *,
    candidate_id: str,
    status: str = "staged",
    claim: str = "Agents cannot commit inside a sandboxed worktree.",
) -> None:
    now = utc_now_iso()
    with store._lock:
        store._connection.execute(
            """
            INSERT INTO memlife_candidates (
                id, key, claim, conditions, evidence_ids_json, cluster_size,
                status, rejection_count, salience, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                f"key/{candidate_id}",
                claim,
                "",
                "[]",
                1,
                status,
                0,
                None,
                now,
                now,
            ),
        )
        store._connection.execute(
            """
            INSERT INTO memlife_decisions (id, candidate_id, action, at, actor, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (f"dec_{candidate_id}_stage", candidate_id, "stage", now, "dream-cycle", ""),
        )


# ---------------------------------------------------------------------------
# Decisive: three states, three distinct payloads / status codes
# ---------------------------------------------------------------------------


def test_queue_pending_n(
    client: httpx.AsyncClient,
    store: SqliteStore,
    store_root: Path,
) -> None:
    """State 1: store present with N staged candidates → 200 {pending: N}."""
    assert store_root.is_dir()
    _insert_candidate(store, candidate_id="cand_a", status="staged")
    _insert_candidate(store, candidate_id="cand_b", status="staged")
    _insert_candidate(store, candidate_id="cand_c", status="reopened")
    # Graduated/rejected must not inflate the pending count.
    _insert_candidate(store, candidate_id="cand_done", status="graduated")
    _insert_candidate(store, candidate_id="cand_no", status="rejected")

    response = _run(client.get("/api/memlife/queue"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"pending": 3}
    assert "error" not in body


def test_queue_pending_zero_when_store_present(
    client: httpx.AsyncClient,
    store_root: Path,
) -> None:
    """State 2: store present and empty → 200 {pending: 0}.

    This is a *true* empty answer — the directory exists and the table is empty.
    """
    assert store_root.is_dir()

    response = _run(client.get("/api/memlife/queue"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"pending": 0}
    assert "error" not in body


def test_queue_store_unavailable_when_path_missing(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State 3: store root does not exist → explicit error, never empty-pending.

    Named counterfeit: a ``200 {pending: 0}`` response MUST fail this test.
    That is the agentic-stack review_state defect (missing dir ≡ empty queue).
    """
    missing = tmp_path / "does-not-exist"
    assert not missing.exists()
    monkeypatch.setenv("OMNIAGENTOS_MEMLIFE_STORE", str(missing))
    import omniagentos.api.routes.memlife as memlife_routes

    if hasattr(memlife_routes, "_clear_store_root_cache"):
        memlife_routes._clear_store_root_cache()

    response = _run(client.get("/api/memlife/queue"))

    # Counterfeit that must fail: treating unavailable as empty.
    if response.status_code == 200 and response.json() == {"pending": 0}:
        pytest.fail(
            "COUNTERFEIT: missing store directory returned 200 {pending: 0} — "
            "absent must not render as a true empty answer"
        )

    assert response.status_code != 200
    body = response.json()
    assert body.get("error") == "store_unavailable", (
        f"expected {{error: store_unavailable}}, got {body!r}"
    )
    # Must not also claim a flattering empty queue.
    assert body.get("pending") is None or "pending" not in body


# ---------------------------------------------------------------------------
# Lifecycle routes (reflection-shaped): graduate / reject / reopen
# ---------------------------------------------------------------------------


def test_graduate_writes_lesson_and_decision(
    client: httpx.AsyncClient,
    store: SqliteStore,
    store_root: Path,
) -> None:
    _insert_candidate(store, candidate_id="cand_grad", status="staged")

    response = _run(
        client.post(
            "/api/memlife/cand_grad/graduate",
            json={"decided_by": "owner", "reason": "reproducible across three runs"},
        )
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == "cand_grad"
    assert body["status"] == "graduated"

    with store._lock:
        cand = store._connection.execute(
            "SELECT status FROM memlife_candidates WHERE id = ?",
            ("cand_grad",),
        ).fetchone()
        lesson = store._connection.execute(
            "SELECT candidate_id, claim, status, graduated_by FROM memlife_lessons "
            "WHERE candidate_id = ?",
            ("cand_grad",),
        ).fetchone()
        decisions = store._connection.execute(
            "SELECT action FROM memlife_decisions WHERE candidate_id = ? ORDER BY at",
            ("cand_grad",),
        ).fetchall()

    assert cand["status"] == "graduated"
    assert lesson is not None, "graduation must write a lesson row (not just flip status)"
    assert lesson["graduated_by"] == "owner"
    assert lesson["status"] == "accepted"
    assert [d["action"] for d in decisions] == ["stage", "graduate"]


def test_reject_and_reopen_cycle(
    client: httpx.AsyncClient,
    store: SqliteStore,
    store_root: Path,
) -> None:
    _insert_candidate(store, candidate_id="cand_cycle", status="staged")

    rejected = _run(
        client.post(
            "/api/memlife/cand_cycle/reject",
            json={"decided_by": "owner", "reason": "not general enough"},
        )
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"

    reopened = _run(
        client.post(
            "/api/memlife/cand_cycle/reopen",
            json={"decided_by": "owner", "reason": "new evidence arrived"},
        )
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "reopened"

    queue = _run(client.get("/api/memlife/queue"))
    assert queue.status_code == 200
    assert queue.json() == {"pending": 1}
