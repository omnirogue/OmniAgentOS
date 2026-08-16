"""Team fleet board — leaders, live agents, spawn events (no token stream)."""

from __future__ import annotations

from pathlib import Path

from omniagentos.api.routes.swarm import _build_team, _parse_formation_from_plan
from omniagentos.db.store import SqliteStore
from omniagentos.swarm.dal import SwarmDal
from tests.support.db_template import make_store


def test_parse_formation_from_assumptions() -> None:
    plan = {
        "assumptions": [
            "formation: coding implementers=grok,gemini reviewer=opus mechanical=True",
            "allocation: topology=map_reduce",
        ]
    }
    formation = _parse_formation_from_plan(plan)
    assert formation is not None
    assert formation["id"] == "coding"
    assert formation["implementers"] == ["grok", "gemini"]
    assert formation["reviewer"] == "opus"


def test_build_team_empty_fleet(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = make_store(SqliteStore, str(db))
    dal = SwarmDal(str(db))
    team = _build_team(dal, store)
    assert team["running_agents"] == 0
    assert team["active_swarms"] == 0
    assert team["leaders"] == []
    assert team["workers"] == []
    assert "generated_at" in team
