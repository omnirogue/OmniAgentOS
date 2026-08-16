"""Persistence for companies, taxonomy, classifications, Grok agent profiles."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from omniagentos.cognitive_txn import atomic
from omniagentos.contracts import default_db_path, new_id, utc_now_iso
from omniagentos.db.migrate import migrate_connection
from omniagentos.db.store import _connect, _row, _rows
from omniagentos.orgdims.contracts import (
    OMNIAGENTOS_METACOG_AGENTS,
    ClassificationResult,
    DimensionBundle,
    GrokAgentProfile,
    Provenance,
)
from omniagentos.orgdims.taxonomy import (
    CHANNELS,
    COMPANIES,
    LIFECYCLES,
    PRIORITIES,
    RISK_CLASSES,
    TAXONOMY_VERSION,
    WORKSTREAMS,
)

#: The top-level ``org_json`` keys THIS module owns. Derived from the model
#: rather than typed out, so a field added to :class:`DimensionBundle` cannot
#: drift out of the merge rule that protects everybody else's keys.
_ORGDIMS_KEYS: frozenset[str] = frozenset(DimensionBundle.model_fields)



def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class _Unreadable:
    """Sentinel: JSON parse failed (distinct from genuine empty / missing)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNREADABLE"


_UNREADABLE = _Unreadable()


def _loads_or_unreadable(raw: Any, default: Any) -> Any:
    """Like ``_loads`` but returns ``_UNREADABLE`` when the source cannot be parsed.

    Collapsing parse failure onto the same default as empty input is a
    non-result-as-favourable defect: callers cannot tell "no dimensions" from
    "could not read dimensions".
    """
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return _UNREADABLE


class OrgDimsStore:
    def __init__(self, database: str | Path | sqlite3.Connection | None = None) -> None:
        self._lock = RLock()
        if isinstance(database, sqlite3.Connection):
            self._connection = database
            self._connection.row_factory = sqlite3.Row
            self._owns = False
            migrate_connection(self._connection)
        else:
            self._connection = _connect(str(database or default_db_path()))
            self._owns = True
            migrate_connection(self._connection)

    def close(self) -> None:
        if self._owns:
            self._connection.close()
            self._owns = False

    @contextmanager
    def transaction(self, name: str) -> Iterator[None]:
        """Scope several store writes into one all-or-nothing transaction.

        Store methods are individually atomic and nest as savepoints, so a
        caller can compose them without any inner call committing this scope.
        """
        with self._lock, atomic(self._connection, name):
            yield

    def seed_taxonomy(self) -> dict[str, int]:
        """Idempotent seed of companies, products, vocab, Grok metacog agents."""
        now = utc_now_iso()
        counts = {"companies": 0, "products": 0, "taxonomy": 0, "agents": 0}
        with self._lock, atomic(self._connection, "orgdims_seed_taxonomy"):
            for co in COMPANIES:
                cid = new_id("co")
                existing = self._connection.execute(
                    "SELECT id FROM org_companies WHERE slug = ?", (co["slug"],)
                ).fetchone()
                if existing is None:
                    self._connection.execute(
                        "INSERT INTO org_companies (id, slug, name, status, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (cid, co["slug"], co["name"], "active", now),
                    )
                    counts["companies"] += 1
                else:
                    cid = str(existing["id"])
                for pr in co.get("products") or []:
                    exists = self._connection.execute(
                        "SELECT id FROM org_products WHERE company_id = ? AND slug = ?",
                        (cid, pr["slug"]),
                    ).fetchone()
                    if exists is None:
                        self._connection.execute(
                            "INSERT INTO org_products "
                            "(id, company_id, slug, name, status, created_at) "
                            "VALUES (?,?,?,?,?,?)",
                            (new_id("prd"), cid, pr["slug"], pr["name"], "active", now),
                        )
                        counts["products"] += 1

            def _upsert_tax(
                dimension: str, slug: str, label: str, aliases: list[str] | None = None
            ) -> None:
                nonlocal counts
                row = self._connection.execute(
                    "SELECT id FROM org_taxonomy_values WHERE dimension = ? AND slug = ?",
                    (dimension, slug),
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        "INSERT INTO org_taxonomy_values "
                        "(id, dimension, slug, label, parent_slug, aliases_json, enabled, created_at) "
                        "VALUES (?,?,?,?,?,?,1,?)",
                        (
                            new_id("tax"),
                            dimension,
                            slug,
                            label,
                            None,
                            _j(aliases or []),
                            now,
                        ),
                    )
                    counts["taxonomy"] += 1

            for w in WORKSTREAMS:
                _upsert_tax("workstream", w["slug"], w["label"], list(w.get("aliases") or []))
                for d in w.get("domains") or []:
                    _upsert_tax("domain", d, d.replace("-", " ").title(), [])

            for ch in CHANNELS:
                _upsert_tax("channel", ch, ch.replace("-", " ").title())
            for lc in LIFECYCLES:
                _upsert_tax("lifecycle", lc, lc.replace("_", " ").title())
            for rc in RISK_CLASSES:
                _upsert_tax("risk_class", rc, rc.replace("_", " ").title())
            for pr in PRIORITIES:
                _upsert_tax("priority", pr, pr.title())

            for agent in OMNIAGENTOS_METACOG_AGENTS:
                exists = self._connection.execute(
                    "SELECT id FROM org_agent_profiles WHERE slug = ?", (agent.slug,)
                ).fetchone()
                if exists is None:
                    self._connection.execute(
                        "INSERT INTO org_agent_profiles "
                        "(id, slug, name, lineage, model, role, metacog_primary, "
                        "expertise_json, workstreams_json, risk_ceiling, charter, "
                        "enabled, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            agent.id,
                            agent.slug,
                            agent.name,
                            agent.lineage,
                            agent.model,
                            agent.role,
                            1 if agent.metacog_primary else 0,
                            _j(agent.expertise),
                            _j(agent.workstreams),
                            agent.risk_ceiling,
                            agent.charter,
                            1 if agent.enabled else 0,
                            now,
                            now,
                        ),
                    )
                    counts["agents"] += 1

        return counts

    def list_companies(self) -> list[dict[str, Any]]:
        with self._lock:
            companies = _rows(
                self._connection.execute("SELECT * FROM org_companies ORDER BY name").fetchall()
            )
            out = []
            for c in companies:
                products = _rows(
                    self._connection.execute(
                        "SELECT * FROM org_products WHERE company_id = ? ORDER BY name",
                        (c["id"],),
                    ).fetchall()
                )
                out.append({**c, "products": products})
            return out

    def list_workstreams(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM org_taxonomy_values WHERE dimension = 'workstream' "
                    "AND enabled = 1 ORDER BY label"
                ).fetchall()
            )
        return [
            {
                "slug": r["slug"],
                "label": r["label"],
                "aliases": _loads(r.get("aliases_json"), []),
            }
            for r in rows
        ]

    def list_grok_agents(self, *, primary_only: bool = False) -> list[GrokAgentProfile]:
        with self._lock:
            sql = "SELECT * FROM org_agent_profiles WHERE enabled = 1"
            if primary_only:
                sql += " AND metacog_primary = 1"
            sql += " ORDER BY role, name"
            rows = _rows(self._connection.execute(sql).fetchall())
        return [
            GrokAgentProfile(
                id=str(r["id"]),
                slug=str(r["slug"]),
                name=str(r["name"]),
                lineage=str(r.get("lineage") or "cli-grok"),
                model=str(r.get("model") or "grok-4.5"),
                role=str(r["role"]),
                metacog_primary=bool(r.get("metacog_primary")),
                expertise=list(_loads(r.get("expertise_json"), [])),
                workstreams=list(_loads(r.get("workstreams_json"), [])),
                risk_ceiling=str(r.get("risk_ceiling") or "bounded_external"),
                charter=str(r.get("charter") or ""),
                enabled=bool(r.get("enabled")),
                created_at=str(r.get("created_at") or ""),
                updated_at=str(r.get("updated_at") or ""),
            )
            for r in rows
        ]

    def get_board_org(self, task_id: str) -> DimensionBundle | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT org_json FROM board_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        payload = row["org_json"] if isinstance(row, sqlite3.Row) else row[0]
        raw = _loads_or_unreadable(payload, {})
        if raw is _UNREADABLE:
            # Unreadable source must not look like a genuine empty bundle
            # (which previously shared provenance source=ai_classification).
            return DimensionBundle(
                provenance=Provenance(
                    source="unreadable",
                    confidence=0.0,
                    rationale="org_json could not be parsed",
                    evidence_refs=["parse_error:org_json"],
                    taxonomy_version=TAXONOMY_VERSION,
                    classifier_agent=None,
                )
            )
        if not raw:
            return DimensionBundle(
                provenance=Provenance(
                    source="empty",
                    confidence=0.0,
                    rationale="no dimensions recorded",
                    taxonomy_version=TAXONOMY_VERSION,
                    classifier_agent=None,
                )
            )
        return DimensionBundle.model_validate(raw)

    def set_board_org(self, task_id: str, bundle: DimensionBundle, *, commit: bool = True) -> None:
        with self._lock:
            if not commit:  # caller owns the surrounding transaction
                self._write_board_org(task_id, bundle)
                return
            with atomic(self._connection, "orgdims_set_board_org"):
                self._write_board_org(task_id, bundle)

    def _write_board_org(self, task_id: str, bundle: DimensionBundle) -> None:
        """Write org_json without committing (caller owns the transaction).

        MERGE, not replace. ``org_json`` started as this module's own envelope
        and has since become a SHARED one: the automation backlog writes
        ``proposal`` (who proposed a card, and for whom) and ``dispatch`` (the
        compute-pool routing the team dispatcher reads) at the same top level.
        A whole-column write is invisible destruction — a reclassification would
        silently delete the assignee hint and stop an approved AI card from ever
        being dispatchable, with nothing anywhere reporting the loss.

        This module keeps FULL authority over its own five keys (they are
        replaced wholesale, exactly as before, so a re-classification cannot
        leave half of a previous verdict behind). Every other top-level key is
        preserved verbatim, including keys that do not exist yet — the rule is
        "not mine, not mine to delete", which needs no registry to maintain.

        Reads inside the caller's transaction, so the row it merges against is
        the row it is about to write.
        """
        row = self._connection.execute(
            "SELECT org_json FROM board_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        envelope: dict[str, Any] = {}
        raw = None if row is None else row["org_json"]
        if isinstance(raw, str) and raw.strip():
            try:
                loaded = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                envelope = loaded
        foreign = {key: value for key, value in envelope.items() if key not in _ORGDIMS_KEYS}
        merged = {**foreign, **bundle.model_dump()}
        self._connection.execute(
            "UPDATE board_tasks SET org_json = ?, updated_at = ? WHERE id = ?",
            (_j(merged), utc_now_iso(), task_id),
        )

    def save_classification(self, result: ClassificationResult, *, commit: bool = True) -> str:
        now = utc_now_iso()
        cid = new_id("ocl")
        with self._lock:
            if not commit:  # caller owns the surrounding transaction
                self._write_classification(cid, result, now)
                return cid
            with atomic(self._connection, "orgdims_save_classification"):
                self._write_classification(cid, result, now)
        return cid

    def _write_classification(
        self, cid: str, result: ClassificationResult, now: str | None = None
    ) -> None:
        """Insert a classification audit row without committing."""
        ts = now or utc_now_iso()
        self._connection.execute(
            "INSERT INTO org_classifications "
            "(id, object_type, object_id, dimensions_json, confidence, source, "
            "rationale, evidence_json, taxonomy_version, status, classifier_agent, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                result.object_type,
                result.object_id,
                _j(result.bundle.model_dump()),
                result.bundle.provenance.confidence,
                result.bundle.provenance.source,
                result.bundle.provenance.rationale,
                _j(result.bundle.provenance.evidence_refs),
                TAXONOMY_VERSION,
                result.status,
                result.bundle.provenance.classifier_agent,
                ts,
                ts,
            ),
        )

    def apply_classification_batch(
        self,
        items: list[tuple[str, DimensionBundle, ClassificationResult]],
    ) -> list[str]:
        """Apply many board_org + classification writes in a single transaction.

        M-24: bulk reclassify must not open a commit per card. Empty input is a
        no-op (no transaction opened).

        M-40: the batch is genuinely all-or-nothing. Under the store's
        autocommit connection the previous ``commit()`` was a no-op, so a
        failure partway through left an arbitrary prefix of cards half-applied
        — org_json rewritten with no matching audit row, or the reverse.
        """
        if not items:
            return []
        cids: list[str] = []
        with self._lock, atomic(self._connection, "orgdims_apply_batch"):
            now = utc_now_iso()
            for task_id, bundle, result in items:
                self._write_board_org(task_id, bundle)
                cid = new_id("ocl")
                self._write_classification(cid, result, now)
                cids.append(cid)
        return cids

    def get_company_by_slug(self, slug: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._connection.execute(
                    "SELECT * FROM org_companies WHERE slug = ?", (slug,)
                ).fetchone()
            )

    def get_product_by_slug(self, company_id: str, slug: str) -> dict[str, Any] | None:
        with self._lock:
            return _row(
                self._connection.execute(
                    "SELECT * FROM org_products WHERE company_id = ? AND slug = ?",
                    (company_id, slug),
                ).fetchone()
            )

    def list_board_task_rows(
        self,
        *,
        limit: int = 500,
        after_id: str | None = None,
        ascending_id: bool = False,
    ) -> list[dict[str, Any]]:
        """Raw board_tasks rows for bulk reclassify / matrix / portfolio views.

        When ``ascending_id`` is True (bulk reclassify path), rows are ordered by
        ``id ASC`` and optional ``after_id`` enables an idempotent resume cursor
        (M-24): callers process ``id > after_id`` pages until empty.
        """
        with self._lock:
            # org_json may be missing on pre-061 DBs mid-migration; SELECT * is fine.
            try:
                if ascending_id:
                    if after_id:
                        return _rows(
                            self._connection.execute(
                                "SELECT id, title, description, discipline, priority, "
                                "status, org_json, archived_at, created_at, updated_at, project_id "
                                "FROM board_tasks WHERE id > ? ORDER BY id ASC LIMIT ?",
                                (after_id, limit),
                            ).fetchall()
                        )
                    return _rows(
                        self._connection.execute(
                            "SELECT id, title, description, discipline, priority, "
                            "status, org_json, archived_at, created_at, updated_at, project_id "
                            "FROM board_tasks ORDER BY id ASC LIMIT ?",
                            (limit,),
                        ).fetchall()
                    )
                return _rows(
                    self._connection.execute(
                        "SELECT id, title, description, discipline, priority, status, "
                        "org_json, archived_at, created_at, updated_at, project_id "
                        "FROM board_tasks ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                )
            except sqlite3.OperationalError:
                # Column missing — fall back without org_json
                if ascending_id:
                    if after_id:
                        return _rows(
                            self._connection.execute(
                                "SELECT id, title, description, discipline, priority, "
                                "status, archived_at, created_at, updated_at, project_id "
                                "FROM board_tasks WHERE id > ? ORDER BY id ASC LIMIT ?",
                                (after_id, limit),
                            ).fetchall()
                        )
                    return _rows(
                        self._connection.execute(
                            "SELECT id, title, description, discipline, priority, "
                            "status, archived_at, created_at, updated_at, project_id "
                            "FROM board_tasks ORDER BY id ASC LIMIT ?",
                            (limit,),
                        ).fetchall()
                    )
                return _rows(
                    self._connection.execute(
                        "SELECT id, title, description, discipline, priority, status, "
                        "archived_at, created_at, updated_at, project_id "
                        "FROM board_tasks ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                )

    def _ensure_object_dims_table(self) -> None:
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS org_object_dims ("
            "object_type TEXT NOT NULL, "
            "object_id TEXT NOT NULL, "
            "dimensions_json TEXT NOT NULL DEFAULT '{}', "
            "updated_at TEXT NOT NULL, "
            "PRIMARY KEY (object_type, object_id))"
        )

    def upsert_object_dims(self, object_type: str, object_id: str, bundle: DimensionBundle) -> None:
        from omniagentos.contracts import utc_now_iso

        with self._lock:
            self._ensure_object_dims_table()
            now = utc_now_iso()
            with atomic(self._connection, "orgdims_upsert_object_dims"):
                self._connection.execute(
                    "INSERT INTO org_object_dims "
                    "(object_type, object_id, dimensions_json, updated_at) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(object_type, object_id) DO UPDATE SET "
                    "dimensions_json = excluded.dimensions_json, "
                    "updated_at = excluded.updated_at",
                    (object_type, object_id, _j(bundle.model_dump()), now),
                )

    def list_object_dims(self, object_type: str) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_object_dims_table()
            rows = _rows(
                self._connection.execute(
                    "SELECT object_type, object_id, dimensions_json, updated_at "
                    "FROM org_object_dims WHERE object_type = ? ORDER BY updated_at DESC",
                    (object_type,),
                ).fetchall()
            )
        out = []
        for r in rows:
            out.append(
                {
                    "object_type": r["object_type"],
                    "object_id": r["object_id"],
                    "dimensions": _loads(r.get("dimensions_json"), {}),
                    "updated_at": r.get("updated_at"),
                }
            )
        return out

    def get_object_dims(self, object_type: str, object_id: str) -> DimensionBundle | None:
        with self._lock:
            self._ensure_object_dims_table()
            row = self._connection.execute(
                "SELECT dimensions_json FROM org_object_dims "
                "WHERE object_type = ? AND object_id = ?",
                (object_type, object_id),
            ).fetchone()
        if row is None:
            return None
        payload = row["dimensions_json"] if isinstance(row, sqlite3.Row) else row[0]
        raw = _loads_or_unreadable(payload, {})
        if raw is _UNREADABLE:
            return DimensionBundle(
                provenance=Provenance(
                    source="unreadable",
                    confidence=0.0,
                    rationale="dimensions_json could not be parsed",
                    evidence_refs=["parse_error:dimensions_json"],
                    taxonomy_version=TAXONOMY_VERSION,
                    classifier_agent=None,
                )
            )
        if not raw:
            return DimensionBundle(
                provenance=Provenance(
                    source="empty",
                    confidence=0.0,
                    rationale="no dimensions recorded",
                    taxonomy_version=TAXONOMY_VERSION,
                    classifier_agent=None,
                )
            )
        return DimensionBundle.model_validate(raw)
