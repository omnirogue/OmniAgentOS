"""Integrated concurrent wave: graph + CBM + orgdims + metacog + scope together."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pytest

from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.graph_runtime.service import GraphRuntimeService
from omniagentos.metacog.config import clear_metacog_config_cache
from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore
from omniagentos.orgdims.service import OrgDimsService
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim


def test_integrated_parallel_wave(
    tmp_path: Path, workers: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One DB, many systems, concurrent operations — no silent corruption."""
    db = str(tmp_path / "wave.db")
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    clear_metacog_config_cache()

    graph = GraphRuntimeService(db_path=db)
    cbm = CognitiveBudgetService(database=db)
    org = OrgDimsService(db_path=db)
    org.ensure_seeded()
    metacog = MetacogService(store=MetacogStore(db))
    collab = CollabStore(db)
    store = SqliteStore(db)
    locks = PathLockStore(store)
    realm = str(tmp_path / "proj")
    Path(realm).mkdir()

    errors: list[str] = []
    stats = {
        "diamonds": 0,
        "allocs": 0,
        "classifies": 0,
        "artifacts": 0,
        "locks_granted": 0,
        "evals": 0,
    }
    lock = __import__("threading").Lock()

    def graph_job(i: int) -> None:
        try:
            r = graph.run_diamond_deterministic(title=f"wave-d-{i}")
            assert r["status"] == "completed"
            with lock:
                stats["diamonds"] += 1
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"graph:{exc}")

    def cbm_job(i: int) -> None:
        try:
            a = cbm.allocate(task_id=f"wave-t-{i}", stage="execution")
            if i % 2 == 0:
                cbm.escalate(a["id"], trigger_code="gate_failure", evidence=["w"])
            cbm.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=1.5)
            with lock:
                stats["allocs"] += 1
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"cbm:{exc}")

    def org_job(i: int) -> None:
        try:
            task = BoardTask(
                title=f"Wave feature {i} implement API",
                description="Add endpoints and unit tests for parallel wave",
                discipline="coding",
                priority="normal",
            )
            collab.create_board_task(task)
            org.classify_board_task(
                task_id=task.id,
                title=task.title,
                description=task.description or "",
                discipline="coding",
                priority="normal",
                apply=True,
            )
            with lock:
                stats["classifies"] += 1
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"org:{exc}")

    def metacog_job(i: int) -> None:
        try:
            metacog.register_artifact(
                artifact_type="finding",
                content=f'{{"i":{i},"wave":true}}',
                task_id=f"wave-t-{i}",
                run_id=f"wave-r-{i}",
            )
            metacog.evaluate(
                task_id=f"wave-t-{i}",
                run_id=f"wave-r-{i}",
                criteria_total=5,
                criteria_passed=3 + (i % 2),
                previous_progress=0.4,
            )
            with lock:
                stats["artifacts"] += 1
                stats["evals"] += 1
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"metacog:{exc}")

    def scope_job(i: int) -> None:
        try:
            r = locks.try_acquire_scope(
                [ScopeClaim.for_path(realm, f"src/wave_{i}.py")],
                LockHolder(kind="run", id=f"wave-run-{i}", lane="runner"),
                enforce=True,
            )
            if r.status == "granted":
                with lock:
                    stats["locks_granted"] += 1
            else:
                with lock:
                    errors.append(f"scope:expected grant got {r.status}")
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"scope:{exc}")

    n = max(8, min(workers, 16))
    jobs = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(n):
            jobs.append(pool.submit(graph_job, i))
            jobs.append(pool.submit(cbm_job, i))
            jobs.append(pool.submit(org_job, i))
            jobs.append(pool.submit(metacog_job, i))
            jobs.append(pool.submit(scope_job, i))
        for f in as_completed(jobs):
            f.result(timeout=90)

    store.close()

    assert not errors, f"wave errors: {errors[:10]} ({len(errors)} total)"
    assert stats["diamonds"] == n
    assert stats["allocs"] == n
    assert stats["classifies"] == n
    assert stats["artifacts"] == n
    assert stats["evals"] == n
    assert stats["locks_granted"] == n

    # Cross-checks after the wave
    assert graph.health()["live"] is True
    assert cbm.health()["live"] is True
    assert org.health()["primary_orchestrator"] == "grok-orchestrator"
    assert metacog.health()["mode"] == "enforce"
    runs = graph.list_runs(limit=n + 5)
    assert len(runs) >= n


def test_cbm_guides_graph_roles_under_parallel(product_db: str, workers: int) -> None:
    """CBM allocations for diamond node roles stay consistent under concurrency."""
    graph = GraphRuntimeService(db_path=product_db)
    cbm = CognitiveBudgetService(database=product_db)

    roles = [
        ("fan_out", "execution", "low"),
        ("verify", "verification", "low"),
        ("synthesize", "synthesis", "medium"),
    ]

    def one(i: int) -> dict[str, Any]:
        stage = roles[i % 3][1]
        a = cbm.allocate(
            task_id=f"role-{i}",
            stage=stage,
            difficulty=roles[i % 3][2],
        )
        run = graph.run_diamond_deterministic(title=f"role-d-{i}")
        return {"rung": a["rung"], "stage": stage, "graph": run["status"]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, i) for i in range(min(workers, 12))]
        out = [f.result(timeout=45) for f in as_completed(futs)]

    assert all(o["graph"] == "completed" for o in out)
    # verification stage prefers mechanical rung 0
    verify_allocs = [o for o in out if o["stage"] == "verification"]
    if verify_allocs:
        assert all(o["rung"] == 0 for o in verify_allocs)
