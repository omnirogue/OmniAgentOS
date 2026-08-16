"""Migrations 060–063 apply cleanly and create expected tables."""

from __future__ import annotations

from pathlib import Path

from omniagentos.db.migrate import migrate


def test_migrations_through_063(tmp_path: Path) -> None:
    db = str(tmp_path / "m.db")
    version = migrate(db)
    assert version >= 63
    import sqlite3

    con = sqlite3.connect(db)
    tables = {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for must in (
        "metacog_artifacts",
        "metacog_checkpoints",
        "metacog_memory_records",
        "metacog_snapshots",
        "org_companies",
        "org_products",
        "org_classifications",
        "org_agent_profiles",
        "graph_templates",
        "graph_runs",
        "graph_nodes",
        "graph_edges",
        "graph_artifacts",
        "cbm_allocations",
        "cbm_escalations",
        "cbm_outcomes",
        "cbm_role_stats",
    ):
        assert must in tables, f"missing {must}; have {sorted(tables)}"
    con.close()
