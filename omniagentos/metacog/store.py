"""SQLite access for metacog tables (migration 060)."""

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
from omniagentos.metacog.contracts import (
    ArtifactEnvelope,
    CheckpointPacket,
    MemoryRecord,
    MetacognitiveState,
    ReflectionReport,
    SkillVersion,
    StrategyDef,
)


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


class MetacogStore:
    """Thread-safe store for the metacognition backbone."""

    def __init__(self, database: str | Path | sqlite3.Connection | None = None) -> None:
        self._lock = RLock()
        if isinstance(database, sqlite3.Connection):
            self._connection = database
            self._connection.row_factory = sqlite3.Row
            self._owns = False
            migrate_connection(self._connection)
        else:
            path = str(database or default_db_path())
            self._connection = _connect(path)
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

    # --- artifacts ---------------------------------------------------------

    def register_artifact(self, art: ArtifactEnvelope) -> ArtifactEnvelope:
        """Register an envelope and its provenance edges atomically (M-40).

        The envelope insert and the ``input`` edge inserts are one transaction.
        Under autocommit a mid-loop failure committed the envelope with only
        some of its edges, and the identity dedupe then made that state
        permanent: every retry matched the existing envelope and returned early,
        so the missing provenance edges were never written.
        """
        with self._lock:
            # Identity = content hash + registration scope.
            # Content may be shared (same bytes/hash), but envelopes are never
            # reused across tasks, runs, or artifact types (H-21 / F-31).
            # Within the same identity key, re-registration is a pure dedupe.
            existing = self._connection.execute(
                "SELECT * FROM metacog_artifacts "
                "WHERE content_hash = ? AND company_id = ? "
                "AND IFNULL(project_id, '') = IFNULL(?, '') "
                "AND IFNULL(task_id, '') = IFNULL(?, '') "
                "AND IFNULL(run_id, '') = IFNULL(?, '') "
                "AND artifact_type = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (
                    art.content_hash,
                    art.company_id,
                    art.project_id,
                    art.task_id,
                    art.run_id,
                    art.artifact_type,
                ),
            ).fetchone()
            if existing is not None:
                return self._artifact_from_row(_row(existing) or {})

            now = utc_now_iso()
            art.created_at = art.created_at or now
            art.updated_at = now
            with atomic(self._connection, "metacog_register_artifact"):
                self._connection.execute(
                    "INSERT INTO metacog_artifacts ("
                    "id, artifact_type, company_id, project_id, run_id, task_id, "
                    "attempt_id, producer_agent_id, content_uri, content_hash, format, "
                    "schema_version, base_repo, base_sha, provenance_json, quality_json, "
                    "retention_class, access_scope, supersedes_json, created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        art.id,
                        art.artifact_type,
                        art.company_id,
                        art.project_id,
                        art.run_id,
                        art.task_id,
                        art.attempt_id,
                        art.producer_agent_id,
                        art.content_uri,
                        art.content_hash,
                        art.format,
                        art.schema_version,
                        art.base_repo,
                        art.base_sha,
                        _j(art.provenance),
                        _j(art.quality),
                        art.retention_class,
                        art.access_scope,
                        _j(art.supersedes),
                        art.created_at,
                        art.updated_at,
                    ),
                )
                # Provenance edges — only link existing artifact ids (never raw paths).
                for src in art.provenance.get("inputs") or []:
                    if not isinstance(src, str) or not src:
                        continue
                    exists = self._connection.execute(
                        "SELECT 1 FROM metacog_artifacts WHERE id = ?", (src,)
                    ).fetchone()
                    if exists is None:
                        continue
                    self._connection.execute(
                        "INSERT INTO metacog_artifact_edges "
                        "(id, from_artifact, to_artifact, edge_type, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (new_id("aed"), src, art.id, "input", now),
                    )
            return art

    def get_artifact(self, artifact_id: str) -> ArtifactEnvelope | None:
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM metacog_artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
            )
        return None if row is None else self._artifact_from_row(row)

    def list_artifacts(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        limit: int = 50,
    ) -> list[ArtifactEnvelope]:
        with self._lock:
            if task_id:
                rows = _rows(
                    self._connection.execute(
                        "SELECT * FROM metacog_artifacts WHERE task_id = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (task_id, limit),
                    ).fetchall()
                )
            elif run_id:
                rows = _rows(
                    self._connection.execute(
                        "SELECT * FROM metacog_artifacts WHERE run_id = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (run_id, limit),
                    ).fetchall()
                )
            else:
                rows = _rows(
                    self._connection.execute(
                        "SELECT * FROM metacog_artifacts ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                )
        return [self._artifact_from_row(r) for r in rows]

    def _artifact_from_row(self, row: dict[str, Any]) -> ArtifactEnvelope:
        return ArtifactEnvelope(
            id=str(row["id"]),
            artifact_type=str(row["artifact_type"]),
            company_id=str(row.get("company_id") or "default"),
            project_id=row.get("project_id"),
            run_id=row.get("run_id"),
            task_id=row.get("task_id"),
            attempt_id=row.get("attempt_id"),
            producer_agent_id=row.get("producer_agent_id"),
            content_uri=str(row.get("content_uri") or ""),
            content_hash=str(row.get("content_hash") or ""),
            format=str(row.get("format") or "json"),
            schema_version=int(row.get("schema_version") or 1),
            base_repo=row.get("base_repo"),
            base_sha=row.get("base_sha"),
            provenance=_loads(row.get("provenance_json"), {}),
            quality=_loads(row.get("quality_json"), {}),
            retention_class=str(row.get("retention_class") or "project"),
            access_scope=str(row.get("access_scope") or "task"),
            supersedes=list(_loads(row.get("supersedes_json"), [])),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )

    # --- checkpoints -------------------------------------------------------

    def save_checkpoint(self, ckp: CheckpointPacket) -> CheckpointPacket:
        with self._lock:
            self._connection.execute(
                "INSERT INTO metacog_checkpoints ("
                "id, run_id, task_id, attempt_id, company_id, project_id, state_json, "
                "completed_criteria_json, pending_criteria_json, side_effects_json, "
                "artifact_ids_json, context_digest, safe_to_resume, replay_risk, created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ckp.id,
                    ckp.run_id,
                    ckp.task_id,
                    ckp.attempt_id,
                    ckp.company_id,
                    ckp.project_id,
                    _j(ckp.state),
                    _j(ckp.completed_criteria),
                    _j(ckp.pending_criteria),
                    _j(ckp.side_effects),
                    _j(ckp.artifact_ids),
                    ckp.context_digest,
                    1 if ckp.safe_to_resume else 0,
                    ckp.replay_risk,
                    ckp.created_at,
                ),
            )
        return ckp

    def get_checkpoint(self, checkpoint_id: str) -> CheckpointPacket | None:
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM metacog_checkpoints WHERE id = ?", (checkpoint_id,)
                ).fetchone()
            )
        if row is None:
            return None
        return CheckpointPacket(
            id=str(row["id"]),
            run_id=row.get("run_id"),
            task_id=row.get("task_id"),
            attempt_id=row.get("attempt_id"),
            company_id=str(row.get("company_id") or "default"),
            project_id=row.get("project_id"),
            state=_loads(row.get("state_json"), {}),
            completed_criteria=list(_loads(row.get("completed_criteria_json"), [])),
            pending_criteria=list(_loads(row.get("pending_criteria_json"), [])),
            side_effects=list(_loads(row.get("side_effects_json"), [])),
            artifact_ids=list(_loads(row.get("artifact_ids_json"), [])),
            context_digest=row.get("context_digest"),
            safe_to_resume=bool(row.get("safe_to_resume")),
            replay_risk=str(row.get("replay_risk") or "none"),
            created_at=str(row.get("created_at") or ""),
        )

    def latest_checkpoint(self, task_id: str) -> CheckpointPacket | None:
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM metacog_checkpoints WHERE task_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
            )
        if row is None:
            return None
        return self.get_checkpoint(str(row["id"]))

    # --- memory ------------------------------------------------------------

    def upsert_memory(self, mem: MemoryRecord) -> MemoryRecord:
        # The insert/update branch is chosen from a read, so the read and the
        # write belong to the same transaction (M-40).
        with self._lock, atomic(self._connection, "metacog_upsert_memory"):
            now = utc_now_iso()
            mem.updated_at = now
            existing = self._connection.execute(
                "SELECT id FROM metacog_memory_records WHERE id = ?", (mem.id,)
            ).fetchone()
            if existing is None:
                mem.created_at = mem.created_at or now
                self._connection.execute(
                    "INSERT INTO metacog_memory_records ("
                    "id, type, statement, applicability_json, evidence_json, confidence, "
                    "sample_count, success_count, failure_count, promotion_status, "
                    "valid_from, expires_at, invalidated_by_json, supersedes_json, owner, "
                    "company_id, project_id, domain, helpfulness_score, created_at, updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        mem.id,
                        mem.type,
                        mem.statement,
                        _j(mem.applicability),
                        _j(mem.evidence),
                        mem.confidence,
                        mem.sample_count,
                        mem.success_count,
                        mem.failure_count,
                        mem.promotion_status,
                        mem.valid_from,
                        mem.expires_at,
                        _j(mem.invalidated_by),
                        _j(mem.supersedes),
                        mem.owner,
                        mem.company_id,
                        mem.project_id,
                        mem.domain,
                        mem.helpfulness_score,
                        mem.created_at,
                        mem.updated_at,
                    ),
                )
            else:
                self._connection.execute(
                    "UPDATE metacog_memory_records SET type=?, statement=?, "
                    "applicability_json=?, evidence_json=?, confidence=?, sample_count=?, "
                    "success_count=?, failure_count=?, promotion_status=?, valid_from=?, "
                    "expires_at=?, invalidated_by_json=?, supersedes_json=?, owner=?, "
                    "company_id=?, project_id=?, domain=?, helpfulness_score=?, updated_at=? "
                    "WHERE id=?",
                    (
                        mem.type,
                        mem.statement,
                        _j(mem.applicability),
                        _j(mem.evidence),
                        mem.confidence,
                        mem.sample_count,
                        mem.success_count,
                        mem.failure_count,
                        mem.promotion_status,
                        mem.valid_from,
                        mem.expires_at,
                        _j(mem.invalidated_by),
                        _j(mem.supersedes),
                        mem.owner,
                        mem.company_id,
                        mem.project_id,
                        mem.domain,
                        mem.helpfulness_score,
                        mem.updated_at,
                        mem.id,
                    ),
                )
        return mem

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM metacog_memory_records WHERE id = ?", (memory_id,)
                ).fetchone()
            )
        return None if row is None else self._memory_from_row(row)

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock, atomic(self._connection, "metacog_delete_memory"):
            cursor = self._connection.execute(
                "DELETE FROM metacog_memory_records WHERE id = ?", (memory_id,)
            )
            return cursor.rowcount > 0

    def list_memories(
        self,
        *,
        promotion_status: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        with self._lock:
            if promotion_status:
                rows = _rows(
                    self._connection.execute(
                        "SELECT * FROM metacog_memory_records WHERE promotion_status = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (promotion_status, limit),
                    ).fetchall()
                )
            else:
                rows = _rows(
                    self._connection.execute(
                        "SELECT * FROM metacog_memory_records ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                )
        return [self._memory_from_row(r) for r in rows]

    def search_memory(
        self,
        query: str,
        *,
        company_id: str = "default",
        project_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        statuses = statuses or ["promoted", "shadow"]
        placeholders = ",".join("?" * len(statuses))
        q = query.lower().strip()
        with self._lock:
            rows = _rows(
                self._connection.execute(
                    f"SELECT * FROM metacog_memory_records "
                    f"WHERE company_id = ? AND promotion_status IN ({placeholders}) "
                    f"ORDER BY confidence DESC, helpfulness_score DESC LIMIT ?",
                    (company_id, *statuses, max(limit * 4, 40)),
                ).fetchall()
            )
        scored: list[tuple[float, MemoryRecord]] = []
        for row in rows:
            mem = self._memory_from_row(row)
            if project_id and mem.project_id and mem.project_id != project_id:
                # Prefer project match but still allow domain/company.
                score_boost = 0.0
            else:
                score_boost = 0.15 if project_id and mem.project_id == project_id else 0.0
            # Invalidated or expired → down-rank hard
            if mem.invalidated_by:
                continue
            text = mem.statement.lower()
            if q and q not in text and not any(tok in text for tok in q.split() if len(tok) > 2):
                if score_boost == 0.0:
                    continue
            score = mem.confidence + mem.helpfulness_score + score_boost
            scored.append((score, mem))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [m for _, m in scored[:limit]]

    def record_memory_retrieval_event(
        self,
        *,
        memory_id: str,
        task_id: str | None,
        run_id: str | None,
        query: str,
        rank: int,
    ) -> str | None:
        """Record one selected memory injection, without creating orphan/duplicate rows.

        A run can assemble its prompt more than once while being retried.  Retrieval
        telemetry describes what actually reached that run's brief, so a memory is
        recorded at most once per run.  The ``INSERT .. SELECT`` makes the foreign-key
        invariant explicit even on a connection whose FK pragma was changed by a host.
        """
        event_id = new_id("mre")
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO metacog_memory_retrieval_events "
                "(id, memory_id, task_id, run_id, query, rank, selected, created_at) "
                "SELECT ?, ?, ?, ?, ?, ?, 1, ? "
                "WHERE EXISTS (SELECT 1 FROM metacog_memory_records WHERE id = ?) "
                "AND (? IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM metacog_memory_retrieval_events "
                "WHERE memory_id = ? AND run_id = ?"
                "))",
                (
                    event_id,
                    memory_id,
                    task_id,
                    run_id,
                    query,
                    rank,
                    utc_now_iso(),
                    memory_id,
                    run_id,
                    memory_id,
                    run_id,
                ),
            )
        return event_id if cursor.rowcount else None

    def _memory_from_row(self, row: dict[str, Any]) -> MemoryRecord:
        # Confidence is three-valued at the storage edge: missing/NULL → schema
        # default 0.5; a stored 0.0 is a measured zero and must not be inflated
        # by ``x or 0.5`` (which collapses 0.0 → 0.5 and lets promote_memory
        # treat zero-confidence candidates as mid-band).
        raw_confidence = row.get("confidence")
        if raw_confidence is None or raw_confidence == "":
            confidence = 0.5
        else:
            confidence = float(raw_confidence)
        return MemoryRecord(
            id=str(row["id"]),
            type=row["type"],  # type: ignore[arg-type]
            statement=str(row["statement"]),
            applicability=_loads(row.get("applicability_json"), {}),
            evidence=list(_loads(row.get("evidence_json"), [])),
            confidence=confidence,
            sample_count=int(row.get("sample_count") or 0),
            success_count=int(row.get("success_count") or 0),
            failure_count=int(row.get("failure_count") or 0),
            promotion_status=row.get("promotion_status") or "pending",  # type: ignore[arg-type]
            valid_from=row.get("valid_from"),
            expires_at=row.get("expires_at"),
            invalidated_by=_loads(row.get("invalidated_by_json"), {}),
            supersedes=list(_loads(row.get("supersedes_json"), [])),
            owner=str(row.get("owner") or "memory_curator"),
            company_id=str(row.get("company_id") or "default"),
            project_id=row.get("project_id"),
            domain=row.get("domain"),
            helpfulness_score=float(row.get("helpfulness_score") or 0.0),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )

    # --- metacog snapshots / decisions ------------------------------------

    def save_snapshot(
        self,
        state: MetacognitiveState,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        sid = new_id("mcs")
        with self._lock:
            self._connection.execute(
                "INSERT INTO metacog_snapshots "
                "(id, run_id, task_id, snapshot_json, next_control_action, "
                "reason_codes_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    sid,
                    run_id,
                    task_id,
                    _j(state.model_dump()),
                    state.next_control_action,
                    _j(state.reason_codes),
                    utc_now_iso(),
                ),
            )
        return sid

    def list_snapshots(self, task_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM metacog_snapshots WHERE task_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            )
        out = []
        for row in rows:
            snap = _loads(row.get("snapshot_json"), {})
            out.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "next_control_action": row["next_control_action"],
                    "state": snap,
                }
            )
        return out

    def record_decision(
        self,
        *,
        action: str,
        run_id: str | None = None,
        task_id: str | None = None,
        from_strategy: str | None = None,
        to_strategy: str | None = None,
        evidence: list[str] | None = None,
        reason_codes: list[str] | None = None,
        mode: str = "shadow",
        applied: bool = False,
    ) -> str:
        did = new_id("mcd")
        with self._lock:
            self._connection.execute(
                "INSERT INTO metacog_decision_records "
                "(id, run_id, task_id, action, from_strategy, to_strategy, "
                "evidence_json, reason_codes_json, mode, applied, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    did,
                    run_id,
                    task_id,
                    action,
                    from_strategy,
                    to_strategy,
                    _j(evidence or []),
                    _j(reason_codes or []),
                    mode,
                    1 if applied else 0,
                    utc_now_iso(),
                ),
            )
        return did

    # --- strategies --------------------------------------------------------

    def ensure_builtin_strategies(self, strategies: list[StrategyDef]) -> None:
        now = utc_now_iso()
        with self._lock, atomic(self._connection, "metacog_seed_strategies"):
            for s in strategies:
                row = self._connection.execute(
                    "SELECT id FROM metacog_strategies WHERE id = ?", (s.id,)
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        "INSERT INTO metacog_strategies "
                        "(id, title, description, topology, effort, max_fanout, "
                        "requires_verifier, eligible_domains_json, performance_json, "
                        "enabled, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            s.id,
                            s.title,
                            s.description,
                            s.topology,
                            s.effort,
                            s.max_fanout,
                            1 if s.requires_verifier else 0,
                            _j(s.eligible_domains),
                            _j(s.performance),
                            1 if s.enabled else 0,
                            now,
                            now,
                        ),
                    )

    def list_strategies(self, *, enabled_only: bool = True) -> list[StrategyDef]:
        with self._lock:
            if enabled_only:
                rows = _rows(
                    self._connection.execute(
                        "SELECT * FROM metacog_strategies WHERE enabled = 1 ORDER BY id"
                    ).fetchall()
                )
            else:
                rows = _rows(
                    self._connection.execute(
                        "SELECT * FROM metacog_strategies ORDER BY id"
                    ).fetchall()
                )
        return [
            StrategyDef(
                id=str(r["id"]),
                title=str(r["title"]),
                description=str(r.get("description") or ""),
                topology=str(r.get("topology") or "solo"),
                effort=str(r.get("effort") or "medium"),
                max_fanout=int(r.get("max_fanout") or 1),
                requires_verifier=bool(r.get("requires_verifier")),
                eligible_domains=list(_loads(r.get("eligible_domains_json"), ["coding"])),
                performance=_loads(r.get("performance_json"), {}),
                enabled=bool(r.get("enabled")),
            )
            for r in rows
        ]

    # --- reflection / skills ----------------------------------------------

    def save_reflection(self, report: ReflectionReport) -> ReflectionReport:
        with self._lock:
            self._connection.execute(
                "INSERT INTO metacog_reflection_reports "
                "(id, run_id, task_id, outcome, taxonomy, report_json, "
                "candidate_ids_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    report.id,
                    report.run_id,
                    report.task_id,
                    report.outcome,
                    report.taxonomy,
                    _j(report.report),
                    _j(report.candidate_ids),
                    report.created_at,
                ),
            )
        return report

    def save_skill_version(self, skill: SkillVersion) -> SkillVersion:
        with self._lock:
            self._connection.execute(
                "INSERT INTO metacog_skill_versions "
                "(id, skill_slug, version, body_md, status, source_memory_ids_json, "
                "fixtures_json, metrics_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    skill.id,
                    skill.skill_slug,
                    skill.version,
                    skill.body_md,
                    skill.status,
                    _j(skill.source_memory_ids),
                    _j(skill.fixtures),
                    _j(skill.metrics),
                    skill.created_at,
                    skill.updated_at,
                ),
            )
        return skill

    def list_skill_versions(self, skill_slug: str | None = None) -> list[SkillVersion]:
        with self._lock:
            if skill_slug:
                rows = _rows(
                    self._connection.execute(
                        "SELECT * FROM metacog_skill_versions WHERE skill_slug = ? "
                        "ORDER BY created_at DESC",
                        (skill_slug,),
                    ).fetchall()
                )
            else:
                rows = _rows(
                    self._connection.execute(
                        "SELECT * FROM metacog_skill_versions ORDER BY created_at DESC LIMIT 100"
                    ).fetchall()
                )
        return [
            SkillVersion(
                id=str(r["id"]),
                skill_slug=str(r["skill_slug"]),
                version=str(r["version"]),
                body_md=str(r["body_md"]),
                status=str(r.get("status") or "shadow"),
                source_memory_ids=list(_loads(r.get("source_memory_ids_json"), [])),
                fixtures=list(_loads(r.get("fixtures_json"), [])),
                metrics=_loads(r.get("metrics_json"), {}),
                created_at=str(r.get("created_at") or ""),
                updated_at=str(r.get("updated_at") or ""),
            )
            for r in rows
        ]
