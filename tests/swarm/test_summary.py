"""WP7 ``swarm/summary.py``: metric math (golden numbers), the frozen-estimate
rule, Throughput Score bounds/penalty, ``est_completion``, and ``write_summary``
(vault note + degraded narrative + best-effort knowledge ingest + event).

Scheduler terminal-hook wiring lives in ``test_summary_scheduler_hook.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import new_id
from omniagentos.swarm import summary
from omniagentos.swarm.contracts import SWARM_EVENT_KIND, SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.planner import provision_run
from tests.support.db_template import migrated_db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    # Pre-migrated template copy: the shared schema (swarm_runs/attempts +
    # events) without re-applying all 86 migrations per test.
    return migrated_db(CollabStore, tmp_path / "summary.db")


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _provision(
    dal: SwarmDal,
    working_dir: str,
    task_specs: list[dict],
    *,
    target_n: int = 2,
    parallelism_ratio: float | None = 2.0,
    integration_id: str = "integration",
    assumptions: list[str] | None = None,
    budget_usd_max: float | None = None,
) -> dict:
    tasks = [
        SwarmTaskSpec(
            id=spec["id"],
            title=spec.get("title", spec["id"]),
            description=spec.get("description", ""),
            depends_on=list(spec.get("depends_on", [])),
            complexity=spec.get("complexity", "simple"),
            est_manual_minutes=int(spec.get("est_manual", 0)),
            est_agent_minutes=int(spec.get("est_agent", 10)),
            owned_paths=list(spec.get("owned_paths", [f"src/{spec['id']}.txt"])),
            acceptance=spec.get("acceptance", "delivered"),
            verify_command=spec.get("verify_command", ""),
        )
        for spec in task_specs
    ]
    plan = SwarmPlan(
        goal="test goal",
        tasks=tasks,
        assumptions=list(assumptions or []),
        integration_task_id=integration_id,
        mode="swarm",
        version=1,
        target_n=target_n,
        parallelism_ratio=parallelism_ratio,
    )
    return provision_run(
        plan,
        dal=dal,
        working_dir=working_dir,
        budget_usd_max=budget_usd_max,
        max_concurrency=10,
        write_plan_doc=False,
    )


def _insert_attempt(
    dal: SwarmDal,
    run_id: str,
    board_task_id: str,
    seq: int,
    started_at: str,
    ended_at: str | None,
    end_reason: str | None,
    *,
    provider: str = "claude",
    model: str = "sonnet",
) -> str:
    attempt_id = new_id("swa")
    dal._connection.execute(
        "INSERT INTO swarm_attempts "
        "(id, swarm_run_id, board_task_id, seq, session_id, provider, model, tier, "
        "account_id, started_at, ended_at, end_reason, detail) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?, 'standard', NULL, ?, ?, ?, '')",
        (attempt_id, run_id, board_task_id, seq, provider, model, started_at, ended_at, end_reason),
    )
    return attempt_id


def _set_run_window(
    dal: SwarmDal,
    run_id: str,
    *,
    started_at: str,
    finished_at: str,
    status: str = "completed",
    cost_usd: float = 0.0,
    budget_usd_max: float | None = None,
    target_concurrency: int | None = None,
) -> None:
    fields = ["started_at = ?", "finished_at = ?", "status = ?", "cost_usd = ?"]
    values: list = [started_at, finished_at, status, cost_usd]
    if budget_usd_max is not None:
        fields.append("budget_usd_max = ?")
        values.append(budget_usd_max)
    if target_concurrency is not None:
        fields.append("target_concurrency = ?")
        values.append(target_concurrency)
    values.append(run_id)
    dal._connection.execute(f"UPDATE swarm_runs SET {', '.join(fields)} WHERE id = ?", values)


def _insert_swarm_event(dal: SwarmDal, run_id: str, action: str, payload: dict, ts: str) -> None:
    dal._connection.execute(
        "INSERT INTO events (ts, type, actor, action, target_type, target_id, payload_json, trace_id) "
        "VALUES (?, ?, 'test', ?, 'swarm_run', ?, ?, '')",
        (ts, SWARM_EVENT_KIND, action, run_id, json.dumps(payload)),
    )


def _set_status(dal: SwarmDal, board_task_id: str, status: str) -> None:
    dal._connection.execute(
        "UPDATE board_tasks SET status = ? WHERE id = ?", (status, board_task_id)
    )


# ---------------------------------------------------------------------------
# compute_metrics — golden numbers
# ---------------------------------------------------------------------------


class TestComputeMetricsGolden:
    def test_golden_numbers(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [
                    {"id": "a", "est_manual": 60, "est_agent": 10},
                    {"id": "b", "est_manual": 90, "est_agent": 15},
                    {
                        "id": "integration",
                        "est_manual": 0,
                        "est_agent": 5,
                        "depends_on": ["a", "b"],
                    },
                ],
                target_n=2,
                parallelism_ratio=2.0,
            )
            run_id = str(provisioned["run"]["id"])
            card_ids = provisioned["card_ids"]

            t0 = "2026-01-01T00:00:00Z"
            _insert_attempt(dal, run_id, card_ids["a"], 0, t0, "2026-01-01T00:10:00Z", "completed")
            _insert_attempt(dal, run_id, card_ids["b"], 0, t0, "2026-01-01T00:15:00Z", "completed")
            _insert_attempt(
                dal,
                run_id,
                card_ids["integration"],
                0,
                "2026-01-01T00:15:00Z",
                "2026-01-01T00:20:00Z",
                "completed",
            )
            for card_id in card_ids.values():
                _set_status(dal, card_id, "done")
            _set_run_window(
                dal,
                run_id,
                started_at=t0,
                finished_at="2026-01-01T00:20:00Z",
                status="completed",
                cost_usd=0.0,
                target_concurrency=2,
            )

            metrics = summary.compute_metrics(run_id, dal)

            assert metrics["wall_seconds"] == pytest.approx(1200.0)
            assert metrics["est_manual_minutes_total"] == 150
            assert metrics["speedup"] == pytest.approx(7.5)
            assert metrics["parallelism_ratio"] == pytest.approx(2.0)
            assert metrics["mean_target_n"] == pytest.approx(2.0)
            assert metrics["utilization"] == pytest.approx(0.75)
            assert metrics["first_attempt_rate"] == pytest.approx(1.0)
            assert metrics["rate_limit_stall_fraction"] == pytest.approx(0.0)
            assert metrics["estimate_error"] == pytest.approx(0.0)
            assert metrics["estimate_error_penalty"] == pytest.approx(0.0)
            assert metrics["score"] == pytest.approx(93.75)
            assert metrics["tasks_total"] == 3
            assert metrics["task_durations_minutes"] == {
                "a": pytest.approx(10.0),
                "b": pytest.approx(15.0),
                "integration": pytest.approx(5.0),
            }

            stored = json.loads(dal.get_run(run_id)["metrics_json"])
            assert stored["score"] == pytest.approx(93.75)
        finally:
            dal.close()

    def test_mean_target_n_time_weighted_over_resize_history(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [{"id": "a", "est_manual": 0, "est_agent": 1}],
                target_n=2,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            t0 = "2026-01-01T00:00:00Z"
            finished = "2026-01-01T00:20:00Z"  # 1200s total
            _set_run_window(
                dal,
                run_id,
                started_at=t0,
                finished_at=finished,
                status="completed",
                target_concurrency=2,
            )
            # [0,300s): n=2 -> 600 slot-seconds
            # [300,900s): n=4 -> 2400 slot-seconds
            # [900,1200s): n=1 -> 300 slot-seconds
            # total 3300 / 1200 = 2.75
            _insert_swarm_event(dal, run_id, "resize", {"from": 2, "to": 4}, "2026-01-01T00:05:00Z")
            _insert_swarm_event(dal, run_id, "resize", {"from": 4, "to": 1}, "2026-01-01T00:15:00Z")

            metrics = summary.compute_metrics(run_id, dal)
            assert metrics["mean_target_n"] == pytest.approx(2.75)
        finally:
            dal.close()

    def test_rate_limit_stall_fraction_and_cooldown_seconds(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [{"id": "a", "est_manual": 0, "est_agent": 1}],
                target_n=1,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            t0 = "2026-01-01T00:00:00Z"
            finished = "2026-01-01T00:16:40Z"  # 1000s total
            _set_run_window(
                dal,
                run_id,
                started_at=t0,
                finished_at=finished,
                status="completed",
                target_concurrency=1,
            )
            _insert_swarm_event(dal, run_id, "rate_limit_stall", {"seconds": 100}, t0)
            _insert_swarm_event(dal, run_id, "rate_limit_stall", {"seconds": 150}, t0)
            _insert_swarm_event(dal, run_id, "provider_switched", {}, t0)
            _insert_swarm_event(dal, run_id, "provider_switched", {}, t0)

            metrics = summary.compute_metrics(run_id, dal)
            assert metrics["bottlenecks"]["cooldown_wait_seconds"] == pytest.approx(250.0)
            assert metrics["bottlenecks"]["provider_switches"] == 2
            # No attempts means no exposure population for the rate, even
            # though the raw cooldown seconds remain useful bottleneck data.
            assert metrics["rate_limit_stall_fraction"] is None
            assert metrics["score"] == pytest.approx(0.0)
        finally:
            dal.close()

    def test_bottlenecks_critical_path_and_most_retried(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [
                    {"id": "a", "est_manual": 0, "est_agent": 10},
                    {"id": "b", "est_manual": 0, "est_agent": 20, "depends_on": ["a"]},
                    {"id": "integration", "est_manual": 0, "est_agent": 5, "depends_on": ["b"]},
                ],
                target_n=1,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            card_ids = provisioned["card_ids"]
            t0 = "2026-01-01T00:00:00Z"

            # a: one retry (first attempt crashed, second completed).
            _insert_attempt(dal, run_id, card_ids["a"], 0, t0, "2026-01-01T00:05:00Z", "crashed")
            _insert_attempt(
                dal,
                run_id,
                card_ids["a"],
                1,
                "2026-01-01T00:05:00Z",
                "2026-01-01T00:15:00Z",
                "completed",
            )
            _insert_attempt(
                dal,
                run_id,
                card_ids["b"],
                0,
                "2026-01-01T00:15:00Z",
                "2026-01-01T00:35:00Z",
                "completed",
            )
            _insert_attempt(
                dal,
                run_id,
                card_ids["integration"],
                0,
                "2026-01-01T00:35:00Z",
                "2026-01-01T00:40:00Z",
                "completed",
            )
            _insert_swarm_event(dal, run_id, "provider_switched", {}, t0)
            _set_run_window(
                dal,
                run_id,
                started_at=t0,
                finished_at="2026-01-01T00:40:00Z",
                status="completed",
                target_concurrency=1,
            )

            metrics = summary.compute_metrics(run_id, dal)
            assert metrics["bottlenecks"]["critical_path_task_keys"] == ["a", "b", "integration"]
            assert metrics["bottlenecks"]["most_retried_task_key"] == "a"
            assert metrics["bottlenecks"]["most_retried_count"] == 1
            assert metrics["bottlenecks"]["provider_switches"] == 1
            # a's FIRST attempt (seq0) crashed -> not first-attempt-completed;
            # b and integration succeeded first try -> 2/3.
            assert metrics["first_attempt_rate"] == pytest.approx(2.0 / 3.0, abs=1e-4)
        finally:
            dal.close()

    def test_cost_overshoot(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [{"id": "a", "est_manual": 0, "est_agent": 1}],
                target_n=1,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            _set_run_window(
                dal,
                run_id,
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:01:00Z",
                status="completed",
                cost_usd=12.0,
                budget_usd_max=10.0,
                target_concurrency=1,
            )
            metrics = summary.compute_metrics(run_id, dal)
            assert metrics["cost_usd"] == pytest.approx(12.0)
            assert metrics["cost_overshoot_usd"] == pytest.approx(2.0)
        finally:
            dal.close()

    def test_compute_metrics_surfaces_cost_to_green_with_confidence(
        self, tmp_path: Path, db_path: str
    ) -> None:
        """Production wiring: costgreen rates + confidence land on metrics_json.

        A mechanism nothing invokes does not exist. compute_metrics is the
        finished-run hook; sparse cost must read as confidence=sparse with
        cost_per_green=None, never a favourable $0.00.

        Overall metrics.cost_usd and the vault ``- Cost:`` line must also stay
        unknown for tokens-only — not ``0.0`` / ``$0.0000`` while the bucket
        correctly says unknown (the free-look that rejected this lane).
        """
        from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT, SOURCE_TOKENS_ONLY

        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [{"id": "a", "est_manual": 10, "est_agent": 5}],
                target_n=1,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            card_id = provisioned["card_ids"]["a"]
            attempt_id = _insert_attempt(
                dal,
                run_id,
                card_id,
                0,
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:01:00Z",
                "completed",
            )
            # Tokens-only: exact tokens/wall, cost unknown — must not look free.
            dal._connection.execute(
                "UPDATE swarm_attempts SET effort = ?, cost_usd = NULL, "
                "input_tokens = ?, output_tokens = ?, wall_ms = ?, usage_source = ? "
                "WHERE id = ?",
                ("medium", 500, 500, 60_000, SOURCE_TOKENS_ONLY, attempt_id),
            )
            _set_status(dal, card_id, "done")
            _set_run_window(
                dal,
                run_id,
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:01:00Z",
                status="completed",
                # Legacy floor on the run column — still must not free-look.
                cost_usd=0.0,
            )

            metrics = summary.compute_metrics(run_id, dal)
            buckets = metrics["cost_to_green_by_effort"]
            assert len(buckets) == 1
            bucket = buckets[0]
            assert bucket["effort"] == "medium"
            assert bucket["confidence"] == "sparse"
            assert bucket["cost_per_green"] is None  # not 0.0
            assert bucket["tokens_per_green"] == 1000.0
            assert bucket["wall_ms_per_green"] == 60_000.0
            # Overall cost is three-valued: unknown, not coerced free.
            assert metrics["cost_usd"] is None
            assert metrics["cost_overshoot_usd"] is None
            body = "\n".join(
                summary._mechanical_sections(run_id, dal.get_run(run_id) or {}, metrics)
            )
            assert "- Cost: unknown" in body
            assert "- Cost: $0.0000" not in body

            # Fully measured CLI report still publishes numbers.
            dal._connection.execute(
                "UPDATE swarm_attempts SET cost_usd = ?, usage_source = ? WHERE id = ?",
                (2.5, SOURCE_CLI_REPORT, attempt_id),
            )
            measured_metrics = summary.compute_metrics(run_id, dal)
            measured = measured_metrics["cost_to_green_by_effort"][0]
            assert measured["confidence"] == "measured"
            assert measured["cost_per_green"] == 2.5
            assert measured_metrics["cost_usd"] == pytest.approx(2.5)
            measured_body = "\n".join(
                summary._mechanical_sections(run_id, dal.get_run(run_id) or {}, measured_metrics)
            )
            assert "- Cost: $2.5000" in measured_body
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# frozen-estimate rule: recomputing/mutating board_tasks.swarm_json must NEVER
# change a metric — every estimate is read ONLY from swarm_runs.plan_json.
# ---------------------------------------------------------------------------


class TestFrozenEstimateRule:
    def test_mutating_live_swarm_json_estimates_does_not_change_metrics(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [
                    {"id": "a", "est_manual": 60, "est_agent": 10},
                    {"id": "integration", "est_manual": 0, "est_agent": 5, "depends_on": ["a"]},
                ],
                target_n=1,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            card_ids = provisioned["card_ids"]
            t0 = "2026-01-01T00:00:00Z"
            _insert_attempt(dal, run_id, card_ids["a"], 0, t0, "2026-01-01T00:10:00Z", "completed")
            _insert_attempt(
                dal,
                run_id,
                card_ids["integration"],
                0,
                "2026-01-01T00:10:00Z",
                "2026-01-01T00:15:00Z",
                "completed",
            )
            _set_run_window(
                dal,
                run_id,
                started_at=t0,
                finished_at="2026-01-01T00:15:00Z",
                status="completed",
                target_concurrency=1,
            )

            baseline = summary.compute_metrics(run_id, dal)

            # "Recompute"/inflate the live board_tasks.swarm_json estimates —
            # this must be completely invisible to compute_metrics.
            live = dal.get_swarm_json(card_ids["a"])
            live["est_manual_minutes"] = 999999
            live["est_agent_minutes"] = 999999
            dal.set_swarm_json(card_ids["a"], live)

            after = summary.compute_metrics(run_id, dal)
            assert after["est_manual_minutes_total"] == baseline["est_manual_minutes_total"]
            assert after["speedup"] == baseline["speedup"]
            assert after["estimate_error"] == baseline["estimate_error"]
            assert after["score"] == baseline["score"]
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# Throughput Score: formula constants, min(1, ...), clamp 0-100, penalty
# ---------------------------------------------------------------------------


class TestThroughputScoreFormula:
    def test_score_clamps_to_bounds(self) -> None:
        low = summary._throughput_score(
            speedup=0.0,
            parallelism_ratio=1.0,
            utilization=0.0,
            first_attempt_rate=0.0,
            rate_limit_stall_fraction=1.0,
            estimate_error_penalty=summary.ESTIMATE_ERROR_PENALTY_CAP,
        )
        assert low == 0.0

        high = summary._throughput_score(
            speedup=100.0,
            parallelism_ratio=1.0,
            utilization=1.0,
            first_attempt_rate=1.0,
            rate_limit_stall_fraction=0.0,
            estimate_error_penalty=0.0,
        )
        assert high == 100.0

    def test_speedup_term_caps_at_one_via_min(self) -> None:
        # speedup way past parallelism_ratio must not blow the 40-point term.
        capped = summary._throughput_score(
            speedup=1000.0,
            parallelism_ratio=2.0,
            utilization=0.0,
            first_attempt_rate=0.0,
            rate_limit_stall_fraction=1.0,
            estimate_error_penalty=0.0,
        )
        assert capped == pytest.approx(40.0)

    def test_estimate_error_penalty_capped_at_15(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [{"id": "a", "est_manual": 0, "est_agent": 10}],
                target_n=1,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            card_ids = provisioned["card_ids"]
            t0 = "2026-01-01T00:00:00Z"
            finished = "2026-01-01T01:40:00Z"  # 100 minutes actual vs 10 est
            _insert_attempt(dal, run_id, card_ids["a"], 0, t0, finished, "completed")
            _set_run_window(
                dal,
                run_id,
                started_at=t0,
                finished_at=finished,
                status="completed",
                target_concurrency=1,
            )
            metrics = summary.compute_metrics(run_id, dal)
            # |100-10|/10 = 9.0 -> penalty = min(15, 30*9) = 15 (capped)
            assert metrics["estimate_error"] == pytest.approx(9.0)
            assert metrics["estimate_error_penalty"] == pytest.approx(15.0)
            # speedup=0 (no manual estimate); utilization=1 (single task fills
            # the whole wall on 1 slot); first_attempt_rate=1; no stall.
            # 0 + 25 + 25 + 10 - 15 = 45
            assert metrics["score"] == pytest.approx(45.0)
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# est_completion — remaining critical-path minutes (C2 overview tile)
# ---------------------------------------------------------------------------


class TestEstCompletion:
    def test_remaining_critical_path_excludes_done_tasks(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [
                    {"id": "a", "est_manual": 0, "est_agent": 10},
                    {"id": "b", "est_manual": 0, "est_agent": 20, "depends_on": ["a"]},
                    {"id": "c", "est_manual": 0, "est_agent": 5},
                    {
                        "id": "integration",
                        "est_manual": 0,
                        "est_agent": 5,
                        "depends_on": ["b", "c"],
                    },
                ],
                target_n=2,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            card_ids = provisioned["card_ids"]

            _set_status(dal, card_ids["a"], "done")

            # b(20) -> integration(5) = 25 remaining (a already done, contributes 0)
            # c(5) -> integration(5) = 10 remaining
            assert summary.est_completion(run_id, dal) == pytest.approx(25.0)
        finally:
            dal.close()

    def test_zero_when_everything_done(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [{"id": "a", "est_manual": 0, "est_agent": 10}],
                target_n=1,
                integration_id=None,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            card_ids = provisioned["card_ids"]
            _set_status(dal, card_ids["a"], "done")
            assert summary.est_completion(run_id, dal) == 0.0
        finally:
            dal.close()

    def test_unknown_run_returns_zero(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            assert summary.est_completion("swr_does_not_exist", dal) == 0.0
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# write_summary — vault note + degraded narrative + event + best-effort ingest
# ---------------------------------------------------------------------------


class _RecordingEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def emit(self, run_id: str, action: str, payload: dict | None = None) -> None:
        self.events.append((run_id, action, dict(payload or {})))


class TestWriteSummary:
    def _completed_run(self, dal: SwarmDal, tmp_path: Path) -> tuple[str, dict]:
        provisioned = _provision(
            dal,
            str(tmp_path / "ws"),
            [
                {"id": "a", "est_manual": 30, "est_agent": 10},
                {"id": "integration", "est_manual": 0, "est_agent": 5, "depends_on": ["a"]},
            ],
            target_n=1,
            parallelism_ratio=1.0,
            assumptions=["the API would not rate-limit us"],
        )
        run_id = str(provisioned["run"]["id"])
        card_ids = provisioned["card_ids"]
        t0 = "2026-01-01T00:00:00Z"
        _insert_attempt(dal, run_id, card_ids["a"], 0, t0, "2026-01-01T00:10:00Z", "completed")
        _insert_attempt(
            dal,
            run_id,
            card_ids["integration"],
            0,
            "2026-01-01T00:10:00Z",
            "2026-01-01T00:15:00Z",
            "completed",
        )
        _set_run_window(
            dal,
            run_id,
            started_at=t0,
            finished_at="2026-01-01T00:15:00Z",
            status="completed",
            target_concurrency=1,
        )
        return run_id, card_ids

    def test_degrades_to_mechanical_only_on_fable_failure(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, _ = self._completed_run(dal, tmp_path)
            emitter = _RecordingEmitter()
            vault_dir = str(tmp_path / "vault")

            def boom(*args, **kwargs):
                raise RuntimeError("fable unavailable")

            metrics = summary.write_summary(
                run_id, dal=dal, emitter=emitter, vault_dir=vault_dir, fable_runner=boom
            )

            note_path = metrics["summary_note_path"]
            assert note_path is not None
            content = Path(note_path).read_text(encoding="utf-8")
            assert "# Swarm run" in content
            assert "## Result" in content
            assert "## Bottlenecks" in content
            assert "## Planner assumptions" in content
            assert "the API would not rate-limit us" in content
            assert "Improvement opportunities" not in content

            assert dal.get_run(run_id)["summary_note_path"] == note_path
            assert len(emitter.events) == 1
            evt_run_id, action, payload = emitter.events[0]
            assert evt_run_id == run_id
            assert action == "run_completed"
            assert payload["score"] == metrics["score"]
            assert payload["summary_note_path"] == note_path
        finally:
            dal.close()

    def test_includes_narrative_section_on_fable_success(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, _ = self._completed_run(dal, tmp_path)
            vault_dir = str(tmp_path / "vault")

            def fake_fable(prompt, schema, **kwargs):
                return {"narrative": "- ship it\n- watch the retries"}

            metrics = summary.write_summary(
                run_id, dal=dal, vault_dir=vault_dir, fable_runner=fake_fable
            )
            content = Path(metrics["summary_note_path"]).read_text(encoding="utf-8")
            assert "## Improvement opportunities (Kimi)" in content
            assert "ship it" in content
        finally:
            dal.close()

    def test_degrades_when_fable_returns_garbage(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, _ = self._completed_run(dal, tmp_path)
            vault_dir = str(tmp_path / "vault")
            metrics = summary.write_summary(
                run_id, dal=dal, vault_dir=vault_dir, fable_runner=lambda *a, **k: None
            )
            content = Path(metrics["summary_note_path"]).read_text(encoding="utf-8")
            assert "Improvement opportunities" not in content
        finally:
            dal.close()

    def test_failed_run_emits_run_failed_action(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            provisioned = _provision(
                dal,
                str(tmp_path / "ws"),
                [{"id": "a", "est_manual": 0, "est_agent": 1}],
                target_n=1,
                integration_id=None,
                parallelism_ratio=1.0,
            )
            run_id = str(provisioned["run"]["id"])
            _set_run_window(
                dal,
                run_id,
                started_at="2026-01-01T00:00:00Z",
                finished_at="2026-01-01T00:01:00Z",
                status="failed",
                target_concurrency=1,
            )
            emitter = _RecordingEmitter()
            summary.write_summary(
                run_id,
                dal=dal,
                emitter=emitter,
                vault_dir=str(tmp_path / "vault"),
                fable_runner=lambda *a, **k: None,
            )
            assert emitter.events[0][1] == "run_failed"
        finally:
            dal.close()

    def test_knowledge_ingest_seam_invoked_best_effort(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, _ = self._completed_run(dal, tmp_path)
            calls: list[tuple[str, str]] = []

            def fake_ingest(rid: str, content: str) -> None:
                calls.append((rid, content))

            summary.write_summary(
                run_id,
                dal=dal,
                vault_dir=str(tmp_path / "vault"),
                fable_runner=lambda *a, **k: None,
                knowledge_ingest=fake_ingest,
            )
            assert len(calls) == 1
            assert calls[0][0] == run_id

        finally:
            dal.close()

    def test_knowledge_ingest_failure_never_raises(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, _ = self._completed_run(dal, tmp_path)

            def boom(rid: str, content: str) -> None:
                raise RuntimeError("knowledge store down")

            # Must not raise even though the injected seam blows up.
            metrics = summary.write_summary(
                run_id,
                dal=dal,
                vault_dir=str(tmp_path / "vault"),
                fable_runner=lambda *a, **k: None,
                knowledge_ingest=boom,
            )
            assert metrics.get("score") is not None
        finally:
            dal.close()

    def test_unknown_run_is_a_noop(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            assert summary.write_summary("swr_missing", dal=dal) == {}
        finally:
            dal.close()

    def test_write_summary_never_raises_even_on_total_vault_failure(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id, _ = self._completed_run(dal, tmp_path)
            # A file at the vault_dir path (instead of a directory) makes
            # every write_note call under it fail; write_summary must swallow
            # this and still return the computed metrics.
            bogus_vault = tmp_path / "not-a-dir"
            bogus_vault.write_text("nope", encoding="utf-8")
            metrics = summary.write_summary(
                run_id,
                dal=dal,
                vault_dir=str(bogus_vault),
                fable_runner=lambda *a, **k: None,
            )
            assert metrics.get("score") is not None
            assert metrics.get("summary_note_path") is None
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# three-valued formatters + vault cost-to-green section (binding tests)
# ---------------------------------------------------------------------------


def test_fmt_per_green_distinguishes_measured_zero_from_unknown() -> None:
    """Bare truthiness on a three-valued rate is the defect class.

    ``if not value`` collapses measured $0.00 and unknown into the same branch.
    Counterfeit: ``if not value: return "unknown"`` — this test fails because
    a genuine free report must still render as a number, not the word unknown.
    """
    assert summary._fmt_per_green(None, money=True) == "unknown"
    assert summary._fmt_per_green(None) == "unknown"
    # Measured free / zero must NOT become "unknown".
    assert summary._fmt_per_green(0.0, money=True) == "$0.0000"
    assert summary._fmt_per_green(0.0) == "0"
    assert summary._fmt_per_green(0, money=True) == "$0.0000"
    # Non-zero still formats.
    assert summary._fmt_per_green(2.5, money=True) == "$2.5000"
    assert summary._fmt_per_green(1500.0) == "1500"


def test_run_cost_usd_tokens_only_is_unknown_not_free() -> None:
    """``_run_cost_usd`` must not free-look tokens-only as 0.0 via run cache.

    Counterfeit: ``float(run.get("cost_usd") or 0.0)`` ignores attempt
    usage_source and publishes the legacy run-column floor as measured free.
    """
    from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT, SOURCE_TOKENS_ONLY

    class _FakeDal:
        def __init__(self, rows: list[dict]) -> None:
            self._rows = rows

        def attempts_with_usage(self, _run_id: str) -> list[dict]:
            return self._rows

    # Tokens-only + run floor 0.0 → unknown.
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [
                    {
                        "usage_source": SOURCE_TOKENS_ONLY,
                        "cost_usd": None,
                        "input_tokens": 500,
                        "output_tokens": 500,
                    }
                ]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    # Measured CLI free is still 0.0, not unknown.
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [{"usage_source": SOURCE_CLI_REPORT, "cost_usd": 0.0}]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        == 0.0
    )
    # Measured CLI sum wins over a stale run floor.
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [{"usage_source": SOURCE_CLI_REPORT, "cost_usd": 2.5}]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        == 2.5
    )
    # No usage markers: NULL run cost stays unknown (not coerced to 0.0).
    assert summary._run_cost_usd(_FakeDal([]), "swr_x", {"cost_usd": None}) is None  # type: ignore[arg-type]
    assert summary._run_cost_usd(_FakeDal([]), "swr_x", {"cost_usd": 12.0}) == 12.0  # type: ignore[arg-type]


def test_run_cost_usd_incomplete_cli_and_source_none_are_unknown() -> None:
    """CLI without parseable cost, and SOURCE_NONE, must not free-look as complete.

    Binding counterfeits this catches (production-only mutations):
    - CLI marker with missing/garbage cost coerced to ``0.0`` and summed
    - SOURCE_NONE ignored → legacy run floor ``0.0`` published as free
    - SOURCE_NONE mixed with a measured CLI row → partial floor as complete total
    """
    from omniagentos.swarm.usage_capture import (
        SOURCE_CLI_REPORT,
        SOURCE_NONE,
        SOURCE_TOKENS_ONLY,
    )

    class _FakeDal:
        def __init__(self, rows: list[dict]) -> None:
            self._rows = rows

        def attempts_with_usage(self, _run_id: str) -> list[dict]:
            return self._rows

    # SOURCE_CLI_REPORT alone cannot certify absent cost as free.
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [{"usage_source": SOURCE_CLI_REPORT, "cost_usd": None}]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [{"usage_source": SOURCE_CLI_REPORT, "cost_usd": "not-a-number"}]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    # SOURCE_NONE means usage was not measured — not free via run cache.
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [{"usage_source": SOURCE_NONE, "cost_usd": None}]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    # Mixed measured + unmeasured must not publish the partial floor as complete.
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [
                    {"usage_source": SOURCE_CLI_REPORT, "cost_usd": 2.5},
                    {"usage_source": SOURCE_NONE},
                ]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    # Tokens-only still incomplete (regression guard alongside SOURCE_NONE).
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [
                    {"usage_source": SOURCE_CLI_REPORT, "cost_usd": 1.0},
                    {"usage_source": SOURCE_TOKENS_ONLY, "cost_usd": None},
                ]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )


def test_run_cost_usd_invalid_cli_numeric_costs_are_unknown() -> None:
    """SOURCE_CLI_REPORT with bool / non-finite / negative cost cannot certify spend.

    Summary-boundary contract for ``_optional_nonneg_float``: only finite
    non-negative numeric costs certify measured spend. Bool is rejected because
    ``True``/``False`` are ``int`` subclasses (``True`` → 1.0, ``False`` → free).

    Binding counterfeits (production-only mutations this must turn red):
    - drop finite/non-negative gate → ``inf`` / ``-inf`` / negatives sum as measured
    - drop ``isinstance(value, bool)`` → ``True`` certifies 1.0, ``False`` as 0.0
    """
    from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT

    class _FakeDal:
        def __init__(self, rows: list[dict]) -> None:
            self._rows = rows

        def attempts_with_usage(self, _run_id: str) -> list[dict]:
            return self._rows

    def _cli_cost(cost: object) -> float | None:
        return summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [{"usage_source": SOURCE_CLI_REPORT, "cost_usd": cost}]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )

    # Non-finite cannot certify measured spend (would free-look as complete total).
    assert _cli_cost(float("inf")) is None
    assert _cli_cost(float("-inf")) is None
    # Negative is not valid usage — unknown, not a measured total.
    assert _cli_cost(-0.01) is None
    assert _cli_cost(-1.0) is None
    # Bool is not a cost report: True must not become 1.0, False must not free-look.
    assert _cli_cost(True) is None
    assert _cli_cost(False) is None
    # Explicit measured free and positive still certify (regression guard).
    assert _cli_cost(0.0) == 0.0
    assert _cli_cost(2.5) == 2.5


def test_run_cost_usd_absent_estimator_unknown_source_are_unknown_not_free() -> None:
    """Absent / legacy estimator / unrecognized usage_source must not free-look.

    Production shape (SwarmDal.attempts_with_usage LEFT JOIN): attempts without
    a session keep NULL usage fields. Historical sessions used the ``estimator``
    marker. The previous helper only flagged SOURCE_TOKENS_ONLY / SOURCE_NONE /
    malformed SOURCE_CLI_REPORT — so these rows were ignored and the legacy run
    floor or a partial measured sum published as a complete total.

    Binding counterfeits (production-only mutations this must turn red):
    - ignore non-(CLI/tokens/none) sources → absent free-looks as 0.0
    - same ignore path → measured CLI + absent publishes partial floor as complete
    """
    from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT

    class _FakeDal:
        def __init__(self, rows: list[dict]) -> None:
            self._rows = rows

        def attempts_with_usage(self, _run_id: str) -> list[dict]:
            return self._rows

    # Absent / NULL usage_source + run floor 0.0 → unknown (not free).
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [{"usage_source": None, "cost_usd": None}]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    # Missing key is the same production shape as NULL.
    assert (
        summary._run_cost_usd(
            _FakeDal([{}]),  # type: ignore[arg-type]
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    # Legacy sessions schema marker "estimator" is not certified CLI cost.
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [{"usage_source": "estimator", "cost_usd": 0.0}]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    # Unrecognized tag must not free-look as complete either.
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [{"usage_source": "unknown_tag", "cost_usd": 1.0}]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    # Mixed measured + absent source must not publish the partial floor as complete.
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [
                    {"usage_source": SOURCE_CLI_REPORT, "cost_usd": 2.5},
                    {"usage_source": None, "cost_usd": None},
                ]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        is None
    )
    # Fully measured CLI still sums (regression: fix must not blank all totals).
    assert (
        summary._run_cost_usd(
            _FakeDal(  # type: ignore[arg-type]
                [
                    {"usage_source": SOURCE_CLI_REPORT, "cost_usd": 1.0},
                    {"usage_source": SOURCE_CLI_REPORT, "cost_usd": 1.5},
                ]
            ),
            "swr_x",
            {"cost_usd": 0.0},
        )
        == 2.5
    )


def test_vault_note_renders_cost_to_green_section_with_unknown_and_zero() -> None:
    """Vault section must be wired and must not free-look unknown as $0.00.

    Counterfeits this catches:
    - drop ``*_cost_to_green_section(metrics)`` from the mechanical note → no heading
    - bare truthiness in ``_fmt_per_green`` → measured 0.0 renders "unknown"
    - always render None as "$0.0000" → free-look of missing cost
    - overall ``- Cost: ${... or 0}`` → unknown overall cost free-looks as $0.0000
    """
    metrics = {
        "score": 50,
        "wall_seconds": 10,
        "speedup": 1.0,
        "parallelism_ratio": 1.0,
        "utilization": 0.5,
        "mean_target_n": 1.0,
        "first_attempt_rate": 1.0,
        "rate_limit_stall_fraction": 0.0,
        "estimate_error": 0.0,
        "estimate_error_penalty": 0.0,
        "cost_usd": None,  # overall unknown — must not render Cost: $0.0000
        "cost_overshoot_usd": None,
        "tasks_done": 2,
        "tasks_blocked": 0,
        "tasks_total": 2,
        "bottlenecks": {},
        "cost_to_green_by_effort": [
            {
                "effort": "medium",
                "cost_per_green": 0.0,  # measured free — must render $0.0000
                "tokens_per_green": 100.0,
                "wall_ms_per_green": 500.0,
                "green": 1,
                "tasks": 1,
                "confidence": "measured",
            },
            {
                "effort": "high",
                "cost_per_green": None,  # unknown — must render "unknown", not $0
                "tokens_per_green": 200.0,
                "wall_ms_per_green": None,
                "green": 1,
                "tasks": 1,
                "confidence": "sparse",
            },
        ],
    }
    run = {"goal": "prove vault section", "status": "completed", "plan_json": "{}"}
    body = "\n".join(summary._mechanical_sections("swr_test", run, metrics))

    assert "## Cost-to-green by starting effort" in body
    assert "cost/green $0.0000" in body  # measured zero, not "unknown"
    assert "cost/green unknown" in body  # unknown, not "$0.0000"
    assert "tokens/green 100" in body
    assert "tokens/green 200" in body
    assert "wall_ms/green unknown" in body
    assert "confidence=measured" in body
    assert "confidence=sparse" in body
    # Overall Cost line is three-valued (binding on summary.py render site).
    assert "- Cost: unknown" in body
    assert "- Cost: $0.0000" not in body

    # Measured free overall still renders as a dollar amount, not the word unknown.
    metrics_free = {**metrics, "cost_usd": 0.0, "cost_overshoot_usd": 0.0}
    free_body = "\n".join(summary._mechanical_sections("swr_test", run, metrics_free))
    assert "- Cost: $0.0000" in free_body
    assert "- Cost: unknown" not in free_body

    # Section helper itself must emit the heading (mutation of the helper body).
    section = "\n".join(summary._cost_to_green_section(metrics))
    assert section.startswith("## Cost-to-green by starting effort")
    assert "cost/green $0.0000" in section
    assert "cost/green unknown" in section
