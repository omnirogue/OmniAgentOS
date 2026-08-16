"""Append-only schema growth for the external fleet.sqlite database."""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from omniagentos.db.busy import execute_write_transaction
from omniagentos.fleetcap.schema import EXTRACT_COLUMNS

COLUMNS = EXTRACT_COLUMNS


def default_db_path() -> Path:
    return Path(os.environ.get("FLEETCAP_DB", "~/.omniagentos/ops/telemetry/fleet.sqlite")).expanduser()


def migrate(connection: sqlite3.Connection) -> list[str]:
    connection.execute("PRAGMA busy_timeout=5000")
    present = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")}
    if not present:
        raise RuntimeError("fleetcap: sessions table does not exist")
    missing = [
        (name, sql_type) for name, sql_type in COLUMNS.items() if name not in present
    ]
    if not missing:
        return []

    # Busy-seam adoption (tests/db/test_busy_seam_adoption.py): the column adds
    # run as ONE busy-retried write transaction instead of a hand-rolled
    # commit(), so a locked telemetry DB retries instead of erroring.
    def _add_columns(conn: sqlite3.Connection) -> list[str]:
        added: list[str] = []
        for name, sql_type in missing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {sql_type}")
            added.append(name)
        return added

    return execute_write_transaction(connection, _add_columns, op="fleetcap_migrate")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db_path())
    args = parser.parse_args(argv)
    with sqlite3.connect(args.db) as connection:
        added = migrate(connection)
    print("fleetcap migrate: " + (", ".join(added) if added else "already current"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
