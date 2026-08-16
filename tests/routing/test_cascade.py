"""Tests for omniagentos.routing.cascade -- the verification-gated router
cascade. work()/verify()/reflect() are local closures; cascade.py is
adapter-agnostic by design (see its module docstring), so no adapter, CLI,
or network call is ever exercised here."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omniagentos.routing.cascade import (
    AttemptOutcome,
    CascadeTier,
    load_ladder,
    record_trace,
    run_cascade,
)
from omniagentos.swarm.router import SwarmRouter
from tests.swarm.scheduler_fakes import make_harness, make_scheduler
from tests.swarm.test_router_lanes import FakeRouterLimits, _agent

TIER0 = CascadeTier(name="tier0", adapter="cli-codex", model="gpt-5.6-luna", cost_rank=1.0)
TIER1 = CascadeTier(name="tier1", adapter="cli-claude", model="sonnet", cost_rank=4.0)
TIER2 = CascadeTier(name="tier2", adapter="cli-claude", model="opus", cost_rank=12.0)


def _read_trace_rows(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_cheap_tier_pass_no_escalation(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"
    calls: list[tuple[str, str | None]] = []

    def work(tier: CascadeTier, reflection: str | None) -> AttemptOutcome:
        calls.append((tier.name, reflection))
        return AttemptOutcome(output="ok-output", evidence="all tests passed")

    def verify(outcome: AttemptOutcome) -> tuple[bool, str]:
        return True, "verified ok"

    result = run_cascade(
        work,
        verify,
        [TIER0, TIER1],
        task_class="unit-test-simple",
        trace_path=str(trace_path),
    )

    assert result.succeeded is True
    assert len(result.attempts) == 1
    assert result.attempts[0].tier.name == "tier0"
    assert result.attempts[0].verified is True
    assert result.final_output == "ok-output"
    assert calls == [("tier0", None)]

    rows = _read_trace_rows(trace_path)
    assert len(rows) == 1
    assert rows[0]["tier_name"] == "tier0"
    assert rows[0]["verified"] is True
    assert rows[0]["error"] is False
    assert rows[0]["escalated_from"] is None


def test_cheap_fail_escalates_with_reflection(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"
    calls: list[tuple[str, str | None]] = []

    def work(tier: CascadeTier, reflection: str | None) -> AttemptOutcome:
        calls.append((tier.name, reflection))
        if tier.name == "tier0":
            return AttemptOutcome(output="bad", evidence="AssertionError: off by one")
        return AttemptOutcome(output="good", evidence="all tests passed")

    def verify(outcome: AttemptOutcome) -> tuple[bool, str]:
        return (outcome.output == "good"), f"checked {outcome.output}"

    def reflect(verify_detail: str, evidence: str) -> str:
        return f"reflection-because:{evidence}"

    result = run_cascade(
        work,
        verify,
        [TIER0, TIER1],
        task_class="unit-test-escalate",
        reflect=reflect,
        trace_path=str(trace_path),
    )

    assert result.succeeded is True
    assert len(result.attempts) == 2
    assert calls[0] == ("tier0", None)
    assert calls[1] == ("tier1", "reflection-because:AssertionError: off by one")
    assert result.attempts[1].reflection == "reflection-because:AssertionError: off by one"
    assert result.attempts[0].reflection is None

    rows = _read_trace_rows(trace_path)
    assert len(rows) == 2
    assert rows[0]["verified"] is False
    assert rows[0]["escalated_from"] is None
    assert rows[1]["verified"] is True
    assert rows[1]["escalated_from"] == "tier0"


def test_work_raises_recorded_error_tier1_still_runs(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"

    def work(tier: CascadeTier, reflection: str | None) -> AttemptOutcome:
        if tier.name == "tier0":
            raise RuntimeError("cli crashed")
        return AttemptOutcome(output="good", evidence="fine")

    def verify(outcome: AttemptOutcome) -> tuple[bool, str]:
        return True, "ok"

    result = run_cascade(
        work,
        verify,
        [TIER0, TIER1],
        task_class="unit-test-error",
        trace_path=str(trace_path),
    )

    assert len(result.attempts) == 2
    assert result.attempts[0].verified is False
    assert result.attempts[0].error == "cli crashed"
    assert result.attempts[1].verified is True
    assert result.succeeded is True

    rows = _read_trace_rows(trace_path)
    assert rows[0]["error"] is True
    assert rows[1]["error"] is False


def test_all_tiers_fail(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"

    def work(tier: CascadeTier, reflection: str | None) -> AttemptOutcome:
        return AttemptOutcome(output="never-good", evidence="still broken")

    def verify(outcome: AttemptOutcome) -> tuple[bool, str]:
        return False, "still failing"

    ladder = [TIER0, TIER1, TIER2]
    result = run_cascade(
        work,
        verify,
        ladder,
        task_class="unit-test-allfail",
        trace_path=str(trace_path),
    )

    assert result.succeeded is False
    assert len(result.attempts) == len(ladder)
    assert result.final_output is None
    assert all(not attempt.verified for attempt in result.attempts)


def test_start_tier_honored(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"
    calls: list[str] = []

    def work(tier: CascadeTier, reflection: str | None) -> AttemptOutcome:
        calls.append(tier.name)
        return AttemptOutcome(output="ok", evidence="fine")

    def verify(outcome: AttemptOutcome) -> tuple[bool, str]:
        return True, "ok"

    result = run_cascade(
        work,
        verify,
        [TIER0, TIER1, TIER2],
        task_class="unit-test-starttier",
        trace_path=str(trace_path),
        start_tier=2,
    )

    assert calls == ["tier2"]
    assert len(result.attempts) == 1
    assert result.attempts[0].tier.name == "tier2"


def test_start_tier_defaults_to_recommendation_from_history(tmp_path: Path) -> None:
    """No explicit start_tier + strong recorded history that tier0 always
    fails and tier1 always wins for this class -> the cascade should skip
    straight to tier1 (via learn.recommend_start_tier), never calling
    tier0's work() at all."""
    trace_path = tmp_path / "traces.jsonl"
    rows = []
    for _ in range(20):
        rows.append(
            {
                "ts": 0.0,
                "task_class": "seeded-class",
                "tier_name": "tier0",
                "adapter": "cli-codex",
                "model": "gpt-5.6-luna",
                "verified": False,
                "seconds": 0.1,
                "error": False,
                "escalated_from": None,
            }
        )
        rows.append(
            {
                "ts": 0.0,
                "task_class": "seeded-class",
                "tier_name": "tier1",
                "adapter": "cli-claude",
                "model": "sonnet",
                "verified": True,
                "seconds": 0.1,
                "error": False,
                "escalated_from": "tier0",
            }
        )
    trace_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    calls: list[str] = []

    def work(tier: CascadeTier, reflection: str | None) -> AttemptOutcome:
        calls.append(tier.name)
        return AttemptOutcome(output="ok", evidence="fine")

    def verify(outcome: AttemptOutcome) -> tuple[bool, str]:
        return True, "ok"

    result = run_cascade(
        work,
        verify,
        [TIER0, TIER1],
        task_class="seeded-class",
        trace_path=str(trace_path),
    )

    assert calls == ["tier1"]
    assert result.succeeded is True


def test_trace_write_failure_does_not_raise(tmp_path: Path) -> None:
    # A regular FILE where a directory is expected: os.makedirs on the
    # "parent" raises NotADirectoryError, exercising the write-failure path
    # without depending on a real permissions-restricted location.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    trace_path = blocker / "sub" / "traces.jsonl"

    def work(tier: CascadeTier, reflection: str | None) -> AttemptOutcome:
        return AttemptOutcome(output="ok", evidence="fine")

    def verify(outcome: AttemptOutcome) -> tuple[bool, str]:
        return True, "ok"

    result = run_cascade(
        work,
        verify,
        [TIER0],
        task_class="unit-test-tracefail",
        trace_path=str(trace_path),
    )

    assert result.succeeded is True
    assert not trace_path.exists()


def _record(path: Path, max_bytes: int) -> None:
    record_trace(
        str(path),
        task_class="rot",
        tier_name="tier0",
        adapter="cli-codex",
        model=None,
        verified=True,
        seconds=0.1,
        error=False,
        escalated_from=None,
        max_bytes=max_bytes,
    )


def test_record_trace_rotates_at_size_threshold(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"
    # A tiny threshold so a couple of rows trip rotation deterministically.
    _record(trace_path, max_bytes=50)  # first write: file created, below threshold
    first_size = trace_path.stat().st_size
    assert first_size > 50  # one row already exceeds the tiny threshold
    assert not (tmp_path / "traces.jsonl.1").exists()  # nothing to rotate yet

    _record(trace_path, max_bytes=50)  # second write: rotates the oversized file first
    rotated = tmp_path / "traces.jsonl.1"
    assert rotated.exists()  # exactly one rotation kept
    assert len(_read_trace_rows(rotated)) == 1  # the first row moved to .1
    assert len(_read_trace_rows(trace_path)) == 1  # fresh file holds only the new row


def test_record_trace_keeps_single_rotation(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"
    for _ in range(4):
        _record(trace_path, max_bytes=50)
    # Only one backup is ever kept (each rotation overwrites the previous .1).
    assert (tmp_path / "traces.jsonl.1").exists()
    assert not (tmp_path / "traces.jsonl.2").exists()


def test_record_trace_no_rotation_when_disabled(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces.jsonl"
    for _ in range(5):
        _record(trace_path, max_bytes=0)  # rotation disabled
    assert not (tmp_path / "traces.jsonl.1").exists()
    assert len(_read_trace_rows(trace_path)) == 5


def test_load_ladder_reads_shipped_default_config() -> None:
    ladder = load_ladder()
    assert len(ladder) >= 2
    assert all(isinstance(tier, CascadeTier) for tier in ladder)
    # Owner formation 2026-07-27: gemini coder x2 -> Grok 4.5 -> Sol -> Fable.
    assert [tier.adapter for tier in ladder] == [
        "cli-gemini",
        "cli-gemini",
        "cli-grok",
        "cli-codex",
        "cli-claude",
    ]
    assert [tier.model for tier in ladder] == [
        "gemini-3.6-flash",
        "gemini-3.6-flash",
        "grok-4.5",
        "gpt-5.6-sol",
        "fable",
    ]
    # Gemini must get exactly two attempts before any escalation.
    assert [t.adapter for t in ladder[:2]] == ["cli-gemini", "cli-gemini"]
    assert ladder[2].model == "grok-4.5"
    # cost_rank must be strictly increasing (cheapest tier first) for the
    # learner's expected-cost fallback to make sense.
    cost_ranks = [tier.cost_rank for tier in ladder]
    assert cost_ranks == sorted(cost_ranks)
    assert cost_ranks[0] < cost_ranks[-1]


def _assert_shipped_swarm_ladder(tmp_path: Path) -> None:
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "configs" / "swarm.yaml").read_text(encoding="utf-8")
    )
    rankings = {
        "sol": _agent("sol", "codex", "gpt-5.6-sol", coding=0.95),
        "opus": _agent("opus", "claude", "claude-opus-5", coding=0.90),
        "grok": _agent("grok", "grok", "grok-4.5", coding=0.85),
    }
    harness = make_harness(
        tmp_path, [{"id": "owner-ladder", "complexity": "simple"}], integration=False
    )
    router = SwarmRouter(
        config=config,
        limits=FakeRouterLimits(),
        lineage_providers={},
        rankings_loader=lambda: rankings,
        digest_loader=lambda: None,
        provider_health_loader=lambda: None,
        samples_loader=lambda: [],
        attempts_loader=harness.dal.list_attempts,
        semantic_pins=False,
        model_lineages={
            "gemini-3.6-flash": "gemini",
            "grok-4.5": "grok",
            "gpt-5.6-sol": "codex",
            "claude-opus-5": "claude",
            "claude-fable-5": "claude",
        },
    )
    failures = 0

    def verifier(task, swarm_json, working_dir):
        nonlocal failures
        del task, swarm_json, working_dir
        failures += 1
        return (failures > 4, "coder failed")

    try:
        scheduler = make_scheduler(harness, router=router, verifier=verifier, retry_cap=4)
        handle = scheduler.start_run(harness.run_id)
        assert handle is not None
        assert handle.join(timeout=20)
        assert [
            (attempt["tier"], attempt["model"])
            for attempt in harness.attempts_of("owner-ladder")
        ] == [
            ("simple", "gemini-3.6-flash"),
            ("simple", "gemini-3.6-flash"),
            ("standard", "grok-4.5"),
            ("complex", "gpt-5.6-sol"),
            ("complex", "fable"),
        ]
    finally:
        harness.close()


def test_shipped_swarm_ladder_retries_then_escalates_models(tmp_path: Path) -> None:
    """A scheduler task fails four times and walks the shipped model sequence."""
    _assert_shipped_swarm_ladder(tmp_path)


def test_shipped_swarm_ladder_ignores_ambient_config_and_health_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped-ladder regression remains pinned after hostile predecessor state."""
    hostile_config = tmp_path / "hostile-swarm.yaml"
    hostile_config.write_text(
        "router:\n  model_ladder:\n    - gpt-5.6-sol\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(hostile_config))

    from omniagentos.swarm import router as router_module

    monkeypatch.setattr(
        router_module,
        "healthy_providers_from_snapshot",
        lambda *_args, **_kwargs: {"codex"},
    )

    _assert_shipped_swarm_ladder(tmp_path)
