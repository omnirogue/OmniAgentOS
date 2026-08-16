"""Multidimensional org + Grok metacog agent tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.orgdims.service import OrgDimsService
from omniagentos.orgdims.taxonomy import resolve_workstream_alias


@pytest.fixture()
def svc(tmp_path: Path) -> OrgDimsService:
    return OrgDimsService(db_path=str(tmp_path / "org.db"))


def test_seed_companies_workstreams_and_grok_agents(svc: OrgDimsService) -> None:
    counts = svc.ensure_seeded()
    # JG2-E13c: the roster is six companies, not three.
    assert counts["companies"] >= 6
    assert counts["agents"] >= 6
    health = svc.health()
    assert health["primary_orchestrator"] == "grok-orchestrator"
    assert "grok-orchestrator" in health["grok_metacog_agents"]
    assert "grok-self-repair" in health["grok_metacog_agents"]
    assert "grok-self-learn" in health["grok_metacog_agents"]
    companies = svc.list_companies()
    slugs = {c["slug"] for c in companies}
    assert {
        "initech",
        "globex",
        "acmeuni",
        "hooli",
        "omniagentos",
        "personal",
    } <= slugs
    assert all(slug == slug.lower() for slug in slugs)
    ws = {w["slug"] for w in svc.list_workstreams()}
    assert "engineering" in ws
    assert "creative" in ws
    assert "finance-admin" in ws


def test_legacy_category_aliases() -> None:
    assert resolve_workstream_alias("Coding") == "engineering"
    assert resolve_workstream_alias("prototypes") == "product"
    assert resolve_workstream_alias("Creatives") == "creative"
    assert resolve_workstream_alias("advertising") == "advertising"


def test_classify_board_task_persists_org_and_prefers_grok(
    svc: OrgDimsService, tmp_path: Path
) -> None:
    collab = CollabStore(str(tmp_path / "org.db"))
    task = BoardTask(
        title="Implement artifact registration API",
        description="Add SHA-256 dedupe endpoints and unit tests in omniagentos",
        discipline="coding",
        priority="high",
    )
    collab.create_board_task(task)

    result = svc.classify_board_task(
        task_id=task.id,
        title=task.title,
        description=task.description,
        discipline=task.discipline,
        priority=task.priority,
        company_slug="initech",
        product_slug="initech-enterprise",
        apply=True,
    )
    assert result.bundle.classification.primary_workstream == "engineering"
    assert result.bundle.organization_context.company_slug == "initech"
    assert result.bundle.provenance.classifier_agent == "grok-orchestrator"
    agents = result.bundle.execution_links.get("preferred_agent_profiles") or []
    assert "grok-orchestrator" in agents

    row = collab.get_board_task(task.id)
    assert row is not None
    assert row.get("org", {}).get("classification", {}).get("primary_workstream") == "engineering"


def test_creative_task_routes_generate_loop(svc: OrgDimsService, tmp_path: Path) -> None:
    collab = CollabStore(str(tmp_path / "org.db"))
    task = BoardTask(
        title="Write webinar V15 script",
        description="Creative copy for enterprise webinar email sequence",
        discipline="creatives",
    )
    collab.create_board_task(task)
    result = svc.classify_board_task(
        task_id=task.id,
        title=task.title,
        description=task.description,
        discipline=task.discipline,
        apply=True,
    )
    assert result.bundle.classification.primary_workstream == "creative"
    assert result.bundle.execution_links.get("loop_template_id") == (
        "generate_critique_repair_verify"
    )


def test_irreversible_risk_needs_review(svc: OrgDimsService, tmp_path: Path) -> None:
    collab = CollabStore(str(tmp_path / "org.db"))
    task = BoardTask(
        title="Wire payment refund",
        description="Irreversible stripe payment wire transfer cleanup",
    )
    collab.create_board_task(task)
    result = svc.classify_board_task(
        task_id=task.id,
        title=task.title,
        description=task.description,
        apply=True,
    )
    assert result.bundle.classification.risk_class == "irreversible"
    assert "risk_class" in result.needs_review


def test_bulk_reclassify_and_matrix_portfolio(svc: OrgDimsService, tmp_path: Path) -> None:
    db = str(tmp_path / "org.db")
    collab = CollabStore(db)
    for title, disc in [
        ("Backend API endpoint", "coding"),
        ("Webinar script draft", "creatives"),
        ("Meta ads campaign setup", "advertising"),
    ]:
        t = BoardTask(title=title, description=title, discipline=disc)
        collab.create_board_task(t)
    out = svc.bulk_reclassify(only_missing=True, limit=50)
    assert out["classified"] >= 3
    matrix = svc.matrix_view()
    assert "engineering" in matrix["columns"]
    assert matrix["card_total"] >= 3
    portfolio = svc.portfolio_view()
    assert portfolio["primary_orchestrator"] == "grok-orchestrator"
    assert "by_workstream" in portfolio
    loops = svc.list_loop_templates()
    assert any(loop_item["id"] == "generator_critic" for loop_item in loops)
    rec = svc.recommend_loop(workstream="creative", risk_class="bounded_external")
    assert rec["recommended"]["id"]


def test_object_dims_skill_agent_loop(svc: OrgDimsService) -> None:
    r = svc.classify_object(
        object_type="skill",
        object_id="create-webinar",
        title="Create webinar skill",
        description="Generate webinar scripts and emails",
        apply=True,
    )
    assert r.object_type == "skill"
    items = svc.list_object_dims("skill")
    assert any(i["object_id"] == "create-webinar" for i in items)
    bundle = svc.set_object_dims(
        "agent",
        "grok-verifier",
        {
            "classification": {
                "primary_workstream": "engineering",
                "domains": ["qa"],
                "channels": [],
                "lifecycle": "ready",
                "priority": "high",
                "risk_class": "read_only",
            },
            "organization_context": {},
            "execution_links": {},
            "provenance": {"source": "human_edit", "confidence": 1.0},
        },
    )
    assert bundle.classification.primary_workstream == "engineering"
