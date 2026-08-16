"""M-14 OrgDims visibility + account outcome honesty; M-15 Decision Center contract."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.routing.account_pool import Outcome, classify_outcome
from omniagentos.routing.decision_center import decision_center_contract
from omniagentos.swarm.scheduler import SpawnRequest
from omniagentos.swarm.spawn import UnifiedSpawner

_USAGE = AgentUsage(wall_ms=1)


def test_m14_classification_error_text_is_not_account_success() -> None:
    """Classification-related adapter errors classify as ERROR, never OK."""
    res = AgentResult(
        status=ResultStatus.ERROR,
        output_text="",
        error="classification_failure occurred",
        usage=_USAGE,
    )
    assert classify_outcome(res) is Outcome.ERROR
    assert classify_outcome(res) is not Outcome.OK

    res2 = AgentResult(
        status=ResultStatus.ERROR,
        output_text="",
        error="unclassified task",
        usage=_USAGE,
    )
    assert classify_outcome(res2) is Outcome.ERROR


def test_m14_no_dead_classification_failure_outcome() -> None:
    """Outcome has no dead CLASSIFICATION_FAILURE member (M-14)."""
    assert not hasattr(Outcome, "CLASSIFICATION_FAILURE")
    assert {o.value for o in Outcome} == {"ok", "rate_limited", "error"}


class _Supervisor:
    def spawn(self, **kwargs: Any) -> str:
        return "ses_claude_1"


class _Runner:
    def spawn(self, **kwargs: Any) -> str:
        return f"ses_{kwargs.get('provider')}_1"


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


def test_m14_spawn_warns_on_orgdims_low_confidence_or_unclassified(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Real spawn path emits WARNING for low-conf/unclassified OrgDims results."""
    db = str(tmp_path / "spawn.db")
    dal = _SwarmDal()
    task_id = "task_noise"
    # Gibberish title/description → low confidence / weak classification
    dal.tasks[task_id] = {
        "id": task_id,
        "title": "asdf qwerty zxcv",
        "description": "zzz qqq mmm",
        "discipline": None,
        "priority": "normal",
    }
    dal.swarm_jsons[task_id] = {
        "task_key": "noise",
        "risk_class": "none",
        "acceptance": "ok",
        "novelty": "low",
        "difficulty": "medium",
    }
    spawner = UnifiedSpawner(
        supervisor=_Supervisor(),
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

    with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.spawn"):
        sid = spawner.spawn(
            SpawnRequest(
                run_id="swr1",
                task_id=task_id,
                task_key="noise",
                attempt_id="swa1",
                working_dir=str(ws),
                prompt="do it",
                provider="codex",
                model="gpt-5.6-sol",
                tier="standard",
                account_id="acct_codex",
                idle_minutes=20.0,
                budget_usd_max=5.0,
                reservation_id="rsv1",
                effort="medium",
            )
        )
    assert sid.startswith("ses_")

    org_warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "orgdims classify on swarm spawn" in r.getMessage()
    ]
    # Either low-confidence, unclassified, or exception-skipped — all visible.
    assert org_warnings, "expected OrgDims WARNING on real spawn path; got: " + repr(
        [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    )


def test_m14_spawn_warns_when_orgdims_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "spawn.db")
    dal = _SwarmDal()
    task_id = "task_fail"
    dal.tasks[task_id] = {
        "id": task_id,
        "title": "Implement feature",
        "description": "backend API",
        "discipline": "coding",
        "priority": "high",
    }
    dal.swarm_jsons[task_id] = {
        "task_key": "codex",
        "risk_class": "none",
        "acceptance": "ok",
    }

    class _Boom:
        def classify_board_task(self, **kwargs: Any) -> Any:
            raise RuntimeError("orgdims backend down")

    monkeypatch.setattr(
        "omniagentos.orgdims.service.OrgDimsService",
        lambda *a, **k: _Boom(),
    )

    spawner = UnifiedSpawner(
        supervisor=_Supervisor(),
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
    with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.spawn"):
        spawner.spawn(
            SpawnRequest(
                run_id="swr1",
                task_id=task_id,
                task_key="codex",
                attempt_id="swa1",
                working_dir=str(ws),
                prompt="do it",
                provider="codex",
                model="gpt-5.6-sol",
                tier="standard",
                account_id="acct_codex",
                idle_minutes=20.0,
                budget_usd_max=5.0,
                reservation_id="rsv1",
                effort="medium",
            )
        )
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("orgdims classify on swarm spawn skipped" in m for m in messages)


def test_m15_decision_center_contract_truthful() -> None:
    # EDC P4 flipped this from the not-implemented placeholder to the honest
    # implemented contract pointing at the live /api/decisions surfaces.
    contract = decision_center_contract()
    assert contract["implemented"] is True
    assert contract["status"] == "implemented"
    assert contract["http_status"] == 200
    assert contract["producer"] == "omniagentos.edc"
    assert "/api/decisions" in contract["surfaces"]
    assert "Decision Center" in contract["message"]


def test_m15_api_surface_returns_implemented_contract() -> None:
    from omniagentos.api.routes.grok_ops import router
    from omniagentos.api.services import ApiError

    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def _api_error_handler(request, exc: ApiError):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "detail": exc.detail,
                }
            },
        )

    client = TestClient(app)
    for path in ("/api/grok/decision-center", "/api/grok/recommended-next-action"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        contract = resp.json().get("contract") or {}
        assert contract.get("implemented") is True
        assert contract.get("producer") == "omniagentos.edc"
        assert "/api/decisions" in contract.get("surfaces", [])
