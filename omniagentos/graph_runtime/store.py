"""SQLite store for graph templates, runs, nodes, edges, artifacts."""

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
from omniagentos.graph_runtime.contracts import BUILTIN_TEMPLATES, GraphTemplate


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class GraphStore:
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

    def seed_templates(self) -> int:
        now = utc_now_iso()
        n = 0
        with self._lock, atomic(self._connection, "graph_seed_templates"):
            for t in BUILTIN_TEMPLATES:
                exists = self._connection.execute(
                    "SELECT id FROM graph_templates WHERE slug = ?", (t.slug,)
                ).fetchone()
                if exists is None:
                    self._connection.execute(
                        "INSERT INTO graph_templates "
                        "(id, slug, version, title, description, body_json, enabled, "
                        "created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
                        (
                            new_id("gtp"),
                            t.slug,
                            t.version,
                            t.title,
                            t.description,
                            _j(t.model_dump()),
                            now,
                            now,
                        ),
                    )
                    n += 1
        return n

    def get_template(self, slug: str) -> GraphTemplate | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT body_json FROM graph_templates WHERE slug = ? AND enabled = 1",
                (slug,),
            ).fetchone()
        if row is None:
            return None
        body = _loads(row["body_json"] if isinstance(row, sqlite3.Row) else row[0], {})
        return GraphTemplate.model_validate(body)

    def list_templates(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = _rows(
                self._connection.execute(
                    "SELECT id, slug, version, title, description, enabled, created_at "
                    "FROM graph_templates ORDER BY slug"
                ).fetchall()
            )
        return rows

    def create_run(
        self,
        *,
        title: str,
        template: GraphTemplate,
        completeness_policy: str | None = None,
        company_id: str | None = None,
        project_id: str | None = None,
        swarm_run_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        run_id = new_id("grn")
        policy = completeness_policy or template.completeness_policy

        from omniagentos.graph_runtime.contracts import EdgeSpec

        if not template.edges:
            edges_spec = [
                EdgeSpec(from_node=dep, to_node=node.key)
                for node in template.nodes
                for dep in node.depends_on
            ]
        else:
            edges_spec = list(template.edges)

        # M-40: run header, nodes and edges are one transaction. Under
        # autocommit a mid-loop failure left a durable ``running`` run with a
        # partial node/edge set, and a retry allocates a fresh run id — so the
        # half-built run stayed visible to schedulers and operators forever.
        with self._lock, atomic(self._connection, "graph_create_run"):
            self._connection.execute(
                "INSERT INTO graph_runs "
                "(id, template_id, template_slug, title, status, completeness_policy, "
                "plan_json, critical_path_json, cost_usd, swarm_run_id, company_id, "
                "project_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
                (
                    run_id,
                    None,
                    template.slug,
                    title,
                    "running",
                    policy,
                    _j(template.model_dump()),
                    _j([n.key for n in template.nodes]),
                    swarm_run_id,
                    company_id,
                    project_id,
                    now,
                    now,
                ),
            )
            for node in template.nodes:
                # Ready if no deps
                deps = [e.from_node for e in edges_spec if e.to_node == node.key]
                status = "ready" if not deps else "pending"
                self._connection.execute(
                    "INSERT INTO graph_nodes "
                    "(id, graph_run_id, key, node_type, title, status, "
                    "input_ports_json, output_ports_json, input_bindings_json, "
                    "output_artifacts_json, resource_claims_json, verify_policy_json, "
                    "model_role, effort, cost_usd, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                    (
                        new_id("gnd"),
                        run_id,
                        node.key,
                        node.node_type,
                        node.title,
                        status,
                        _j([p.model_dump() for p in node.inputs]),
                        _j([p.model_dump() for p in node.outputs]),
                        _j({}),
                        _j({}),
                        _j(node.resource_claims),
                        _j(node.verify_policy),
                        node.model_role,
                        node.effort,
                        now,
                        now,
                    ),
                )
            for edge in edges_spec:
                self._connection.execute(
                    "INSERT INTO graph_edges "
                    "(id, graph_run_id, from_node_key, to_node_key, from_port, to_port, "
                    "artifact_type, required, edge_kind, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id("ged"),
                        run_id,
                        edge.from_node,
                        edge.to_node,
                        edge.from_port,
                        edge.to_port,
                        edge.artifact_type,
                        1 if edge.required else 0,
                        edge.edge_kind,
                        now,
                    ),
                )
        return self.get_run(run_id) or {"id": run_id}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM graph_runs WHERE id = ?", (run_id,)
                ).fetchone()
            )
            if row is None:
                return None
            nodes = _rows(
                self._connection.execute(
                    "SELECT * FROM graph_nodes WHERE graph_run_id = ? ORDER BY created_at",
                    (run_id,),
                ).fetchall()
            )
            edges = _rows(
                self._connection.execute(
                    "SELECT * FROM graph_edges WHERE graph_run_id = ?",
                    (run_id,),
                ).fetchall()
            )
            arts = _rows(
                self._connection.execute(
                    "SELECT * FROM graph_artifacts WHERE graph_run_id = ?",
                    (run_id,),
                ).fetchall()
            )
        for n in nodes:
            n["input_ports"] = _loads(n.pop("input_ports_json", "[]"), [])
            n["output_ports"] = _loads(n.pop("output_ports_json", "[]"), [])
            n["input_bindings"] = _loads(n.pop("input_bindings_json", "{}"), {})
            n["output_artifacts"] = _loads(n.pop("output_artifacts_json", "{}"), {})
            n["resource_claims"] = _loads(n.pop("resource_claims_json", "[]"), [])
            n["verify_policy"] = _loads(n.pop("verify_policy_json", "{}"), {})
        row["nodes"] = nodes
        row["edges"] = edges
        row["artifacts"] = arts
        row["plan"] = _loads(row.pop("plan_json", "{}"), {})
        row["critical_path"] = _loads(row.pop("critical_path_json", "[]"), [])
        return row

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return _rows(
                self._connection.execute(
                    "SELECT id, title, status, template_slug, cost_usd, created_at, updated_at "
                    "FROM graph_runs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            )

    def update_node(
        self,
        run_id: str,
        node_key: str,
        *,
        status: str | None = None,
        input_bindings: dict[str, str] | None = None,
        output_artifacts: dict[str, str] | None = None,
        error: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        now = utc_now_iso()
        with self._lock, atomic(self._connection, "graph_update_node"):
            row = self._connection.execute(
                "SELECT * FROM graph_nodes WHERE graph_run_id = ? AND key = ?",
                (run_id, node_key),
            ).fetchone()
            if row is None:
                raise KeyError(f"node {node_key} not in run {run_id}")
            fields: dict[str, Any] = {"updated_at": now}
            if status is not None:
                fields["status"] = status
            if input_bindings is not None:
                fields["input_bindings_json"] = _j(input_bindings)
            if output_artifacts is not None:
                fields["output_artifacts_json"] = _j(output_artifacts)
            if error is not None:
                fields["error"] = error
            if cost_usd is not None:
                fields["cost_usd"] = cost_usd
            sets = ", ".join(f"{k} = ?" for k in fields)
            self._connection.execute(
                f"UPDATE graph_nodes SET {sets} WHERE graph_run_id = ? AND key = ?",
                (*fields.values(), run_id, node_key),
            )

    def transition_node(
        self,
        run_id: str,
        node_key: str,
        *,
        to_status: str,
        expect: tuple[str, ...] = ("ready", "running"),
        error: str | None = None,
    ) -> bool:
        """Compare-and-swap a node's status; False when it was no longer ``expect``.

        FH-001: a status a caller read is stale the moment it leaves the store,
        so two racers could both see ``ready`` and both publish outputs for the
        same node. The guard makes the UPDATE itself the decision point and the
        rowcount tells the caller whether it owns the transition.
        """
        now = utc_now_iso()
        fields: dict[str, Any] = {"status": to_status, "updated_at": now}
        if error is not None:
            fields["error"] = error
        sets = ", ".join(f"{k} = ?" for k in fields)
        guard = ", ".join("?" for _ in expect)
        with self._lock, atomic(self._connection, "graph_transition_node"):
            cursor = self._connection.execute(
                f"UPDATE graph_nodes SET {sets} "
                f"WHERE graph_run_id = ? AND key = ? AND status IN ({guard})",
                (*fields.values(), run_id, node_key, *expect),
            )
            return cursor.rowcount > 0

    def insert_artifact(self, art: dict[str, Any]) -> str:
        art_id = art.get("id") or new_id("gar")
        with self._lock, atomic(self._connection, "graph_insert_artifact"):
            self._connection.execute(
                "INSERT INTO graph_artifacts "
                "(id, graph_run_id, node_key, port, artifact_type, content_hash, "
                "content_uri, schema_ok, verified, metacog_art_id, payload_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    art_id,
                    art["graph_run_id"],
                    art["node_key"],
                    art["port"],
                    art["artifact_type"],
                    art["content_hash"],
                    art["content_uri"],
                    1 if art.get("schema_ok", True) else 0,
                    1 if art.get("verified") else 0,
                    art.get("metacog_art_id"),
                    _j(art.get("payload") or {}),
                    art.get("created_at") or utc_now_iso(),
                ),
            )
        return str(art_id)

    def set_run_status(self, run_id: str, status: str, *, cost_usd: float | None = None) -> None:
        now = utc_now_iso()
        with self._lock, atomic(self._connection, "graph_set_run_status"):
            if cost_usd is not None:
                self._connection.execute(
                    "UPDATE graph_runs SET status = ?, cost_usd = ?, updated_at = ?, "
                    "completed_at = CASE WHEN ? IN ('completed','failed','cancelled','partial') "
                    "THEN ? ELSE completed_at END WHERE id = ?",
                    (status, cost_usd, now, status, now, run_id),
                )
            else:
                self._connection.execute(
                    "UPDATE graph_runs SET status = ?, updated_at = ?, "
                    "completed_at = CASE WHEN ? IN ('completed','failed','cancelled','partial') "
                    "THEN ? ELSE completed_at END WHERE id = ?",
                    (status, now, status, now, run_id),
                )

    def list_edges(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return _rows(
                self._connection.execute(
                    "SELECT * FROM graph_edges WHERE graph_run_id = ?", (run_id,)
                ).fetchall()
            )

    def list_nodes(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM graph_nodes WHERE graph_run_id = ?", (run_id,)
                ).fetchall()
            )
        for n in rows:
            n["input_bindings"] = _loads(n.get("input_bindings_json"), {})
            n["output_artifacts"] = _loads(n.get("output_artifacts_json"), {})
        return rows
