"""Tier3 REAL-HTTP concurrency probe: the board-claim CAS over the wire.

Boots uvicorn on a free loopback port IN A THREAD (no subprocess, so the
pytest-pinned isolation env and the root conftest's dependency overrides —
including the session-token bypass — apply to the served app object) against a
per-test tmp DB, seeds one open card, then races two httpx clients on
``POST /api/collab/board/{id}/claim``.

The claim route snapshots the card's ``claim_version`` server-side and CASes on
it, so two concurrent claims contend on the same expect_version: exactly one
may win, the loser must get the ``claim_conflict`` error envelope, and a
release + reclaim must show ``claim_version`` monotonically bumped (claim 0→1,
release 1→2, reclaim 2→3) so stale claimants keep losing.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore

_READY_TIMEOUT_S = 30.0
_HTTP_TIMEOUT_S = 10.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def live_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[str, str]]:
    """(base_url, db_path) for a uvicorn-served app bound to a tmp DB."""
    db_path = str(tmp_path / "fh-claim.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db_path)
    get_collab_store.cache_clear()

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="fh-claim-uvicorn", daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + _READY_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/api/health", timeout=2.0).status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    try:
        assert ready, f"uvicorn did not become ready on {base_url} within {_READY_TIMEOUT_S}s"
        yield base_url, db_path
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        get_collab_store.cache_clear()


def test_claim_race_one_winner_then_release_reclaim_bumps_version(
    live_server: tuple[str, str],
) -> None:
    base_url, db_path = live_server
    store = CollabStore(db_path=db_path)
    task = BoardTask(title="fh claim-race card", description="seeded by feature-health tier3")
    store.create_board_task(task)
    seeded = store.get_board_task(task.id)
    assert seeded is not None and seeded["status"] == "open" and seeded["claim_version"] == 0

    barrier = threading.Barrier(2)
    responses: dict[str, httpx.Response] = {}
    errors: dict[str, BaseException] = {}

    def racer(agent_id: str) -> None:
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_S) as http:
                barrier.wait(timeout=_HTTP_TIMEOUT_S)
                responses[agent_id] = http.post(
                    f"{base_url}/api/collab/board/{task.id}/claim",
                    json={"agent_id": agent_id},
                )
        except BaseException as exc:  # noqa: BLE001 — surfaced in the main thread
            errors[agent_id] = exc

    threads = [threading.Thread(target=racer, args=(name,)) for name in ("fh-racer-a", "fh-racer-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, f"racer thread failed: {errors!r}"
    assert sorted(responses) == ["fh-racer-a", "fh-racer-b"]

    codes = sorted(response.status_code for response in responses.values())
    assert codes == [200, 409], (
        f"CAS race must produce exactly one winner: got {codes} "
        f"({ {k: v.text for k, v in responses.items()} })"
    )
    winner = next(k for k, v in responses.items() if v.status_code == 200)
    loser = next(k for k, v in responses.items() if v.status_code == 409)
    assert responses[winner].json() == {
        "success": True,
        "owner_employee_id": None,
        "claimed_by": winner,
    }
    loser_body = responses[loser].json()
    assert "error" in loser_body, f"loser missing error envelope: {loser_body!r}"
    for key in ("code", "message", "detail"):
        assert key in loser_body["error"]
    assert loser_body["error"]["code"] == "claim_conflict"

    claimed = store.get_board_task(task.id)
    assert claimed is not None
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == winner
    assert claimed["claim_version"] == 1

    # Release (claim → open) bumps the version so any stale claimant's CAS loses.
    assert store.release_claim(task.id) is True
    released = store.get_board_task(task.id)
    assert released is not None
    assert released["status"] == "open"
    assert released["claimed_by"] is None
    assert released["claim_version"] == 2

    reclaim = httpx.post(
        f"{base_url}/api/collab/board/{task.id}/claim",
        json={"agent_id": "fh-reclaimer"},
        timeout=_HTTP_TIMEOUT_S,
    )
    assert reclaim.status_code == 200, reclaim.text
    reclaimed = store.get_board_task(task.id)
    assert reclaimed is not None
    assert reclaimed["claimed_by"] == "fh-reclaimer"
    assert reclaimed["claim_version"] == 3, (
        f"claim_version must bump monotonically across release+reclaim, "
        f"got {reclaimed['claim_version']}"
    )
