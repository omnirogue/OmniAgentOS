"""Real-components end-to-end test — the H2 anti-fake safeguard.

Drives the FULL memory loop with ZERO knowledge-base test doubles:

    ingest real text --REAL Ollama bge-m3 embedding--> REAL Postgres
        --> REAL recall CTE --> REAL runner mock-harness run
        --> run completion writes a reflection back to the KB

Only the LLM extraction step is faked, via ``FixtureExtractor`` (episode ingest)
and the already-deterministic ``DeterministicReflectionExtractor`` (run
reflection, the runner's own default) — no live LLM call is made anywhere in
this test. Embeddings, Postgres, the hybrid recall CTE, the role-separated
promotion boundary, and the runner's recall-injection/reflection hooks are all
exercised for real.

If Ollama is genuinely unreachable this test SKIPS with a reason. It never
substitutes a fake embedder to force a pass — that would defeat the entire
point of this file (see ADR-005).

MARKER (was missing): this file requires REAL Ollama by design, so it belongs to
``live_ollama`` — the marker ``tests/knowledge/conftest.py`` already registers
and ``pyproject``'s default ``addopts`` already excludes. Without it the file ran
in the OFFLINE default lane and made 18 real POSTs to :11434 on any host with
Ollama up, and merely *skipped* on every host without — so its anti-fake
guarantee was silently absent from the signal it appeared to be part of. It now
runs, for real, under ``make test-live`` (``-m 'live or live_ollama'``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from omniagentos.knowledge.contracts import RECALL_HEADER, EmbeddingUnavailable, EpisodeSource
from omniagentos.knowledge.embeddings import OllamaEmbedding
from omniagentos.knowledge.extract import FixtureExtractor
from omniagentos.knowledge.ingest import ingest_episode
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.knowledge.testing import make_test_gate
from omniagentos.mock_adapter import MockAdapter
from omniagentos.runner.core import Runner, RunnerDependencies

from .conftest import _role_dsn, _truncate_all

pytestmark = pytest.mark.live_ollama

# A distinctive statement: real lexical + semantic overlap with the runner prompt
# below, so both the FTS leg and the real bge-m3 vector leg of the hybrid CTE can
# genuinely surface it (no reliance on a hash-based fake embedder's coincidences).
_STATEMENT = (
    "Wraithcopper production deploys require a two-person signed release manifest before promotion."
)
_PROMPT = "What does a Wraithcopper production deploy require before promotion?"


class TrackingAdapter(MockAdapter):
    """Mock adapter that records every AgentInput it receives, unmodified."""

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
            "title": "e2e recall integration",
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
            "agent": "e2e-agent",
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


def _ollama_unreachable_reason() -> str | None:
    """Probe real Ollama; return a skip reason, or None if it is genuinely up."""
    try:
        OllamaEmbedding().embed(["e2e reachability probe"])
    except EmbeddingUnavailable as exc:
        return str(exc)
    return None


def _has_embedding(store: KnowledgeStore, fact_id: int) -> bool:
    """Real check against the pgvector column — Fact/get_fact() never exposes it."""
    with store._conn().cursor() as cur:
        cur.execute("SELECT embedding IS NOT NULL FROM facts WHERE id = %s", (fact_id,))
        row = cur.fetchone()
    assert row is not None
    return bool(row[0])


def _reflection_episode_ids(store: KnowledgeStore, run_id: str) -> list[int]:
    with store._conn().cursor() as cur:
        cur.execute(
            "SELECT id FROM episodes WHERE source = 'run' AND source_ref = %s",
            (run_id,),
        )
        return [int(row[0]) for row in cur.fetchall()]


def test_real_chain_ingest_embed_recall_inject_reflect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every leg is real except the extraction fixture; see module docstring."""
    unreachable = _ollama_unreachable_reason()
    if unreachable is not None:
        pytest.skip(f"Ollama bge-m3 is genuinely unreachable: {unreachable}")

    # NOTE on OMNIAGENTOS_KNOWLEDGE_EMBED: deliberately left UNSET so both these
    # directly-constructed stores AND the runner's process-wide recall singleton
    # (config.embed_spec() defaults to "ollama:bge-m3") use REAL Ollama embeddings.
    admin_store = KnowledgeStore(dsn=_role_dsn("knowledge_admin"), embedder=OllamaEmbedding())
    agent_store = KnowledgeStore(dsn=_role_dsn("knowledge_agent"), embedder=OllamaEmbedding())
    try:
        # --- Step 1: ingest two REAL, corroborating episodes (real Ollama embeds). ---
        # A human/vault source via the ADMIN store (trust survives, still quarantined
        # by ingest policy) plus a run source via the AGENT store (the quarantine
        # trigger clamps it further) — two distinct source types for the same
        # statement, i.e. the shape the offline consolidator's promotion predicate
        # (R2-7: >=2 distinct source types) is meant to confirm. consolidate()'s
        # promotion pass is still scaffolding (TODO stubs) in this build, so — per
        # the brief — this test promotes directly via the proven admin+gate path
        # (the same one test_runner_integration.py's _seed_active_fact uses) rather
        # than depending on that stub.
        admin_result = ingest_episode(
            admin_store,
            source=EpisodeSource.HUMAN.value,
            content=f"FACT: {_STATEMENT}",
            extractor=FixtureExtractor(),
            discipline="ops",
        )
        agent_result = ingest_episode(
            agent_store,
            source=EpisodeSource.RUN.value,
            content=f"FACT: {_STATEMENT}",
            extractor=FixtureExtractor(),
            discipline="ops",
        )
        assert admin_result.fact_ids and agent_result.fact_ids
        admin_fact_id = admin_result.fact_ids[0]
        agent_fact_id = agent_result.fact_ids[0]

        # Assert non-NULL embeddings — the real Ollama leg actually ran and the
        # 1024-d vector actually landed in Postgres, for BOTH corroborating facts.
        assert _has_embedding(admin_store, admin_fact_id)
        assert _has_embedding(admin_store, agent_fact_id)

        # --- Step 2: promote the run-sourced fact to active (admin + gate) so it
        # is genuinely eligible for recall, exactly like the DB's real promotion
        # boundary requires (knowledge_agent cannot UPDATE facts.status). ---
        admin_store.promote_fact(agent_fact_id, make_test_gate())
        promoted = admin_store.get_fact(agent_fact_id)
        assert promoted is not None
        assert promoted.status.value == "active"

        # --- Step 3: drive a REAL runner run in mock-harness mode. ---
        db_path = tmp_path / "runner.db"
        migrate(str(db_path))
        h1_store = SqliteStore(str(db_path))
        adapter = TrackingAdapter()
        run_id = _create_run(h1_store, _PROMPT)

        monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE", "1")
        monkeypatch.setenv("OMNIAGENTOS_KNOWLEDGE_PG_DSN", _role_dsn("knowledge_agent"))
        # OMNIAGENTOS_KNOWLEDGE_EMBED intentionally NOT set (real Ollama by default).

        runner = Runner(h1_store, "worker", dependencies=_dependencies(adapter))
        assert runner.tick()
        completed = h1_store.get_run(run_id)
        assert completed is not None and completed["state"] == RunState.COMPLETED.value

        # The real recall CTE found the promoted fact and the runner injected it
        # into the AgentInput.prompt actually sent to the (mock) adapter.
        assert adapter.calls, "adapter was never invoked"
        sent_prompt = adapter.calls[0].prompt
        assert sent_prompt.startswith(RECALL_HEADER)
        assert _STATEMENT in sent_prompt
        recall_meta = adapter.calls[0].metadata["knowledge_recall"]
        assert recall_meta["recall_id"] is not None

        # --- Step 4: run completion wrote a reflection episode back to the KB,
        # and record_helped bumped helped_count on the recalled fact. ---
        reflection_ids = _reflection_episode_ids(admin_store, run_id)
        assert reflection_ids, "expected a run-sourced reflection episode for this run_id"

        recalled_after = admin_store.get_fact(agent_fact_id)
        assert recalled_after is not None
        assert recalled_after.access_count >= 1
        assert recalled_after.helped_count == promoted.helped_count + 1
    finally:
        admin_store.close()
        agent_store.close()
        _truncate_all()
