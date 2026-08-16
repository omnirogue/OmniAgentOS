"""T2 — review denial must change the coder MODEL, not just the attempt seq.

Entry point: ``POST /api/swarm?sync=1`` on the real ASGI app (the production
HTTP surface that plans, provisions, and — with ``OMNIAGENTOS_SWARM_EXECUTE=1``
— activates the run).

Mechanism observed WITHOUT importing it: rows in ``swarm_attempts`` for the
task show that after a denial the next attempt's ``model`` differs from the
previous one, and more than one distinct model was used overall. We never
import ``_escalate``, ``run_cascade``, or the router.

Outside-only fakes:
  * planner/clarify/recall FastAPI deps (deterministic single-task plan)
  * provider CLI binaries on PATH (scripted REVIEW-DENIED)

Defect: O-6 — ``configs/cascade.yaml`` declares fable above sol, but the live
scheduler ladder is ``TIER_LADDER = (simple, standard, complex)`` which clamps
at complex; once the first coder is already sol/complex, every further
escalation keeps the same model while ``seq`` increments. Marked
``xfail(strict=True)`` so an XPASS announces the cascade ladder is wired.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.deps import get_store
from omniagentos.api.routes import swarm as swarm_routes
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.store import CollabStore
from omniagentos.swarm.dal import SwarmDal

_DEADLINE_SECONDS = 45.0  # fake CLIs are instant; bound for scheduler settle
_TERMINAL_RUN = frozenset({"completed", "failed", "cancelled", "killed"})


def _fake_planner_llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
    """Single complex-tier task so the live ladder starts at its top rung (sol)."""
    del prompt, schema, effort
    return {
        "goal": "entrypoint escalation probe",
        "assumptions": [],
        "tasks": [
            {
                "id": "escalate_probe",
                "title": "Escalation probe",
                "description": "A task that must escalate past sol after review denials.",
                "depends_on": [],
                "owned_paths": ["notes/probe.txt"],
                "complexity": "complex",
                "tier_hint": "complex",
                "est_agent_minutes": 5,
                "est_manual_minutes": 10,
                "acceptance": "model must change after a denial",
                # A valid strict verifier targeting an absent test fails closed,
                # so settle records review_denied and
                # the mechanical/escalation ladder advances (same end_reason the
                # LLM deny path writes). Empty fake-CLI worker output is a second
                # independent produced_nothing deny if verify is skipped by
                # formation_mechanical_gate=false.
                "verify_command": "pytest tests/absent_escalation_probe.py",
            }
        ],
        "suite_command": "pytest tests/absent_escalation_probe.py",
    }


def _fake_clarify_llm(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    del prompt, schema
    return {
        "mode": "spec",
        "spec": {
            "title": "Escalation probe",
            "description": "Ship the probe.",
            "acceptance_criteria": [],
        },
    }


def _fake_recall_fn(goal: str) -> str:
    del goal
    return ""


def _init_git_workspace(workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "notes").mkdir(parents=True, exist_ok=True)
    (workdir / "notes" / "probe.txt").write_text("seed\n", encoding="utf-8")
    (workdir / "README.md").write_text("entrypoint probe workspace\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "ep", "GIT_AUTHOR_EMAIL": "ep@test"}
    subprocess.run(["git", "init"], cwd=workdir, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=ep@test", "-c", "user.name=ep", "add", "-A"],
        cwd=workdir,
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-c", "user.email=ep@test", "-c", "user.name=ep", "commit", "-m", "seed"],
        cwd=workdir,
        check=True,
        capture_output=True,
        env=env,
    )


def _list_attempts(db_path: str, run_id: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT a.seq, a.model, a.provider, a.tier, a.end_reason,
                   a.ended_at, a.board_task_id, a.started_at
            FROM swarm_attempts a
            JOIN board_tasks t ON t.id = a.board_task_id
            WHERE a.swarm_run_id = ?
              AND COALESCE(json_extract(t.swarm_json, '$.task_key'), '') != 'integration'
            ORDER BY a.seq ASC, a.started_at ASC
            """,
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def _run_status(db_path: str, run_id: str) -> str | None:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT status FROM swarm_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return str(row[0]) if row else None
    finally:
        connection.close()


def _coder_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attempts that finished (or are open) as coder turns — drop pure infra noise if needed."""
    return attempts


@pytest.mark.xfail(
    strict=True,
    reason=(
        "O-6: cascade.yaml declares fable above sol, but the live scheduler "
        "TIER_LADDER clamps at complex and never calls run_cascade; a sol-first "
        "task burns repeated same-model attempts. XPASS means escalation changes "
        "the coder model on the production HTTP path."
    ),
)
def test_swarm_route_escalation_changes_coder_model(
    campaign_root: Path,
    auth_headers: dict[str, str],
    fake_cli_path: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/swarm?sync=1 must escalate the coder model after review denials.

    (a) Entry point: real ``omniagentos.api:app`` via TestClient, ``POST /api/swarm?sync=1``.
    (b) Mechanism observed without importing it: ``swarm_attempts.model`` differs
        across seq after denials, and >1 distinct models appear. Fake CLI log
        proves no real provider binary ran.
    """
    _bin_dir, log_path = fake_cli_path
    monkeypatch.setenv("OMNIAGENTOS_SWARM_EXECUTE", "1")

    db_path = str(campaign_root / "state.sqlite3")
    # Workspace under OMNIAGENTOS_VAR_DIR so F-015 floor accepts it without
    # monkeypatching board_files (floor honors that env knob).
    workdir = Path(os.environ["OMNIAGENTOS_VAR_DIR"]) / "workspaces" / "escalate-probe"
    _init_git_workspace(workdir)

    collab = CollabStore(db_path)
    store = collab._store
    dal = SwarmDal(db_path)

    from omniagentos.api import app as production_app

    production_app.dependency_overrides[get_store] = lambda: store
    production_app.dependency_overrides[get_collab_store] = lambda: collab
    production_app.dependency_overrides[swarm_routes.get_swarm_dal] = lambda: dal
    production_app.dependency_overrides[swarm_routes.get_swarm_planner_llm] = (
        lambda: _fake_planner_llm
    )
    production_app.dependency_overrides[swarm_routes.get_swarm_clarify_llm] = (
        lambda: _fake_clarify_llm
    )
    production_app.dependency_overrides[swarm_routes.get_swarm_recall_fn] = lambda: _fake_recall_fn

    try:
        with TestClient(production_app) as client:
            response = client.post(
                "/api/swarm?sync=1",
                headers=auth_headers,
                json={
                    "brief": "entrypoint escalation probe: force review denials",
                    "working_dir": str(workdir.resolve()),
                    "budget_usd_max": 5.0,
                },
            )
            assert response.status_code == 202, response.text
            body = response.json()
            run_id = body.get("swarm_run_id")
            assert run_id, body

            deadline = time.monotonic() + _DEADLINE_SECONDS
            attempts: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                attempts = _list_attempts(db_path, str(run_id))
                status = _run_status(db_path, str(run_id))
                finished = [
                    a
                    for a in attempts
                    if a.get("end_reason") or a.get("ended_at")
                ]
                # Need at least two coder attempts to compare models across a denial.
                if len(finished) >= 2 and status in _TERMINAL_RUN:
                    break
                if len(finished) >= 3:
                    # Enough denials recorded even if run not fully terminal yet.
                    break
                time.sleep(0.1)

            # Real CLIs must never have launched — only our PATH fakes.
            assert log_path.is_file(), "fake CLI log missing — no provider binary was invoked"
            log_lines = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert log_lines, "fake CLI was never invoked"
            # At least one coder CLI invocation (provider_exec / bridge spawn).
            assert any(not entry.get("review") for entry in log_lines), log_lines

            coder = _coder_attempts(attempts)
            assert len(coder) >= 2, (
                f"expected >=2 attempts after denials, got {coder!r}; "
                f"run_status={_run_status(db_path, str(run_id))!r}; "
                f"cli_log={log_lines!r}"
            )
            # Prefer the production end_reason the deny/escalation path writes.
            denied = [
                a
                for a in coder
                if str(a.get("end_reason") or "") in {"review_denied", "crashed", "blocked"}
            ]
            assert denied, f"expected denial/failure attempts, got {coder!r}"

            models = [str(a.get("model") or "") for a in coder]
            # After a denial, seq N+1 model must differ from seq N.
            model_changed = any(
                models[i] and models[i + 1] and models[i] != models[i + 1]
                for i in range(len(models) - 1)
            )
            distinct = {m for m in models if m}
            assert model_changed, (
                f"escalation did not change the coder model across attempts: {models!r} "
                f"(full attempts={coder!r}; cli_log={log_lines!r})"
            )
            assert len(distinct) > 1, (
                f"expected >1 distinct models after denials, got {distinct!r} from {models!r}"
            )
    finally:
        production_app.dependency_overrides.clear()
        dal.close()
