from __future__ import annotations

import sqlite3
from pathlib import Path

from omniagentos.db.migrate import _migration_files, migrate


def test_migration_082_adds_verdict_provenance_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "lab.db"
    assert migrate(str(db_path)) >= 82

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]: row[2]
            for row in connection.execute("PRAGMA table_info(lab_verdict_provenance)").fetchall()
        }
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}

    assert {
        "panel_composition_json",
        "panel_lineage_count",
        "replicate_count",
        "effective_n",
        "agreement",
        "mde",
        "observed_effect",
        "invalidation_status",
        "blind_presentation_seed",
    } <= columns.keys()
    assert 82 in versions
    assert 75 not in versions


def test_migration_sequence_keeps_075_gap_and_unique_versions() -> None:
    migrations = _migration_files()
    versions = [version for version, _ in migrations]

    assert versions.count(82) == 1
    assert 75 not in versions
    assert len(versions) == len(set(versions))
