"""T3.3: the longhaul lane's cross-lane scope-lock wiring.

Four properties, in the order they matter:

1. **OFF IS BYTE-FOR-BYTE.** ``scope_locks_mode()`` defaults to ``off`` and the
   whole mechanism ships dark. With it off this lane must resolve no realm,
   construct no lock store, write no lock row and no waiter row, and produce the
   same attempt rows and the same exceptions it always did. That is the single
   most important acceptance criterion, so it is tested three ways: an A/B of the
   attempt rows with and without a ``working_dir``, a booby-trapped
   ``PathLockStore``/``realm_of`` that explodes if either is touched, and an
   emptiness assertion on both new tables.
2. **ENFORCE REFUSES, AND THE LANE PARKS RATHER THAN EXECUTING.** A refused
   claim must leave NO attempt row and must never reach a supervisor.
3. **RELEASE HAPPENS ON EVERY TERMINAL PATH**, including the ones that are not
   clean exits: an insert that fails after the claim was granted, a duplicate
   terminal delivery whose CAS loses, and a process that dies holding a lease.
4. **THE FENCE REJECTS A STALE GENERATION**, and the lane parks instead of
   re-entering the fleet next to whoever displaced it.

``test_park_state_value_is_the_one_the_schema_accepts`` is load-bearing
documentation, not a formality: it pins the migration-043 CHECK constraint that
is the entire reason this lane parks as ``waiting_capacity`` with
``longhaul_json.park_reason='scope'`` instead of the ``'waiting_scope'`` the
design called for.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.longhaul import store as store_module
from omniagentos.longhaul import workbook
from omniagentos.longhaul.engine import LonghaulEngine
from omniagentos.longhaul.store import (
    SCOPE_PARK_REASON,
    SCOPE_PARK_STATE,
    LonghaulStore,
    ScopeUnavailable,
)
from omniagentos.scope import config as scope_config
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim
from omniagentos.scope.paths import realm_of


def async_test(function: Any) -> Any:
    @functools.wraps(function)
    def run(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(function(*args, **kwargs))

    return run


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = str(tmp_path / "longhaul.db")
    migrate(path)
    monkeypatch.setattr(workbook, "WORKBOOK_ROOT", tmp_path / "workbooks")
    # Never inherit the host's mode: every test below states the one it means.
    monkeypatch.delenv(scope_config.SCOPE_LOCKS_ENV, raising=False)
    return path


def _mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv(scope_config.SCOPE_LOCKS_ENV, mode)


class FakeSupervisor:
    """Records spawns. If this list is non-empty after a refused claim, the lane
    executed work it had been told it did not own — the failure this exists to
    catch."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"ses_fake_{len(self.calls)}"


def _cfg(supervisor: FakeSupervisor, working_dir: Path, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "max_sessions": 8,
        "default_cooldown_s": 3600,
        "cross_harness_fallback": False,
        "static_fallback_order": [{"harness": "cli-claude", "model": "opus"}],
        "review": {"enabled": False, "deny_respawns": 2},
        "spawn_grace_s": 0,
        "working_dir": str(working_dir),
        "_supervisor": supervisor,
    }
    cfg.update(overrides)
    return cfg


def _task(
    store: LonghaulStore,
    task_id: str,
    *,
    status: str = "pending",
    created_at: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    now = created_at or utc_now_iso()
    store._connection.execute(
        "INSERT INTO board_tasks "
        "(id,title,description,status,created_at,updated_at,lane,longhaul_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            task_id,
            f"Task {task_id}",
            "Implement the requested code.\n\nAcceptance criteria:\n- Tests pass",
            status,
            now,
            now,
            "longhaul",
            json.dumps(state or {}),
        ),
    )
    store._connection.commit()


def _account(store: LonghaulStore, account_id: str) -> None:
    now = utc_now_iso()
    store._connection.execute(
        "INSERT INTO claude_accounts "
        "(id,label,enabled,status,created_at,updated_at,cooldown_until) "
        "VALUES (?,?,?,?,?,?,?)",
        (account_id, account_id, 1, "ok", now, now, None),
    )
    store._connection.commit()


def _live_locks(store: LonghaulStore) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store._connection.execute(
            "SELECT * FROM resource_locks WHERE released_at IS NULL ORDER BY id"
        ).fetchall()
    ]


def _all_locks(store: LonghaulStore) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store._connection.execute("SELECT * FROM resource_locks ORDER BY id").fetchall()
    ]


def _waiters(store: LonghaulStore) -> list[dict[str, Any]]:
    return [
        dict(row) for row in store._connection.execute("SELECT * FROM scope_waiters").fetchall()
    ]


def _attempt_rows(store: LonghaulStore) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store._connection.execute(
            "SELECT * FROM task_sessions ORDER BY board_task_id, seq"
        ).fetchall()
    ]


def _volatile_stripped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attempt rows with only the id and the two timestamps removed.

    Everything else — seq, harness, model, account_id, session_id, end_reason,
    detail — has to match exactly, because those are the columns the wiring
    could plausibly have disturbed.
    """
    stripped = []
    for row in rows:
        copy = dict(row)
        copy.pop("id")
        copy.pop("started_at")
        copy.pop("ended_at")
        stripped.append(copy)
    return stripped


def _board(store: LonghaulStore, task_id: str) -> dict[str, Any]:
    return dict(
        store._connection.execute("SELECT * FROM board_tasks WHERE id = ?", (task_id,)).fetchone()
    )


def _actions(store: LonghaulStore, task_id: str) -> list[str]:
    return [
        str(row["action"])
        for row in store._connection.execute(
            "SELECT action FROM events WHERE target_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    ]


def _rival_lock(
    store: LonghaulStore,
    realm: str,
    path: str,
    *,
    kind: str = "file",
    holder_id: str = "swt_rival",
) -> str:
    """A live lock held by ANOTHER lane, so the conflict is a real cross-lane one.

    Deliberately a swarm holder rather than a second longhaul attempt: the
    contention this phase exists to prevent is between lanes, and using a rival
    lane also proves the exclusion is not an artifact of the longhaul-only path.
    """
    locks = PathLockStore(store)
    result = locks.try_acquire_scope(
        [ScopeClaim.for_path(realm, path, kind=kind)],  # type: ignore[arg-type]
        LockHolder(kind="swarm_task", id=holder_id, lane="swarm"),
    )
    assert result.status == "granted", result
    return result.lock_ids[0]


# ---------------------------------------------------------------------------
# 1. OFF IS BYTE-FOR-BYTE
# ---------------------------------------------------------------------------


def test_the_lane_ships_shadow_by_default(db_path: str) -> None:
    """No env var: Grok product config ships scope locks in shadow mode.

    Evidence: configs/parallelism.yaml scope_locks.mode = shadow (Grok product
    divergence for measurement soak). Explicit OMNIAGENTOS_SCOPE_LOCKS=off still
    disables writes (covered by off-mode tests below).
    """
    assert scope_config.scope_locks_mode() == "shadow"
    assert scope_config.scope_locks_enabled() is True
    assert scope_config.scope_locks_enforcing() is False


def test_off_writes_no_lock_and_no_waiter_rows(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "off")
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_one")
        attempt = store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))
        assert _live_locks(store) == []
        assert _all_locks(store) == []
        assert _waiters(store) == []

        assert store.close_attempt(attempt["id"], "completed") is True
        assert _all_locks(store) == []
        assert _waiters(store) == []
    finally:
        store.close()


def test_off_produces_identical_attempt_rows_with_and_without_working_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A/B on two databases: passing a working_dir must change nothing at all."""
    _mode(monkeypatch, "off")
    monkeypatch.setattr(workbook, "WORKBOOK_ROOT", tmp_path / "workbooks")

    def run(name: str, working_dir: str | None) -> list[dict[str, Any]]:
        path = str(tmp_path / f"{name}.db")
        migrate(path)
        store = LonghaulStore(path)
        try:
            _task(store, "btk_one")
            first = store.open_attempt(
                "btk_one",
                "cli-claude",
                "opus",
                account_id="acct_a",
                working_dir=working_dir,
            )
            assert store.close_attempt(first["id"], "crashed", "boom") is True
            second = store.open_attempt(
                "btk_one", "cli-codex", "gpt", session_id=None, working_dir=working_dir
            )
            assert store.close_attempt(second["id"], "completed") is True
            assert _all_locks(store) == []
            return _volatile_stripped(_attempt_rows(store))
        finally:
            store.close()

    assert run("with", str(tmp_path)) == run("without", None)


def test_off_never_resolves_a_realm_or_builds_a_lock_store(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Booby-trap both seams.

    ``realm_of`` shells out to ``git rev-parse`` and ``PathLockStore`` opens
    transactions; "byte-for-byte unchanged" has to mean neither one runs, not
    merely that they run and decline. Exploding stand-ins are the only way to
    assert that positively.
    """
    _mode(monkeypatch, "off")

    def exploding_realm(path: str) -> str:
        raise AssertionError(f"realm_of called with locks off: {path!r}")

    class ExplodingLocks:
        def __init__(self, store: Any) -> None:
            raise AssertionError("PathLockStore constructed with locks off")

    monkeypatch.setattr(store_module, "realm_of", exploding_realm)
    monkeypatch.setattr(store_module, "PathLockStore", ExplodingLocks)

    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_one")
        attempt = store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))
        assert store.close_attempt(attempt["id"], "completed") is True
        assert store.close_attempt(attempt["id"], "completed") is False
        assert store._path_locks is None
    finally:
        store.close()


def test_off_preserves_the_existing_refusals(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two exceptions open_attempt already raised still raise, unchanged."""
    _mode(monkeypatch, "off")
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_live")
        _task(store, "btk_done", status="done")

        store.open_attempt("btk_live", "cli-claude", "opus", working_dir=str(tmp_path))
        with pytest.raises(RuntimeError, match="already has an open attempt"):
            store.open_attempt("btk_live", "cli-claude", "opus", working_dir=str(tmp_path))
        with pytest.raises(ValueError, match="terminal/archived"):
            store.open_attempt("btk_done", "cli-claude", "opus", working_dir=str(tmp_path))
        assert _all_locks(store) == []
    finally:
        store.close()


@async_test
async def test_off_dispatch_executes_exactly_as_before(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "off")
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    try:
        _account(store, "acct_a")
        _task(store, "btk_one")
        engine = LonghaulEngine(store, _cfg(supervisor, tmp_path), db_path)
        await engine.dispatch("btk_one")

        assert len(supervisor.calls) == 1
        board = _board(store, "btk_one")
        assert board["status"] == "in_progress"
        assert board["park_state"] is None
        assert len(_attempt_rows(store)) == 1
        assert _all_locks(store) == []
        assert _waiters(store) == []
        assert "longhaul.waiting_scope" not in _actions(store, "btk_one")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. ENFORCE REFUSES, AND THE LANE PARKS RATHER THAN EXECUTING
# ---------------------------------------------------------------------------


def test_enforce_claims_the_whole_working_dir_realm(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1's honest claim: kind='root' on '.', i.e. the entire project realm."""
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_one")
        attempt = store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))
        locks = _live_locks(store)
        assert len(locks) == 1
        held = locks[0]
        assert held["realm"] == realm_of(str(tmp_path))
        assert held["rel_path"] == "."
        assert held["depth"] == 0
        assert held["kind"] == "root"
        assert held["lane"] == "longhaul"
        assert held["holder_kind"] == "attempt"
        assert held["holder_id"] == attempt["id"]
        assert held["holder_generation"] == 0
        assert held["purpose"] == "work"
    finally:
        store.close()


def test_enforce_refuses_a_conflicting_claim_and_opens_no_attempt(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rival's FILE claim inside the realm blocks longhaul's ROOT claim.

    Using a nested file rather than an identical root proves the refusal comes
    from the containment algebra (scope.conflict), not merely from the partial
    unique index.
    """
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    try:
        realm = realm_of(str(tmp_path))
        assert realm is not None
        rival_id = _rival_lock(store, realm, "src/app.py")
        _task(store, "btk_one")

        with pytest.raises(ScopeUnavailable) as caught:
            store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))
        assert caught.value.status == "blocked"
        assert caught.value.realm == realm
        assert caught.value.blocked_on == rival_id

        # The refusal must be total: no attempt row, and no lock of ours.
        assert _attempt_rows(store) == []
        assert [lock["id"] for lock in _live_locks(store)] == [rival_id]
    finally:
        store.close()


@async_test
async def test_enforce_engine_parks_instead_of_executing_and_wakes_fifo(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    try:
        realm = realm_of(str(tmp_path))
        assert realm is not None
        _rival_lock(store, realm, "src/app.py")
        _account(store, "acct_a")
        _task(store, "btk_one")
        engine = LonghaulEngine(store, _cfg(supervisor, tmp_path), db_path)

        await engine.dispatch("btk_one")

        # Parked, not executed. The empty spawn list is the whole point.
        assert supervisor.calls == []
        assert _attempt_rows(store) == []
        board = _board(store, "btk_one")
        assert board["park_state"] == SCOPE_PARK_STATE
        assert board["status"] == "in_progress"
        state = json.loads(board["longhaul_json"])
        assert state["park_reason"] == SCOPE_PARK_REASON
        assert state["scope_status"] == "blocked"
        assert state["scope_realm"] == realm
        assert state["phase"] == "parked"
        assert "longhaul.waiting_scope" in _actions(store, "btk_one")

        # The durable park IS the queue: the FIFO wake finds it.
        assert store.next_waiting_scope() == "btk_one"

        # Give the realm back; the woken task now runs.
        PathLockStore(store).release_scope("swarm_task", "swt_rival", 0)
        await engine.dispatch("btk_one")
        assert len(supervisor.calls) == 1
        assert _board(store, "btk_one")["park_state"] is None
        assert len(_live_locks(store)) == 1
    finally:
        store.close()


def test_next_waiting_scope_is_lane_scoped_oldest_first(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    try:
        parked = {"park_reason": SCOPE_PARK_REASON}
        _task(store, "btk_old", created_at="2026-01-01T00:00:00Z", state=parked)
        _task(store, "btk_new", created_at="2026-01-02T00:00:00Z", state=parked)
        _task(store, "btk_capacity", created_at="2025-01-01T00:00:00Z")
        for task_id in ("btk_old", "btk_new", "btk_capacity"):
            store.set_park_state(task_id, SCOPE_PARK_STATE)
        # A card in another lane parked on the same value must never be handed
        # back to the longhaul dispatcher.
        _task(store, "btk_swarm", created_at="2024-01-01T00:00:00Z", state=parked)
        store.set_lane("btk_swarm", "fast")
        store.set_park_state("btk_swarm", SCOPE_PARK_STATE)

        assert store.next_waiting_scope() == "btk_old"

        # An archived card cannot run, so the single peek must skip it.
        store._connection.execute(
            "UPDATE board_tasks SET archived_at = ? WHERE id = 'btk_old'",
            (utc_now_iso(),),
        )
        store._connection.commit()
        assert store.next_waiting_scope() == "btk_new"
    finally:
        store.close()


def test_shadow_records_the_contention_but_never_refuses(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The soak mode: rows ARE written, the conflict is observed, nobody parks."""
    _mode(monkeypatch, "shadow")
    store = LonghaulStore(db_path)
    try:
        realm = realm_of(str(tmp_path))
        assert realm is not None
        rival_id = _rival_lock(store, realm, "src/app.py")
        _task(store, "btk_one")

        attempt = store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))
        holders = {lock["holder_id"] for lock in _live_locks(store)}
        assert holders == {"swt_rival", attempt["id"]}
        assert rival_id in {lock["id"] for lock in _live_locks(store)}
    finally:
        store.close()


def test_the_wiring_hazard_fails_by_name(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Touching the lock store from inside a LonghaulStore transaction is refused.

    PathLockStore opens its own BEGIN IMMEDIATE and the store's mutex is an
    RLock, so a nested call sails past the mutex and then dies on sqlite's
    "cannot start a transaction within a transaction" — under contention, in the
    path hardest to reproduce. The guard converts that into a named failure at
    the seam that caused it, and this test is what keeps the guard honest.
    """
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    try:
        with store._lock:
            store._begin()
            try:
                with pytest.raises(RuntimeError, match="inside an open LonghaulStore"):
                    store._acquire_attempt_scope("tks_nested", str(tmp_path))
                with pytest.raises(RuntimeError, match="inside an open LonghaulStore"):
                    store._release_attempt_scope("tks_nested")
            finally:
                store._rollback()

        # The real call sites are outside the transaction, so they work.
        _task(store, "btk_one")
        attempt = store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))
        assert store.close_attempt(attempt["id"], "completed") is True
    finally:
        store.close()


def test_park_state_value_is_the_one_the_schema_accepts(db_path: str) -> None:
    """WHY THIS LANE DOES NOT PARK AS ``'waiting_scope'``.

    Migration 043 pinned ``board_tasks.park_state`` to a three-value CHECK, and
    SQLite cannot widen a CHECK without rebuilding the table. Writing
    ``'waiting_scope'`` raises IntegrityError out of ``_transition`` — in enforce
    mode only, which is the worst possible place to discover it. Hence
    ``SCOPE_PARK_STATE = 'waiting_capacity'`` plus a ``park_reason``
    discriminator.

    When a later migration widens the CHECK, this test fails and points at the
    one constant to re-point.
    """
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_one")
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                "UPDATE board_tasks SET park_state = 'waiting_scope' WHERE id = ?",
                ("btk_one",),
            )
        assert store.set_park_state("btk_one", SCOPE_PARK_STATE) is True
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. RELEASE HAPPENS ON EVERY TERMINAL PATH
# ---------------------------------------------------------------------------


def test_close_attempt_releases_and_the_realm_is_reusable(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_one")
        attempt = store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))
        assert len(_live_locks(store)) == 1

        assert store.close_attempt(attempt["id"], "crashed", "boom") is True
        assert _live_locks(store) == []
        assert _all_locks(store)[0]["released_at"] is not None

        successor = store.open_attempt("btk_one", "cli-codex", "gpt", working_dir=str(tmp_path))
        assert successor["seq"] == 1
        assert len(_live_locks(store)) == 1
    finally:
        store.close()


def test_release_still_happens_when_the_close_cas_loses(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash window between the CAS commit and the release.

    A process that dies right after the CAS leaves the row closed and the locks
    held. The release is therefore unconditional: the duplicate terminal
    delivery that loses the CAS still gives the realm back.
    """
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_one")
        attempt = store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))
        # Close the row behind the store's back == the crash.
        store._connection.execute(
            "UPDATE task_sessions SET ended_at = ?, end_reason = 'crashed' WHERE id = ?",
            (utc_now_iso(), attempt["id"]),
        )
        store._connection.commit()
        assert len(_live_locks(store)) == 1

        assert store.close_attempt(attempt["id"], "crashed", "late") is False
        assert _live_locks(store) == []
    finally:
        store.close()


def test_a_granted_claim_is_released_when_the_attempt_insert_fails(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both non-crash failure paths inside open_attempt give the realm back.

    Without this the lane would hold a whole project hostage for a full TTL
    because of a task that was already done, or a duplicate dispatch.
    """
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_done", status="done")
        with pytest.raises(ValueError, match="terminal/archived"):
            store.open_attempt("btk_done", "cli-claude", "opus", working_dir=str(tmp_path))
        assert _live_locks(store) == []

        # Second path: a live attempt already exists. The first attempt here
        # takes NO lock (no working_dir), so the realm is free when the second
        # call claims it and then trips the live-attempt guard.
        _task(store, "btk_live")
        store.open_attempt("btk_live", "cli-claude", "opus")
        with pytest.raises(RuntimeError, match="already has an open attempt"):
            store.open_attempt("btk_live", "cli-claude", "opus", working_dir=str(tmp_path))
        assert _live_locks(store) == []

        # And the realm really is usable afterwards.
        _task(store, "btk_next")
        store.open_attempt("btk_next", "cli-claude", "opus", working_dir=str(tmp_path))
        assert len(_live_locks(store)) == 1
    finally:
        store.close()


def test_a_failing_release_owns_neither_the_cas_nor_the_original_error(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup never owns the outcome of the call it cleans up after.

    If a lock-store failure could propagate, ``close_attempt`` would report a
    won CAS as a raised exception — inviting the duplicate terminal delivery the
    CAS exists to prevent — and ``open_attempt`` would bury "this task is
    terminal" under a lock error. The lease TTL is what makes swallowing safe.
    """
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_one")
        _task(store, "btk_done", status="done")
        attempt = store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))

        def broken(*args: Any, **kwargs: Any) -> int:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "_release_attempt_scope", broken)

        # The CAS was won, and the broken release did not turn that into a raise.
        assert store.close_attempt(attempt["id"], "completed") is True

        # A SECOND realm, because the failed release means the first one is
        # still held — legitimately, and that is what the TTL is for. Here the
        # claim is granted and the attempt insert is what fails, so the error
        # the caller sees must be the real one.
        other = tmp_path / "other-project"
        other.mkdir()
        with pytest.raises(ValueError, match="terminal/archived"):
            store.open_attempt("btk_done", "cli-claude", "opus", working_dir=str(other))
    finally:
        store.close()


def test_a_lapsed_lease_does_not_wedge_the_realm(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one failure the release path cannot cover: a process that just dies.

    Nothing runs to release the lock, so the TTL is the whole safety net. Once
    the lease lapses the next claimant takes the realm.
    """
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    try:
        _task(store, "btk_dead")
        _task(store, "btk_next")
        store.open_attempt("btk_dead", "cli-claude", "opus", working_dir=str(tmp_path))

        with pytest.raises(ScopeUnavailable):
            store.open_attempt("btk_next", "cli-claude", "opus", working_dir=str(tmp_path))

        # The holder's process is gone; only its lease expiring frees the realm.
        store._connection.execute("UPDATE resource_locks SET expires_at = '2000-01-01T00:00:00Z'")
        store._connection.commit()

        revived = store.open_attempt("btk_next", "cli-claude", "opus", working_dir=str(tmp_path))
        assert revived["board_task_id"] == "btk_next"
        assert [lock["holder_id"] for lock in _live_locks(store)] == [revived["id"]]
    finally:
        store.close()


@async_test
async def test_tick_renews_a_live_lease_and_throttles_itself(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without renewal the claim is decorative: 90s lease, hours-long attempts.

    Also asserts the throttle, because tick() rides the supervisor's sub-second
    poll loop and an unthrottled heartbeat would push one UPDATE per live
    attempt per poll through the single process-wide writer lock.
    """
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    try:
        _account(store, "acct_a")
        _task(store, "btk_one")
        engine = LonghaulEngine(store, _cfg(supervisor, tmp_path), db_path)
        await engine.dispatch("btk_one")
        assert len(_live_locks(store)) == 1

        # Wind the lease down to a few seconds out — still LIVE, because an
        # already-expired lock is deliberately not renewable (another holder may
        # legitimately have taken it).
        soon = (datetime.now(UTC) + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        store._connection.execute("UPDATE resource_locks SET expires_at = ?", (soon,))
        store._connection.commit()

        renewals: list[str] = []
        real = store.renew_attempt_scope
        monkeypatch.setattr(
            store,
            "renew_attempt_scope",
            lambda attempt_id: (renewals.append(attempt_id), real(attempt_id))[1],
        )

        await engine.tick()
        assert len(renewals) == 1
        assert _live_locks(store)[0]["expires_at"] > soon

        # Second tick, same second: throttled to one renewal per TTL/3.
        await engine.tick()
        assert len(renewals) == 1
    finally:
        store.close()


@async_test
async def test_tick_never_renews_when_locks_are_off(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mode(monkeypatch, "off")
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    try:
        _account(store, "acct_a")
        _task(store, "btk_one")
        engine = LonghaulEngine(store, _cfg(supervisor, tmp_path), db_path)
        await engine.dispatch("btk_one")

        def explode(attempt_id: str) -> bool:
            raise AssertionError("renewed a lease with locks off")

        monkeypatch.setattr(store, "renew_attempt_scope", explode)
        await engine.tick()
        assert engine._scope_renewed_at == {}
        assert _all_locks(store) == []
    finally:
        store.close()


@async_test
async def test_the_renewal_throttle_is_pruned_to_live_attempts(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is a rate limiter, not state: it must not grow with attempts-ever-seen."""
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    try:
        _account(store, "acct_a")
        _task(store, "btk_one")
        engine = LonghaulEngine(store, _cfg(supervisor, tmp_path), db_path)
        await engine.dispatch("btk_one")
        attempt = store.current_attempt("btk_one")
        assert attempt is not None

        await engine.tick()
        assert set(engine._scope_renewed_at) == {attempt["id"]}

        store.close_attempt(str(attempt["id"]), "crashed", "gone")
        # tick redispatches the task, so a NEW attempt replaces the old entry.
        await engine.tick()
        assert attempt["id"] not in engine._scope_renewed_at
        assert len(engine._scope_renewed_at) <= 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 4. THE FENCE REJECTS A STALE GENERATION
# ---------------------------------------------------------------------------


@async_test
async def test_a_stale_generation_is_fenced_and_the_lane_parks(
    db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attempt identity that already carries a HIGHER generation is refused.

    Longhaul always claims at generation 0 and mints a fresh attempt id per
    attempt, so this is unreachable in production — which is exactly why it is
    tested: the wiring must treat ``status='fenced'`` as "do not proceed" rather
    than lumping it in with a granted result. Pinning ``new_id`` is the only way
    to make the store claim under an identity the fence has already seen.
    """
    _mode(monkeypatch, "enforce")
    store = LonghaulStore(db_path)
    supervisor = FakeSupervisor()
    try:
        realm = realm_of(str(tmp_path))
        assert realm is not None

        def pinned(prefix: str) -> str:
            return "tks_pinned" if prefix == "tks" else new_id(prefix)

        monkeypatch.setattr(store_module, "new_id", pinned)

        # Somebody adopted this identity at a higher generation and has since
        # released. The historical maximum is what fences, not the live rows —
        # otherwise a displaced worker re-enters the fleet the moment its
        # adopter's locks lapse.
        locks = PathLockStore(store)
        holder = LockHolder(kind="attempt", id="tks_pinned", lane="longhaul")
        granted = locks.try_acquire_scope(
            [ScopeClaim.for_path(realm, ".", kind="root")], holder, generation=9
        )
        assert granted.status == "granted"
        assert locks.release_scope("attempt", "tks_pinned", 9) == 1
        assert _live_locks(store) == []

        _task(store, "btk_one")
        with pytest.raises(ScopeUnavailable) as caught:
            store.open_attempt("btk_one", "cli-claude", "opus", working_dir=str(tmp_path))
        assert caught.value.status == "fenced"
        assert _attempt_rows(store) == []

        # And the engine parks on it rather than crashing dispatch or running.
        _account(store, "acct_a")
        engine = LonghaulEngine(store, _cfg(supervisor, tmp_path), db_path)
        await engine.dispatch("btk_one")
        assert supervisor.calls == []
        board = _board(store, "btk_one")
        assert board["park_state"] == SCOPE_PARK_STATE
        assert json.loads(board["longhaul_json"])["scope_status"] == "fenced"
    finally:
        store.close()
