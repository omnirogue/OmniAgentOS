"""P1-COST-EDGE: runner cost truth — unknown must never render as free.

``AgentUsage.cost_usd`` is three-valued (float | None). Callers must handle
``None`` explicitly — bare ``usage.cost_usd or 0.0`` is the defect class that
let unknown spend pass a cost ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.contracts import (
    AgentResult,
    AgentUsage,
    ResultStatus,
)
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.runner.core import Runner
from tests.runner.test_state_machine import (
    FinalizationSpy,
    TrackingAdapter,
    agent_step,
    create_run,
    dependencies,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runner-cost.db"
    migrate(str(path))
    return path


def _runner(
    store: SqliteStore,
    tmp_path: Path,
    adapter: TrackingAdapter | None = None,
    *,
    worker_id: str = "w-cost",
) -> Runner:
    return Runner(
        store,
        worker_id,
        dependencies=dependencies(
            adapter or TrackingAdapter(),
            FinalizationSpy(),
        ),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "workspace"),
        stale_s=30,
    )


class UnknownCostAdapter(TrackingAdapter):
    """Reports tokens without a dollar figure — cost is unknown, not free."""

    def run(self, input):  # type: ignore[no-untyped-def]
        with self._lock:
            self.calls.append(input)
        return AgentResult(
            status=ResultStatus.OK,
            output_text="done",
            usage=AgentUsage(
                wall_ms=12,
                turns=1,
                input_tokens=100,
                output_tokens=40,
                cost_usd=None,
                estimated=True,
                source="mixed",
            ),
        )


class ExactCostAdapter(TrackingAdapter):
    def __init__(self, cost: float = 0.01144063) -> None:
        super().__init__()
        self.cost = cost

    def run(self, input):  # type: ignore[no-untyped-def]
        with self._lock:
            self.calls.append(input)
        return AgentResult(
            status=ResultStatus.OK,
            output_text="done",
            usage=AgentUsage(
                wall_ms=12,
                turns=1,
                input_tokens=3,
                output_tokens=5,
                cost_usd=self.cost,
                estimated=False,
                source="cli-report",
            ),
        )


class TestRecordUsageThreeValued:
    def test_unknown_cost_does_not_become_zero_on_run(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        store = SqliteStore(str(db_path))
        adapter = UnknownCostAdapter()
        runner = _runner(store, tmp_path, adapter)
        _task_id, run_id = create_run(
            store,
            [agent_step("agent")],
            budget={"cost_usd_max": None},
        )
        claimed = store.claim_next_run(runner.worker_id)
        assert claimed and claimed["id"] == run_id

        usage = AgentUsage(
            wall_ms=10,
            turns=1,
            input_tokens=50,
            output_tokens=10,
            cost_usd=None,
            estimated=True,
            source="mixed",
        )
        runner._record_usage(run_id, usage)
        run = store.get_run(run_id)
        assert run is not None
        assert run["cost_usd"] is None, f"unknown collapsed to {run['cost_usd']!r}"
        assert int(run["input_tokens"] or 0) == 50
        assert int(run["output_tokens"] or 0) == 10

    def test_exact_cost_is_accumulated(self, db_path: Path, tmp_path: Path) -> None:
        store = SqliteStore(str(db_path))
        runner = _runner(store, tmp_path)
        _task_id, run_id = create_run(store, [agent_step("agent")])
        claimed = store.claim_next_run(runner.worker_id)
        assert claimed and claimed["id"] == run_id

        runner._record_usage(
            run_id,
            AgentUsage(
                wall_ms=5,
                turns=1,
                input_tokens=3,
                output_tokens=5,
                cost_usd=0.01144063,
                estimated=False,
                source="cli-report",
            ),
        )
        run = store.get_run(run_id)
        assert run is not None
        assert float(run["cost_usd"]) == pytest.approx(0.01144063)

        # Second unknown usage must NOT wipe the known total to zero.
        runner._record_usage(
            run_id,
            AgentUsage(
                wall_ms=5,
                turns=1,
                input_tokens=1,
                output_tokens=1,
                cost_usd=None,
                estimated=True,
                source="mixed",
            ),
        )
        run = store.get_run(run_id)
        assert run is not None
        assert float(run["cost_usd"]) == pytest.approx(0.01144063)

    def test_budget_allows_fails_closed_on_unknown_with_cost_ceiling(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        store = SqliteStore(str(db_path))
        runner = _runner(store, tmp_path)
        _task_id, run_id = create_run(
            store,
            [agent_step("agent")],
            budget={"cost_usd_max": 1.0},
        )
        claimed = store.claim_next_run(runner.worker_id)
        assert claimed and claimed["id"] == run_id

        # Tokens used, cost still unknown → must not look free under a ceiling.
        store.update_run(
            run_id,
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cost_usd": None,
                "budget_json": json.dumps({"cost_usd_max": 1.0}),
            },
            expect_worker=runner.worker_id,
        )
        run = store.get_run(run_id)
        assert run is not None
        assert runner._budget_allows(run) is False

    def test_budget_allows_known_zero_under_ceiling(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        store = SqliteStore(str(db_path))
        runner = _runner(store, tmp_path)
        _task_id, run_id = create_run(
            store,
            [agent_step("agent")],
            budget={"cost_usd_max": 1.0},
        )
        claimed = store.claim_next_run(runner.worker_id)
        assert claimed and claimed["id"] == run_id
        store.update_run(
            run_id,
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_usd": 0.0,
                "budget_json": json.dumps({"cost_usd_max": 1.0}),
            },
            expect_worker=runner.worker_id,
        )
        run = store.get_run(run_id)
        assert run is not None
        assert runner._budget_allows(run) is True

    def test_manifest_preserves_unknown_cost(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        store = SqliteStore(str(db_path))
        runner = _runner(store, tmp_path)
        _task_id, run_id = create_run(store, [agent_step("agent")])
        claimed = store.claim_next_run(runner.worker_id)
        assert claimed and claimed["id"] == run_id
        store.update_run(
            run_id,
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_usd": None,
                "usage_estimated": 1,
                "usage_source": "mixed",
                "wall_ms": 12,
            },
            expect_worker=runner.worker_id,
        )
        run = store.get_run(run_id)
        assert run is not None
        manifest = runner._manifest(run, [])
        assert manifest.usage.cost_usd is None


class TestEndToEndUnknownCost:
    def test_agent_step_with_unknown_cost_leaves_run_cost_null(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        store = SqliteStore(str(db_path))
        adapter = UnknownCostAdapter()
        runner = _runner(store, tmp_path, adapter)
        _task_id, run_id = create_run(
            store,
            [agent_step("agent")],
            budget={},  # no cost ceiling — run should complete
        )
        runner.tick()
        run = store.get_run(run_id)
        assert run is not None
        assert run["cost_usd"] is None, f"unknown became {run['cost_usd']!r}"
        assert int(run.get("input_tokens") or 0) >= 100
