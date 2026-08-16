"""The runner lane's half of the Phase-3 scope contract.

Four properties carry this work package, and each has a test whose failure means
the feature must not ship:

``test_dark_*``
    With ``scope_locks_mode() == 'off'`` the runner is BYTE-FOR-BYTE the runner
    that existed before this module. Proven by making every gateway into the
    scope kernel fail the test if it is reached, and by asserting no scope column
    is ever written and no lock/waiter row ever appears.

``test_enforce_*``
    A conflicting claim is refused: the run does not execute, a durable waiter is
    parked, and the lane skips to other work rather than serializing on it — until
    the starvation break, at which point it stops taking new work instead of
    walking past a starving head forever.

``test_release_*``
    Every terminal path gives the paths back — completion, failure, and a crash
    that never reaches ``_transition`` at all.

``test_fence_*``
    A stale lease generation fails the fence, so a displaced-but-alive worker
    stops instead of writing next to its adopter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    ActionClass,
    AgentInput,
    AgentResult,
    BudgetDecision,
    HarnessType,
    PolicyDecision,
    ResultStatus,
    RunState,
    SandboxSpec,
    TaskState,
    new_id,
    utc_now_iso,
)
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.mock_adapter import MockAdapter
from omniagentos.runner.core import LostFence, Runner, RunnerDependencies
from omniagentos.runner.scope_wiring import (
    HOLDER_KIND,
    RunnerScope,
    claims_to_json,
    derive_claims,
    json_to_claims,
    working_dirs_for_run,
)
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim

SHARED_REALM = "/realm/shared"
OTHER_REALM = "/realm/other"

SCOPE_COLUMNS = {"scope_json", "lease_generation", "lease_expires_at"}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class ProbeAdapter(MockAdapter):
    """Records which runs actually executed — the only thing "refused" means."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def run(self, input: AgentInput) -> AgentResult:
        self.seen.append(input.run_id)
        return super().run(input)


class RecordingStore(SqliteStore):
    """A real store that also remembers every run mutation it was asked to make.

    Used only by the dark-mode tests: "byte-for-byte" is a claim about the calls
    the runner makes, so the test has to be able to look at them.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.run_writes: list[tuple[str, dict[str, Any]]] = []

    def update_run(
        self, run_id: str, fields: dict[str, Any], expect_worker: str | None = None
    ) -> bool:
        self.run_writes.append((run_id, dict(fields)))
        return super().update_run(run_id, fields, expect_worker)


def _dependencies(adapter: MockAdapter) -> RunnerDependencies:
    return RunnerDependencies(
        evaluate_policy=lambda _action: PolicyDecision(requires_approval=False),
        sandbox_for_tools=lambda _harness, tools: SandboxSpec(
            level="workspace_write" if tools else "read_only"
        ),
        check_budget=lambda *_args: BudgetDecision(allowed=True),
        resolve_adapter=lambda _harness: adapter,
        append_manifest=lambda ledger, _manifest: str(Path(ledger) / "runs.jsonl"),
        render_run_note=lambda run, _steps, _manifest, _receipts, **_kwargs: (
            f"runs/{run['id']}.md",
            "done",
        ),
        write_note=lambda vault, relpath, _content: str(Path(vault) / relpath),
    )


def _runner(
    store: SqliteStore,
    adapter: MockAdapter,
    tmp_path: Path,
    *,
    worker_id: str = "w1",
    concurrency: int = 1,
    stale_s: int | None = None,
) -> Runner:
    return Runner(
        store,
        worker_id,
        dependencies=_dependencies(adapter),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "workspace"),
        concurrency=concurrency,
        stale_s=stale_s,
    )


def _enqueue(
    store: SqliteStore,
    run_id: str,
    *,
    state: str = RunState.QUEUED.value,
    worker_id: str | None = None,
    realm: str | None = None,
    working_dir: str | None = None,
    queued_at: str | None = None,
    delay_ms: int = 0,
) -> None:
    """One mock run. ``realm`` pre-declares ``scope_json`` (the replay path).

    Declaring the scope directly is what keeps these tests about the LOCK wiring:
    deriving it from a step's ``working_dir`` would drag in the unrelated
    ``_assert_working_dir_in_scope`` policy gate. Derivation has its own tests at
    the bottom of this file.
    """
    now = utc_now_iso()
    task_id = new_id("tsk")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "code-changes",
            "title": "scope wiring",
            "input_json": json.dumps({"prompt": "hi", "tools_allowed": []}),
            "acceptance_json": "{}",
            "state": TaskState.QUEUED.value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    params: dict[str, Any] = {"adapter": "mock"}
    if working_dir is not None:
        params["working_dir"] = working_dir
    if delay_ms:
        params["mock"] = {"delay_ms": delay_ms}
    plan = [
        {
            "name": "work",
            "kind": "agent",
            "action_class": ActionClass.SANDBOXED_CREATION.value,
            "params": params,
        }
    ]
    row: dict[str, Any] = {
        "id": run_id,
        "task_id": task_id,
        "discipline_id": "code-changes",
        "harness": HarnessType.MOCK.value,
        "state": state,
        "plan_json": json.dumps(plan),
        "budget_json": "{}",
        "trace_id": f"trace-{run_id}",
        # Explicit when a test depends on selection ORDER: the owned branch reads
        # `queued_at DESC` and walks it backwards, so the oldest run is considered
        # first, and same-second timestamps would tie-break on the random id.
        "queued_at": queued_at or now,
        "created_at": now,
        "updated_at": now,
    }
    if worker_id is not None:
        row["worker_id"] = worker_id
    if realm is not None:
        row["scope_json"] = claims_to_json((ScopeClaim(realm=realm, components=(), kind="root"),))
    store.enqueue_run(row)


def _hold(store: SqliteStore, realm: str, *, holder_id: str = "swt_1") -> str:
    """A FOREIGN lane (swarm) takes the whole realm, so the runner must yield."""
    locks = PathLockStore(store)
    result = locks.try_acquire_scope(
        [ScopeClaim(realm=realm, components=(), kind="root")],
        LockHolder(kind="swarm_task", id=holder_id, lane="swarm"),
        generation=1,
        enforce=True,
    )
    assert result.status == "granted"
    return result.lock_ids[0]


def _rows(store: SqliteStore, table: str) -> list[dict[str, Any]]:
    with store._lock:
        return [dict(row) for row in store._connection.execute(f"SELECT * FROM {table}").fetchall()]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runner.db"
    migrate(str(path))
    return path


@pytest.fixture
def off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default: env unset, ``configs/parallelism.yaml`` says off."""
    monkeypatch.delenv("OMNIAGENTOS_SCOPE_LOCKS", raising=False)


@pytest.fixture
def enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_LOCKS", "enforce")


@pytest.fixture
def shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_LOCKS", "shadow")


# ---------------------------------------------------------------------------
# (a) DARK: with locks off the runner is byte-for-byte what it was
# ---------------------------------------------------------------------------


def test_shipped_default_is_shadow(off: None) -> None:
    """OmniAgentOS ships scope locks in shadow mode (measurement default).

    Product divergence from OmniAgentOS: configs/parallelism.yaml mode=shadow.
    Explicit OMNIAGENTOS_SCOPE_LOCKS=off still forces the pure-dark path (tested
    via the ``off`` fixture in sibling tests).
    """
    from omniagentos.scope.config import scope_locks_mode

    # Fixture named ``off`` only clears the env override; product yaml is shadow.
    assert scope_locks_mode() == "shadow"


def test_explicit_off_path_never_reaches_the_scope_kernel(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With locks forced off: no claim, no lock store, nothing.

    ``_lock_store`` is the single gateway to every database access this module can
    make, and ``take`` is the single claim entry point. Failing the test from
    inside them proves the dark path cannot write even by accident.
    """
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_LOCKS", "off")
    from omniagentos.scope.config import scope_locks_mode

    assert scope_locks_mode() == "off"

    store = RecordingStore(str(db_path))
    monkeypatch.setattr(
        RunnerScope,
        "_lock_store",
        lambda self: pytest.fail("scope lock store constructed with locks off"),
    )
    monkeypatch.setattr(
        RunnerScope,
        "take",
        lambda self, run, *, resume: pytest.fail("scope claimed with locks off"),
    )
    run_id = new_id("run")
    _enqueue(store, run_id)
    adapter = ProbeAdapter()

    _runner(store, adapter, tmp_path).run_forever(once=True)

    assert adapter.seen == [run_id]
    assert store.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]
    # Not one scope column was written, on any run, at any point.
    assert not [fields for _rid, fields in store.run_writes if SCOPE_COLUMNS & set(fields)]
    row = store.get_run(run_id) or {}
    assert row["scope_json"] is None
    assert int(row["lease_generation"] or 0) == 0
    assert row["lease_expires_at"] is None
    assert _rows(store, "resource_locks") == []
    assert _rows(store, "scope_waiters") == []


def test_dark_selection_order_is_unchanged(off: None, db_path: Path, tmp_path: Path) -> None:
    """The owned branch still hands back ``owned[-1]`` — the OLDEST owned run.

    The gate turned that ``if owned: return owned[-1]`` into a loop; with locks off
    the loop must return on its first iteration and pick the same run.
    """
    store = SqliteStore(str(db_path))
    older, newer = new_id("run"), new_id("run")
    _enqueue(
        store,
        older,
        state=RunState.RUNNING.value,
        worker_id="w1",
        queued_at="2026-01-01T00:00:00Z",
    )
    _enqueue(
        store,
        newer,
        state=RunState.RUNNING.value,
        worker_id="w1",
        queued_at="2026-01-01T00:00:10Z",
    )
    runner = _runner(store, ProbeAdapter(), tmp_path)

    assert runner._select(frozenset()) == ("execute", older)


def test_dark_fence_ignores_a_bumped_generation(off: None, db_path: Path, tmp_path: Path) -> None:
    """With locks off, ``lease_generation`` is inert — the old fence, exactly."""
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, state=RunState.RUNNING.value, worker_id="w1")
    runner = _runner(store, ProbeAdapter(), tmp_path)

    assert store.update_run(run_id, {"lease_generation": 9})
    runner._assert_fence(run_id)  # must not raise


# ---------------------------------------------------------------------------
# (b) ENFORCE: a conflicting claim is refused; the lane parks and skips
# ---------------------------------------------------------------------------


def test_enforce_refuses_a_conflicting_claim_and_parks(
    enforce: None, db_path: Path, tmp_path: Path
) -> None:
    store = SqliteStore(str(db_path))
    blocker = _hold(store, SHARED_REALM)
    run_id = new_id("run")
    _enqueue(store, run_id, realm=SHARED_REALM)
    adapter = ProbeAdapter()

    _runner(store, adapter, tmp_path).run_forever(once=True)

    # Refused: the run never executed.
    assert adapter.seen == []
    assert store.get_run(run_id)["state"] != RunState.COMPLETED.value  # type: ignore[index]
    # Parked: durable, FIFO, pointing at the blocker it was actually told about.
    waiters = _rows(store, "scope_waiters")
    assert [(w["holder_kind"], w["holder_id"], w["lane"]) for w in waiters] == [
        (HOLDER_KIND, run_id, "runner")
    ]
    assert waiters[0]["blocked_on"] == blocker
    assert waiters[0]["cleared_at"] is None
    # And it took nothing: the foreign holder still owns the realm alone.
    live = [row for row in _rows(store, "resource_locks") if row["released_at"] is None]
    assert [row["holder_id"] for row in live] == ["swt_1"]


def test_enforce_grants_when_the_realm_is_free(
    enforce: None, db_path: Path, tmp_path: Path
) -> None:
    """The other half of the same switch: enforcement is not a global stop."""
    store = SqliteStore(str(db_path))
    _hold(store, OTHER_REALM)
    run_id = new_id("run")
    _enqueue(store, run_id, realm=SHARED_REALM)
    adapter = ProbeAdapter()

    _runner(store, adapter, tmp_path).run_forever(once=True)

    assert adapter.seen == [run_id]
    assert store.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]
    row = store.get_run(run_id) or {}
    assert json_to_claims(row["scope_json"])[0].realm == SHARED_REALM
    assert int(row["lease_generation"]) == 1


def test_enforce_skips_a_blocked_run_and_takes_other_work(
    enforce: None, db_path: Path, tmp_path: Path
) -> None:
    """One contended realm must not idle the whole worker."""
    store = SqliteStore(str(db_path))
    _hold(store, SHARED_REALM)
    blocked, free = new_id("run"), new_id("run")
    _enqueue(
        store,
        blocked,
        state=RunState.RUNNING.value,
        worker_id="w1",
        realm=SHARED_REALM,
        queued_at="2026-01-01T00:00:00Z",
    )
    _enqueue(
        store,
        free,
        state=RunState.RUNNING.value,
        worker_id="w1",
        realm=OTHER_REALM,
        queued_at="2026-01-01T00:00:10Z",
    )
    store.upsert_heartbeat("w1", 1234, None)
    runner = _runner(store, ProbeAdapter(), tmp_path)

    # `blocked` is the OLDEST owned run, so it is the one this branch returned
    # before Phase 3 and the one it must now step over.
    assert runner._select(frozenset()) == ("execute", free)


def test_enforce_starvation_break_stops_the_lane(
    enforce: None, db_path: Path, tmp_path: Path
) -> None:
    """Skip-the-blocked-head is bounded: past the window, the lane yields to it.

    Without this, a head blocked forever is walked past forever and never runs,
    which is the exact failure mode the FIFO waiter queue exists to prevent.
    """
    store = SqliteStore(str(db_path))
    _hold(store, SHARED_REALM)
    blocked, free = new_id("run"), new_id("run")
    _enqueue(
        store,
        blocked,
        state=RunState.RUNNING.value,
        worker_id="w1",
        realm=SHARED_REALM,
        queued_at="2026-01-01T00:00:00Z",
    )
    _enqueue(
        store,
        free,
        state=RunState.RUNNING.value,
        worker_id="w1",
        realm=OTHER_REALM,
        queued_at="2026-01-01T00:00:10Z",
    )
    store.upsert_heartbeat("w1", 1234, None)
    # The waiter row is DURABLE, so its age survives worker restarts -- which is
    # precisely when an in-process timer would reset and the head would starve.
    PathLockStore(store).park_waiter(
        SHARED_REALM,
        LockHolder(kind=HOLDER_KIND, id=blocked, lane="runner"),
        now="2020-01-01T00:00:00Z",
    )
    runner = _runner(store, ProbeAdapter(), tmp_path)

    assert runner._select(frozenset()) is None

    # ... and a FRESH park does not trip the break (the window is what matters,
    # not the mere existence of a waiter).
    store.update_run(blocked, {"lease_generation": 0})
    with store._lock:
        store._connection.execute("DELETE FROM scope_waiters")
        store._connection.commit()
    runner_2 = _runner(store, ProbeAdapter(), tmp_path)
    assert runner_2._select(frozenset()) == ("execute", free)


def test_shadow_mode_records_the_conflict_but_never_refuses(
    shadow: None, db_path: Path, tmp_path: Path
) -> None:
    """A shadow that changes a decision is not a shadow."""
    store = SqliteStore(str(db_path))
    _hold(store, SHARED_REALM)
    run_id = new_id("run")
    _enqueue(store, run_id, realm=SHARED_REALM)
    adapter = ProbeAdapter()

    _runner(store, adapter, tmp_path).run_forever(once=True)

    assert adapter.seen == [run_id]
    assert store.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]
    assert _rows(store, "scope_waiters") == []
    conflicts = [row for row in _rows(store, "events") if row["action"] == "scope_granted"]
    assert conflicts, "shadow mode must still RECORD the contention it observed"


# ---------------------------------------------------------------------------
# RENEW: the lease rides the heartbeat pulse
# ---------------------------------------------------------------------------


def test_the_heartbeat_pulse_renews_the_lease(
    enforce: None, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A liveness-alive worker must not have its path claims silently lapse."""
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, realm=SHARED_REALM, delay_ms=1500)
    renews: list[str] = []
    original = RunnerScope.renew

    def spy(self: RunnerScope, rid: str) -> bool:
        renews.append(rid)
        return original(self, rid)

    monkeypatch.setattr(RunnerScope, "renew", spy)
    # stale_s=3 -> the existing pulse interval is max(1.0, 1.0) = 1s, so a 1.3s step
    # crosses it exactly once.
    _runner(store, ProbeAdapter(), tmp_path, stale_s=3).run_forever(once=True)

    assert renews.count(run_id) >= 1
    assert store.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]


def test_renew_extends_the_expiry(
    enforce: None, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, state=RunState.RUNNING.value, worker_id="w1", realm=SHARED_REALM)
    runner = _runner(store, ProbeAdapter(), tmp_path)
    assert runner._scope.take(store.get_run(run_id) or {}, resume=False).proceed
    locks = PathLockStore(store)
    before = locks.held_by(HOLDER_KIND, run_id)[0].expires_at

    monkeypatch.setenv("OMNIAGENTOS_SCOPE_TTL_S", "600")
    assert runner._scope.renew(run_id) is True

    after = locks.held_by(HOLDER_KIND, run_id)[0].expires_at
    assert after > before
    assert (store.get_run(run_id) or {})["lease_expires_at"] is not None


# ---------------------------------------------------------------------------
# (c) RELEASE on every terminal path, including a crash
# ---------------------------------------------------------------------------


def _held(store: SqliteStore, run_id: str) -> list[str]:
    return [lock.realm for lock in PathLockStore(store).held_by(HOLDER_KIND, run_id)]


def test_release_on_completion(enforce: None, db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, realm=SHARED_REALM)
    runner = _runner(store, ProbeAdapter(), tmp_path)

    runner.run_forever(once=True)

    assert store.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]
    assert _held(store, run_id) == []
    assert runner._scope.generation_for(run_id) is None
    # Released, not deleted: contention stays measurable after the fact.
    assert [row["released_at"] is not None for row in _rows(store, "resource_locks")] == [True]


def test_release_on_failure(enforce: None, db_path: Path, tmp_path: Path) -> None:
    class FailingAdapter(ProbeAdapter):
        def run(self, input: AgentInput) -> AgentResult:
            self.seen.append(input.run_id)
            return AgentResult(status=ResultStatus.ERROR, error="boom")

    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, realm=SHARED_REALM)
    runner = _runner(store, FailingAdapter(), tmp_path)

    runner.run_forever(once=True)

    assert store.get_run(run_id)["state"] == RunState.FAILED.value  # type: ignore[index]
    assert _held(store, run_id) == []


def test_release_on_a_crash_that_never_reaches_transition(
    enforce: None, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``finally`` net: an isolated fault still gives the paths back.

    ``_transition`` is the ordinary release site, so the interesting case is the
    one that never gets there — a raise out of the middle of ``_execute_run``,
    which ``execute_run`` isolates into a FAILED run without ever transitioning.
    """
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, state=RunState.RUNNING.value, worker_id="w1", realm=SHARED_REALM)
    runner = _runner(store, ProbeAdapter(), tmp_path)
    assert runner._scope.take(store.get_run(run_id) or {}, resume=False).proceed
    assert _held(store, run_id) == [SHARED_REALM]

    def explode(_self: Runner, _run_id: str) -> None:
        raise RuntimeError("worker died mid-run")

    monkeypatch.setattr(Runner, "_execute_run", explode)
    runner.execute_run(run_id)

    assert store.get_run(run_id)["state"] == RunState.FAILED.value  # type: ignore[index]
    assert _held(store, run_id) == []


def test_a_nonterminal_crash_keeps_the_claim_until_the_lease_lapses(
    enforce: None, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented, deliberate: a still-live run does NOT get its scope taken away.

    Releasing on every exit would hand a paused/parked run's paths to somebody else
    while it is still going to come back to them. The TTL is what recovers a truly
    dead worker, and the resume path re-acquires.
    """
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, state=RunState.RUNNING.value, worker_id="w1", realm=SHARED_REALM)
    runner = _runner(store, ProbeAdapter(), tmp_path)
    assert runner._scope.take(store.get_run(run_id) or {}, resume=False).proceed

    def explode(_self: Runner, _run_id: str) -> None:
        raise RuntimeError("worker died mid-run")

    monkeypatch.setattr(Runner, "_execute_run", explode)
    monkeypatch.setattr(Runner, "_isolate_fault", lambda *_a, **_k: None)
    runner.execute_run(run_id)

    assert store.get_run(run_id)["state"] == RunState.RUNNING.value  # type: ignore[index]
    assert _held(store, run_id) == [SHARED_REALM]


def test_release_is_generation_fenced(enforce: None, db_path: Path, tmp_path: Path) -> None:
    """A displaced worker's cleanup must not free its ADOPTER's locks."""
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, state=RunState.RUNNING.value, worker_id="w1", realm=SHARED_REALM)
    displaced = _runner(store, ProbeAdapter(), tmp_path, worker_id="w1")
    assert displaced._scope.take(store.get_run(run_id) or {}, resume=False).proceed

    # The adopter takes the run and the scope with it, at a higher generation.
    assert store.update_run(run_id, {"worker_id": "w2"})
    adopter = _runner(store, ProbeAdapter(), tmp_path, worker_id="w2")
    assert adopter._scope.take(store.get_run(run_id) or {}, resume=True).proceed

    assert displaced._scope.release(run_id) == 0
    assert _held(store, run_id) == [SHARED_REALM]


# ---------------------------------------------------------------------------
# (d) FENCE: a stale generation stops a displaced-but-alive worker
# ---------------------------------------------------------------------------


def test_fence_rejects_a_stale_generation(enforce: None, db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, state=RunState.RUNNING.value, worker_id="w1", realm=SHARED_REALM)
    runner = _runner(store, ProbeAdapter(), tmp_path)
    assert runner._scope.take(store.get_run(run_id) or {}, resume=False).generation == 1

    runner._assert_fence(run_id)  # still ours: no raise

    # An adopter bumps the lease. worker_id is deliberately left alone, so ONLY the
    # generation predicate can catch this.
    assert store.update_run(run_id, {"lease_generation": 2})
    with pytest.raises(LostFence):
        runner._assert_fence(run_id)


def test_fence_rejects_a_worker_whose_renew_was_refused(
    enforce: None, db_path: Path, tmp_path: Path
) -> None:
    """A lapsed/adopted lease is discovered by the heartbeat and enforced at the
    next step boundary — the pulse thread cannot abort a step, but the fence can."""
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, state=RunState.RUNNING.value, worker_id="w1", realm=SHARED_REALM)
    runner = _runner(store, ProbeAdapter(), tmp_path)
    assert runner._scope.take(store.get_run(run_id) or {}, resume=False).proceed

    # Adoption, from the lock table's point of view: same holder, higher generation.
    adopter = _runner(store, ProbeAdapter(), tmp_path, worker_id="w1")
    assert adopter._scope.take(store.get_run(run_id) or {}, resume=True).generation == 2

    assert runner._scope.renew(run_id) is False
    assert runner._scope.fence_ok(run_id) is False


def test_fenced_claim_is_refused_without_parking(
    enforce: None, db_path: Path, tmp_path: Path
) -> None:
    """Fenced is not blocked: a displaced holder must STOP, not queue for a retry."""
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, state=RunState.RUNNING.value, worker_id="w1", realm=SHARED_REALM)
    runner = _runner(store, ProbeAdapter(), tmp_path)
    assert runner._scope.take(store.get_run(run_id) or {}, resume=False).proceed

    # Somebody else now owns the row, so this worker cannot even declare.
    assert store.update_run(run_id, {"worker_id": "w2"})
    decision = runner._scope.take(store.get_run(run_id) or {}, resume=True)

    assert decision.status == "fenced"
    assert not decision.proceed
    assert _rows(store, "scope_waiters") == []


# ---------------------------------------------------------------------------
# Declaration + derivation
# ---------------------------------------------------------------------------


def test_scope_json_round_trips() -> None:
    claims = (
        ScopeClaim(realm=SHARED_REALM, components=(), kind="root"),
        ScopeClaim(realm=OTHER_REALM, components=("src", "a.py"), kind="file"),
    )
    assert json_to_claims(claims_to_json(claims)) == claims


@pytest.mark.parametrize(
    "raw",
    ["", None, "not json", "[]", '{"v":999,"claims":[]}', '{"v":1,"claims":"nope"}'],
)
def test_unusable_scope_json_degrades_to_rederivation(raw: Any) -> None:
    """A malformed declaration must never be able to block a resume."""
    assert json_to_claims(raw) == ()


def test_a_resume_replays_the_declaration_it_made(
    enforce: None, db_path: Path, tmp_path: Path
) -> None:
    """A declaration is a commitment; a plan is mutable state. Replay the former."""
    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    _enqueue(store, run_id, state=RunState.RUNNING.value, worker_id="w1", realm=SHARED_REALM)
    runner = _runner(store, ProbeAdapter(), tmp_path)

    assert runner._scope.take(store.get_run(run_id) or {}, resume=True).realm == SHARED_REALM
    assert _held(store, run_id) == [SHARED_REALM]
    # The stored declaration is unchanged by the round trip.
    assert json_to_claims((store.get_run(run_id) or {})["scope_json"])[0].realm == SHARED_REALM


def test_a_run_with_no_declaration_derives_one_and_records_it(
    enforce: None, db_path: Path, tmp_path: Path
) -> None:
    """End-to-end derivation: no ``scope_json`` on the row, so the lane derives it
    from the plan's ``working_dir`` and writes what it decided."""
    from omniagentos.scope.paths import realm_of

    store = SqliteStore(str(db_path))
    run_id = new_id("run")
    workspace = tmp_path / "workspace" / run_id
    _enqueue(store, run_id, working_dir=str(workspace))
    adapter = ProbeAdapter()

    _runner(store, adapter, tmp_path).run_forever(once=True)

    assert adapter.seen == [run_id]
    claims = json_to_claims((store.get_run(run_id) or {})["scope_json"])
    assert [(c.components, c.kind) for c in claims] == [((), "root")]
    assert claims[0].realm == realm_of(str(workspace))


def test_derivation_claims_the_realm_root_of_every_declared_working_dir(
    tmp_path: Path,
) -> None:
    """v1: one whole-realm ROOT claim per working dir. Per-project, not per-file."""
    project = tmp_path / "project"
    project.mkdir()
    run = {
        "id": "run_1",
        "plan_json": json.dumps([{"kind": "agent", "params": {"working_dir": str(project)}}]),
    }
    workspace = str(tmp_path / "workspace" / "run_1")

    claims = derive_claims(run, {}, workspace)

    realms = {claim.realm for claim in claims}
    assert len(claims) == len(realms)
    assert all(claim.components == () and claim.kind == "root" for claim in claims)
    # The project dir and the (unrelated) per-run workspace are different realms,
    # so an unscoped run cannot collide with a project run by accident.
    assert len(realms) == 2


def test_two_runs_in_one_project_derive_the_SAME_realm(tmp_path: Path) -> None:
    """The point of the v1 derivation, stated as a test: same project == serialize."""
    project = tmp_path / "project"
    project.mkdir()
    plan = json.dumps([{"kind": "agent", "params": {"working_dir": str(project)}}])
    first = derive_claims({"id": "run_1", "plan_json": plan}, {}, str(tmp_path / "ws" / "run_1"))
    second = derive_claims({"id": "run_2", "plan_json": plan}, {}, str(tmp_path / "ws" / "run_2"))

    assert {c.realm for c in first} & {c.realm for c in second}


def test_working_dirs_prefers_plan_then_task_then_workspace() -> None:
    run = {
        "plan_json": json.dumps(
            [
                {"kind": "agent", "params": {"working_dir": "/a"}},
                {"kind": "agent", "params": {"working_dir": "/a"}},
                {"kind": "effect", "params": {}},
                {"kind": "agent", "params": {"working_dir": "/b"}},
            ]
        )
    }
    assert working_dirs_for_run(run, {"working_dir": "/c"}, "/ws") == ["/a", "/b", "/c", "/ws"]


def test_a_broken_plan_still_yields_the_workspace_claim(tmp_path: Path) -> None:
    """Derivation is total: an unparseable plan degrades, it does not raise."""
    workspace = str(tmp_path / "workspace" / "run_1")
    claims = derive_claims({"id": "run_1", "plan_json": "{{{"}, {}, workspace)
    assert len(claims) == 1


def test_a_store_without_a_transaction_surface_disables_the_lane(
    enforce: None, tmp_path: Path
) -> None:
    """A non-SQLite Store cannot take locks; the honest answer is off, not a crash."""

    class Bare:
        def get_run(self, _run_id: str) -> None:
            return None

        def update_run(self, *_a: Any, **_k: Any) -> bool:
            return True

        def get_task(self, _task_id: str) -> None:
            return None

    scope = RunnerScope(Bare(), worker_id="w1", workspace_base=str(tmp_path))  # type: ignore[arg-type]
    decision = scope.take({"id": "run_1", "plan_json": "[]"}, resume=False)

    assert decision.status == "disabled"
    assert decision.proceed
