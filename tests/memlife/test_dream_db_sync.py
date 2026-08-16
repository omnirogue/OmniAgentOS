"""Lane B: dream cycle dual-write (SQLite + filesystem) invariants.

Each claimed protection has a test that must go RED when the production
line is mutated back to the counterfeit (failing-on-revert).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.memlife.contracts import (
    Candidate,
    CandidateStatus,
    CycleStatus,
    Decision,
    DecisionAction,
    EpisodicEvent,
    EventResult,
)
from omniagentos.memlife.db import (
    read_capture_watermark,
    stage_candidate,
    write_capture_watermark,
)
from omniagentos.memlife.dream import ensure_dream_cycle_routine, run_dream_cycle
from omniagentos.memlife.store import MemlifeStore
from omniagentos.policy import load_policy
from omniagentos.scheduler.routines_tick import tick

DUE_NOW = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
# Second tick after reject — next night still due under cron.
SECOND_TICK = datetime(2026, 7, 30, 3, 0, tzinfo=UTC)
ENV_STORE = "OMNIAGENTOS_MEMLIFE_STORE"
EVENT_TS = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
NOW_ISO = "2026-07-29T03:00:00Z"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _valid_event_line(event_id: str, *, reflection: str) -> str:
    event = EpisodicEvent(
        id=event_id,
        ts=EVENT_TS,
        skill="swarm.coder",
        action="attempt",
        result=EventResult.FAILURE,
        pain=8.0,
        importance=9.0,
        reflection=reflection,
    )
    return event.model_dump_json()


def _seed_two_events(root: Path) -> None:
    events_path = root / "episodic" / "events.jsonl"
    lines = [
        _valid_event_line(
            "ev_a",
            reflection="Agents cannot commit inside a sandboxed worktree",
        ),
        _valid_event_line(
            "ev_b",
            reflection="Always pin exact dependency versions in lockfiles",
        ),
    ]
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _reseed_same_claims_fresh_evidence(root: Path) -> None:
    """Same claims, *new* event ids → same keys, different candidate ids.

    Cluster id is ``sha256(key|evidence_ids)``; key is skill+claim tokens.
    Fresh evidence is the exact case key-level dedupe exists for (R4 / B1.2):
    id-only ON CONFLICT would let a rejected claim return under a new id.
    """
    events_path = root / "episodic" / "events.jsonl"
    lines = [
        _valid_event_line(
            "ev_a_round2",
            reflection="Agents cannot commit inside a sandboxed worktree",
        ),
        _valid_event_line(
            "ev_b_round2",
            reflection="Always pin exact dependency versions in lockfiles",
        ),
    ]
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _open_migrated(tmp_path: Path, name: str = "lane_b.db") -> sqlite3.Connection:
    db_path = str(tmp_path / name)
    migrate(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _cand(
    *,
    cand_id: str = "cand_x",
    key: str = "swarm.coder/keyx",
    claim: str = "Claims must map evidence and cluster size into SQL",
    evidence_ids: list[str] | None = None,
    cluster_size: int = 3,
    salience: float | None = 0.5,
    actor: str = "dream-cycle",
    reason: str = "dream cycle",
) -> Candidate:
    return Candidate(
        id=cand_id,
        key=key,
        claim=claim,
        cluster_size=cluster_size,
        evidence_ids=list(evidence_ids or ["ev_1", "ev_2", "ev_3"]),
        status=CandidateStatus.STAGED,
        decisions=[
            Decision(
                action=DecisionAction.STAGE,
                at=NOW,
                actor=actor,
                reason=reason,
            )
        ],
        salience=salience,
    )


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "dream_db_sync.db"))


@pytest.fixture
def memlife_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "memlife_store"
    MemlifeStore(root).ensure_layout()
    monkeypatch.setenv(ENV_STORE, str(root))
    import omniagentos.api.routes.memlife as memlife_routes

    if hasattr(memlife_routes, "_clear_store_root_cache"):
        memlife_routes._clear_store_root_cache()
    return root


@pytest.fixture
def client(database: SqliteStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: database
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# B4 decisive paths
# ---------------------------------------------------------------------------


def test_a_rejected_candidate_is_not_restaged(
    database: SqliteStore,
    memlife_root: Path,
    client: httpx.AsyncClient,
) -> None:
    """tick → reject → tick: DB stays rejected; FS stays rejected; no restage."""
    ensure_dream_cycle_routine(database)
    _seed_two_events(memlife_root)

    first = tick(database, load_policy(), now=DUE_NOW)
    assert len(first["fired"]) == 1, first

    queue_snap = MemlifeStore(memlife_root).load_queue()
    ids = list(queue_snap["pending"])
    assert len(ids) == 2, queue_snap
    reject_id = ids[0]

    rejected = _run(
        client.post(
            f"/api/memlife/{reject_id}/reject",
            json={"decided_by": "owner", "reason": "not general enough"},
        )
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"

    # B4: filesystem mirror must also be rejected (not left STAGED).
    fs_cand = MemlifeStore(memlife_root).load_candidate(reject_id)
    assert fs_cand.status is CandidateStatus.REJECTED, fs_cand.status

    # Same claims, fresh evidence ids → same keys, *new* candidate ids.
    # Without key-level dedupe a rejected claim returns under a new id.
    _reseed_same_claims_fresh_evidence(memlife_root)
    second = tick(database, load_policy(), now=SECOND_TICK)
    assert len(second["fired"]) == 1, second

    with database._lock:
        row = database._connection.execute(
            "SELECT status, key FROM memlife_candidates WHERE id = ?",
            (reject_id,),
        ).fetchone()
        decisions = database._connection.execute(
            "SELECT action FROM memlife_decisions WHERE candidate_id = ?",
            (reject_id,),
        ).fetchall()
        pending_n = database._connection.execute(
            "SELECT COUNT(*) AS n FROM memlife_candidates "
            "WHERE status IN ('staged','reopened')"
        ).fetchone()
        same_key_staged = database._connection.execute(
            "SELECT id, status FROM memlife_candidates "
            "WHERE key = ? AND status IN ('staged','reopened')",
            (row["key"],),
        ).fetchall()

    assert row is not None
    assert row["status"] == "rejected"
    actions = [d["action"] for d in decisions]
    assert len(decisions) == 2, decisions
    assert sorted(actions) == ["reject", "stage"], actions
    assert same_key_staged == [], same_key_staged
    assert int(pending_n["n"]) >= 1

    # Mirror must still be rejected after the second tick (not overwritten).
    fs_after = MemlifeStore(memlife_root).load_candidate(reject_id)
    assert fs_after.status is CandidateStatus.REJECTED, fs_after.status

    queue = _run(client.get("/api/memlife/queue"))
    assert queue.status_code == 200
    body = queue.json()
    # Queue is SQLite-authoritative: rejected row must not inflate pending.
    assert body["pending"] >= 1
    assert row["status"] == "rejected"
    with database._lock:
        pending_ids = [
            r["id"]
            for r in database._connection.execute(
                "SELECT id FROM memlife_candidates "
                "WHERE status IN ('staged','reopened')"
            ).fetchall()
        ]
    assert reject_id not in pending_ids, pending_ids


def test_unknown_salience_is_stored_as_null(tmp_path: Path) -> None:
    """salience=None lands as SQL NULL, not 0.0 (contracts.py:16-19)."""
    conn = _open_migrated(tmp_path, "salience.db")
    cand = _cand(cand_id="cand_null_sal", key="swarm.coder/nullsal", salience=None)
    assert stage_candidate(conn, cand, now=NOW_ISO) is True
    conn.commit()
    row = conn.execute(
        "SELECT salience FROM memlife_candidates WHERE id = ?",
        ("cand_null_sal",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["salience"] is None


def test_db_stage_failure_does_not_write_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterfeit kill: stage_candidate raises → FAILED, no FS candidate file."""
    import omniagentos.memlife.dream as dream_mod

    root = tmp_path / "memlife_store"
    MemlifeStore(root).ensure_layout()
    _seed_two_events(root)

    db_path = str(tmp_path / "fail.db")
    migrate(db_path)
    store = SqliteStore(db_path)

    def _boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("simulated db lock")

    monkeypatch.setattr(dream_mod, "stage_candidate", _boom)

    report = run_dream_cycle(
        root,
        now=NOW,
        db_conn=store._connection,
    )
    assert report.status is CycleStatus.FAILED
    assert any("pipeline failed" in e for e in report.errors)

    cand_files = list((root / "candidates").glob("cand_*.json"))
    assert cand_files == [], f"no filesystem candidate on DB failure: {cand_files}"


# ---------------------------------------------------------------------------
# Watermark helpers (B3) — missing / unparseable / invalid ISO → full capture
# ---------------------------------------------------------------------------


def test_watermark_missing_means_full_capture(tmp_path: Path) -> None:
    root = tmp_path / "store"
    (root / "episodic").mkdir(parents=True)
    assert read_capture_watermark(root) is None

    write_capture_watermark(root, "2026-07-28T12:00:00Z")
    assert read_capture_watermark(root) == "2026-07-28T12:00:00Z"

    # Unparseable JSON → full capture
    (root / "episodic" / "capture_watermark.json").write_text(
        "not-json", encoding="utf-8"
    )
    assert read_capture_watermark(root) is None


def test_watermark_invalid_timestamp_means_full_capture(tmp_path: Path) -> None:
    """Valid JSON with a non-ISO last_captured_ts is full capture, not a bomb."""
    root = tmp_path / "store"
    (root / "episodic").mkdir(parents=True)
    (root / "episodic" / "capture_watermark.json").write_text(
        json.dumps({"last_captured_ts": "not-a-timestamp", "written_at": NOW_ISO}),
        encoding="utf-8",
    )
    assert read_capture_watermark(root) is None


# ---------------------------------------------------------------------------
# Unproven → proven: each of these must RED when its production line reverts
# ---------------------------------------------------------------------------


def test_id_conflict_is_noop_not_status_reset(tmp_path: Path) -> None:
    """ON CONFLICT DO NOTHING: second insert skips; never resets status/decisions.

    Counterfeit: ``ON CONFLICT(id) DO UPDATE SET status='staged'`` makes the
    second call report a write (rowcount=1) and append a second stage decision.
    """
    conn = _open_migrated(tmp_path, "id_conflict.db")
    cand = _cand(cand_id="cand_same", key="swarm.coder/same")
    assert stage_candidate(conn, cand, now=NOW_ISO) is True
    conn.commit()

    # Same id, same key — must be a no-op (DO NOTHING), not a status rewrite.
    assert stage_candidate(conn, cand, now="2026-07-30T03:00:00Z") is False
    conn.commit()

    row = conn.execute(
        "SELECT status, updated_at FROM memlife_candidates WHERE id = ?",
        ("cand_same",),
    ).fetchone()
    decisions = conn.execute(
        "SELECT action, at FROM memlife_decisions WHERE candidate_id = ? "
        "ORDER BY at",
        ("cand_same",),
    ).fetchall()
    conn.close()

    assert row is not None
    assert row["status"] == "staged"
    # First insert's timestamp must not be overwritten by a conflict "update".
    assert row["updated_at"] == NOW_ISO
    assert len(decisions) == 1, decisions
    assert decisions[0]["action"] == "stage"


def test_id_conflict_does_not_resurrect_rejected(tmp_path: Path) -> None:
    """Same id after reject: key gate or DO NOTHING must keep status rejected.

    Counterfeit DO UPDATE SET status='staged' resurrects the queue row when the
    INSERT is attempted with a *different* key (key gate does not fire) —
    that is the pure id-conflict path the double-stage test also covers after
    a manual status flip.
    """
    conn = _open_migrated(tmp_path, "id_reject.db")
    cand = _cand(cand_id="cand_rej", key="swarm.coder/orig")
    assert stage_candidate(conn, cand, now=NOW_ISO) is True
    conn.execute(
        "UPDATE memlife_candidates SET status = 'rejected' WHERE id = ?",
        ("cand_rej",),
    )
    conn.commit()

    # Different key so the key-level decided gate does not short-circuit —
    # pure ON CONFLICT(id) behaviour is what we are proving.
    other = _cand(
        cand_id="cand_rej",
        key="swarm.coder/other-key",
        claim="A different claim sharing only the id primary key",
    )
    assert stage_candidate(conn, other, now="2026-07-30T03:00:00Z") is False
    conn.commit()

    row = conn.execute(
        "SELECT status, key FROM memlife_candidates WHERE id = ?",
        ("cand_rej",),
    ).fetchone()
    n_stage = conn.execute(
        "SELECT COUNT(*) AS n FROM memlife_decisions "
        "WHERE candidate_id = ? AND action = 'stage'",
        ("cand_rej",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["status"] == "rejected"
    assert row["key"] == "swarm.coder/orig"  # not overwritten
    assert int(n_stage["n"]) == 1


def test_stage_decision_only_on_real_insert(tmp_path: Path) -> None:
    """Decision row is appended only when the candidate INSERT added a row.

    Counterfeit: logging stage even when rowcount != 1 doubles the decision log
    on a no-op conflict.
    """
    conn = _open_migrated(tmp_path, "decision_once.db")
    cand = _cand(cand_id="cand_once", key="swarm.coder/once")
    assert stage_candidate(conn, cand, now=NOW_ISO) is True
    assert stage_candidate(conn, cand, now="2026-07-30T03:00:00Z") is False
    conn.commit()

    rows = conn.execute(
        "SELECT action FROM memlife_decisions WHERE candidate_id = ?",
        ("cand_once",),
    ).fetchall()
    conn.close()
    assert [r["action"] for r in rows] == ["stage"]


def test_column_mapping_evidence_and_cluster_size(tmp_path: Path) -> None:
    """evidence_ids_json and cluster_size come from the candidate, not defaults.

    Counterfeit: evidence_ids_json='[]' and cluster_size=999 stay green without
    this assertion.
    """
    conn = _open_migrated(tmp_path, "mapping.db")
    cand = _cand(
        cand_id="cand_map",
        key="swarm.coder/map",
        evidence_ids=["ev_alpha", "ev_beta"],
        cluster_size=7,
    )
    assert stage_candidate(conn, cand, now=NOW_ISO) is True
    conn.commit()
    row = conn.execute(
        "SELECT evidence_ids_json, cluster_size FROM memlife_candidates "
        "WHERE id = ?",
        ("cand_map",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert json.loads(row["evidence_ids_json"]) == ["ev_alpha", "ev_beta"]
    assert int(row["cluster_size"]) == 7


def test_stage_decision_uses_candidate_actor_reason(tmp_path: Path) -> None:
    """Stage decision actor/reason come from the candidate's staging decision.

    Counterfeit: ``return "counterfeit", ""`` in _staging_actor_reason.
    """
    conn = _open_migrated(tmp_path, "actor.db")
    cand = _cand(
        cand_id="cand_act",
        key="swarm.coder/act",
        actor="pipeline-unit",
        reason="cluster survived prefilter",
    )
    assert stage_candidate(conn, cand, now=NOW_ISO) is True
    conn.commit()
    row = conn.execute(
        "SELECT actor, reason FROM memlife_decisions WHERE candidate_id = ?",
        ("cand_act",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["actor"] == "pipeline-unit"
    assert row["reason"] == "cluster survived prefilter"


def test_filesystem_failure_rolls_back_db_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial success is failure: FS stage error deletes the just-inserted row.

    Counterfeit: ``pass`` instead of delete_staged_candidate leaves an orphan
    queue row with no backing filesystem candidate.
    """
    from omniagentos.memlife.lifecycle import Lifecycle

    root = tmp_path / "memlife_store"
    MemlifeStore(root).ensure_layout()
    _seed_two_events(root)

    db_path = str(tmp_path / "rollback.db")
    migrate(db_path)
    store = SqliteStore(db_path)

    def _fs_boom(self: Any, *a: Any, **k: Any) -> Any:
        raise OSError("simulated filesystem full")

    monkeypatch.setattr(Lifecycle, "stage", _fs_boom)

    report = run_dream_cycle(root, now=NOW, db_conn=store._connection)
    assert report.status is CycleStatus.FAILED

    with store._lock:
        n = store._connection.execute(
            "SELECT COUNT(*) AS n FROM memlife_candidates"
        ).fetchone()
    assert int(n["n"]) == 0, f"DB row must roll back after FS failure: {n['n']}"


def test_non_staged_filesystem_candidate_is_not_overwritten(
    tmp_path: Path,
) -> None:
    """Existing non-STAGED filesystem candidate must not be restaged over.

    Counterfeit: disable ``_fs_candidate_is_decided`` and Lifecycle.stage
    overwrites REJECTED back to STAGED with a fresh single decision.
    """
    from omniagentos.memlife.lifecycle import Lifecycle

    root = tmp_path / "memlife_store"
    store = MemlifeStore(root)
    store.ensure_layout()
    _seed_two_events(root)

    # First cycle filesystem-only so we know the deterministic ids on disk.
    report1 = run_dream_cycle(root, now=NOW, db_conn=None)
    assert report1.status is CycleStatus.COMPLETED
    assert report1.staged == 2

    pending = store.list_pending()
    assert len(pending) == 2
    target = pending[0]
    Lifecycle(store).reject(target.id, actor="owner", reason="not general")
    rejected = store.load_candidate(target.id)
    assert rejected.status is CandidateStatus.REJECTED
    decision_len_before = len(rejected.decisions)

    # Re-seed same evidence (same ids) so the pipeline would re-encounter them.
    _seed_two_events(root)
    db_path = str(tmp_path / "fs_guard.db")
    migrate(db_path)
    sql = SqliteStore(db_path)
    report2 = run_dream_cycle(root, now=NOW, db_conn=sql._connection)
    # Guard skips the rejected id; the other may stage into DB.
    assert report2.status is CycleStatus.COMPLETED

    after = store.load_candidate(target.id)
    assert after.status is CandidateStatus.REJECTED, after.status
    assert len(after.decisions) == decision_len_before, after.decisions


def test_production_tick_writes_capture_watermark(
    database: SqliteStore,
    memlife_root: Path,
) -> None:
    """Production path (tick → builtin → run_memlife_dream_cycle) persists wm.

    Counterfeit: ``return`` at the top of ``_write_watermark`` leaves no file.
    """
    ensure_dream_cycle_routine(database)
    _seed_two_events(memlife_root)

    result = tick(database, load_policy(), now=DUE_NOW)
    assert len(result["fired"]) == 1, result

    wm = memlife_root / "episodic" / "capture_watermark.json"
    assert wm.is_file(), "production tick must write capture_watermark.json"
    data = json.loads(wm.read_text(encoding="utf-8"))
    assert data.get("last_captured_ts"), data
    # Helper agrees with the file the production path wrote.
    assert read_capture_watermark(memlife_root) == data["last_captured_ts"]


def test_production_tick_reads_persisted_watermark(
    database: SqliteStore,
    memlife_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production path passes the on-disk watermark into capture as ``since``.

    Counterfeit: ``return None`` in ``_read_watermark`` disconnects the file
    from the cycle and always full-captures.
    """
    import omniagentos.memlife.dream as dream_mod

    ensure_dream_cycle_routine(database)
    _seed_two_events(memlife_root)
    write_capture_watermark(memlife_root, "2026-07-20T00:00:00Z")

    seen: dict[str, Any] = {}

    def _spy(db_path: Any, since: Any = None) -> list[Any]:
        seen["since"] = since
        return []

    monkeypatch.setattr(dream_mod, "capture_events", _spy)

    result = tick(database, load_policy(), now=DUE_NOW)
    assert len(result["fired"]) == 1, result
    assert seen.get("since") == "2026-07-20T00:00:00Z", seen
