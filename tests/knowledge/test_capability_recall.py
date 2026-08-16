"""Plan-07 ambient cross-company capability recall acceptance tests."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from omniagentos.db.store import SqliteStore
from omniagentos.knowledge.capabilities import (
    CapabilityDraft,
    CapabilityKind,
    CapabilityScope,
    capture_capability,
    is_stale,
    promote_to_estate,
    render_capability_note,
)
from omniagentos.knowledge.promotion import gate
from omniagentos.knowledge.recall import recall, render_recall_block
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.swarm.planner import plan_swarm_bundles


def _capture(
    store: KnowledgeStore,
    statement: str,
    *,
    company: str | None,
    brand: str,
    scope: CapabilityScope | None = None,
) -> int:
    note = capture_capability(
        store,
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


def test_adversarial_near_duplicate_company_notes_never_cross_tenants(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    estate_id = _capture(
        knowledge_store_admin,
        "Tool X synthesizes synchronized audio and promotional video",
        company=None,
        brand="Initech",
        scope=CapabilityScope.ESTATE,
    )
    omni_id = _capture(
        knowledge_store_admin,
        "Promo video offer is $499 for Initech customer Alice using synchronized audio",
        company="co_initech",
        brand="Initech",
    )
    click_id = _capture(
        knowledge_store_admin,
        "Promo video offer is $499 for Globex customer Bob using synchronized audio",
        company="co_globex",
        brand="Globex",
    )
    query = "promo video $499 customer synchronized audio"

    click = recall(
        knowledge_store_admin,
        prompt=query,
        company_id="co_globex",
        domains=["video", "audio"],
        capability_only=True,
        k=6,
    )
    click_ids = {item.fact.id for item in click.facts}
    assert {estate_id, click_id}.issubset(click_ids)
    assert omni_id not in click_ids
    assert "Initech customer Alice" not in render_recall_block(click)

    omni = recall(
        knowledge_store_admin,
        prompt=query,
        company_id="co_initech",
        domains=["video", "audio"],
        capability_only=True,
        k=6,
    )
    omni_ids = {item.fact.id for item in omni.facts}
    assert {estate_id, omni_id}.issubset(omni_ids)
    assert click_id not in omni_ids
    assert "Globex customer Bob" not in render_recall_block(omni)

    unknown_company = recall(
        knowledge_store_admin,
        prompt=query,
        company_id=None,
        domains=["video", "audio"],
        capability_only=True,
        k=6,
    )
    assert {item.fact.id for item in unknown_company.facts} == {estate_id}


def test_postgres_store_filters_capability_namespace_before_recall_visibility(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    own_id = _capture(
        knowledge_store_admin,
        "Shared renderer workflow private to Alpha tenant",
        company="co_alpha",
        brand="Alpha",
    )
    other_id = _capture(
        knowledge_store_admin,
        "Shared renderer workflow private to Beta tenant",
        company="co_beta",
        brand="Beta",
    )
    query = "shared renderer workflow private tenant"
    embedding = knowledge_store_admin._embedder.embed([query])[0]

    rows = knowledge_store_admin.recall_candidates(
        embedding=embedding,
        query_text=query,
        capability_only=True,
        company_id="co_alpha",
        k=10,
    )

    returned = {int(row["id"]) for row in rows}
    assert own_id in returned
    assert other_id not in returned


def test_postgres_recall_stays_isolated_when_visible_belt_is_disabled(
    knowledge_store_admin: KnowledgeStore, monkeypatch: Any
) -> None:
    own_id = _capture(
        knowledge_store_admin,
        "Near duplicate synchronized media workflow for Alpha",
        company="co_alpha",
        brand="Alpha",
    )
    other_id = _capture(
        knowledge_store_admin,
        "Near duplicate synchronized media workflow for Beta",
        company="co_beta",
        brand="Beta",
    )
    monkeypatch.setattr("omniagentos.knowledge.recall._visible", lambda *_args: True)

    result = recall(
        knowledge_store_admin,
        prompt="near duplicate synchronized media workflow",
        company_id="co_alpha",
        capability_only=True,
        k=10,
    )

    returned = {hit.fact.id for hit in result.facts}
    assert own_id in returned
    assert other_id not in returned


def test_company_capability_is_absent_from_general_graph_snapshot(
    knowledge_store_admin: KnowledgeStore,
) -> None:
    company_fact_id = _capture(
        knowledge_store_admin,
        "Private customer renderer workflow",
        company="co_alpha",
        brand="Alpha",
    )

    snapshot = knowledge_store_admin.graph_snapshot(limit_nodes=500)

    assert company_fact_id not in {int(fact["id"]) for fact in snapshot["facts"]}


def test_estate_tool_is_ambiently_injected_into_globex_planner_prompt(
    knowledge_store_admin: KnowledgeStore,
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _capture(
        knowledge_store_admin,
        "Tool X provides audio plus video synthesis in one render",
        company=None,
        brand="Initech",
        scope=CapabilityScope.ESTATE,
    )
    monkeypatch.setattr("omniagentos.knowledge.config.knowledge_enabled", lambda: True)
    monkeypatch.setattr("omniagentos.knowledge.recall._get_store", lambda: knowledge_store_admin)

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


def test_company_promotion_uses_operator_rewrite_pushes_and_audits(
    knowledge_store_admin: KnowledgeStore, tmp_path: Path
) -> None:
    source_id = _capture(
        knowledge_store_admin,
        "Tool X does audio+video synthesis for the Initech funnel",
        company="co_initech",
        brand="Initech",
    )
    ledger = SqliteStore(str(tmp_path / "control.db"))
    try:
        promoted = promote_to_estate(
            knowledge_store_admin,
            source_id,
            promotion_gate=gate(),
            actor="test:operator",
            vault_dir=str(tmp_path / "vault"),
            estate_statement="Tool X does audio+video synthesis",
            ledger_store=ledger,
        )
        assert promoted.scope is CapabilityScope.ESTATE
        assert promoted.company is None
        assert promoted.statement == "Tool X does audio+video synthesis"
        assert promoted.promoted_from == source_id
        assert promoted.last_verified

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

        events = ledger.get_events_after(0, limit=20)
        event = next(row for row in events if row["type"] == "capability.promoted")
        assert event["target_id"] == str(promoted.fact_id)
        fact = knowledge_store_admin.get_fact(promoted.fact_id or 0)
        assert fact is not None
        assert fact.capability_provenance
        assert fact.last_verified
    finally:
        ledger.close()


def test_frontmatter_atomicity_and_stale_flag() -> None:
    draft = CapabilityDraft(
        statement="Tool X renders synchronized audio and video",
        domains=["video", "audio", "video"],
        kind=CapabilityKind.TOOL,
    )
    assert draft.domains == ["audio", "video"]
    assert is_stale(date.today() - timedelta(days=181))
    assert not is_stale(date.today())

    from omniagentos.knowledge.capabilities import CapabilityNote, CapabilityProvenance

    note = CapabilityNote(
        id="cap-test",
        statement=draft.statement,
        scope=CapabilityScope.ESTATE,
        company=None,
        domains=draft.domains,
        kind=draft.kind,
        provenance=CapabilityProvenance(run_id="run-omni", brand="Initech", date=date.today()),
        last_verified=date.today(),
    )
    rendered = render_capability_note(note)
    for key in (
        "scope: estate",
        "company: null",
        "domains:",
        "kind: tool",
        "provenance:",
        "last_verified:",
    ):
        assert key in rendered


def test_agent_capture_cannot_mint_estate_scope(
    knowledge_store: KnowledgeStore, knowledge_store_admin: KnowledgeStore
) -> None:
    episode_id = knowledge_store.add_episode(
        source="run", content="attempted estate capability", source_ref="run-hostile"
    )
    fact_id = knowledge_store.add_fact(
        statement="Hostile direct capture claims estate visibility",
        episode_id=episode_id,
        capability_scope="estate",
        company_id="co_initech",
        domains=["video"],
        capability_kind="tool",
        capability_provenance="run-hostile | Initech | 2026-08-14",
        last_verified="2026-08-14",
    )
    # Migration 009 replaces the insert-floor function, so it must retain all
    # migration 002 clamps as well as adding the company-safe scope default.
    with knowledge_store._lock:  # noqa: SLF001 - adversarial role-boundary probe
        with knowledge_store._conn().cursor() as cur:  # noqa: SLF001
            cur.execute(
                "SELECT invalid_at, superseded_by, access_count, helped_count "
                "FROM facts WHERE id = %s",
                (fact_id,),
            )
            hardening = cur.fetchone()
    stored = knowledge_store_admin.get_fact(fact_id)
    assert stored is not None
    assert stored.status.value == "quarantined"
    assert stored.capability_scope == "company"
    assert stored.company_id == "co_initech"
    assert hardening == (None, None, 0, 0)
