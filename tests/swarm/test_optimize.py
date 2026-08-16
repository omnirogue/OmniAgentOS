"""WP8 ``swarm/optimize.py``: watermark advance + idempotent re-run, seeded-
history aggregation golden numbers, learned.json shape/bounds, playbook
append-preserves-prior-content, and the degraded-narrative contract.

Every test that reaches ``run_optimize``/``_narrative_section`` injects an
explicit ``fable_runner`` (never ``None``) so the suite NEVER calls the real
Fable CLI -- mirrors ``tests/swarm/test_summary.py``'s ``TestWriteSummary``
convention.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import new_id
from omniagentos.swarm import optimize
from omniagentos.swarm.contracts import SWARM_EVENT_KIND
from omniagentos.swarm.dal import SwarmDal
from tests.support.db_template import migrated_db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    # Pre-migrated template copy: the shared schema (swarm_runs/attempts +
    # events) without re-applying all 86 migrations per test.
    return migrated_db(CollabStore, tmp_path / "optimize.db")


def _never_calls_fable(*args, **kwargs):
    raise AssertionError("optimizer must never call the real Fable CLI in tests")


def _no_narrative(*args, **kwargs):
    return None


# ---------------------------------------------------------------------------
# fixture builders (mirrors tests/swarm/test_summary.py's raw-SQL helpers --
# optimize.py's aggregation only touches swarm_runs/swarm_attempts/events, so
# no board-task provisioning is needed here)
# ---------------------------------------------------------------------------


def _make_run(dal: SwarmDal, **overrides) -> str:
    defaults = {"working_dir": "/tmp/ws", "goal": "test goal"}
    defaults.update(overrides)
    return str(dal.create_run(**defaults, source="test")["id"])


def _finish_run(
    dal: SwarmDal,
    run_id: str,
    *,
    started_at: str,
    finished_at: str,
    status: str = "completed",
    metrics: dict | None = None,
    summary_note_path: str | None = None,
) -> None:
    dal._connection.execute(
        "UPDATE swarm_runs SET started_at = ?, finished_at = ?, status = ? WHERE id = ?",
        (started_at, finished_at, status, run_id),
    )
    if metrics is not None:
        dal.set_metrics(run_id, metrics)
    if summary_note_path is not None:
        dal.set_summary_note_path(run_id, summary_note_path)


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


def _insert_event(dal: SwarmDal, run_id: str, action: str, payload: dict, ts: str) -> None:
    dal._connection.execute(
        "INSERT INTO events (ts, type, actor, action, target_type, target_id, payload_json, trace_id) "
        "VALUES (?, ?, 'test', ?, 'swarm_run', ?, ?, '')",
        (ts, SWARM_EVENT_KIND, action, run_id, json.dumps(payload)),
    )


# ---------------------------------------------------------------------------
# seeded two-run scenario shared by the golden-number tests
#
# Run A: 1 task, small plan (tasks_total=1), high utilization (0.8, "good").
#   1 claude attempt, 5.0 minutes, completed.
# Run B: medium plan (tasks_total=5), low utilization (0.5, not "good").
#   1 claude attempt (7.0 min, completed) + 1 codex attempt (3.0 min,
#   rate_limited). Events: one rate_limit_stall (120s), one task_split, one
#   provider_switched (claude -> codex), one rate_limit (codex).
# ---------------------------------------------------------------------------


def _seed_two_runs(dal: SwarmDal) -> tuple[str, str]:
    run_a = _make_run(dal)
    _insert_attempt(
        dal,
        run_a,
        "btk_a1",
        0,
        "2026-07-20T02:50:00Z",
        "2026-07-20T02:55:00Z",
        "completed",
        provider="claude",
    )
    _finish_run(
        dal,
        run_a,
        started_at="2026-07-20T02:45:00Z",
        finished_at="2026-07-20T03:00:00Z",
        metrics={"tasks_total": 1, "mean_target_n": 1.0, "utilization": 0.8},
    )

    run_b = _make_run(dal)
    _insert_attempt(
        dal,
        run_b,
        "btk_b1",
        0,
        "2026-07-20T03:00:00Z",
        "2026-07-20T03:07:00Z",
        "completed",
        provider="claude",
    )
    _insert_attempt(
        dal,
        run_b,
        "btk_b2",
        0,
        "2026-07-20T03:00:00Z",
        "2026-07-20T03:03:00Z",
        "rate_limited",
        provider="codex",
    )
    _insert_event(
        dal,
        run_b,
        "rate_limit_stall",
        {"seconds": 120, "until": "2026-07-20T03:12:00Z", "reason": "all providers cooling"},
        "2026-07-20T03:05:00Z",
    )
    _insert_event(
        dal,
        run_b,
        "task_split",
        {"task_id": "btk_b2", "subtask_ids": ["btk_b2a", "btk_b2b"], "rewired_dependents": []},
        "2026-07-20T03:06:00Z",
    )
    _insert_event(
        dal,
        run_b,
        "provider_switched",
        {
            "task_id": "btk_b2",
            "from_provider": "claude",
            "to_provider": "codex",
            "reason": "reroute",
        },
        "2026-07-20T03:06:30Z",
    )
    _insert_event(
        dal,
        run_b,
        "rate_limit",
        {"task_id": "btk_b2", "provider": "codex", "detail": "429"},
        "2026-07-20T03:03:00Z",
    )
    _finish_run(
        dal,
        run_b,
        started_at="2026-07-20T02:58:00Z",
        finished_at="2026-07-20T03:10:00Z",
        metrics={"tasks_total": 5, "mean_target_n": 4.0, "utilization": 0.5},
    )
    return run_a, run_b


# ---------------------------------------------------------------------------
# aggregation golden numbers
# ---------------------------------------------------------------------------


class TestAggregationGoldenNumbers:
    def test_provider_success_rates_and_median_latency(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_a, run_b = _seed_two_runs(dal)
            providers = optimize._aggregate_providers(dal, [dal.get_run(run_a), dal.get_run(run_b)])

            assert providers["claude"]["attempts"] == 2
            assert providers["claude"]["successes"] == 2
            assert providers["claude"]["success_rate"] == pytest.approx(1.0)
            assert providers["claude"]["median_attempt_minutes"] == pytest.approx(6.0)

            assert providers["codex"]["attempts"] == 1
            assert providers["codex"]["successes"] == 0
            assert providers["codex"]["success_rate"] == pytest.approx(0.0)
            assert providers["codex"]["median_attempt_minutes"] == pytest.approx(3.0)
        finally:
            dal.close()

    def test_recurring_bottleneck_event_tallies(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_a, run_b = _seed_two_runs(dal)
            bottlenecks = optimize._aggregate_events(dal, [dal.get_run(run_a), dal.get_run(run_b)])

            assert bottlenecks["stall_count"] == 1
            assert bottlenecks["stall_seconds"] == pytest.approx(120.0)
            assert bottlenecks["split_count"] == 1
            assert bottlenecks["switch_count"] == 1
            assert bottlenecks["switch_pairs"] == [(("claude", "codex"), 1)]
            assert bottlenecks["rate_limit_by_provider"] == [("codex", 1)]
        finally:
            dal.close()

    def test_sizing_by_plan_size_bucket_and_observed_good_concurrency(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_a, run_b = _seed_two_runs(dal)
            sizing = optimize._aggregate_sizing([dal.get_run(run_a), dal.get_run(run_b)])

            small = sizing["small (1-3 tasks)"]
            assert small["runs"] == 1
            assert small["median_target_n"] == pytest.approx(1.0)
            assert small["median_utilization"] == pytest.approx(0.8)
            # utilization 0.8 clears the 0.6 "good" bar -> observed_good == target_n itself.
            assert small["observed_good_concurrency"] == 1

            medium = sizing["medium (4-8 tasks)"]
            assert medium["runs"] == 1
            assert medium["median_target_n"] == pytest.approx(4.0)
            assert medium["median_utilization"] == pytest.approx(0.5)
            # utilization 0.5 misses the bar -> falls back to the bucket's own target_n.
            assert medium["observed_good_concurrency"] == 4

            assert "large (9+ tasks)" not in sizing
        finally:
            dal.close()

    def test_runs_with_no_metrics_json_are_skipped_by_sizing(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            _finish_run(
                dal,
                run_id,
                started_at="2026-07-20T00:00:00Z",
                finished_at="2026-07-20T00:05:00Z",
                status="failed",
            )  # no metrics -- e.g. a coordinator crash before write_summary ran
            sizing = optimize._aggregate_sizing([dal.get_run(run_id)])
            assert sizing == {}
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# learned.json shape + bounds
# ---------------------------------------------------------------------------


class TestLearnedJsonShapeAndBounds:
    def test_shape_and_normal_bounds(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            result = optimize.run_optimize(
                dal=dal,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "vault" / "swarm" / "playbook.md"),
                fable_runner=_no_narrative,
                knowledge_ingest=lambda *a: None,
            )
            assert result.learned_written is True
            payload = json.loads(Path(result.learned_path).read_text())

            assert set(payload) == {
                "generated_at",
                "since_finished_at",
                "runs_analyzed_total",
                "providers",
                "concurrency_by_plan_size",
                "win_rate_by_effort",
                "cost_to_green_by_effort",
            }
            assert isinstance(payload["cost_to_green_by_effort"], list)
            assert payload["runs_analyzed_total"] == 2

            for stats in payload["providers"].values():
                assert 0.0 <= stats["success_rate"] <= 1.0
                if stats["median_attempt_minutes"] is not None:
                    assert stats["median_attempt_minutes"] >= 0.0

            for stats in payload["concurrency_by_plan_size"].values():
                assert 0.0 <= stats["median_utilization"] <= 1.0
                assert 1 <= stats["observed_good_concurrency"] <= 10
        finally:
            dal.close()

    def test_extreme_inputs_clamp_to_hard_bounds(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            _finish_run(
                dal,
                run_id,
                started_at="2026-07-20T00:00:00Z",
                finished_at="2026-07-20T00:05:00Z",
                # Simulated corrupt/out-of-range data: utilization > 1.0 and an
                # absurd target_n. The advisory field a router would consult
                # (observed_good_concurrency) must never leave
                # [1, _MAX_CONCURRENCY_CEILING] — the ceiling tracks
                # scheduler.MAX_SLOTS, so assert against the constant, not a
                # literal that goes stale the next time the fleet widens.
                metrics={"tasks_total": 12, "mean_target_n": 999.0, "utilization": 1.5},
            )
            sizing = optimize._aggregate_sizing([dal.get_run(run_id)])
            large = sizing["large (9+ tasks)"]
            assert large["median_utilization"] == pytest.approx(1.0)  # clamped
            assert (
                large["observed_good_concurrency"] == optimize._MAX_CONCURRENCY_CEILING
            )  # clamped to the ceiling
        finally:
            dal.close()

    def test_zero_attempt_provider_never_divides_by_zero(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            _finish_run(
                dal,
                run_id,
                started_at="2026-07-20T00:00:00Z",
                finished_at="2026-07-20T00:05:00Z",
                metrics={"tasks_total": 1},
            )
            providers = optimize._aggregate_providers(dal, [dal.get_run(run_id)])
            assert providers == {}  # no attempts recorded at all -> nothing to report
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# watermark advance + idempotent re-run (no duplicate playbook sections)
# ---------------------------------------------------------------------------


class TestWatermarkAndIdempotentRerun:
    def _run(self, dal: SwarmDal, tmp_path: Path, **kwargs):
        return optimize.run_optimize(
            dal=dal,
            state_path=str(tmp_path / "state.json"),
            learned_path=str(tmp_path / "learned.json"),
            playbook_path=str(tmp_path / "vault" / "swarm" / "playbook.md"),
            fable_runner=_no_narrative,
            knowledge_ingest=lambda *a: None,
            **kwargs,
        )

    def test_first_pass_processes_all_and_advances_watermark(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            run_a, run_b = _seed_two_runs(dal)
            result = self._run(dal, tmp_path)

            assert result.runs_analyzed == 2
            assert result.watermark_advanced is True
            assert result.playbook_written is True

            state = json.loads(Path(result.state_path).read_text())
            assert state["since_finished_at"] == dal.get_run(run_b)["finished_at"]
            assert state["runs_processed_total"] == 2
        finally:
            dal.close()

    def test_rerun_with_no_new_runs_is_a_pure_noop_on_the_playbook(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            first = self._run(dal, tmp_path)
            assert first.playbook_written is True
            content_after_first = Path(first.playbook_path).read_text()

            second = self._run(dal, tmp_path)
            assert second.runs_analyzed == 0
            assert second.watermark_advanced is False
            assert second.playbook_written is False
            content_after_second = Path(second.playbook_path).read_text()

            # Byte-for-byte identical: no duplicate section appended.
            assert content_after_second == content_after_first
            assert content_after_second.count("Swarm optimizer pass") == 1
        finally:
            dal.close()

    def test_third_run_only_analyzes_the_new_run(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            self._run(dal, tmp_path)

            run_c = _make_run(dal)
            _insert_attempt(
                dal,
                run_c,
                "btk_c1",
                0,
                "2026-07-21T00:00:00Z",
                "2026-07-21T00:04:00Z",
                "completed",
                provider="gemini",
            )
            _finish_run(
                dal,
                run_c,
                started_at="2026-07-20T23:58:00Z",
                finished_at="2026-07-21T00:05:00Z",
                metrics={"tasks_total": 2, "mean_target_n": 2.0, "utilization": 0.9},
            )

            third = self._run(dal, tmp_path)
            assert third.runs_analyzed == 1
            assert third.watermark_advanced is True
            content = Path(third.playbook_path).read_text()
            assert content.count("Swarm optimizer pass") == 2
            assert "gemini" in content
        finally:
            dal.close()

    def test_watermark_missing_or_corrupt_state_file_means_from_the_beginning(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            state_path = tmp_path / "state.json"
            state_path.write_text("{not json", encoding="utf-8")
            result = self._run(dal, tmp_path)
            assert result.runs_analyzed == 2
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# playbook append preserves prior content
# ---------------------------------------------------------------------------


class TestPlaybookAppendPreservesPriorContent:
    def test_second_pass_appends_after_first_verbatim(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_a, _ = _seed_two_runs(dal)  # only run_a will be processed first
            state_path = str(tmp_path / "state.json")
            learned_path = str(tmp_path / "learned.json")
            playbook_path = str(tmp_path / "vault" / "swarm" / "playbook.md")

            # Isolate run_a as the ONLY finished run for pass 1 by re-queuing run_b.
            dal._connection.execute(
                "UPDATE swarm_runs SET status = 'running', finished_at = NULL WHERE id != ?",
                (run_a,),
            )
            first = optimize.run_optimize(
                dal=dal,
                state_path=state_path,
                learned_path=learned_path,
                playbook_path=playbook_path,
                fable_runner=_no_narrative,
                knowledge_ingest=lambda *a: None,
            )
            assert first.playbook_written is True
            content1 = Path(playbook_path).read_text()
            assert "# Swarm optimizer playbook" in content1
            assert content1.count("### Provider success rates & latency") == 1

            # Re-finish run_b (a distinct later finished_at) so pass 2 has new data.
            dal._connection.execute(
                "UPDATE swarm_runs SET status = 'completed', "
                "finished_at = '2026-07-22T00:00:00Z' WHERE id != ?",
                (run_a,),
            )
            second = optimize.run_optimize(
                dal=dal,
                state_path=state_path,
                learned_path=learned_path,
                playbook_path=playbook_path,
                fable_runner=_no_narrative,
                knowledge_ingest=lambda *a: None,
            )
            assert second.playbook_written is True
            content2 = Path(playbook_path).read_text()

            # Every byte of pass 1's output survives, unmodified, as a prefix.
            assert content2.startswith(content1.rstrip("\n"))
            assert content2.count("### Provider success rates & latency") == 2
            assert content2.count("# Swarm optimizer playbook") == 1  # header written ONCE
        finally:
            dal.close()

    def test_creates_frontmatter_and_intro_on_first_write_only(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            playbook_path = str(tmp_path / "vault" / "swarm" / "playbook.md")
            optimize.run_optimize(
                dal=dal,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=playbook_path,
                fable_runner=_no_narrative,
                knowledge_ingest=lambda *a: None,
            )
            content = Path(playbook_path).read_text()
            assert content.startswith("---\n")
            assert "type: playbook" in content
            assert "discipline: swarm" in content
            assert "[[Home]]" in content
            assert "never auto-edited" in content or "human-applied" in content
        finally:
            dal.close()

    def test_preserves_hand_edited_notes_human_section(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_a, _ = _seed_two_runs(dal)
            dal._connection.execute(
                "UPDATE swarm_runs SET status = 'running', finished_at = NULL WHERE id != ?",
                (run_a,),
            )
            playbook_path = tmp_path / "vault" / "swarm" / "playbook.md"
            state_path = str(tmp_path / "state.json")
            learned_path = str(tmp_path / "learned.json")

            optimize.run_optimize(
                dal=dal,
                state_path=state_path,
                learned_path=learned_path,
                playbook_path=str(playbook_path),
                fable_runner=_no_narrative,
                knowledge_ingest=lambda *a: None,
            )
            # A human hand-edits the file, adding their own trailing notes.
            existing = playbook_path.read_text()
            playbook_path.write_text(existing + "\n## Notes (human)\n\nDon't touch this.\n")

            dal._connection.execute(
                "UPDATE swarm_runs SET status = 'completed', "
                "finished_at = '2026-07-22T00:00:00Z' WHERE id != ?",
                (run_a,),
            )
            optimize.run_optimize(
                dal=dal,
                state_path=state_path,
                learned_path=learned_path,
                playbook_path=str(playbook_path),
                fable_runner=_no_narrative,
                knowledge_ingest=lambda *a: None,
            )
            final = playbook_path.read_text()
            assert "Don't touch this." in final
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# degraded narrative (mirrors swarm.summary's degrade-on-any-failure contract)
# ---------------------------------------------------------------------------


class TestDegradedNarrative:
    def test_degrades_silently_on_fable_exception(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)

            def boom(*args, **kwargs):
                raise RuntimeError("fable unavailable")

            result = optimize.run_optimize(
                dal=dal,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "vault" / "swarm" / "playbook.md"),
                fable_runner=boom,
                knowledge_ingest=lambda *a: None,
            )
            assert result.narrative_included is False
            assert result.playbook_written is True  # mechanical sections still land
            content = Path(result.playbook_path).read_text()
            assert "### Provider success rates & latency" in content
            assert "Improvement opportunities" not in content
        finally:
            dal.close()

    def test_degrades_silently_when_fable_returns_garbage(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            result = optimize.run_optimize(
                dal=dal,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "vault" / "swarm" / "playbook.md"),
                fable_runner=lambda *a, **k: {"not_narrative": "oops"},
                knowledge_ingest=lambda *a: None,
            )
            assert result.narrative_included is False
            content = Path(result.playbook_path).read_text()
            assert "Improvement opportunities" not in content
        finally:
            dal.close()

    def test_degrades_when_fable_returns_empty_string(self, tmp_path: Path, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            result = optimize.run_optimize(
                dal=dal,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "vault" / "swarm" / "playbook.md"),
                fable_runner=lambda *a, **k: {"narrative": "   "},
                knowledge_ingest=lambda *a: None,
            )
            assert result.narrative_included is False
        finally:
            dal.close()

    def test_includes_narrative_section_on_fable_success(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            result = optimize.run_optimize(
                dal=dal,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "vault" / "swarm" / "playbook.md"),
                fable_runner=lambda *a, **k: {
                    "narrative": "- shrink codex timeouts\n- raise claude concurrency"
                },
                knowledge_ingest=lambda *a: None,
            )
            assert result.narrative_included is True
            content = Path(result.playbook_path).read_text()
            assert "### Improvement opportunities (Kimi)" in content
            assert "shrink codex timeouts" in content
        finally:
            dal.close()

    def test_run_optimize_never_raises_even_if_fable_runner_itself_is_buggy(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            # A pathological runner that raises something exotic, not just RuntimeError.
            result = optimize.run_optimize(
                dal=dal,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "vault" / "swarm" / "playbook.md"),
                fable_runner=lambda *a, **k: (_ for _ in ()).throw(ValueError("weird")),
                knowledge_ingest=lambda *a: None,
            )
            assert result.narrative_included is False
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# knowledge ingest (best-effort, discipline "swarm")
# ---------------------------------------------------------------------------


class TestKnowledgeIngest:
    def test_ingest_invoked_best_effort_with_swarm_discipline_content(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)
            calls: list[tuple[str, str]] = []

            def recorder(source_ref: str, content: str) -> None:
                calls.append((source_ref, content))

            result = optimize.run_optimize(
                dal=dal,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "vault" / "swarm" / "playbook.md"),
                fable_runner=_no_narrative,
                knowledge_ingest=recorder,
            )
            assert result.knowledge_ingested is True
            assert len(calls) == 1
            source_ref, content = calls[0]
            assert source_ref == "swarm-optimizer"
            assert "claude" in content
        finally:
            dal.close()

    def test_ingest_failure_never_raises_or_blocks_the_pass(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            _seed_two_runs(dal)

            def boom(*args) -> None:
                raise RuntimeError("knowledge store unavailable")

            result = optimize.run_optimize(
                dal=dal,
                state_path=str(tmp_path / "state.json"),
                learned_path=str(tmp_path / "learned.json"),
                playbook_path=str(tmp_path / "vault" / "swarm" / "playbook.md"),
                fable_runner=_no_narrative,
                knowledge_ingest=boom,
            )
            assert result.knowledge_ingested is False
            assert result.playbook_written is True  # the rest of the pass still landed
        finally:
            dal.close()


# ---------------------------------------------------------------------------
# prior-run narrative excerpts (mechanical text mined from each run's own
# WP7 summary note -- not an LLM call)
# ---------------------------------------------------------------------------


class TestPriorRunNarrativeExcerpts:
    def test_excerpt_pulled_from_the_runs_own_summary_note(
        self, tmp_path: Path, db_path: str
    ) -> None:
        dal = SwarmDal(db_path)
        try:
            run_a, run_b = _seed_two_runs(dal)
            note_path = tmp_path / "run_a_note.md"
            note_path.write_text(
                "---\nid: x\ntype: run\ndiscipline: swarm\ncreated: '2026-07-20T03:00:00Z'\n"
                "source_run: null\nconfidence: null\nstatus: active\nsupersedes: null\n---\n"
                "# Swarm run\n\n## Result\n\n- Status: done\n\n"
                "## Improvement opportunities (Fable)\n\n"
                "- codex kept timing out on task b2\n- consider raising claude concurrency\n",
                encoding="utf-8",
            )
            dal.set_summary_note_path(run_a, str(note_path))

            excerpts = optimize._collect_narrative_excerpts(
                [dal.get_run(run_a), dal.get_run(run_b)]
            )
            assert len(excerpts) == 1
            found_run_id, body = excerpts[0]
            assert found_run_id == run_a
            assert "codex kept timing out" in body
        finally:
            dal.close()

    def test_missing_or_note_less_runs_contribute_nothing(self, db_path: str) -> None:
        dal = SwarmDal(db_path)
        try:
            run_id = _make_run(dal)
            _finish_run(
                dal,
                run_id,
                started_at="2026-07-20T00:00:00Z",
                finished_at="2026-07-20T00:05:00Z",
            )  # summary_note_path left NULL
            excerpts = optimize._collect_narrative_excerpts([dal.get_run(run_id)])
            assert excerpts == []
        finally:
            dal.close()


def test_fmt_optional_number_distinguishes_measured_zero_from_unknown() -> None:
    """Bare truthiness on a three-valued rate is the defect class.

    ``if not value: return "unknown"`` collapses measured 0.0 and None.
    Counterfeit: replace ``if value is None`` with ``if not value`` — this fails
    because a genuine free report must still render as a number.
    """
    assert optimize._fmt_optional_number(None) == "unknown"
    assert optimize._fmt_optional_number(0.0) == "0"
    assert optimize._fmt_optional_number(0) == "0"
    assert optimize._fmt_optional_number(4.0) == "4"
    assert optimize._fmt_optional_number(2.5) == "2.5"
    # Non-numeric / bool also unknown (same three-valued hygiene).
    assert optimize._fmt_optional_number(True) == "unknown"
    assert optimize._fmt_optional_number("x") == "unknown"
