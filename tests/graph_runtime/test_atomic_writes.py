"""M-40 — graph runtime multi-statement writes must be all-or-nothing.

The store's connection runs in autocommit mode, so ``Connection.commit()`` is a
no-op. Two compound writes were exposed:

- ``create_run`` writes a run header, its nodes and its edges. A mid-loop
  failure committed a durable ``running`` run with a partial graph, and a retry
  mints a fresh run id — so the half-built run stayed visible to schedulers and
  operators with no way to repair it.
- ``complete_node`` inserts one artifact per output port, then transitions the
  node and propagates readiness. Artifact ids are minted per call, so a failure
  after the inserts left orphan artifacts that a retry could not adopt: the
  retry minted new ids and the run accumulated duplicates for the same
  ``(node, port)`` while the node still sat in ``ready``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.graph_runtime.service import GraphRuntimeService


@pytest.fixture()
def svc(tmp_path: Path) -> GraphRuntimeService:
    return GraphRuntimeService(db_path=str(tmp_path / "graph.db"))


def _count(svc: GraphRuntimeService, table: str, where: str = "", args: tuple = ()) -> int:
    sql = f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608 - fixed literals below
    if where:
        sql += f" WHERE {where}"
    return int(svc.store._connection.execute(sql, args).fetchone()["n"])


def test_create_run_rolls_back_the_whole_graph(svc: GraphRuntimeService) -> None:
    """A failed edge insert must leave no run header and no nodes behind."""
    connection = svc.store._connection
    runs_before = _count(svc, "graph_runs")
    connection.execute(
        "CREATE TRIGGER block_edges BEFORE INSERT ON graph_edges "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    try:
        with pytest.raises(sqlite3.Error):
            svc.start_from_template("diamond-v1", title="atomic-run")
    finally:
        connection.execute("DROP TRIGGER block_edges")

    assert _count(svc, "graph_runs") == runs_before
    assert _count(svc, "graph_nodes") == 0
    assert _count(svc, "graph_edges") == 0
    # No orphan ``running`` run is left for a scheduler to pick up.
    assert [r for r in svc.list_runs() if r["title"] == "atomic-run"] == []

    run = svc.start_from_template("diamond-v1", title="atomic-run")
    assert run["status"] == "running"
    assert len(run["nodes"]) > 0


def test_complete_node_rolls_back_its_artifacts(svc: GraphRuntimeService) -> None:
    """A failed node transition must not leave the published artifacts behind."""
    run = svc.start_from_template("diamond-v1", title="atomic-complete")
    run_id = run["id"]
    node = svc.ready_nodes(run_id)[0]
    node_key = node["key"]
    port_name = node["output_ports"][0]["name"]
    outputs = {port_name: {"claim": "A"}}
    connection = svc.store._connection

    connection.execute(
        "CREATE TRIGGER block_node_update BEFORE UPDATE ON graph_nodes "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    try:
        with pytest.raises(sqlite3.Error):
            svc.complete_node(run_id, node_key, outputs=outputs)
    finally:
        connection.execute("DROP TRIGGER block_node_update")

    assert _count(svc, "graph_artifacts", "graph_run_id = ?", (run_id,)) == 0
    statuses = {n["key"]: n["status"] for n in svc.get_run(run_id)["nodes"]}
    assert statuses[node_key] in {"ready", "running"}

    # The retry publishes exactly one artifact per port — no duplicates from a
    # previous partial attempt.
    svc.complete_node(run_id, node_key, outputs=outputs)
    after = svc.get_run(run_id)
    assert {n["key"]: n["status"] for n in after["nodes"]}[node_key] == "completed"
    ports = svc.store._connection.execute(
        "SELECT port, COUNT(*) AS n FROM graph_artifacts "
        "WHERE graph_run_id = ? AND node_key = ? GROUP BY port",
        (run_id, node_key),
    ).fetchall()
    assert ports and all(int(r["n"]) == 1 for r in ports)
