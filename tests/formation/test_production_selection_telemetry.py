"""B6 production writer: provision_run records formation_selections."""

from __future__ import annotations

from pathlib import Path

from omniagentos.db.migrate import migrate
from omniagentos.formation.telemetry import list_selections
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.planner import build_plan, provision_run


def test_provision_records_production_selection(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    migrate(str(db))
    work = tmp_path / "ws"
    work.mkdir()
    (work / "outputs" / "deliverables").mkdir(parents=True)
    (work / "outputs" / "deliverables" / "out.txt").write_text("x\n", encoding="utf-8")
    verifier = work / "tests" / "test_output.py"
    verifier.parent.mkdir()
    verifier.write_text(
        "from pathlib import Path\n\n\n"
        "def test_output_exists() -> None:\n"
        "    assert Path('outputs/deliverables/out.txt').is_file()\n",
        encoding="utf-8",
    )

    plan = build_plan(
        "Fix the login bug in auth adapter",
        [
            {
                "id": "main",
                "title": "Main",
                "description": "Fix the login bug",
                "depends_on": [],
                "owned_paths": ["outputs/deliverables/out.txt"],
                "est_agent_minutes": 10,
                "est_manual_minutes": 30,
                "acceptance": "out present",
                "verify_command": "pytest -q tests/test_output.py",
            }
        ],
    )
    assert plan.formation is not None
    assert plan.formation.id == "coding"

    dal = SwarmDal(str(db))
    result = provision_run(plan, dal=dal, working_dir=str(work), write_plan_doc=False)
    assert result.get("run")

    rows = list_selections(dal._connection, source="production")
    assert len(rows) >= 1
    row = rows[0]
    assert row["formation_id"] == "coding"
    assert row["arm"] == "formation"
    assert row["outcome"] == "predicted"
    assert row["source"] == "production"
