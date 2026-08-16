"""End-to-end runner integration tests for recall, reflection, and degradation."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import omniagentos.knowledge
from omniagentos.contracts import (
    ActionClass,
    AgentInput,
    AgentResult,
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
from omniagentos.knowledge import config as knowledge_config
from omniagentos.knowledge.contracts import RECALL_HEADER
from omniagentos.knowledge.recall import safe_recall_block
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.testing import make_fake_embedder, make_test_gate
from omniagentos.mock_adapter import MockAdapter
from omniagentos.runner.core import Runner, RunnerDependencies
from tests.knowledge.db_ownership import parse_dbname_from_dsn


class TrackingAdapter(MockAdapter):
    def __init__(self) -> None:
        self.calls: list[AgentInput] = []

    def run(self, input: AgentInput) -> AgentResult:
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


def _create_run(store: SqliteStore, prompt: str) -> str:
    now = utc_now_iso()
    task_id = new_id("tsk")
    run_id = new_id("run")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "code-changes",
            "title": "recall integration",
            "input_json": json.dumps({"prompt": prompt}),
            "acceptance_json": "{}",
            "state": TaskState.QUEUED.value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "discipline_id": "code-changes",
            "agent": "integration-agent",
            "harness": HarnessType.MOCK.value,
            "state": RunState.QUEUED.value,
            "plan_json": json.dumps(
                [
                    {
                        "name": "agent",
                        "kind": "agent",
                        "action_class": ActionClass.SANDBOXED_CREATION.value,
                        "params": {"adapter": "mock", "prompt": prompt},
                    }
                ]
            ),
            "budget_json": "{}",
            "trace_id": f"trace-{run_id}",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    return run_id


def _agent_dsn() -> str:
    # Parse with libpq (via db_ownership), never rsplit("/"): under xdist the
    # claimed OMNIAGENTOS_KNOWLEDGE_TEST_DSN is rebuilt by normalize_dsn_with_dbname
    # into keyword form ("dbname=... host=localhost"), where a URI rsplit returns
    # garbage and recall silently degrades to unavailable_or_empty.
    database = parse_dbname_from_dsn(knowledge_config.test_dsn())
    return f"postgresql://knowledge_agent@localhost/{database}"


def _seed_active_fact(store: KnowledgeStore, statement: str) -> int:
    episode_id = store.add_episode(source="run", content=statement)
    fact_id = store.add_fact(
        statement=statement,
        episode_id=episode_id,
        discipline="code",
        trust=0.9,
        importance=0.8,
        embedding=make_fake_embedder().embed([statement])[0],
    )
    store.promote_fact(fact_id, make_test_gate())
    return fact_id


def test_completed_run_injects_records_helped_and_writes_reflection(
    knowledge_store_admin: KnowledgeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement = "saffron deploys require a signed release manifest"
    fact_id = _seed_active_fact(knowledge_store_admin, statement)
    db_path = tmp_path / "runner.db"
    migrate(str(db_path))
    h1_store = SqliteStore(str(db_path))
    adapter = TrackingAdapter()
    run_id = _create_run(h1_store, "How do saffron deploys work?")
    reflected_episode_ids: list[int] = []
    fake_ingest = types.ModuleType("omniagentos.knowledge.ingest")

    def ingest_run_reflection(
        store: KnowledgeStore,
        run_row: dict[str, Any],
        steps: list[dict[str, Any]],
        *,
        extractor: Any = None,
    ) -> None:
        assert extractor is None
        assert steps
        reflected_episode_ids.append(
            store.add_episode(
                source="run",
                source_ref=str(run_row["id"]),
                content="runner reflection",
            )
        )

    fake_ingest.ingest_run_reflection = ingest_run_reflection  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniagentos.knowledge.ingest", fake_ingest)
    # `from omniagentos.knowledge import ingest` resolves via the PACKAGE ATTRIBUTE once
    # the real submodule has been imported by any earlier test (import binds it on the
    # parent package), which would otherwise shadow the sys.modules fake — so patch the
    # attribute too. Without this the test passes in isolation but the fake is ignored
    # in the full suite.
    monkeypatch.setattr(omniagentos.knowledge, "ingest", fake_ingest, raising=False)
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "1")
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_EMBED", "fake")
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_PG_DSN", _agent_dsn())

    runner = Runner(h1_store, "worker", dependencies=_dependencies(adapter))
    assert runner.tick()
    completed = h1_store.get_run(run_id)
    assert completed is not None and completed["state"] == RunState.COMPLETED.value
    assert adapter.calls and adapter.calls[0].prompt.startswith(RECALL_HEADER)
    assert statement in adapter.calls[0].prompt
    assert adapter.calls[0].metadata["knowledge_recall"]["recall_id"] is not None

    recalled = knowledge_store_admin.get_fact(fact_id)
    assert recalled is not None
    assert recalled.access_count == 1
    assert recalled.helped_count == 1
    assert len(reflected_episode_ids) == 1
    episode = knowledge_store_admin.get_episode(reflected_episode_ids[0])
    assert episode is not None and episode.source_ref == run_id


def test_missing_reflection_package_is_a_real_noop_on_success(
    knowledge_store_admin: KnowledgeStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_active_fact(knowledge_store_admin, "indigo rollback uses the prior manifest")
    db_path = tmp_path / "runner.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))
    adapter = TrackingAdapter()
    run_id = _create_run(store, "Explain indigo rollback")
    # Force a real ImportError for the reflection hook's lazy `import ...ingest`.
    # Deleting it from sys.modules is NOT enough now that p3's ingest.py exists on disk
    # (the lazy import just reloads it, reflection runs, and the duplicated module leaves
    # a PG connection stuck). Setting the sys.modules entry to None makes Python raise
    # ImportError on import — cleanly exercising the F8 no-op-on-missing-ingest path.
    monkeypatch.setitem(sys.modules, "omniagentos.knowledge.ingest", None)
    monkeypatch.delattr(omniagentos.knowledge, "ingest", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "1")
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_EMBED", "fake")
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_PG_DSN", _agent_dsn())

    assert Runner(store, "worker", dependencies=_dependencies(adapter)).tick()
    completed = store.get_run(run_id)
    assert completed is not None and completed["state"] == RunState.COMPLETED.value


def test_dead_pg_dsn_never_fails_run_and_emits_sanitized_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "runner.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))
    adapter = TrackingAdapter()
    run_id = _create_run(store, "complete even without postgres")
    monkeypatch.setenv("OMNIAGENTOS_DB", str(db_path))
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "1")
    monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_EMBED", "fake")
    monkeypatch.setenv(
        "OMNIAGENTOS_KNOWLEDGE_PG_DSN",
        "postgresql://secret-user:secret-password@127.0.0.1:1/dead",
    )
    monkeypatch.setattr("omniagentos.knowledge.recall._last_failure_ts", None)
    monkeypatch.setattr("omniagentos.knowledge.recall._consecutive_failures", 0)

    assert Runner(store, "worker", dependencies=_dependencies(adapter)).tick()
    completed = store.get_run(run_id)
    assert completed is not None and completed["state"] == RunState.COMPLETED.value
    assert adapter.calls[0].prompt == "complete even without postgres"
    assert adapter.calls[0].metadata["knowledge_recall"] == {"status": "unavailable_or_empty"}
    assert (
        safe_recall_block(
            prompt="second failure inside rate-limit window",
            discipline="code-changes",
            agent_id="integration-agent",
            run_id="run-second-failure",
        )
        is None
    )

    events = store.get_events_after(0, types=["knowledge_recall_failed"])
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["dsn_host"] == "127.0.0.1"
    assert payload["consecutive_failures"] == 1
    serialized = json.dumps(payload)
    assert "secret-password" not in serialized
    assert "secret-user" not in serialized
