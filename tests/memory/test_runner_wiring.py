"""End-to-end: a dispatched run gets prior context prepended and its turns persisted.

Drives the real :class:`Runner` against the mock adapter with the frozen conversations
table (migration 031, owned by W3-hierarchy — shipped for real via plain ``migrate()``
in this integrated repo), proving the runner wiring (a) injects the node's prior
conversation into the actual AgentInput and (b) persists the operator's brief + the
agent result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    ActionClass,
    AgentInput,
    BudgetDecision,
    HarnessType,
    PolicyDecision,
    RunState,
    SandboxSpec,
    TaskState,
    new_id,
    utc_now_iso,
)
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.memory.store import ConversationStore
from omniagentos.mock_adapter import MockAdapter
from omniagentos.runner.core import Runner, RunnerDependencies


class TrackingAdapter(MockAdapter):
    def __init__(self) -> None:
        self.calls: list[AgentInput] = []

    def run(self, input: AgentInput) -> Any:
        self.calls.append(input)
        return super().run(input)


def _dependencies(adapter: TrackingAdapter) -> RunnerDependencies:
    return RunnerDependencies(
        evaluate_policy=lambda _action: PolicyDecision(requires_approval=False),
        sandbox_for_tools=lambda _harness, _tools: SandboxSpec(level="read_only"),
        check_budget=lambda _spec, _wall, _tokens, _cost: BudgetDecision(allowed=True),
        resolve_adapter=lambda _harness: adapter,
        append_manifest=lambda root, _manifest: str(Path(root) / "runs.jsonl"),
        render_run_note=lambda run, _steps, _manifest, _receipts, **_kwargs: (
            f"runs/{run['id']}.md",
            "done",
        ),
        write_note=lambda root, relpath, _content: str(Path(root) / relpath),
    )


def _create_run(store: SqliteStore, task_id: str, prompt: str) -> str:
    now = utc_now_iso()
    run_id = new_id("run")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "code-changes",
            "title": "billing task",
            "input_json": json.dumps({"prompt": "fallback"}),
            "acceptance_json": "{}",
            "state": TaskState.QUEUED.value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    plan = [
        {
            "name": "agent",
            "kind": "agent",
            "action_class": ActionClass.SANDBOXED_CREATION.value,
            "params": {"adapter": "mock", "prompt": prompt, "mock": {"reply": "the-agent-answer"}},
        }
    ]
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "discipline_id": "code-changes",
            "agent": "worker-1",
            "harness": HarnessType.MOCK.value,
            "state": RunState.QUEUED.value,
            "plan_json": json.dumps(plan),
            "budget_json": "{}",
            "trace_id": f"trace-{run_id}",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    return run_id


def test_dispatched_run_gets_prior_context_and_persists_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_MEMORY", "1")
    monkeypatch.delenv("OMNIAGENTOS_KNOWLEDGE", raising=False)  # isolate memory injection
    db_path = tmp_path / "runner.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))

    task_id = new_id("tsk")
    conv = ConversationStore(store)
    # Prior context the operator should NOT have to repeat.
    conv.append_turn("task", task_id, "user", "The API base is https://api.example.test")
    conv.append_turn("task", task_id, "agent", "Understood, using that base URL")

    run_id = _create_run(store, task_id, "Add a retry to the client")
    adapter = TrackingAdapter()
    runner = Runner(store, "worker-1", dependencies=_dependencies(adapter))
    assert runner.tick()

    assert store.get_run(run_id)["state"] == RunState.COMPLETED.value

    # (a) The prior conversation was prepended to the ACTUAL agent input.
    assert len(adapter.calls) == 1
    sent = adapter.calls[0].prompt
    assert "https://api.example.test" in sent
    assert sent.strip().endswith("Add a retry to the client")
    assert sent.index("https://api.example.test") < sent.index("Add a retry to the client")
    memory_context = adapter.calls[0].metadata["memory_context"]
    assert memory_context["status"] == "injected"
    assert memory_context["estimated_tokens"] > 0
    assert memory_context["truncated"] is False
    assert memory_context["node_turns"] == 2
    assert memory_context["ancestor_summaries"] == 0
    assert memory_context["recalls"] == 0
    assert isinstance(memory_context["has_summary"], bool)

    # (b) The operator's brief + the agent result were persisted as new turns.
    turns = conv.recent_turns("task", task_id, 20)
    contents = [(t.role, t.content) for t in turns]
    assert ("user", "The API base is https://api.example.test") in contents
    assert ("user", "Add a retry to the client") in contents
    assert ("agent", "the-agent-answer") in contents
    # The freshly-persisted brief comes after the seeded history (accrual, in order).
    assert [t.content for t in turns][-2:] == ["Add a retry to the client", "the-agent-answer"]


def test_memory_disabled_leaves_prompt_and_history_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_MEMORY", "0")
    monkeypatch.delenv("OMNIAGENTOS_KNOWLEDGE", raising=False)
    db_path = tmp_path / "runner.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))

    task_id = new_id("tsk")
    run_id = _create_run(store, task_id, "just do it")
    adapter = TrackingAdapter()
    runner = Runner(store, "worker-1", dependencies=_dependencies(adapter))
    assert runner.tick()

    assert store.get_run(run_id)["state"] == RunState.COMPLETED.value
    assert adapter.calls[0].prompt == "just do it"
    assert "memory_context" not in adapter.calls[0].metadata
    assert ConversationStore(store).recent_turns("task", task_id, 20) == []
