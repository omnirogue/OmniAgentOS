"""Flag-gated wiring of FAST DISPATCH into intake.service.dispatch_spec.

Hermetic: the swarm path is monkeypatched to a sentinel (no real planning) and
sessions spawn through a mock (no real ``claude`` process). Asserts:

* flag OFF -> the gate never runs; an auto dispatch takes the existing swarm path
  byte-identically (the full tests/intake suite proves the rest).
* flag ON, solo_fast -> re-routed to the session-spawn path at the haiku band,
  swarm planning skipped.
* flag ON, risk brief -> NEVER fast-laned; stays on the swarm path.
* flag ON, simple sweep -> swarm path with speed_hint threaded into ``speed``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import HarnessType
from omniagentos.intake import service
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.service import dispatch_spec
from omniagentos.policy import load_policy


@dataclass
class _MockSpawner:
    calls: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    def spawn(
        self,
        project_dir: str,
        model: str,
        prompt: str,
        budget_usd_max: float | None = None,
        title: str | None = None,
        extra_write_roots: list[str] | None = None,
        orchestrator_owned: bool = False,
        orchestrator_run_id: str | None = None,
        granted_roots: list[str] | None = None,
    ) -> str:
        self._seq += 1
        session_id = f"ses_wire{self._seq:04d}"
        self.calls.append({"project_dir": project_dir, "model": model, "title": title})
        return session_id


def _spec(title: str, description: str | None = None) -> RefinedSpec:
    return RefinedSpec(
        title=title,
        description=description if description is not None else title,
        acceptance_criteria=["done"],
    )


def _stores(tmp_path: Path) -> tuple[Any, CollabStore, Any]:
    collab = CollabStore(str(tmp_path / "wire.db"))
    return collab._store, collab, load_policy()


@pytest.fixture
def _sentinel_swarm(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace _dispatch_swarm with a sentinel that records its kwargs and returns
    a non-None result (so dispatch_spec returns immediately, no real planning)."""
    captured: dict[str, Any] = {}

    def _fake(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["called"] = True
        captured["kwargs"] = kwargs
        return {"execute": "swarm", "swarm_run_id": "swm_fake", "board_task": {"id": "b"}}

    monkeypatch.setattr(service, "_dispatch_swarm", _fake)
    return captured


def test_flag_off_takes_existing_swarm_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sentinel_swarm: dict[str, Any]
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_FAST_DISPATCH", raising=False)
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    # A brief that WOULD be solo_fast if the gate ran.
    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec("fix a typo in the README"),
        harness=HarnessType.MOCK.value,
        execute="auto",
        session_spawner=spawner,
    )
    # Gate never ran -> swarm path taken, no session spawned.
    assert _sentinel_swarm.get("called") is True
    assert spawner.calls == []
    assert result["execute"] == "swarm"


def test_flag_on_solo_fast_routes_to_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sentinel_swarm: dict[str, Any]
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_FAST_DISPATCH", "1")
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec("add a docstring to the parse_config function"),
        harness=HarnessType.MOCK.value,
        execute="auto",
        session_spawner=spawner,
    )
    # Swarm planning SKIPPED; a monitored session spawned at the haiku (simple) band.
    assert _sentinel_swarm.get("called") is None
    assert result["execute"] == "session"
    assert result["session_id"] == "ses_wire0001"
    assert len(spawner.calls) == 1
    assert spawner.calls[0]["model"] == "haiku"

    # Telemetry row recorded, marked applied.
    row = store._connection.execute(
        "SELECT decision, gate, applied, dispatch_kind FROM dispatch_decisions"
    ).fetchone()
    assert row["decision"] == "solo_fast"
    assert row["applied"] == 1
    assert row["dispatch_kind"] == "session"


def test_flag_on_risk_brief_never_fast_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sentinel_swarm: dict[str, Any]
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_FAST_DISPATCH", "1")
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec("wire up Stripe payments for the checkout"),
        harness=HarnessType.MOCK.value,
        execute="auto",
        session_spawner=spawner,
    )
    # Risk -> fallthrough -> existing swarm path, no session.
    assert _sentinel_swarm.get("called") is True
    assert spawner.calls == []
    assert result["execute"] == "swarm"

    row = store._connection.execute(
        "SELECT decision, risk_flagged, applied FROM dispatch_decisions"
    ).fetchone()
    assert row["decision"] == "fallthrough"
    assert row["risk_flagged"] == 1
    assert row["applied"] == 0


def test_flag_on_simple_sweep_threads_speed_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sentinel_swarm: dict[str, Any]
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_FAST_DISPATCH", "1")
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec("rename the logger in every module"),
        harness=HarnessType.MOCK.value,
        execute="auto",
        session_spawner=spawner,
    )
    # Swarm decision -> existing swarm path, speed_hint "fast" threaded in.
    assert _sentinel_swarm.get("called") is True
    assert _sentinel_swarm["kwargs"].get("speed") == "fast"
    assert spawner.calls == []

    row = store._connection.execute("SELECT decision, applied FROM dispatch_decisions").fetchone()
    assert row["decision"] == "swarm"
    assert row["applied"] == 1  # speed_hint was injected -> routing changed


# ---------------------------------------------------------------------------
# F1 -- the gate classifies the FULL spec (title + description + acceptance),
# not the description alone.
# ---------------------------------------------------------------------------


def test_flag_on_risk_term_only_in_title_never_fast_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sentinel_swarm: dict[str, Any]
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_FAST_DISPATCH", "1")
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    # A benign description, but the risk lives in the TITLE.
    spec = RefinedSpec(
        title="deploy the payment service to production",
        description="add a small helper",
        acceptance_criteria=["done"],
    )
    result = dispatch_spec(
        store,
        collab,
        cfg,
        spec,
        harness=HarnessType.MOCK.value,
        execute="auto",
        session_spawner=spawner,
    )
    assert _sentinel_swarm.get("called") is True
    assert spawner.calls == []
    assert result["execute"] == "swarm"

    row = store._connection.execute(
        "SELECT decision, risk_flagged FROM dispatch_decisions"
    ).fetchone()
    assert row["decision"] == "fallthrough"
    assert row["risk_flagged"] == 1


def test_flag_on_risk_term_only_in_acceptance_never_fast_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sentinel_swarm: dict[str, Any]
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_FAST_DISPATCH", "1")
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    # Title + description look like a tiny edit; the risk hides in acceptance.
    spec = RefinedSpec(
        title="add a small script",
        description="write a short helper file",
        acceptance_criteria=["it must delete all user records from the database"],
    )
    result = dispatch_spec(
        store,
        collab,
        cfg,
        spec,
        harness=HarnessType.MOCK.value,
        execute="auto",
        session_spawner=spawner,
    )
    assert _sentinel_swarm.get("called") is True
    assert spawner.calls == []
    assert result["execute"] == "swarm"

    row = store._connection.execute(
        "SELECT decision, risk_flagged FROM dispatch_decisions"
    ).fetchone()
    assert row["decision"] == "fallthrough"
    assert row["risk_flagged"] == 1


# ---------------------------------------------------------------------------
# F6 -- solo_standard / solo_complex band wiring (not just solo_fast). The gate
# is monkeypatched to force the band; the session model must match the tier.
# ---------------------------------------------------------------------------


def _force_decision(monkeypatch: pytest.MonkeyPatch, decision: str) -> None:
    import omniagentos.dispatch as dispatch_pkg
    from omniagentos.dispatch.gate import GateDecision

    monkeypatch.setattr(
        dispatch_pkg,
        "decide",
        lambda *_a, **_k: GateDecision(decision=decision, gate="semantic", confidence=0.9),
    )


def test_flag_on_solo_standard_routes_to_sonnet_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sentinel_swarm: dict[str, Any]
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_FAST_DISPATCH", "1")
    _force_decision(monkeypatch, "solo_standard")
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec("build a bounded feature"),
        harness=HarnessType.MOCK.value,
        execute="auto",
        session_spawner=spawner,
    )
    assert _sentinel_swarm.get("called") is None
    assert result["execute"] == "session"
    assert spawner.calls[0]["model"] == "sonnet"


def test_flag_on_solo_complex_routes_to_fable_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sentinel_swarm: dict[str, Any]
) -> None:
    from omniagentos.intake.fable import FABLE_MODEL

    monkeypatch.setenv("OMNIAGENTOS_FAST_DISPATCH", "1")
    _force_decision(monkeypatch, "solo_complex")
    store, collab, cfg = _stores(tmp_path)
    spawner = _MockSpawner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec("untangle a hard subsystem"),
        harness=HarnessType.MOCK.value,
        execute="auto",
        session_spawner=spawner,
    )
    assert _sentinel_swarm.get("called") is None
    assert result["execute"] == "session"
    assert spawner.calls[0]["model"] == FABLE_MODEL


# ---------------------------------------------------------------------------
# F4 -- a solo re-route terminal-closes the pre-created orchestration and records
# the decision AFTER spawn with ref_id = the session id.
# ---------------------------------------------------------------------------


def test_solo_reroute_closes_orchestration_and_records_session_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _sentinel_swarm: dict[str, Any]
) -> None:
    from omniagentos.intake.orchestrations import OrchestrationsDal

    monkeypatch.setenv("OMNIAGENTOS_FAST_DISPATCH", "1")
    store, collab, cfg = _stores(tmp_path)
    db_path = str(tmp_path / "wire.db")
    spawner = _MockSpawner()

    # The quick front door's planned lane: an instant placeholder card + a queued
    # orchestration row created BEFORE dispatch.
    card = service.create_queued_goal_card(collab, "add a docstring", "orch_reroute1")
    dal = OrchestrationsDal(db_path)
    dal.create(
        "orch_reroute1",
        board_task_id=str(card["id"]),
        working_dir=str(tmp_path),
        goal="add a docstring",
    )
    assert dal.get("orch_reroute1")["status"] == "queued"

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec("add a docstring to the parse_config function"),
        harness=HarnessType.MOCK.value,
        execute="auto",
        session_spawner=spawner,
        board_task_id=str(card["id"]),
        orchestration_run_id="orch_reroute1",
    )
    assert result["execute"] == "session"
    session_id = result["session_id"]

    # The superseded orchestration is terminal-closed (zero non-terminal rows).
    dal2 = OrchestrationsDal(db_path)
    row = dal2.get("orch_reroute1")
    assert row["status"] == "cancelled"
    non_terminal = dal2._connection.execute(
        "SELECT COUNT(*) AS c FROM orchestrations "
        "WHERE status NOT IN ('completed', 'failed', 'cancelled')"
    ).fetchone()["c"]
    assert non_terminal == 0
    dal.close()
    dal2.close()

    # The decision row was recorded AFTER spawn with the real session id.
    drow = store._connection.execute(
        "SELECT decision, applied, dispatch_kind, ref_id FROM dispatch_decisions"
    ).fetchone()
    assert drow["decision"] == "solo_fast"
    assert drow["applied"] == 1
    assert drow["dispatch_kind"] == "session"
    assert drow["ref_id"] == session_id


# ---------------------------------------------------------------------------
# F6 -- flag-OFF import purity: importing + running a Gate-0 decision with the
# flag unset must NOT import semantic_router. Asserted in a clean subprocess so
# no earlier test can pollute sys.modules.
# ---------------------------------------------------------------------------


def test_flag_off_does_not_import_semantic_router() -> None:
    import os
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import omniagentos.dispatch.gate as g\n"
        "d = g.decide('fix a typo in the README')\n"
        "assert d.decision == 'solo_fast', d.decision\n"
        "assert 'semantic_router' not in sys.modules, 'semantic_router was imported'\n"
        "print('OK')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "OMNIAGENTOS_FAST_DISPATCH"}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
