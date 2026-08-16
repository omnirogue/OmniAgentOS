"""B6: Test finished_at schema addition and outcome-telemetry updates on formation_selections."""

from __future__ import annotations

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.formation.telemetry import record_selection


def test_finished_at_column_exists() -> None:
    """Assert that the finished_at column now exists in the formation_selections table."""
    store = SqliteStore(":memory:")
    cursor = store._connection.execute("PRAGMA table_info(formation_selections)")
    columns = [row["name"] for row in cursor.fetchall()]
    assert "finished_at" in columns


def test_outcome_update_round_trip() -> None:
    """Verify that we can update outcome and finished_at for a seeded selection without error."""
    store = SqliteStore(":memory:")
    conn = store._connection

    # Seed first row for accepted outcome update
    row_id_1 = record_selection(
        conn,
        task_id="task_001",
        goal="Verify outcome telemetry update works",
        arm="formation",
        formation_id="coding",
        outcome="predicted",
        source="production",
    )

    # Seed second row for cancelled outcome update
    row_id_2 = record_selection(
        conn,
        task_id="task_002",
        goal="Verify outcome telemetry cancel works",
        arm="formation",
        formation_id="coding",
        outcome="predicted",
        source="production",
    )

    # Execute the UPDATE statement the route runs for accepted
    finished_at_1 = utc_now_iso()
    conn.execute(
        "UPDATE formation_selections SET outcome = ?, finished_at = ? WHERE id = ?",
        ("accepted", finished_at_1, row_id_1),
    )

    # Execute the UPDATE statement the route runs for cancelled
    finished_at_2 = utc_now_iso()
    conn.execute(
        "UPDATE formation_selections SET outcome = 'cancelled', finished_at = ? WHERE id = ?",
        (finished_at_2, row_id_2),
    )

    # SELECT the first row back and assert
    row_1 = conn.execute(
        "SELECT outcome, finished_at FROM formation_selections WHERE id = ?", (row_id_1,)
    ).fetchone()
    assert row_1 is not None
    assert row_1["outcome"] == "accepted"
    assert row_1["finished_at"] == finished_at_1

    # SELECT the second row back and assert
    row_2 = conn.execute(
        "SELECT outcome, finished_at FROM formation_selections WHERE id = ?", (row_id_2,)
    ).fetchone()
    assert row_2 is not None
    assert row_2["outcome"] == "cancelled"
    assert row_2["finished_at"] == finished_at_2
