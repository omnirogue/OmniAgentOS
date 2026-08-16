"""Postgres-free Plan-07 proofs using the shared FakeEmbedding implementation."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.api.routes.control import ApiError
from omniagentos.api.routes.knowledge import get_fact as get_fact_route
from omniagentos.db.store import SqliteStore
from omniagentos.knowledge.capabilities import (
    CapabilityDraft,
    CapabilityKind,
    CapabilityScope,
    capture_capability,
    is_stale,
    normalize_company_id,
    promote_to_estate,
    resolve_company_id,
    safe_ambient_capability_block,
)
from omniagentos.knowledge.memory_store import InMemoryKnowledgeStore
from omniagentos.knowledge.promotion import gate
from omniagentos.knowledge.recall import recall, render_recall_block
from omniagentos.knowledge.testing import make_fake_embedder
from omniagentos.swarm.planner import plan_swarm_bundles
from omniagentos.swarm.scheduler import build_worker_brief


def _store() -> InMemoryKnowledgeStore:
    return InMemoryKnowledgeStore(embedder=make_fake_embedder())


def _capture(
    store: InMemoryKnowledgeStore,
    statement: str,
    *,
    company: str | None,
    brand: str,
    scope: CapabilityScope | None = None,
) -> int:
    note = capture_capability(
        store,  # type: ignore[arg-type]
        CapabilityDraft(
            statement=statement,
            domains=["video", "audio"],
            kind=CapabilityKind.TOOL,
        ),
        run_id=f"run-{brand.lower()}",
        brand=brand,
        company_id=company,
        requested_scope=scope,
    )
    assert note.fact_id is not None
    return note.fact_id


def test_fake_embedding_leak_barrier_in_both_directions_with_near_duplicates() -> None:
    store = _store()
    estate_id = _capture(
        store,
        "Tool X synthesizes synchronized audio and promotional video",
        company=None,
        brand="Initech",
        scope=CapabilityScope.ESTATE,
    )
    omni_id = _capture(
        store,
        "Promo video offer is $499 for Initech customer Alice using synchronized audio",
        company="co_initech",
        brand="Initech",
    )
    click_id = _capture(
        store,
        "Promo video offer is $499 for Globex customer Bob using synchronized audio",
        company="co_globex",
        brand="Globex",
    )
    query = "promo video $499 customer synchronized audio"

    click = recall(
        store,  # type: ignore[arg-type]
        prompt=query,
        company_id="co_globex",
        domains=["video", "audio"],
        capability_only=True,
        k=6,
    )
    assert {estate_id, click_id}.issubset({hit.fact.id for hit in click.facts})
    assert omni_id not in {hit.fact.id for hit in click.facts}
    assert "Initech customer Alice" not in render_recall_block(click)

    omni = recall(
        store,  # type: ignore[arg-type]
        prompt=query,
        company_id="co_initech",
        domains=["video", "audio"],
        capability_only=True,
        k=6,
    )
    assert {estate_id, omni_id}.issubset({hit.fact.id for hit in omni.facts})
    assert click_id not in {hit.fact.id for hit in omni.facts}
    assert "Globex customer Bob" not in render_recall_block(omni)

    unknown_company = recall(
        store,  # type: ignore[arg-type]
        prompt=query,
        company_id=None,
        domains=["video", "audio"],
        capability_only=True,
        k=6,
    )
    assert {hit.fact.id for hit in unknown_company.facts} == {estate_id}


def test_in_memory_store_filters_capability_namespace_before_recall_visibility() -> None:
    store = _store()
    own_id = _capture(
        store,
        "Shared renderer workflow private to Alpha tenant",
        company="co_alpha",
        brand="Alpha",
    )
    other_id = _capture(
        store,
        "Shared renderer workflow private to Beta tenant",
        company="co_beta",
        brand="Beta",
    )
    query = "shared renderer workflow private tenant"
    embedding = store._embedder.embed([query])[0] if store._embedder is not None else None

    rows = store.recall_candidates(
        embedding=embedding,
        query_text=query,
        capability_only=True,
        company_id="co_alpha",
        k=10,
    )

    returned = {int(row["id"]) for row in rows}
    assert own_id in returned
    assert other_id not in returned


def test_recall_stays_tenant_isolated_when_visible_belt_is_disabled(monkeypatch: Any) -> None:
    store = _store()
    own_id = _capture(
        store,
        "Near duplicate synchronized media workflow for Alpha",
        company="co_alpha",
        brand="Alpha",
    )
    other_id = _capture(
        store,
        "Near duplicate synchronized media workflow for Beta",
        company="co_beta",
        brand="Beta",
    )
    monkeypatch.setattr("omniagentos.knowledge.recall._visible", lambda *_args: True)

    result = recall(
        store,  # type: ignore[arg-type]
        prompt="near duplicate synchronized media workflow",
        company_id="co_alpha",
        capability_only=True,
        k=10,
    )

    returned = {hit.fact.id for hit in result.facts}
    assert own_id in returned
    assert other_id not in returned


def test_capture_and_project_resolution_share_canonical_company_key() -> None:
    store = _store()
    fact_id = _capture(
        store,
        "Renderer supports synchronized media",
        company=" Initech ",
        brand="Initech",
    )
    fact = store.get_fact(fact_id)
    assert fact is not None
    assert fact.company_id == "co_initech"
    assert normalize_company_id("co_initech") == normalize_company_id("initech")

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE projects (id TEXT, org_company_id TEXT)")
        connection.execute(
            "INSERT INTO projects (id, org_company_id) VALUES (?, ?)",
            ("proj_omni", "initech"),
        )
        assert resolve_company_id(connection, "proj_omni") == fact.company_id
    finally:
        connection.close()


def test_general_fact_detail_route_refuses_company_capability() -> None:
    store = _store()
    fact_id = _capture(
        store,
        "Private customer renderer workflow",
        company="co_alpha",
        brand="Alpha",
    )

    with pytest.raises(ApiError):
        asyncio.run(get_fact_route(fact_id, store))  # type: ignore[arg-type]


def test_captured_initech_estate_tool_appears_in_globex_plan_without_ask(
    monkeypatch: Any, tmp_path: Path
) -> None:
    store = _store()
    _capture(
        store,
        "Tool X provides audio plus video synthesis in one render",
        company=None,
        brand="Initech",
        scope=CapabilityScope.ESTATE,
    )
    monkeypatch.setattr("omniagentos.knowledge.config.knowledge_enabled", lambda: True)
    monkeypatch.setattr("omniagentos.knowledge.recall._get_store", lambda: store)

    class Planner:
        prompt = ""

        def __call__(self, prompt: str, _schema: dict[str, Any], _effort: str) -> dict[str, Any]:
            self.prompt = prompt
            return {
                "goal": "Promo video for Globex",
                "tasks": [
                    {"id": "script", "title": "Script", "owned_paths": []},
                    {"id": "audio", "title": "Audio", "owned_paths": []},
                    {"id": "video", "title": "Video", "owned_paths": []},
                ],
            }

    planner = Planner()
    plans = plan_swarm_bundles(
        "Promo video for Globex",
        str(tmp_path),
        planner_llm=planner,
        clarify_llm=lambda *_args: None,
        recall_fn=lambda _goal: "",
        playbook_path=tmp_path / "missing.json",
        company_id="co_globex",
    )
    assert plans
    assert "AMBIENT CAPABILITIES (company + estate)" in planner.prompt
    assert "Tool X provides audio plus video synthesis" in planner.prompt


@pytest.mark.parametrize(
    "estate_statement",
    [
        "renderer latency is 1.5 seconds",
        "renderer success reaches target %",
        "renderer requires € billing",
        "renderer requires £ billing",
        "renderer requires ¥ billing",
        "Alice Smith recommends the renderer",
        "renderer improves customer delivery",
    ],
)
def test_promotion_rejects_missing_or_residual_private_estate_statement(
    tmp_path: Path, estate_statement: str
) -> None:
    store = _store()
    source_id = _capture(
        store,
        "Nimbus Metrics customer Alice Smith pays €99.5 for 95% delivery",
        company="co_initech",
        brand="Initech",
    )

    with pytest.raises(ValueError, match="operator-supplied estate_statement"):
        promote_to_estate(
            store,  # type: ignore[arg-type]
            source_id,
            promotion_gate=gate(),
            actor="test:operator",
            vault_dir=str(tmp_path / "vault"),
        )
    with pytest.raises(ValueError, match="estate_statement contains"):
        promote_to_estate(
            store,  # type: ignore[arg-type]
            source_id,
            promotion_gate=gate(),
            actor="test:operator",
            vault_dir=str(tmp_path / "vault"),
            estate_statement=estate_statement,
        )


def test_promotion_uses_only_operator_supplied_clean_estate_statement(tmp_path: Path) -> None:
    store = _store()
    source_id = _capture(
        store,
        "Tool X does audio+video synthesis for Initech customer Alice Smith at €99.5 with 95%",
        company="co_initech",
        brand="Initech",
    )
    ledger = SqliteStore(str(tmp_path / "control.db"))
    try:
        promoted = promote_to_estate(
            store,  # type: ignore[arg-type]
            source_id,
            promotion_gate=gate(),
            actor="test:operator",
            vault_dir=str(tmp_path / "vault"),
            estate_statement="Tool X does audio+video synthesis",
            ledger_store=ledger,
        )
        assert promoted.statement == "Tool X does audio+video synthesis"
        assert promoted.scope is CapabilityScope.ESTATE
        assert promoted.company is None
        estate_note = tmp_path / "vault" / "capabilities" / f"{promoted.id}.md"
        estate_content = estate_note.read_text(encoding="utf-8")
        assert "scope: estate" in estate_content
        assert "company: null" in estate_content
        assert "provenance:" in estate_content
        assert "last_verified:" in estate_content
        playbook = tmp_path / "vault" / "playbook" / "domains" / "video.md"
        content = playbook.read_text(encoding="utf-8")
        assert "- Tool X does audio+video synthesis" in content
        assert "Initech" not in content
        assert any(
            event["type"] == "capability.promoted" for event in ledger.get_events_after(0, limit=20)
        )
        assert not is_stale(promoted.last_verified)
        assert is_stale(date.today() - timedelta(days=181))
    finally:
        ledger.close()


def test_stale_estate_note_is_flagged_in_the_recall_block() -> None:
    store = _store()
    note = capture_capability(
        store,  # type: ignore[arg-type]
        CapabilityDraft(
            statement="Legacy renderer can synthesize audio and video",
            domains=["video"],
            kind=CapabilityKind.TOOL,
        ),
        run_id="run-legacy",
        brand="Initech",
        company_id=None,
        requested_scope=CapabilityScope.ESTATE,
        verified_at=date.today() - timedelta(days=181),
    )
    result = recall(
        store,  # type: ignore[arg-type]
        prompt="synthesize audio video",
        company_id="co_globex",
        domains=["video"],
        capability_only=True,
    )
    assert note.fact_id in {hit.fact.id for hit in result.facts}
    assert "[STALE—REVERIFY]" in render_recall_block(result)


def test_worker_brief_ambiently_queries_company_and_estate(
    monkeypatch: Any,
) -> None:
    observed: dict[str, Any] = {}

    monkeypatch.setattr("omniagentos.knowledge.config.knowledge_enabled", lambda: True)
    monkeypatch.setattr(
        "omniagentos.knowledge.capabilities.resolve_company_id",
        lambda _store, project_id: "co_globex" if project_id == "proj_click" else None,
    )

    def recalled(summary: str, **kwargs: Any) -> str:
        observed.update({"summary": summary, **kwargs})
        return "<recalled-knowledge>\n[tool] Tool X audio+video\n</recalled-knowledge>"

    monkeypatch.setattr(
        "omniagentos.knowledge.capabilities.safe_ambient_capability_block", recalled
    )
    brief = build_worker_brief(
        {"id": "swr_1", "project_id": "proj_click"},
        {"id": "btk_1", "title": "Globex promo", "description": "Make a video"},
        {
            "plan_version": 1,
            "plan_hash": "abc",
            "acceptance": "video ready",
            "owned_paths": ["assets/video"],
        },
        {},
        project_store=object(),
    )
    assert observed["company_id"] == "co_globex"
    assert "Globex promo" in observed["summary"]
    assert "## Ambient capabilities" in brief
    assert "Tool X audio+video" in brief


def test_ambient_recall_store_failure_is_fail_open(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "omniagentos.knowledge.capabilities.ambient_capability_block",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("store down")),
    )
    assert safe_ambient_capability_block("Globex promo", company_id="co_globex") == ""
