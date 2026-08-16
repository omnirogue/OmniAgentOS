"""Durable, queryable results store for benchmark captures.

Two projections, both append-only:

* ``var/benchmarks/results.jsonl`` — the raw record, one JSON object per
  fixture x arm x replicate. Survives any schema change here.
* ``var/benchmarks/baseline.db`` — a STANDALONE SQLite file with its own
  schema, created by this module.

Deliberately *not* the control-plane database. ``contracts/schema.sql`` is
frozen and migrations 055/056 belong to another workstream, so a benchmark
capture must not need a migration to be recorded. The columns mirror the names
the ``runs`` table already uses (arm, harness, harness_version, env_hash,
harness_params) so a later join or import is mechanical.

``scripts/benchmarks/runner.py`` can additionally write a real RunManifest
through ``omniagentos.wrapper.durable.finalize_manifest`` (``--append-ledger``),
which is off by default so a benchmark never pollutes the operator's live DB.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from omniagentos.contracts import utc_now_iso

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "var" / "benchmarks"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    capture_id         TEXT PRIMARY KEY,
    arm                TEXT NOT NULL,
    label              TEXT NOT NULL DEFAULT '',
    model              TEXT,
    repo_rev           TEXT NOT NULL DEFAULT '',
    env_hash           TEXT NOT NULL DEFAULT '',
    fixture_set_digest TEXT NOT NULL DEFAULT '',
    fixture_count      INTEGER NOT NULL DEFAULT 0,
    replicates         INTEGER NOT NULL DEFAULT 1,
    dry_run            INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    notes              TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bench_runs (
    run_id                     TEXT PRIMARY KEY,
    capture_id                 TEXT NOT NULL REFERENCES captures(capture_id),
    fixture_id                 TEXT NOT NULL,
    fixture_digest             TEXT NOT NULL DEFAULT '',
    fixture_kind               TEXT NOT NULL DEFAULT '',
    fixture_tier               TEXT NOT NULL DEFAULT '',
    fixture_repo_rev           TEXT NOT NULL DEFAULT '',
    replicate                  INTEGER NOT NULL DEFAULT 0,
    arm                        TEXT NOT NULL,
    harness                    TEXT NOT NULL DEFAULT '',
    harness_version            TEXT NOT NULL DEFAULT '',
    env_hash                   TEXT NOT NULL DEFAULT '',
    harness_params             TEXT NOT NULL DEFAULT '{}',
    model                      TEXT,
    success                    INTEGER NOT NULL DEFAULT 0,
    status                     TEXT NOT NULL DEFAULT '',
    error                      TEXT,
    wall_ms                    INTEGER,
    agent_wall_ms              INTEGER,
    acceptance_ms              INTEGER,
    turns                      INTEGER,
    input_tokens               INTEGER,
    output_tokens              INTEGER,
    total_tokens               INTEGER,
    cost_usd                   REAL,
    usage_estimated            INTEGER NOT NULL DEFAULT 1,
    usage_source               TEXT NOT NULL DEFAULT 'estimator',
    tool_calls                 INTEGER,
    tool_calls_observable      INTEGER NOT NULL DEFAULT 0,
    files_changed              INTEGER NOT NULL DEFAULT 0,
    undeclared_changes         INTEGER NOT NULL DEFAULT 0,
    undeclared_change_paths    TEXT NOT NULL DEFAULT '[]',
    undeclared_write_attempts  INTEGER NOT NULL DEFAULT 0,
    undeclared_read_attempts   INTEGER NOT NULL DEFAULT 0,
    outside_workspace_attempts INTEGER NOT NULL DEFAULT 0,
    canary_tripped             INTEGER NOT NULL DEFAULT 0,
    acceptance_rc              INTEGER,
    acceptance_tail            TEXT NOT NULL DEFAULT '',
    started_at                 TEXT,
    finished_at                TEXT,
    created_at                 TEXT NOT NULL,
    record_json                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bench_runs_capture ON bench_runs(capture_id);
CREATE INDEX IF NOT EXISTS idx_bench_runs_arm_fixture ON bench_runs(arm, fixture_id);

CREATE VIEW IF NOT EXISTS bench_capture_summary AS
SELECT
    c.capture_id,
    c.arm,
    c.label,
    c.model,
    c.fixture_set_digest,
    COUNT(r.run_id)                                        AS runs,
    SUM(r.success)                                         AS passed,
    ROUND(1.0 * SUM(r.success) / MAX(COUNT(r.run_id), 1), 4) AS pass_rate,
    ROUND(AVG(r.wall_ms) / 1000.0, 1)                      AS avg_wall_s,
    SUM(r.total_tokens)                                    AS total_tokens,
    ROUND(SUM(r.cost_usd), 4)                              AS total_cost_usd,
    SUM(r.tool_calls)                                      AS tool_calls,
    SUM(r.undeclared_changes)                              AS undeclared_changes,
    SUM(r.undeclared_write_attempts)                       AS undeclared_write_attempts,
    SUM(r.undeclared_read_attempts)                        AS undeclared_read_attempts,
    SUM(r.outside_workspace_attempts)                      AS outside_workspace_attempts,
    SUM(r.canary_tripped)                                  AS canary_trips
FROM captures c
LEFT JOIN bench_runs r ON r.capture_id = c.capture_id
GROUP BY c.capture_id;
"""


class BenchStore:
    """Standalone SQLite store for benchmark captures (schema owned here)."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> BenchStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start_capture(
        self,
        *,
        capture_id: str,
        arm: str,
        label: str = "",
        model: str | None = None,
        repo_rev: str = "",
        env_hash: str = "",
        fixture_set_digest: str = "",
        fixture_count: int = 0,
        replicates: int = 1,
        dry_run: bool = False,
        notes: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO captures (
                capture_id, arm, label, model, repo_rev, env_hash,
                fixture_set_digest, fixture_count, replicates, dry_run,
                started_at, finished_at, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?)
            """,
            (
                capture_id,
                arm,
                label,
                model,
                repo_rev,
                env_hash,
                fixture_set_digest,
                fixture_count,
                replicates,
                1 if dry_run else 0,
                utc_now_iso(),
                notes,
            ),
        )
        self.conn.commit()

    def finish_capture(self, capture_id: str) -> None:
        self.conn.execute(
            "UPDATE captures SET finished_at = ? WHERE capture_id = ?",
            (utc_now_iso(), capture_id),
        )
        self.conn.commit()

    def record_run(self, record: dict[str, Any]) -> None:
        """Persist one fixture x arm x replicate record (idempotent on run_id)."""
        usage = record.get("usage") or {}
        governance = record.get("governance") or {}
        access = governance.get("access") or {}
        harness = record.get("harness") or {}
        acceptance = record.get("acceptance") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = None
        if input_tokens is not None or output_tokens is not None:
            total_tokens = (input_tokens or 0) + (output_tokens or 0)

        self.conn.execute(
            """
            INSERT OR REPLACE INTO bench_runs (
                run_id, capture_id, fixture_id, fixture_digest, fixture_kind,
                fixture_tier, fixture_repo_rev, replicate, arm, harness,
                harness_version, env_hash, harness_params, model, success,
                status, error, wall_ms, agent_wall_ms, acceptance_ms, turns,
                input_tokens, output_tokens, total_tokens, cost_usd,
                usage_estimated, usage_source, tool_calls, tool_calls_observable,
                files_changed, undeclared_changes, undeclared_change_paths,
                undeclared_write_attempts, undeclared_read_attempts,
                outside_workspace_attempts, canary_tripped, acceptance_rc,
                acceptance_tail, started_at, finished_at, created_at, record_json
            ) VALUES (
                ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,
                ?,?,?,?, ?,?,?,?, ?,?,?, ?,?, ?,?,?, ?,?,?,?,?
            )
            """,
            (
                record["run_id"],
                record["capture_id"],
                record["fixture_id"],
                record.get("fixture_digest", ""),
                record.get("fixture_kind", ""),
                record.get("fixture_tier", ""),
                record.get("fixture_repo_rev", ""),
                int(record.get("replicate") or 0),
                record["arm"],
                harness.get("harness", ""),
                harness.get("version", ""),
                harness.get("env_hash", ""),
                json.dumps(harness.get("params") or {}, sort_keys=True),
                record.get("model"),
                1 if record.get("success") else 0,
                str(record.get("status") or ""),
                record.get("error"),
                record.get("wall_ms"),
                record.get("agent_wall_ms"),
                acceptance.get("duration_ms"),
                usage.get("turns"),
                input_tokens,
                output_tokens,
                total_tokens,
                usage.get("cost_usd"),
                1 if usage.get("estimated", True) else 0,
                str(usage.get("source") or "estimator"),
                access.get("tool_calls"),
                1 if access.get("observable") else 0,
                int(governance.get("files_changed") or 0),
                len(governance.get("undeclared_changes") or []),
                json.dumps(governance.get("undeclared_changes") or []),
                len(access.get("undeclared_write_attempts") or []),
                len(access.get("undeclared_read_attempts") or []),
                len(access.get("outside_workspace_attempts") or []),
                1 if governance.get("canary_tripped") else 0,
                acceptance.get("returncode"),
                str(acceptance.get("tail") or ""),
                record.get("started_at"),
                record.get("finished_at"),
                utc_now_iso(),
                json.dumps(record, sort_keys=True, default=str),
            ),
        )
        self.conn.commit()

    def capture_summary(self, capture_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM bench_capture_summary WHERE capture_id = ?", (capture_id,)
        ).fetchone()
        return dict(row) if row else None

    def per_fixture(self, capture_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT fixture_id, fixture_kind, fixture_tier,
                   COUNT(*) AS runs,
                   SUM(success) AS passed,
                   ROUND(AVG(wall_ms) / 1000.0, 1) AS avg_wall_s,
                   SUM(total_tokens) AS total_tokens,
                   ROUND(SUM(cost_usd), 4) AS cost_usd,
                   SUM(tool_calls) AS tool_calls,
                   SUM(undeclared_changes) AS undeclared_changes,
                   SUM(undeclared_read_attempts) AS undeclared_read_attempts,
                   SUM(canary_tripped) AS canary_trips
            FROM bench_runs WHERE capture_id = ?
            GROUP BY fixture_id ORDER BY fixture_id
            """,
            (capture_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_captures(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM captures ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def append_jsonl(path: str | Path, records: Sequence[dict[str, Any]]) -> Path:
    """Append records to the durable JSONL projection."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return target
