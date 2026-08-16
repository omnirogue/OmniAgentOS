"""L5 / B6: ETAR priors + formation_selections telemetry."""

from __future__ import annotations

from pathlib import Path

from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.formation.etar import compare_arms, compute_etar, latency_from_speed
from omniagentos.formation.telemetry import list_selections, record_selection


def test_latency_inverse_of_speed() -> None:
    fast = latency_from_speed(0.9)
    slow = latency_from_speed(0.3)
    assert fast < slow
    assert fast > 0


def test_formation_etar_lower_than_solo_on_fast_path() -> None:
    """Hypothesis under current priors: formation+fast impl beats opus solo on medium coding."""
    form = compute_etar(
        arm="formation",
        formation_id="coding",
        implementer="grok",
        reviewer="opus",
        mechanical_gate=True,
        difficulty="medium",
    )
    solo = compute_etar(
        arm="opus_solo",
        formation_id="coding",
        implementer="opus",
        reviewer="opus",
        mechanical_gate=False,
        difficulty="medium",
    )
    assert form.etar_s > 0 and solo.etar_s > 0
    cmp = compare_arms(form, solo)
    assert cmp["winner"] in {"formation", "opus_solo", "tie"}
    assert "formation_etar_s" in cmp


def test_mechanical_gate_reduces_p_repair() -> None:
    with_gate = compute_etar(
        arm="formation",
        formation_id="coding",
        implementer="grok",
        reviewer="opus",
        mechanical_gate=True,
    )
    no_gate = compute_etar(
        arm="formation",
        formation_id="coding",
        implementer="grok",
        reviewer="opus",
        mechanical_gate=False,
    )
    assert with_gate.p_repair < no_gate.p_repair


def test_record_and_list_selections(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    migrate(str(db))
    store = SqliteStore(str(db))
    conn = store._connection
    rid = record_selection(
        conn,
        task_id="t01",
        goal="Fix login bug",
        arm="formation",
        formation_id="coding",
        confidence=0.9,
        implementers=["grok", "gemini"],
        reviewer="opus",
        predicted_etar_s=120.5,
        etar_components={"etar_s": 120.5},
        source="calibration",
    )
    assert rid.startswith("fsel_")
    rows = list_selections(conn, source="calibration")
    assert len(rows) == 1
    assert rows[0]["formation_id"] == "coding"
    assert rows[0]["predicted_etar_s"] == 120.5
    assert rows[0].get("implementers") == ["grok", "gemini"]


def test_migration_065_creates_table(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    migrate(str(db))
    store = SqliteStore(str(db))
    row = store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='formation_selections'"
    ).fetchone()
    assert row is not None
