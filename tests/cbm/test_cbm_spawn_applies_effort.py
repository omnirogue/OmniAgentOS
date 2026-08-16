"""CBM allocation is read back into the spawn execution envelope (Phase 1.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.swarm.scheduler import SpawnRequest
from omniagentos.swarm.spawn import UnifiedSpawner


class _Supervisor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "ses_claude_cbm_1"


class _Runner:
    def spawn(self, **kwargs: Any) -> str:
        return "ses_codex_1"


class _SwarmDal:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.swarm_jsons: dict[str, dict[str, Any]] = {}

    def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.tasks.values())

    def get_swarm_json(self, task_id: str) -> dict[str, Any] | None:
        return self.swarm_jsons.get(task_id)

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        return []


class _SessionsDal:
    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return None

    def set_idle_minutes(self, session_id: str, idle_minutes: float | None) -> bool:
        return True


def test_cbm_rung_influences_spawn_effort(tmp_path: Path) -> None:
    db = str(tmp_path / "cbm-spawn.db")
    dal = _SwarmDal()
    task_id = "task_high"
    dal.tasks[task_id] = {
        "id": task_id,
        "title": "Hard novel work",
        "description": "architecture redesign",
        "discipline": "coding",
        "priority": "high",
    }
    # High novelty + irreversible forces higher start rung → higher effort.
    dal.swarm_jsons[task_id] = {
        "task_key": "hard",
        "risk_class": "irreversible",
        "novelty": "high",
        "difficulty": "high",
        "acceptance": "ok",
    }
    supervisor = _Supervisor()
    spawner = UnifiedSpawner(
        supervisor=supervisor,
        provider_runner=_Runner(),
        swarm_dal=dal,
        sessions_dal=_SessionsDal(),
        convert_reservation=lambda r, s: True,
        release_reservation=lambda r: True,
        var_root=tmp_path / "var",
        db_path=db,
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    req = SpawnRequest(
        run_id="swr_cbm",
        task_id=task_id,
        task_key="hard",
        attempt_id="swa1",
        working_dir=str(ws),
        prompt="do hard thing",
        provider="claude",
        model="sonnet",
        tier="standard",
        account_id="acct",
        idle_minutes=20.0,
        budget_usd_max=5.0,
        reservation_id=None,
        effort=None,  # unset so CBM can apply
    )
    sid = spawner.spawn(req)
    assert sid.startswith("ses_")
    # Allocation row exists and is non-trivial rung
    cbm = CognitiveBudgetService(database=db)
    rows = cbm._connection.execute(
        "SELECT rung, reasoning_effort FROM cbm_allocations WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    assert len(rows) >= 1
    assert int(rows[0]["rung"]) >= 2
    # Claude spawn received a prompt that embeds the allocation
    assert supervisor.calls
    prompt = str(supervisor.calls[0].get("prompt") or "")
    assert "[cbm allocation" in prompt
    assert "rung=" in prompt
