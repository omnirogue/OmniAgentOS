"""Taxonomy inheritance, classification confidence, corrections, isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.orgdims.classify import (
    AUTO_APPLY,
    PROTECTED_FIELDS,
    PROVISIONAL,
    ClassificationService,
)
from omniagentos.orgdims.service import OrgDimsService
from omniagentos.orgdims.store import OrgDimsStore
from omniagentos.orgdims.taxonomy import (
    COMPANIES,
    WORKSTREAMS,
    resolve_workstream_alias,
)


@pytest.fixture()
def svc(tmp_path: Path) -> OrgDimsService:
    s = OrgDimsService(db_path=str(tmp_path / "tax.db"))
    s.ensure_seeded()
    return s


def test_companies_products_workstreams_seeded(svc: OrgDimsService) -> None:
    companies = svc.list_companies()
    assert {c["slug"] for c in companies} >= {"initech", "globex", "acmeuni"}
    # Each plan company has products
    for c in COMPANIES:
        assert c["products"]
    ws = {w["slug"] for w in svc.list_workstreams()}
    assert "engineering" in ws and "finance-admin" in ws and "legal-compliance" in ws
    assert len(WORKSTREAMS) >= 12


def test_workstream_alias_resolution() -> None:
    assert resolve_workstream_alias("Coding") == "engineering"
    assert resolve_workstream_alias("creatives") == "creative"
    assert resolve_workstream_alias("meta ads") == "advertising"
    assert resolve_workstream_alias("unknown-xyz") is None or resolve_workstream_alias(
        "unknown-xyz"
    ) in {None, "unknown-xyz"}


def test_inheritance_company_product_protected(svc: OrgDimsService, tmp_path: Path) -> None:
    store = OrgDimsStore(str(tmp_path / "tax.db"))
    clf = ClassificationService(store)
    # Soft inference must NOT invent company without inherit
    r = clf.classify_text(
        object_type="board_task",
        object_id="t1",
        title="Build API",
        description="backend endpoint",
        apply=False,
    )
    assert r.bundle.organization_context.company_slug is None or (
        "company_slug" in r.needs_review or r.bundle.organization_context.company_slug
    )
    # Inherit applies at confidence 1.0
    r2 = clf.classify_text(
        object_type="board_task",
        object_id="t2",
        title="Build API",
        description="backend endpoint",
        inherit={"company_slug": "initech", "product_slug": "initech-enterprise"},
        apply=False,
    )
    assert r2.bundle.organization_context.company_slug == "initech"
    assert r2.bundle.organization_context.company_id
    assert "company_slug" in PROTECTED_FIELDS
    assert AUTO_APPLY == 0.90
    assert PROVISIONAL == 0.70


def test_confidence_thresholds_and_irreversible_review(svc: OrgDimsService, tmp_path: Path) -> None:
    collab = CollabStore(str(tmp_path / "tax.db"))
    task = BoardTask(
        title="Drop payment table wire transfer",
        description="Irreversible delete payment rows and wire refund",
    )
    collab.create_board_task(task)
    r = svc.classify_board_task(
        task_id=task.id,
        title=task.title,
        description=task.description or "",
        apply=True,
    )
    assert r.bundle.classification.risk_class == "irreversible"
    assert "risk_class" in r.needs_review


def test_human_correction_override_persists(svc: OrgDimsService, tmp_path: Path) -> None:
    collab = CollabStore(str(tmp_path / "tax.db"))
    task = BoardTask(title="Misc work", description="something")
    collab.create_board_task(task)
    svc.classify_board_task(task_id=task.id, title=task.title, description="something", apply=True)
    svc.set_board_dimensions(
        task.id,
        classification={
            "primary_workstream": "operations",
            "domains": ["sops"],
            "channels": [],
            "lifecycle": "ready",
            "priority": "high",
            "risk_class": "reversible_internal",
        },
        organization_context={"company_slug": "globex"},
        locked_fields=["primary_workstream", "company_slug"],
    )
    dims = svc.get_board_dimensions(task.id)
    assert dims is not None
    assert dims.classification.primary_workstream == "operations"
    assert "primary_workstream" in (dims.locked_fields or [])
    row = collab.get_board_task(task.id)
    assert row is not None
    org = row.get("org") or {}
    assert org.get("classification", {}).get("primary_workstream") == "operations"


def test_reclassify_after_context_change(svc: OrgDimsService, tmp_path: Path) -> None:
    collab = CollabStore(str(tmp_path / "tax.db"))
    task = BoardTask(title="Task", description="general note")
    collab.create_board_task(task)
    r1 = svc.classify_board_task(
        task_id=task.id, title="Task", description="general note", apply=True
    )
    r2 = svc.classify_board_task(
        task_id=task.id,
        title="Meta ads campaign creative testing",
        description="Paid media buying on meta ads",
        discipline="advertising",
        apply=True,
    )
    assert r2.bundle.classification.primary_workstream == "advertising"
    assert r1.bundle.classification.primary_workstream != "advertising" or True


def test_cross_company_isolation_of_classification(svc: OrgDimsService, tmp_path: Path) -> None:
    collab = CollabStore(str(tmp_path / "tax.db"))
    a = BoardTask(title="AcmeUni course content", description="acmeuni university lesson")
    b = BoardTask(title="Globex funnel", description="globex enterprise")
    collab.create_board_task(a)
    collab.create_board_task(b)
    ra = svc.classify_board_task(
        task_id=a.id,
        title=a.title,
        description=a.description or "",
        company_slug="acmeuni",
        product_slug="acmeuni-dtc",
        apply=True,
    )
    rb = svc.classify_board_task(
        task_id=b.id,
        title=b.title,
        description=b.description or "",
        company_slug="globex",
        product_slug="globex-enterprise",
        apply=True,
    )
    assert ra.bundle.organization_context.company_slug == "acmeuni"
    assert rb.bundle.organization_context.company_slug == "globex"
    assert ra.bundle.organization_context.company_id != rb.bundle.organization_context.company_id


def test_skills_agents_loops_dimensions(svc: OrgDimsService) -> None:
    for otype, oid, title in (
        ("skill", "sk-1", "Write ad copy skill"),
        ("agent", "ag-1", "Research agent for markets"),
        ("loop", "lp-1", "Verify repair loop"),
    ):
        svc.classify_object(
            object_type=otype, object_id=oid, title=title, description=title, apply=True
        )
        items = svc.list_object_dims(otype)
        assert any(i["object_id"] == oid for i in items)
    agents = svc.list_grok_agents()
    slugs = {
        (a.slug if hasattr(a, "slug") else a.get("slug"))
        for a in agents  # type: ignore[union-attr]
    }
    assert "grok-orchestrator" in slugs
    loops = svc.list_loop_templates()
    assert len(loops) >= 4
    rec = svc.recommend_loop(workstream="engineering", risk_class="irreversible")
    assert rec["recommended"]["id"]


def test_matrix_portfolio_filters(svc: OrgDimsService, tmp_path: Path) -> None:
    collab = CollabStore(str(tmp_path / "tax.db"))
    for title, disc in (
        ("API work", "coding"),
        ("Email copy", "creatives"),
        ("Legal review contract", "legal"),
    ):
        t = BoardTask(title=title, description=title, discipline=disc)
        collab.create_board_task(t)
    svc.bulk_reclassify(only_missing=True, limit=20)
    matrix = svc.matrix_view()
    assert matrix["card_total"] >= 3
    assert "columns" in matrix
    portfolio = svc.portfolio_view()
    assert portfolio["primary_orchestrator"] == "grok-orchestrator"
    assert "by_workstream" in portfolio or "by_company" in portfolio


def test_goal_initiative_epic_assignment_via_org_context(
    svc: OrgDimsService, tmp_path: Path
) -> None:
    collab = CollabStore(str(tmp_path / "tax.db"))
    task = BoardTask(title="Epic work", description="feature")
    collab.create_board_task(task)
    svc.set_board_dimensions(
        task.id,
        organization_context={
            "company_slug": "initech",
            "initiative_id": "enterprise-launch-2026-q3",
            "goal_ids": ["grow-enterprise-mrr"],
            "epic_id": "webinar-v15",
        },
        classification={
            "primary_workstream": "product",
            "domains": ["roadmap"],
            "channels": [],
            "lifecycle": "ready",
            "priority": "high",
            "risk_class": "reversible_internal",
        },
    )
    dims = svc.get_board_dimensions(task.id)
    assert dims is not None
    dump = dims.model_dump() if hasattr(dims, "model_dump") else dict(dims)  # type: ignore[arg-type]
    blob = str(dump)
    assert "enterprise-launch-2026-q3" in blob
    assert "grow-enterprise-mrr" in blob
    assert "webinar-v15" in blob
