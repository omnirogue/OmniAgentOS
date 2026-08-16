"""Facade for multidimensional org + Grok metacog agents."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from omniagentos.orgdims.classify import ClassificationService
from omniagentos.orgdims.contracts import (
    Classification,
    ClassificationResult,
    DimensionBundle,
    GrokAgentProfile,
    OrganizationContext,
    Provenance,
)
from omniagentos.orgdims.store import OrgDimsStore
from omniagentos.orgdims.taxonomy import TAXONOMY_VERSION, WORKSTREAMS

LOG = logging.getLogger(__name__)

# Reusable loop templates (P6) — static registry for applicability/recommendation.
LOOP_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "solo_executor",
        "title": "Solo executor",
        "purpose": "execute",
        "topology": "sequential",
        "workstreams": ["engineering", "product", "operations"],
        "risk_classes": ["read_only", "reversible_internal"],
    },
    {
        "id": "generator_critic",
        "title": "Generator + critic",
        "purpose": "verify",
        "topology": "generator-critic",
        "workstreams": ["engineering", "finance-admin", "legal-compliance"],
        "risk_classes": ["bounded_external", "irreversible"],
    },
    {
        "id": "generate_critique_repair_verify",
        "title": "Generate → critique → repair → verify",
        "purpose": "generate",
        "topology": "generator-critic",
        "workstreams": ["creative", "advertising", "marketing"],
        "risk_classes": ["read_only", "reversible_internal", "bounded_external"],
    },
    {
        "id": "hypothesis_portfolio",
        "title": "Hypothesis portfolio",
        "purpose": "research",
        "topology": "portfolio",
        "workstreams": ["research", "data-analytics", "engineering"],
        "risk_classes": ["read_only", "reversible_internal"],
    },
    {
        "id": "verify_then_execute",
        "title": "Verify then execute",
        "purpose": "execute",
        "topology": "sequential",
        "workstreams": ["finance-admin", "legal-compliance", "operations"],
        "risk_classes": ["bounded_external", "irreversible"],
    },
    {
        "id": "monitor_and_repair",
        "title": "Monitor and repair",
        "purpose": "repair",
        "topology": "monitor-and-repair",
        "workstreams": ["engineering", "operations"],
        "risk_classes": ["reversible_internal", "bounded_external"],
    },
]


class OrgDimsService:
    def __init__(self, store: OrgDimsStore | None = None, db_path: str | None = None) -> None:
        self.store = store or OrgDimsStore(db_path)
        self.classifier = ClassificationService(self.store)
        # Ensure taxonomy + Grok agents exist (idempotent).
        self._seeded = False

    def ensure_seeded(self) -> dict[str, int]:
        counts = self.store.seed_taxonomy()
        self._seeded = True
        return counts

    def health(self) -> dict[str, Any]:
        if not self._seeded:
            self.ensure_seeded()
        agents = self.store.list_grok_agents(primary_only=True)
        agent_slugs = [a.slug for a in agents]
        # ok requires a measurable primary metacog roster that includes the
        # declared orchestrator — empty/disabled primary agents is not healthy.
        orchestrator_present = "grok-orchestrator" in agent_slugs
        return {
            "ok": bool(agents) and orchestrator_present,
            "taxonomy_version": TAXONOMY_VERSION,
            "companies": len(self.store.list_companies()),
            "workstreams": len(self.store.list_workstreams()),
            "grok_metacog_agents": agent_slugs,
            "primary_orchestrator": "grok-orchestrator" if orchestrator_present else None,
            "loop_templates": len(LOOP_TEMPLATES),
        }

    def list_companies(self) -> list[dict[str, Any]]:
        self.ensure_seeded()
        return self.store.list_companies()

    def list_workstreams(self) -> list[dict[str, Any]]:
        self.ensure_seeded()
        return self.store.list_workstreams()

    def list_grok_agents(self, *, primary_only: bool = False) -> list[GrokAgentProfile]:
        self.ensure_seeded()
        return self.store.list_grok_agents(primary_only=primary_only)

    def list_loop_templates(self) -> list[dict[str, Any]]:
        return list(LOOP_TEMPLATES)

    def classify_board_task(
        self,
        *,
        task_id: str,
        title: str = "",
        description: str = "",
        discipline: str | None = None,
        priority: str | None = None,
        company_slug: str | None = None,
        product_slug: str | None = None,
        apply: bool = True,
    ) -> ClassificationResult:
        self.ensure_seeded()
        inherit: dict[str, Any] = {}
        if company_slug:
            inherit["company_slug"] = company_slug
        if product_slug:
            inherit["product_slug"] = product_slug
        return self.classifier.classify_text(
            object_type="board_task",
            object_id=task_id,
            title=title,
            description=description,
            discipline=discipline,
            priority=priority,
            inherit=inherit,
            apply=apply,
        )

    def get_board_dimensions(self, task_id: str) -> DimensionBundle | None:
        self.ensure_seeded()
        return self.store.get_board_org(task_id)

    def set_board_dimensions(
        self,
        task_id: str,
        *,
        classification: dict[str, Any] | None = None,
        organization_context: dict[str, Any] | None = None,
        execution_links: dict[str, Any] | None = None,
        locked_fields: list[str] | None = None,
    ) -> DimensionBundle:
        """Manual dimension editor PATCH for a board card."""
        self.ensure_seeded()
        current = self.store.get_board_org(task_id) or DimensionBundle()
        if organization_context:
            current.organization_context = OrganizationContext.model_validate(
                {**current.organization_context.model_dump(), **organization_context}
            )
        if classification:
            current.classification = Classification.model_validate(
                {**current.classification.model_dump(), **classification}
            )
        if execution_links is not None:
            current.execution_links = {**current.execution_links, **execution_links}
        if locked_fields is not None:
            current.locked_fields = list(locked_fields)
        current.provenance = Provenance(
            source="human_edit",
            confidence=1.0,
            rationale="manual dimension editor",
            classifier_agent="operator",
            taxonomy_version=TAXONOMY_VERSION,
            evidence_refs=list(current.provenance.evidence_refs or []),
        )
        self.store.set_board_org(task_id, current)
        return current

    def bulk_reclassify(
        self,
        *,
        only_missing: bool = True,
        limit: int = 500,
        company_slug: str | None = None,
        product_slug: str | None = None,
        cursor: str | None = None,
        batch_size: int = 50,
    ) -> dict[str, Any]:
        """Reclassify historical board cards (P8 bulk migration tool).

        M-24: pages by stable ``id ASC`` cursor, classifies in memory, and
        persists each bounded batch in a single transaction (no per-row commit).
        Resume by passing the returned ``next_cursor`` as ``cursor``.
        """
        self.ensure_seeded()
        batch_size = max(1, min(int(batch_size or 50), 200))
        page_limit = max(1, min(int(limit or 500), 5000))
        rows = self.store.list_board_task_rows(
            limit=page_limit,
            after_id=cursor,
            ascending_id=True,
        )
        classified = 0
        skipped = 0
        errors: list[dict[str, str]] = []
        pending: list[tuple[str, DimensionBundle, ClassificationResult]] = []
        last_id: str | None = cursor
        batches_committed = 0

        def _flush() -> None:
            nonlocal batches_committed
            if not pending:
                return
            self.store.apply_classification_batch(pending)
            batches_committed += 1
            pending.clear()

        for row in rows:
            task_id = str(row.get("id") or "")
            if not task_id:
                continue
            last_id = task_id
            existing = row.get("org_json") or "{}"
            if only_missing and existing not in ("", "{}", None):
                try:
                    parsed = json.loads(existing) if isinstance(existing, str) else existing
                except (TypeError, ValueError):
                    parsed = {}
                # Consider missing if no workstream yet (explicit None/empty check).
                cls = (parsed or {}).get("classification") or {}
                existing_ws = cls.get("primary_workstream")
                if existing_ws is not None and str(existing_ws).strip() != "":
                    skipped += 1
                    continue
            try:
                inherit: dict[str, Any] = {}
                if company_slug:
                    inherit["company_slug"] = company_slug
                if product_slug:
                    inherit["product_slug"] = product_slug
                # apply=False: classify + lock-merge only; batch writer commits.
                result = self.classifier.classify_text(
                    object_type="board_task",
                    object_id=task_id,
                    title=str(row.get("title") or ""),
                    description=str(row.get("description") or ""),
                    discipline=row.get("discipline"),
                    priority=row.get("priority"),
                    inherit=inherit,
                    apply=False,
                )
                pending.append((task_id, result.bundle, result))
                classified += 1
                if len(pending) >= batch_size:
                    _flush()
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": task_id, "error": str(exc)})
        _flush()

        done = len(rows) < page_limit
        next_cursor = None if done else last_id
        # ok is False whenever any row failed — a total miss must not look like
        # a successful bulk migration (was always True even with errors).
        return {
            "ok": not errors,
            "scanned": len(rows),
            "classified": classified,
            "skipped": skipped,
            "errors": errors[:50],
            "only_missing": only_missing,
            "cursor": last_id,
            "next_cursor": next_cursor,
            "done": done,
            "batch_size": batch_size,
            "batches_committed": batches_committed,
        }

    def matrix_view(self, *, limit: int = 500) -> dict[str, Any]:
        """Matrix: rows=company/product (or initiative), columns=workstream."""
        self.ensure_seeded()
        rows = self.store.list_board_task_rows(limit=limit)
        # Parse org_json → org dict FIRST, then enrich from projects
        from omniagentos.orgdims.enrich import enrich_company_from_projects
        for row in rows:
            row["org"] = self._parse_org(row.get("org_json"))
        enrich_company_from_projects(rows, self.store._connection)
        workstream_slugs = [w["slug"] for w in WORKSTREAMS]
        # cell key: (row_key, workstream) -> list of cards
        cells: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: {ws: [] for ws in workstream_slugs}
        )
        uncategorized: list[dict[str, Any]] = []
        for row in rows:
            if row.get("archived_at"):
                continue
            org = row.get("org") or {}
            cls = org.get("classification") or {}
            octx = org.get("organization_context") or {}
            # Unknown workstream is uncategorized — never invent a concrete label.
            raw_ws = cls.get("primary_workstream")
            ws = raw_ws if raw_ws is not None and str(raw_ws).strip() != "" else "uncategorized"
            company = octx.get("company_slug") or "unassigned"
            product = octx.get("product_slug") or ""
            row_key = f"{company}/{product}" if product else company
            card = {
                "id": row["id"],
                "title": row.get("title"),
                "status": row.get("status"),
                "priority": row.get("priority"),
                "workstream": ws,
                "risk_class": cls.get("risk_class"),
                "agents": (org.get("execution_links") or {}).get("preferred_agent_profiles") or [],
            }
            if ws not in workstream_slugs:
                uncategorized.append(card)
                continue
            cells[row_key][ws].append(card)

        matrix_rows = []
        for row_key in sorted(cells.keys()):
            counts = {ws: len(cells[row_key][ws]) for ws in workstream_slugs}
            matrix_rows.append(
                {
                    "row_key": row_key,
                    "counts": counts,
                    "total": sum(counts.values()),
                    "cells": cells[row_key],
                }
            )
        return {
            "columns": workstream_slugs,
            "rows": matrix_rows,
            "uncategorized": uncategorized,
            "card_total": sum(int(str(r.get("total") or 0)) for r in matrix_rows)
            + len(uncategorized),
        }

    def portfolio_view(self, *, limit: int = 500) -> dict[str, Any]:
        """Portfolio aggregates by workstream and company."""
        self.ensure_seeded()
        rows = self.store.list_board_task_rows(limit=limit)
        # Parse org_json → org dict FIRST, then enrich from projects
        from omniagentos.orgdims.enrich import enrich_company_from_projects
        for row in rows:
            row["org"] = self._parse_org(row.get("org_json"))
        enrich_company_from_projects(rows, self.store._connection)
        by_ws: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "total": 0,
                "open": 0,
                "in_progress": 0,
                "blocked": 0,
                "done": 0,
                "cancelled": 0,
            }
        )
        by_company: dict[str, dict[str, int]] = defaultdict(
            lambda: {"total": 0, "in_progress": 0, "blocked": 0, "done": 0}
        )
        risk_counts: dict[str, int] = defaultdict(int)
        grok_assigned = 0
        for row in rows:
            if row.get("archived_at"):
                continue
            org = row.get("org") or {}
            cls = org.get("classification") or {}
            octx = org.get("organization_context") or {}
            raw_ws = cls.get("primary_workstream")
            ws = raw_ws if raw_ws is not None and str(raw_ws).strip() != "" else "uncategorized"
            company = octx.get("company_slug") or "unassigned"
            status = str(row.get("status") or "open")
            by_ws[ws]["total"] += 1
            if status in by_ws[ws]:
                by_ws[ws][status] += 1
            elif status in {"open", "pending", "claimed"}:
                by_ws[ws]["open"] += 1
            by_company[company]["total"] += 1
            if status == "in_progress":
                by_company[company]["in_progress"] += 1
            elif status == "blocked":
                by_company[company]["blocked"] += 1
            elif status == "done":
                by_company[company]["done"] += 1
            risk = cls.get("risk_class") or "unknown"
            risk_counts[str(risk)] += 1
            agents = (org.get("execution_links") or {}).get("preferred_agent_profiles") or []
            if any(str(a).startswith("grok-") for a in agents):
                grok_assigned += 1

        return {
            "by_workstream": dict(by_ws),
            "by_company": dict(by_company),
            "by_risk": dict(risk_counts),
            "grok_preferred_cards": grok_assigned,
            "primary_orchestrator": "grok-orchestrator",
            "workstreams": [w["slug"] for w in WORKSTREAMS],
        }

    def classify_object(
        self,
        *,
        object_type: str,
        object_id: str,
        title: str = "",
        description: str = "",
        apply: bool = True,
    ) -> ClassificationResult:
        """Classify skill/agent/loop definitions (stored as classification audit rows)."""
        self.ensure_seeded()
        if object_type not in {"skill", "agent", "loop", "board_task"}:
            raise ValueError(f"unsupported object_type {object_type}")
        result = self.classifier.classify_text(
            object_type=object_type,
            object_id=object_id,
            title=title,
            description=description,
            apply=False,
        )
        if apply:
            # Persist latest classification for non-board objects via audit table.
            self.store.save_classification(result)
            self.store.upsert_object_dims(object_type, object_id, result.bundle)
        return result

    def list_object_dims(self, object_type: str) -> list[dict[str, Any]]:
        self.ensure_seeded()
        return self.store.list_object_dims(object_type)

    def set_object_dims(
        self, object_type: str, object_id: str, bundle: dict[str, Any]
    ) -> DimensionBundle:
        self.ensure_seeded()
        dim = DimensionBundle.model_validate(bundle)
        dim.provenance.source = "human_edit"
        dim.provenance.classifier_agent = "operator"
        dim.provenance.confidence = 1.0
        self.store.upsert_object_dims(object_type, object_id, dim)
        return dim

    def recommend_loop(
        self, *, workstream: str | None, risk_class: str | None = None
    ) -> dict[str, Any]:
        # Do not invent risk="reversible_internal" when the caller had no signal.
        risk = risk_class
        if workstream is None and risk is None:
            return {
                "recommended": None,
                "reason": "workstream=unset risk=unset; insufficient signal for loop ranking",
                "classifier_agent": "grok-strategy-selector",
            }
        candidates: list[tuple[int, dict[str, Any]]] = []
        for loop in LOOP_TEMPLATES:
            score = 0
            if workstream is not None and workstream in loop["workstreams"]:
                score += 2
            if risk is not None and risk in loop["risk_classes"]:
                score += 1
            candidates.append((score, loop))
        candidates.sort(key=lambda item: item[0], reverse=True)
        # Zero-evidence ranking is not a recommendation: every score can be 0
        # when the workstream/risk strings match no template (stable sort would
        # otherwise surface LOOP_TEMPLATES[0] as "recommended").
        best_score = candidates[0][0] if candidates else 0
        best = candidates[0][1] if candidates and best_score > 0 else None
        if best is None:
            return {
                "recommended": None,
                "reason": (
                    f"workstream={workstream if workstream is not None else 'unset'} "
                    f"risk={risk if risk is not None else 'unset'}; "
                    f"no loop template matched applicability signals"
                ),
                "classifier_agent": "grok-strategy-selector",
            }
        return {
            "recommended": best,
            "reason": (
                f"workstream={workstream if workstream is not None else 'unset'} "
                f"risk={risk if risk is not None else 'unset'}; "
                f"ranked by Grok strategy selector applicability"
            ),
            "classifier_agent": "grok-strategy-selector",
        }

    def enrich_board_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Attach org dimensions to a board API row (additive)."""
        out = dict(row)
        task_id = str(row.get("id") or "")
        if not task_id:
            out["org"] = None
            return out
        # Prefer already-persisted org_json if collab store surfaced it
        raw = row.get("org_json") or row.get("org")
        if isinstance(raw, dict) and raw:
            out["org"] = raw
            return out
        if isinstance(raw, str) and raw.strip() and raw.strip() != "{}":
            try:
                out["org"] = json.loads(raw)
                return out
            except (TypeError, ValueError):
                pass
        bundle = self.store.get_board_org(task_id)
        out["org"] = bundle.model_dump() if bundle else {}
        return out

    @staticmethod
    def _parse_org(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                val = json.loads(raw)
                return val if isinstance(val, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}
