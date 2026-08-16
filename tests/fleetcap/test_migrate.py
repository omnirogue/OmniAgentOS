import sqlite3

from omniagentos.fleetcap.migrate import COLUMNS, migrate


def test_migrate_is_idempotent(real_connection: sqlite3.Connection) -> None:
    connection = real_connection
    before = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    assert migrate(connection) == [name for name in COLUMNS if name not in before]
    assert migrate(connection) == []
    present = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    assert set(COLUMNS) <= present


def test_migrate_covers_every_extract_key(real_connection: sqlite3.Connection) -> None:
    migrate(real_connection)
    present = {row[1] for row in real_connection.execute("PRAGMA table_info(sessions)")}
    assert set(COLUMNS) <= present
