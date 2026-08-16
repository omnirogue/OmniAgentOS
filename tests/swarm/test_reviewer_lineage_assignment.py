from __future__ import annotations

from typing import Any

from omniagentos.contracts import HarnessType
from omniagentos.swarm.scheduler import (
    CrossLineageSwarmReviewer,
    RouteDecision,
)
from tests.swarm.scheduler_fakes import make_harness, make_scheduler


class _SolRouter:
    def route(self, task: dict[str, Any], tier: str) -> RouteDecision:
        del task
        return RouteDecision(
            provider="codex",
            model="gpt-5.6-sol",
            tier=tier,
        )


class _UnknownRouter:
    def route(self, task: dict[str, Any], tier: str) -> RouteDecision:
        del task
        return RouteDecision(
            provider="future-provider",
            model="some-new-model-9",
            tier=tier,
        )


class _ConfirmingAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, agent_input: Any) -> Any:
        del agent_input
        self.calls += 1

        class _Out:
            output_json = {"verdict": "confirm", "feedback": "counterfeit confirm"}

        return _Out()


def test_reviewer_adapter_resolution_preserves_declared_lineage(
    monkeypatch: Any,
) -> None:
    resolved: list[HarnessType] = []
    sentinel = object()

    def fake_resolve_adapter(harness: HarnessType) -> object:
        resolved.append(harness)
        return sentinel

    monkeypatch.setattr(
        "omniagentos.adapters.registry.resolve_adapter",
        fake_resolve_adapter,
    )

    reviewer = CrossLineageSwarmReviewer()
    assert reviewer._resolve({"formation_reviewer": "fable"}) is sentinel
    assert resolved == [HarnessType.CLI_CLAUDE]


def test_scheduler_refuses_sol_implementation_with_codex_reviewer(
    tmp_path: Any,
) -> None:
    """The real assignment path must block before a same-lineage adapter runs."""
    harness = make_harness(
        tmp_path,
        [{"id": "same-lineage"}],
        max_concurrency=1,
        integration=False,
    )
    swarm_json = harness.swarm_json_of("same-lineage")
    swarm_json["formation_reviewer"] = "codex"
    assert harness.dal.set_swarm_json(
        harness.task_id("same-lineage"),
        swarm_json,
    )
    adapter = _ConfirmingAdapter()
    try:
        scheduler = make_scheduler(
            harness,
            router=_SolRouter(),
            reviewer=CrossLineageSwarmReviewer(adapter=adapter),
        )
        handle = scheduler.start_run(harness.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        assert adapter.calls == 0
        assert harness.status_of("same-lineage") == "blocked"
        attempts = harness.attempts_of("same-lineage")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "blocked"
        assert "same lineage" in attempts[0]["detail"]
    finally:
        harness.close()


def test_scheduler_allows_sol_implementation_with_opus_reviewer(
    tmp_path: Any,
) -> None:
    harness = make_harness(
        tmp_path,
        [{"id": "cross-lineage"}],
        max_concurrency=1,
        integration=False,
    )
    swarm_json = harness.swarm_json_of("cross-lineage")
    swarm_json["formation_reviewer"] = "opus"
    assert harness.dal.set_swarm_json(
        harness.task_id("cross-lineage"),
        swarm_json,
    )
    adapter = _ConfirmingAdapter()
    try:
        scheduler = make_scheduler(
            harness,
            router=_SolRouter(),
            reviewer=CrossLineageSwarmReviewer(adapter=adapter),
        )
        handle = scheduler.start_run(harness.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        assert adapter.calls == 1
        assert harness.status_of("cross-lineage") == "done"
        attempts = harness.attempts_of("cross-lineage")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "completed"
    finally:
        harness.close()


def test_scheduler_refuses_one_reviewer_for_security_surface(
    tmp_path: Any,
) -> None:
    harness = make_harness(
        tmp_path,
        [{"id": "security"}],
        max_concurrency=1,
        integration=False,
    )
    swarm_json = harness.swarm_json_of("security")
    swarm_json.update(
        {
            "formation_reviewer": "opus",
            "review_surface": "security",
        }
    )
    assert harness.dal.set_swarm_json(
        harness.task_id("security"),
        swarm_json,
    )
    adapter = _ConfirmingAdapter()
    try:
        scheduler = make_scheduler(
            harness,
            router=_SolRouter(),
            reviewer=CrossLineageSwarmReviewer(adapter=adapter),
        )
        handle = scheduler.start_run(harness.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        assert adapter.calls == 0
        assert harness.status_of("security") == "blocked"
        attempts = harness.attempts_of("security")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "blocked"
        assert "security surface requires 2 reviewers" in attempts[0]["detail"]
    finally:
        harness.close()


def test_scheduler_fails_closed_for_unknown_implementer(tmp_path: Any) -> None:
    harness = make_harness(
        tmp_path,
        [{"id": "unknown"}],
        max_concurrency=1,
        integration=False,
    )
    swarm_json = harness.swarm_json_of("unknown")
    swarm_json["formation_reviewer"] = "opus"
    assert harness.dal.set_swarm_json(
        harness.task_id("unknown"),
        swarm_json,
    )
    adapter = _ConfirmingAdapter()
    try:
        scheduler = make_scheduler(
            harness,
            router=_UnknownRouter(),
            reviewer=CrossLineageSwarmReviewer(adapter=adapter),
        )
        handle = scheduler.start_run(harness.run_id)
        assert handle is not None
        assert handle.join(timeout=20)

        assert adapter.calls == 0
        assert harness.status_of("unknown") == "blocked"
        attempts = harness.attempts_of("unknown")
        assert len(attempts) == 1
        assert attempts[0]["end_reason"] == "blocked"
        assert "unknown model lineage for 'some-new-model-9'" in attempts[0]["detail"]
    finally:
        harness.close()
