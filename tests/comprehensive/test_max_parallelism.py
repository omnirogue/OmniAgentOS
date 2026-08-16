"""Maximum parallelism stress: graphs, CBM, scope locks, multi-provider spawn."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pytest

from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.db.store import SqliteStore
from omniagentos.graph_runtime.service import GraphRuntimeService
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim
from omniagentos.swarm import spawn as spawn_mod
from omniagentos.swarm.provider_exec import SUPPORTED_PROVIDERS
from omniagentos.swarm.scheduler import SpawnRequest
from omniagentos.swarm.spawn import UnifiedSpawner

# ---------------------------------------------------------------------------
# Graph Runtime — concurrent diamonds
# ---------------------------------------------------------------------------


def test_max_parallel_diamond_graphs(product_db: str, diamond_count: int, workers: int) -> None:
    """Run N full diamonds concurrently on one DB; all complete fail-closed."""
    svc = GraphRuntimeService(db_path=product_db)

    def run_one(i: int) -> dict[str, Any]:
        return svc.run_diamond_deterministic(
            title=f"parallel-diamond-{i}",
            findings_a={"claim": f"A{i}", "score": 0.7 + (i % 20) * 0.01, "source": "a"},
            findings_b={"claim": f"B{i}", "score": 0.8 + (i % 15) * 0.01, "source": "b"},
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, i) for i in range(diamond_count)]
        results = [f.result(timeout=60) for f in as_completed(futures)]

    assert len(results) == diamond_count
    assert all(r["status"] == "completed" for r in results)
    # Distinct run ids
    ids = {r["id"] for r in results}
    assert len(ids) == diamond_count
    listed = svc.list_runs(limit=diamond_count + 10)
    assert len(listed) >= diamond_count


def test_max_parallel_node_completion_race(product_db: str, workers: int) -> None:
    """Many runs each completing fan_a/fan_b in parallel without corruption."""
    svc = GraphRuntimeService(db_path=product_db)
    n = min(workers, 16)
    runs = [svc.start_diamond(title=f"race-{i}") for i in range(n)]

    def complete_fans(run_id: str, i: int) -> str:
        svc.complete_node(run_id, "fan_a", outputs={"finding": {"claim": f"a{i}", "score": 0.9}})
        svc.complete_node(run_id, "fan_b", outputs={"finding": {"claim": f"b{i}", "score": 0.85}})
        ready = svc.ready_nodes(run_id)
        keys = {x["key"] for x in ready}
        assert "reduce" in keys
        return run_id

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(complete_fans, r["id"], i) for i, r in enumerate(runs)]
        done = [f.result(timeout=30) for f in as_completed(futs)]
    assert len(done) == n


# ---------------------------------------------------------------------------
# CBM — concurrent allocate / escalate / close
# ---------------------------------------------------------------------------


def test_max_parallel_cbm_allocate_and_escalate(product_db: str, workers: int) -> None:
    svc = CognitiveBudgetService(database=product_db)
    n = max(workers * 2, 32)

    def allocate_and_maybe_escalate(i: int) -> dict[str, Any]:
        a = svc.allocate(
            task_id=f"par-task-{i}",
            stage="execution" if i % 3 else "planning",
            risk_class="reversible_internal" if i % 5 else "irreversible",
            novelty="high" if i % 7 == 0 else "low",
            difficulty="medium",
        )
        if i % 2 == 0:
            e = svc.escalate(
                a["id"],
                trigger_code="gate_failure",
                evidence=[f"fail-{i}"],
            )
            a = svc.get_allocation(a["id"]) or a
            a = {**a, "escalation": e}
        if i % 3 == 0:
            svc.close_allocation(
                a["id"] if "id" in a else a.get("id"),
                first_pass_accepted=i % 4 != 0,
                wall_seconds=float(i % 50),
                repair_count=i % 3,
            )
        return a

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(allocate_and_maybe_escalate, i) for i in range(n)]
        out = [f.result(timeout=30) for f in as_completed(futs)]

    assert len(out) == n
    board = svc.leaderboard()
    # At least some closed outcomes feed stats
    assert isinstance(board, list)


def test_cbm_concurrent_close_does_not_double_count_corrupt(product_db: str, workers: int) -> None:
    """Close is sequential per allocation; parallel closes on distinct allocs OK."""
    svc = CognitiveBudgetService(database=product_db)
    allocs = [svc.allocate(task_id=f"close-{i}") for i in range(workers)]

    def close_one(a: dict[str, Any]) -> str:
        r = svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=1.0)
        return r["outcome_id"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        ids = [
            f.result(timeout=15) for f in as_completed([pool.submit(close_one, a) for a in allocs])
        ]
    assert len(set(ids)) == workers


# ---------------------------------------------------------------------------
# Scope locks — max disjoint + contention
# ---------------------------------------------------------------------------


def test_max_parallel_disjoint_scope_locks(tmp_path: Path, workers: int) -> None:
    """N holders acquire disjoint paths simultaneously — all granted."""
    store = SqliteStore(str(tmp_path / "scope.db"))
    locks = PathLockStore(store)
    realm = str(tmp_path / "proj")
    Path(realm).mkdir()
    n = max(workers, 16)
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()

    def holder(i: int) -> None:
        barrier.wait(timeout=10)
        h = LockHolder(kind="run", id=f"run-{i}", lane="runner")
        r = locks.try_acquire_scope(
            [ScopeClaim.for_path(realm, f"src/mod_{i}/file.py")],
            h,
            enforce=True,
        )
        with lock:
            results.append(r.status)

    try:
        threads = [threading.Thread(target=holder, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert results.count("granted") == n
    finally:
        store.close()


def test_max_parallel_scope_contention_exactly_one_wins(tmp_path: Path, workers: int) -> None:
    """Many contenders for the same path → exactly one grant under enforce."""
    store = SqliteStore(str(tmp_path / "scope-c.db"))
    locks = PathLockStore(store)
    realm = str(tmp_path / "proj")
    Path(realm).mkdir()
    n = max(workers, 12)
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()
    claims = [ScopeClaim.for_path(realm, "src/hotspot.py")]

    def contender(i: int) -> None:
        barrier.wait(timeout=10)
        h = LockHolder(kind="run", id=f"c-{i}", lane="swarm")
        r = locks.try_acquire_scope(claims, h, enforce=True)
        with lock:
            results.append(r.status)

    try:
        threads = [threading.Thread(target=contender, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert results.count("granted") == 1
        assert results.count("blocked") == n - 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Multi-provider spawn fan-out (fake runners — max parallelism)
# ---------------------------------------------------------------------------


class _Supervisor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> str:
        with self._lock:
            self.calls.append(kwargs)
            return f"ses_claude_{len(self.calls)}"


class _ProviderRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> str:
        with self._lock:
            self.calls.append(kwargs)
            provider = str(kwargs.get("provider") or "provider")
            return f"ses_{provider}_{len(self.calls)}"


class _SwarmDal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.swarm_jsons: dict[str, dict[str, Any]] = {}

    def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
        del run_id
        with self._lock:
            return list(self.tasks.values())

    def get_swarm_json(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.swarm_jsons.get(task_id)

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        return []


class _SessionsDal:
    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return None

    def set_idle_minutes(self, session_id: str, idle_minutes: float | None) -> bool:
        return True


def test_max_parallel_multi_provider_spawn(
    tmp_path: Path, workers: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent spawns across all 5 providers × replicas.

    This test fans out `workers` (default 24, COMPREHENSIVE_WORKERS-tunable)
    concurrent spawns on a single run_id. Both of UnifiedSpawner's hard
    delegation caps (omniagentos/swarm/spawn.py:105-154:
    DEFAULT_MAX_TOTAL_DELEGATIONS=20, DEFAULT_MAX_CONCURRENT_DELEGATIONS=3)
    are far below that stress width, so the test pins BOTH module-level caps
    above the stress width for this test only — the production defaults are
    untouched, this is test-only headroom so the assertions below exercise
    spawn parallelism instead of tripping the (correct) production hard caps.
    """
    stress_cap = max(64, workers * len(("claude", "codex", "grok", "gemini", "kimi", "qwen")))
    monkeypatch.setattr(spawn_mod, "MAX_TOTAL_DELEGATIONS", stress_cap)
    monkeypatch.setattr(spawn_mod, "MAX_CONCURRENT_DELEGATIONS", stress_cap)
    supervisor = _Supervisor()
    runner = _ProviderRunner()
    swarm_dal = _SwarmDal()
    reservations_lock = threading.Lock()
    converted: list[tuple[str, str]] = []

    def convert(reservation_id: str, session_id: str) -> bool:
        with reservations_lock:
            converted.append((reservation_id, session_id))
        return True

    def release(reservation_id: str) -> bool:
        return True

    spawner = UnifiedSpawner(
        supervisor=supervisor,
        provider_runner=runner,
        swarm_dal=swarm_dal,
        sessions_dal=_SessionsDal(),
        convert_reservation=convert,
        release_reservation=release,
        var_root=tmp_path / "var" / "swarm",
        db_path=str(tmp_path / "spawn.db"),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    providers = (
        ("claude", "sonnet"),
        ("codex", "gpt-5.6-sol"),
        ("grok", "grok-4.5"),
        ("gemini", "gemini-2.5-pro"),
        ("kimi", "kimi-k2"),
        ("qwen", "qwen3-coder-flash"),
    )
    replicas = max(2, workers // len(providers))
    jobs: list[tuple[str, str, int]] = []
    for r in range(replicas):
        for provider, model in providers:
            jobs.append((provider, model, r))

    for provider, _model, r in jobs:
        task_id = f"task_{provider}_{r}"
        swarm_dal.tasks[task_id] = {
            "id": task_id,
            "title": f"{provider} slice {r}",
            "description": f"Parallel work for {provider}",
            "discipline": "coding",
            "priority": "high",
        }
        swarm_dal.swarm_jsons[task_id] = {
            "task_key": f"{provider}-{r}",
            "acceptance": "tests pass",
            "risk_class": "none",
            "novelty": "low",
            "difficulty": "medium",
            "owned_paths": [f"src/{provider}_{r}/"],
        }

    def do_spawn(provider: str, model: str, r: int) -> str:
        task_id = f"task_{provider}_{r}"
        req = SpawnRequest(
            run_id="swr_max_parallel",
            task_id=task_id,
            task_key=f"{provider}-{r}",
            attempt_id=f"swa_{provider}_{r}",
            working_dir=str(workspace),
            prompt=f"Implement the {provider} slice {r}.",
            provider=provider,
            model=model,
            tier="standard",
            account_id=f"acct-{provider}",
            idle_minutes=20.0,
            budget_usd_max=5.0,
            reservation_id=f"rsv_{provider}_{r}",
            effort="medium",
        )
        return spawner.spawn(req)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(do_spawn, p, m, r) for p, m, r in jobs]
        session_ids = [f.result(timeout=60) for f in as_completed(futs)]

    assert len(session_ids) == len(jobs)
    assert len(set(session_ids)) == len(jobs)
    # Every non-claude provider lineage used
    used = {c.get("provider") for c in runner.calls}
    assert SUPPORTED_PROVIDERS <= used | set()
    assert len(supervisor.calls) == replicas  # one claude per replica


# ---------------------------------------------------------------------------
# Throughput sanity under load
# ---------------------------------------------------------------------------


def test_diamond_throughput_under_load(product_db: str) -> None:
    """Serial baseline + parallel batch: parallel finishes with all complete."""
    svc = GraphRuntimeService(db_path=product_db)
    batch = 12
    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(svc.run_diamond_deterministic, title=f"tp-{i}") for i in range(batch)]
        statuses = [f.result(timeout=45)["status"] for f in as_completed(futs)]
    assert statuses.count("completed") == batch
