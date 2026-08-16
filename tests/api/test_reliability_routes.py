"""Tests for reliability, improvements, org, and autonomy API routes (W7)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from omniagentos.api.deps import _api_reliability_store, get_store
from omniagentos.api.main import app
from omniagentos.api.routes import autonomy
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.reliability.contracts import (
    AutonomySetting,
    Improvement,
    ImprovementVote,
    ReliabilityAudit,
    ReliabilityEvent,
)
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import (
    AgentRequestStatus,
    AuditStatus,
    EventStatus,
    ImprovementStatus,
)
from tests.api.fake_store import FakeStore


class ReliabilityFakeStore(FakeStore):
    """FakeStore extended with reliability methods."""

    def __init__(self) -> None:
        super().__init__()
        self.reliability_events: dict[str, ReliabilityEvent] = {}
        self.improvements: dict[str, Improvement] = {}
        self.improvement_votes: dict[str, ImprovementVote] = {}
        self.audits: dict[str, ReliabilityAudit] = {}
        self.org_units: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self.agent_requests: dict[str, dict] = {}
        self.autonomy_settings: dict[str, AutonomySetting] = {}
        self.scorecards: dict[str, dict] = {}
        self.reliability_state: dict[str, dict] = {}
        self._event_id_counter = 0
        self._init_global_autonomy()

    def _init_global_autonomy(self) -> None:
        """Initialize default global autonomy setting."""
        self.autonomy_settings["global_"] = AutonomySetting(
            id="aut_global",
            scope_type="global",
            scope_id="",
            mode="approve",
            max_auto_risk=0,
            updated_by="system",
            updated_at=utc_now_iso(),
        )

    # --- Reliability Events (different from general events table)

    def insert_reliability_event(
        self,
        failure_class: str,
        severity: str,
        signature: str,
        occurrence_key: str,
        source: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        evidence_json: dict | None = None,
    ) -> str:
        """Insert a reliability event."""
        event_id = new_id("evt")
        now = utc_now_iso()
        self.reliability_events[event_id] = ReliabilityEvent(
            id=event_id,
            failure_class=failure_class,
            severity=severity,
            signature=signature,
            occurrence_key=occurrence_key,
            source=source,
            ref_type=ref_type,
            ref_id=ref_id,
            evidence_json=evidence_json or {},
            status=EventStatus.OPEN.value,
            recovery_json={},
            detected_at=now,
            updated_at=now,
        )
        # Also add to general events table via parent class method
        self.insert_event(
            "reliability.event",
            "system",
            "reliability.detected",
            target_type="reliability_event",
            target_id=event_id,
            payload={"failure_class": failure_class, "severity": severity},
        )
        return event_id

    def get_event(self, event_id: str) -> ReliabilityEvent | None:
        return self.reliability_events.get(event_id)

    def list_events(
        self,
        status: str | None = None,
        severity: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReliabilityEvent]:
        events = list(self.reliability_events.values())
        if status:
            events = [e for e in events if e.status == status]
        if severity:
            events = [e for e in events if e.severity == severity]
        return events[offset : offset + limit]

    def set_event_status(
        self,
        event_id: str,
        status: str,
        actor: str,
        detail: dict | None = None,
        expected: str | None = None,
    ) -> bool:
        event = self.reliability_events.get(event_id)
        if event is None or (expected is not None and event.status != expected):
            return False
        event.status = status
        event.updated_at = utc_now_iso()
        return True

    # --- Improvements

    def create_improvement(
        self,
        origin: str,
        kind: str,
        title: str,
        summary: str = "",
        root_cause: str = "",
        proposal_json: dict | None = None,
        created_by: str = "system",
    ) -> str:
        imp_id = new_id("imp")
        now = utc_now_iso()
        self.improvements[imp_id] = Improvement(
            id=imp_id,
            origin=origin,
            kind=kind,
            title=title,
            summary=summary,
            root_cause=root_cause,
            proposal_json=proposal_json or {},
            status=ImprovementStatus.PROPOSED.value,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        return imp_id

    def get_improvement(self, imp_id: str) -> Improvement | None:
        return self.improvements.get(imp_id)

    def list_improvements(
        self,
        status: str | None = None,
        origin: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Improvement]:
        imps = list(self.improvements.values())
        if status:
            imps = [i for i in imps if i.status == status]
        if origin:
            imps = [i for i in imps if i.origin == origin]
        if kind:
            imps = [i for i in imps if i.kind == kind]
        return imps[offset : offset + limit]

    def transition_improvement(
        self,
        imp_id: str,
        expected_status: str,
        new_status: str,
        actor: str,
        detail_json: dict | None = None,
    ) -> None:
        from omniagentos.reliability.contracts import TransitionConflict

        imp = self.improvements.get(imp_id)
        if not imp:
            raise TransitionConflict(f"Improvement {imp_id} not found")
        if imp.status != expected_status:
            raise TransitionConflict(f"Expected status {expected_status}, got {imp.status}")
        imp.status = new_status
        imp.version += 1
        imp.updated_at = utc_now_iso()

    def update_improvement_fields(self, imp_id: str, **fields) -> None:
        imp = self.improvements.get(imp_id)
        if imp:
            for key, value in fields.items():
                if hasattr(imp, key):
                    setattr(imp, key, value)
            imp.updated_at = utc_now_iso()

    def insert_vote(
        self,
        improvement_id: str,
        panel_attempt_id: str,
        judge_agent: str,
        model_family: str,
        verdict: str,
        scores_json: dict | None = None,
        reasoning: str = "",
        conditions: str = "",
        model: str = "",
    ) -> str:
        vote_id = new_id("vot")
        self.improvement_votes[vote_id] = ImprovementVote(
            id=vote_id,
            improvement_id=improvement_id,
            panel_attempt_id=panel_attempt_id,
            judge_agent=judge_agent,
            model_family=model_family,
            verdict=verdict,
            scores_json=scores_json or {},
            reasoning=reasoning,
            conditions=conditions,
            model=model,
            created_at=utc_now_iso(),
        )
        return vote_id

    def list_votes(
        self,
        improvement_id: str | None = None,
        panel_attempt_id: str | None = None,
        limit: int = 100,
    ) -> list[ImprovementVote]:
        votes = list(self.improvement_votes.values())
        if improvement_id:
            votes = [v for v in votes if v.improvement_id == improvement_id]
        if panel_attempt_id:
            votes = [v for v in votes if v.panel_attempt_id == panel_attempt_id]
        return votes[:limit]

    # --- Audits

    def create_audit(self, kind: str, window_start: str, window_end: str) -> str:
        audit_id = new_id("aud")
        self.audits[audit_id] = ReliabilityAudit(
            id=audit_id,
            kind=kind,
            status=AuditStatus.QUEUED.value,
            window_start=window_start,
            window_end=window_end,
            started_at=utc_now_iso(),
        )
        return audit_id

    def get_audit(self, audit_id: str) -> ReliabilityAudit | None:
        return self.audits.get(audit_id)

    def list_audits(
        self,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReliabilityAudit]:
        audits = list(self.audits.values())
        if kind:
            audits = [a for a in audits if a.kind == kind]
        if status:
            audits = [a for a in audits if a.status == status]
        return audits[offset : offset + limit]

    def start_audit(self, audit_id: str) -> None:
        if audit_id in self.audits:
            self.audits[audit_id].status = AuditStatus.RUNNING.value

    def complete_audit(
        self,
        audit_id: str,
        stats_json: dict | None = None,
        findings: int = 0,
        report_note_path: str | None = None,
    ) -> None:
        if audit_id in self.audits:
            self.audits[audit_id].status = AuditStatus.COMPLETED.value
            self.audits[audit_id].stats_json = stats_json or {}
            self.audits[audit_id].findings = findings
            self.audits[audit_id].report_note_path = report_note_path
            self.audits[audit_id].finished_at = utc_now_iso()

    # --- Scorecards

    def upsert_scorecard(
        self,
        subject_type: str,
        subject_id: str,
        window: str,
        period_start: str,
        metrics_json: dict | None = None,
    ) -> str:
        key = f"{subject_type}_{subject_id}_{window}_{period_start}"
        scorecard_id = self.scorecards.get(key, {}).get("id") or new_id("sc")
        self.scorecards[key] = {
            "id": scorecard_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "window": window,
            "period_start": period_start,
            "metrics_json": metrics_json or {},
            "computed_at": utc_now_iso(),
        }
        return scorecard_id

    def get_scorecard(
        self, subject_type: str, subject_id: str, window: str, period_start: str
    ) -> dict | None:
        key = f"{subject_type}_{subject_id}_{window}_{period_start}"
        return self.scorecards.get(key)

    def list_scorecards(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        window: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        scorecards = list(self.scorecards.values())
        if subject_type:
            scorecards = [s for s in scorecards if s["subject_type"] == subject_type]
        if subject_id:
            scorecards = [s for s in scorecards if s["subject_id"] == subject_id]
        if window:
            scorecards = [s for s in scorecards if s["window"] == window]
        return scorecards[:limit]

    # --- Organization

    def create_org_unit(
        self,
        name: str,
        kind: str,
        parent_id: str | None = None,
        charter: str = "",
    ) -> str:
        unit_id = new_id("org")
        self.org_units[unit_id] = {
            "id": unit_id,
            "name": name,
            "kind": kind,
            "parent_id": parent_id,
            "charter": charter,
            "status": "active",
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        return unit_id

    def get_org_unit(self, unit_id: str) -> dict | None:
        return self.org_units.get(unit_id)

    def list_org_units(
        self, kind: str | None = None, parent_id: str | None = None, status: str = "active"
    ) -> list[dict]:
        units = list(self.org_units.values())
        if status:
            units = [u for u in units if u["status"] == status]
        if kind:
            units = [u for u in units if u["kind"] == kind]
        if parent_id:
            units = [u for u in units if u["parent_id"] == parent_id]
        return units

    def create_agent(
        self,
        name: str,
        org_unit_id: str | None = None,
        org_role: str = "specialist",
        title: str = "",
        charter: str = "",
        model: str | None = None,
        harness: str | None = None,
        schedule_json: dict | None = None,
        vault_note_path: str | None = None,
    ) -> str:
        agent_id = new_id("agt")
        self.agents[agent_id] = {
            "id": agent_id,
            "name": name,
            "model": model,
            "org_unit_id": org_unit_id,
            "org_role": org_role,
            "title": title,
            "charter": charter,
            "harness": harness,
            "schedule_json": schedule_json or {},
            "enabled": 1,
            "vault_note_path": vault_note_path,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        return agent_id

    def get_agent(self, agent_id: str) -> dict | None:
        return self.agents.get(agent_id)

    def list_agents(
        self,
        org_unit_id: str | None = None,
        org_role: str | None = None,
        enabled: int | None = None,
    ) -> list[dict]:
        agents = list(self.agents.values())
        if org_unit_id:
            agents = [a for a in agents if a["org_unit_id"] == org_unit_id]
        if org_role:
            agents = [a for a in agents if a["org_role"] == org_role]
        if enabled is not None:
            agents = [a for a in agents if a["enabled"] == enabled]
        return agents

    def update_agent(self, agent_id: str, **fields) -> None:
        if agent_id in self.agents:
            self.agents[agent_id].update(fields)
            self.agents[agent_id]["updated_at"] = utc_now_iso()

    def create_agent_request(self, description: str, requested_by: str = "owner") -> str:
        req_id = new_id("areq")
        self.agent_requests[req_id] = {
            "id": req_id,
            "description": description,
            "requested_by": requested_by,
            "status": "pending",
            "design_json": {},
            "improvement_id": None,
            "agent_id": None,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        return req_id

    def get_agent_request(self, req_id: str) -> dict | None:
        return self.agent_requests.get(req_id)

    def list_agent_requests(self, status: str | None = None, limit: int = 100) -> list[dict]:
        reqs = list(self.agent_requests.values())
        if status:
            reqs = [r for r in reqs if r["status"] == status]
        return reqs[:limit]

    def update_agent_request_status(
        self,
        req_id: str,
        status: str,
        design_json: dict | None = None,
        improvement_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        if req_id in self.agent_requests:
            self.agent_requests[req_id].update(
                {
                    "status": status,
                    "design_json": design_json or {},
                    "improvement_id": improvement_id,
                    "agent_id": agent_id,
                    "updated_at": utc_now_iso(),
                }
            )

    # --- Autonomy

    def get_autonomy_setting(self, scope_type: str, scope_id: str = "") -> AutonomySetting | None:
        key = f"{scope_type}_{scope_id}"
        return self.autonomy_settings.get(key)

    def list_autonomy_settings(self) -> list[AutonomySetting]:
        return list(self.autonomy_settings.values())

    def upsert_autonomy_setting(
        self,
        scope_type: str,
        scope_id: str,
        mode: str,
        max_auto_risk: int,
        updated_by: str,
    ) -> AutonomySetting:
        key = f"{scope_type}_{scope_id}"
        existing = self.autonomy_settings.get(key)
        setting = AutonomySetting(
            id=existing.id if existing else new_id("aut"),
            scope_type=scope_type,
            scope_id=scope_id,
            mode=mode,
            max_auto_risk=max_auto_risk,
            updated_by=updated_by,
            updated_at=utc_now_iso(),
        )
        self.autonomy_settings[key] = setting
        return setting

    def resolve_autonomy(
        self, agent_id: str | None = None, kind: str | None = None
    ) -> AutonomySetting:
        return self.autonomy_settings.get(
            "global_",
            AutonomySetting(
                id="aut_global",
                scope_type="global",
                scope_id="",
                mode="approve",
                max_auto_risk=0,
            ),
        )

    # --- Watch cursor

    def get_watch_cursor(self) -> str | None:
        return self.reliability_state.get("watch_cursor", {}).get("cursor")

    def advance_watch_cursor(self, new_cursor: str) -> None:
        self.reliability_state["watch_cursor"] = {"cursor": new_cursor}


@pytest.fixture
def reliability_store() -> ReliabilityFakeStore:
    """Reliability-enhanced fake store."""
    return ReliabilityFakeStore()


@pytest.fixture
def asgi_client_rel(reliability_store: ReliabilityFakeStore) -> httpx.AsyncClient:
    """ASGI client with reliability store."""
    app.dependency_overrides[get_store] = lambda: reliability_store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def tier_p_headers(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, str]:
    token_path = tmp_path / "autonomy-token"
    token_path.write_text("tier-p-test-token", encoding="utf-8")
    monkeypatch.setattr(autonomy, "AUTONOMY_TOKEN_PATH", token_path)
    # Round-4 Class A repair (trust model): X-Session-Token is AUTHENTICATION
    # (verified by _authenticated_principal in api/routes/improvements.py,
    # which always resolves a valid shared token to the fixed "operator"
    # label). X-Omni-Authenticated-Principal is an UNVERIFIED, caller-supplied
    # header kept here to prove spoofing it has no effect on authorization --
    # it is recorded only as unverified_principal audit metadata, never
    # trusted for auth. decided_by is free-text ATTRIBUTION, never required to
    # equal the authenticated principal (Round-4 blocker 1: that lockout
    # bricked the identity dialog for real humans); it is compared against
    # created_by for the self-approval guard instead (Round-4 blocker 2).
    return {
        **auth_headers,
        "X-Autonomy-Token": "tier-p-test-token",
        "X-Omni-Authenticated-Principal": "owner",
    }


def test_get_summary(
    asgi_client_rel: httpx.AsyncClient, reliability_store: ReliabilityFakeStore, auth_headers: dict
) -> None:
    response = asyncio.run(asgi_client_rel.get("/api/reliability/summary", headers=auth_headers))
    assert response.status_code == 200
    data = response.json()
    assert "open_events" in data
    assert "last_audit" in data
    assert "watch_heartbeat" in data


def test_list_events(
    asgi_client_rel: httpx.AsyncClient, reliability_store: ReliabilityFakeStore, auth_headers: dict
) -> None:
    event_id = reliability_store.insert_reliability_event(
        "run_failed", "critical", "sig", "occ", "test"
    )
    response = asyncio.run(asgi_client_rel.get("/api/reliability/events", headers=auth_headers))
    assert response.status_code == 200
    events = response.json()
    assert len(events) > 0
    assert any(e["id"] == event_id for e in events)


def test_trigger_audit_spawns_worker(
    asgi_client_rel: httpx.AsyncClient, reliability_store: ReliabilityFakeStore, auth_headers: dict
) -> None:
    with patch("omniagentos.api.routes.reliability.subprocess.Popen") as mock_popen:
        response = asyncio.run(
            asgi_client_rel.post(
                "/api/reliability/audit/run",
                headers=auth_headers,
                json={"kind": "watch"},
            )
        )
        assert response.status_code == 202
        data = response.json()
        assert "audit_id" in data
        mock_popen.assert_called_once()
        command = mock_popen.call_args.args[0]
        audit_id_index = command.index("--audit-id")
        assert command[audit_id_index + 1] == data["audit_id"]


def test_trigger_audit_closes_log_descriptor_on_success(
    asgi_client_rel: httpx.AsyncClient, reliability_store: ReliabilityFakeStore, auth_headers: dict
) -> None:
    opened_handles = []

    def mock_popen(*args, **kwargs):
        stdout = kwargs.get("stdout")
        assert stdout is not None
        assert not stdout.closed
        opened_handles.append(stdout)
        proc = MagicMock()
        return proc

    import warnings

    with (
        warnings.catch_warnings(record=True) as w,
        patch("omniagentos.api.routes.reliability.subprocess.Popen", side_effect=mock_popen),
    ):
        warnings.simplefilter("always", ResourceWarning)
        response = asyncio.run(
            asgi_client_rel.post(
                "/api/reliability/audit/run",
                headers=auth_headers,
                json={"kind": "watch"},
            )
        )
        assert response.status_code == 202
        assert len(opened_handles) == 1
        assert opened_handles[0].closed
        resource_warnings = [item for item in w if issubclass(item.category, ResourceWarning)]
        assert len(resource_warnings) == 0

        queued_events = [e for e in reliability_store.events if e.get("action") == "audit.queued"]
        assert len(queued_events) == 1
        assert queued_events[0]["target_id"] == response.json()["audit_id"]


def test_trigger_audit_closes_log_descriptor_on_popen_failure(
    asgi_client_rel: httpx.AsyncClient, reliability_store: ReliabilityFakeStore, auth_headers: dict
) -> None:
    opened_handles = []

    def mock_popen(*args, **kwargs):
        stdout = kwargs.get("stdout")
        assert stdout is not None
        assert not stdout.closed
        opened_handles.append(stdout)
        raise OSError("subprocess spawn failed")

    import warnings

    with (
        warnings.catch_warnings(record=True) as w,
        patch("omniagentos.api.routes.reliability.subprocess.Popen", side_effect=mock_popen),
    ):
        warnings.simplefilter("always", ResourceWarning)
        response = asyncio.run(
            asgi_client_rel.post(
                "/api/reliability/audit/run",
                headers=auth_headers,
                json={"kind": "watch"},
            )
        )
        assert response.status_code == 500
        data = response.json()
        assert data["error"]["code"] == "spawn_failed"
        assert len(opened_handles) == 1
        assert opened_handles[0].closed
        resource_warnings = [item for item in w if issubclass(item.category, ResourceWarning)]
        assert len(resource_warnings) == 0

        spawn_failed_events = [
            e for e in reliability_store.events if e.get("action") == "audit.spawn_failed"
        ]
        assert len(spawn_failed_events) == 1
        payload = json.loads(spawn_failed_events[0]["payload_json"])
        assert "subprocess spawn failed" in payload["error"]


def test_trigger_audit_handles_log_open_failure(
    asgi_client_rel: httpx.AsyncClient, reliability_store: ReliabilityFakeStore, auth_headers: dict
) -> None:
    import builtins

    real_open = builtins.open

    def mock_open(file, *args, **kwargs):
        if "reliability-audit-" in str(file):
            raise PermissionError("Permission denied opening log file")
        return real_open(file, *args, **kwargs)

    import warnings

    with warnings.catch_warnings(record=True) as w, patch("builtins.open", side_effect=mock_open):
        warnings.simplefilter("always", ResourceWarning)
        response = asyncio.run(
            asgi_client_rel.post(
                "/api/reliability/audit/run",
                headers=auth_headers,
                json={"kind": "watch"},
            )
        )
        assert response.status_code == 500
        data = response.json()
        assert data["error"]["code"] == "spawn_failed"
        resource_warnings = [item for item in w if issubclass(item.category, ResourceWarning)]
        assert len(resource_warnings) == 0

        spawn_failed_events = [
            e for e in reliability_store.events if e.get("action") == "audit.spawn_failed"
        ]
        assert len(spawn_failed_events) == 1
        payload = json.loads(spawn_failed_events[0]["payload_json"])
        assert "Permission denied" in payload["error"]


def test_approve_improvement_202(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    imp_id = reliability_store.create_improvement("audit", "fix", "Test fix")
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        popen.return_value.pid = 1111
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                headers=tier_p_headers,
                # decided_by is free-text attribution (Round-4 repair): it
                # need not match the authenticated principal, only differ
                # from created_by ("system" here) for the self-approval guard.
                json={"decided_by": "owner"},
            )
        )
        assert response.status_code == 202
        assert response.json()["status"] == ImprovementStatus.APPROVED.value


def test_approve_rejects_self_approval_server_side(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """Server-side guard: decided_by matching created_by is a fail-closed 403.

    This is the real enforcement point — the dashboard's client-side check is
    defense in depth only, so the API must refuse this even if a caller goes
    around the UI entirely (curl/devtools/a non-button caller). Round-4
    repair: the guard compares decided_by (typed attribution) against
    created_by directly — never against the authenticated principal or the
    fixed operator-constant label — so this fires for the case it can
    honestly detect: a human typing the SAME machine label that authored the
    proposal.
    """
    imp_id = reliability_store.create_improvement(
        "audit", "fix", "Test fix", created_by="csi"
    )
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                # Spoofing this header has NO effect -- it is never read for
                # authorization, only recorded as unverified audit metadata.
                headers={**tier_p_headers, "X-Omni-Authenticated-Principal": "agent-planner"},
                json={"decided_by": "csi"},
            )
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "self_approval_forbidden"
        popen.assert_not_called()

    # Never transitioned — improvement stays awaiting_human, not silently approved.
    assert (
        reliability_store.improvements[imp_id].status == ImprovementStatus.AWAITING_HUMAN.value
    )


def test_approve_rejects_self_approval_case_insensitive_and_whitespace(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """Identity comparison is trimmed + case-insensitive — no bypass via casing/whitespace."""
    imp_id = reliability_store.create_improvement(
        "audit", "fix", "Test fix", created_by="  Operator  "
    )
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    with patch("omniagentos.api.routes.improvements.subprocess.Popen"):
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                headers=tier_p_headers,
                json={"decided_by": "  Operator  "},
            )
        )
        assert response.status_code == 403, response.text


def test_approve_rejects_empty_decided_by(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """Missing/whitespace-only decided_by never defaults to a silent identity."""
    imp_id = reliability_store.create_improvement("audit", "fix", "Test fix", created_by="system")
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    with patch("omniagentos.api.routes.improvements.subprocess.Popen"):
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                headers=tier_p_headers,
                json={"decided_by": "   "},
            )
        )
        assert response.status_code == 400, response.text


def test_approve_decided_by_is_free_text_attribution_not_bound_to_principal(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """Round-4 Class A repair: decided_by is ATTRIBUTION, not a second auth factor.

    Round-3 required decided_by to equal the authenticated principal
    (identity_mismatch otherwise), which forced every human approver to
    literally type the operator-constant label "operator" -- bricking the
    dashboard identity dialog for real humans (Round-4 blocker 1). decided_by
    is now free text; it need only differ from created_by to clear the
    self-approval guard. Spoofing X-Omni-Authenticated-Principal still has no
    authorization effect -- it is unread for auth.
    """
    imp_id = reliability_store.create_improvement(
        "audit", "fix", "Test fix", created_by="curator"
    )
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        popen.return_value.pid = 8181
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                headers={
                    **tier_p_headers,
                    "X-Omni-Authenticated-Principal": "operator@example.test",
                },
                json={"decided_by": "reviewer-alias"},
            )
        )
    assert response.status_code == 202, response.text
    assert response.json()["decided_by"] == "reviewer-alias"
    popen.assert_called_once()
    assert reliability_store.improvements[imp_id].status == ImprovementStatus.APPROVED.value


def test_approve_fails_closed_on_missing_or_empty_created_by(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """A proposal with no recorded author identity must never be approvable.

    Null/empty on the created_by side fails closed (400) rather than opening
    a path where authorship cannot be verified against the guard.
    """
    imp_id = reliability_store.create_improvement("audit", "fix", "Test fix", created_by="")
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                headers=tier_p_headers,
                json={"decided_by": "operator"},
            )
        )
    assert response.status_code == 400, response.text
    popen.assert_not_called()


def test_approve_fails_closed_on_missing_session_token(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """No/invalid session token never approves, regardless of decided_by.

    Round-3 repair: the authenticated principal is now derived solely from
    the verified X-Session-Token (never the caller-supplied
    X-Omni-Authenticated-Principal header), so this is the ONLY way to reach
    the unauthenticated state now -- an absent/invalid token is refused
    globally (401, ``require_session_token`` in api/main.py) before this
    route's own dependencies even run, and if that gate is ever bypassed the
    route's own principal dependency independently re-verifies the token and
    fails closed (403) rather than trusting anything the caller asserted.
    """
    imp_id = reliability_store.create_improvement(
        "audit", "fix", "Test fix", created_by="agent-planner"
    )
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    headers = dict(tier_p_headers)
    headers["X-Session-Token"] = "not-the-real-token"
    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                headers=headers,
                json={"decided_by": "owner"},
            )
        )
    assert response.status_code == 401, response.text
    popen.assert_not_called()


def test_approve_principal_dependency_fails_closed_when_token_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit-level: the route's own principal dependency never trusts the header.

    Even in isolation from the app-level session-token gate, a spoofed
    X-Omni-Authenticated-Principal header cannot manufacture an identity: the
    dependency signature does not even accept that header any more (Round-3
    repair). An unverifiable X-Session-Token derives to ``None``
    (unauthenticated); a valid one derives to the fixed operator label.
    """
    from omniagentos.api.routes.improvements import _authenticated_principal

    monkeypatch.setattr(
        "omniagentos.sessions.token.verify_token", lambda presented: presented == "the-real-token"
    )

    assert _authenticated_principal(x_session_token="not-the-real-token") is None
    assert _authenticated_principal(x_session_token=None) is None
    assert _authenticated_principal(x_session_token="the-real-token") == "operator"


def test_approve_self_approval_guard_unicode_nfc_normalizes(
    reliability_store: ReliabilityFakeStore,
) -> None:
    """Combining-mark and precomposed forms of the same identity must match."""
    from omniagentos.api.routes.improvements import _is_self_approval

    assert _is_self_approval("A\u030agent", "\u00c5gent") is True
    assert _is_self_approval("", "") is False
    assert _is_self_approval("agent", "") is False


def test_approve_header_spoof_cannot_forge_identity_when_token_says_otherwise(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """Round-3/4 Class A repair (boundary provenance): a caller-supplied
    X-Omni-Authenticated-Principal header can never forge authorization —
    it is unread for auth in both the AUTHENTICATION dependency
    (_authenticated_principal, still token-only, Round-3) and the
    self-approval guard (Round-4: decided_by vs created_by, never vs the
    header). This is still governance-relevant: decided_by and the header
    both equal the proposal's author here, so the guard correctly blocks it
    as self_approval_forbidden -- driven by decided_by (typed attribution),
    NOT by the header having been trusted.
    """
    imp_id = reliability_store.create_improvement(
        "audit", "fix", "Test fix", created_by="owner"
    )
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                # Spoof both the header AND decided_by to "owner" -- the author
                # identity. This must be blocked because decided_by == created_by
                # (self-approval), never because the header was trusted for auth.
                headers={**tier_p_headers, "X-Omni-Authenticated-Principal": "owner"},
                json={"decided_by": "owner"},
            )
        )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "self_approval_forbidden"
    popen.assert_not_called()
    assert (
        reliability_store.improvements[imp_id].status == ImprovementStatus.AWAITING_HUMAN.value
    )


def test_approve_header_spoof_recorded_as_unverified_metadata_only(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """Round-4 blocker 3 (AUDIT): the caller-supplied header IS on the wire —
    it must be recorded for audit purposes, but only as unverified metadata,
    never as authorization. Proves the header value reaches the durable event
    trail labeled ``unverified_principal``, alongside the token-derived
    ``token_principal``, and never overwrites ``decided_by``.
    """
    imp_id = reliability_store.create_improvement(
        "audit", "fix", "Test fix", created_by="curator"
    )
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        popen.return_value.pid = 6262
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                headers={**tier_p_headers, "X-Omni-Authenticated-Principal": "owner@acmeuni.test"},
                json={"decided_by": "owner"},
            )
        )
    assert response.status_code == 202, response.text
    approved_events = [
        e for e in reliability_store.events if e.get("action") == "improvement.approved"
    ]
    assert approved_events, "approve did not leave a durable improvement.approved event"
    payload = json.loads(approved_events[-1]["payload_json"])
    assert payload["decided_by"] == "owner"
    assert payload["token_principal"] == "operator"
    assert payload["unverified_principal"] == "owner@acmeuni.test"


def test_approve_blocked_self_approval_leaves_durable_audit_event(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """A blocked self-approval attempt must leave a durable domain event."""
    author = "operator"
    imp_id = reliability_store.create_improvement("audit", "fix", "Test fix", created_by=author)
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    response = asyncio.run(
        asgi_client_rel.post(
            f"/api/improvements/{imp_id}/approve",
            headers=tier_p_headers,
            json={"decided_by": author},
        )
    )
    assert response.status_code == 403, response.text
    matching = [
        e
        for e in reliability_store.events
        if e.get("target_id") == imp_id
        and "self" in str(e.get("action", "")).lower()
        and "block" in str(e.get("action", "")).lower()
    ]
    assert matching, "403 self-approval attempt left no durable blocked-attempt event"


def test_approve_persists_decided_by_on_success(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """A successful approve must persist decided_by EXACTLY AS TYPED (Round-4
    blocker 3: the audit record must never be silently forced to a constant
    like "operator" regardless of what the approver actually typed).
    """
    imp_id = reliability_store.create_improvement("audit", "fix", "Test fix")
    reliability_store.improvements[imp_id].status = ImprovementStatus.AWAITING_HUMAN.value

    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        popen.return_value.pid = 7777
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/approve",
                headers=tier_p_headers,
                json={"decided_by": "owner"},
            )
        )
    assert response.status_code == 202, response.text
    assert response.json()["decided_by"] == "owner"
    assert reliability_store.improvements[imp_id].decided_by == "owner"


@pytest.mark.parametrize(
    ("route", "status"),
    [
        ("reject", ImprovementStatus.AWAITING_HUMAN.value),
        ("pull", ImprovementStatus.PANEL_BLOCKED.value),
        ("apply", ImprovementStatus.APPROVED.value),
        ("rollback", ImprovementStatus.APPLIED.value),
    ],
)
def test_sibling_decide_routes_reject_blank_decided_by(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
    route: str,
    status: str,
) -> None:
    """Every decision route rejects a blank decided_by and never spawns a worker."""
    imp_id = reliability_store.create_improvement(
        "audit", "fix", f"repro-{route}", created_by="agent-planner"
    )
    reliability_store.improvements[imp_id].status = status

    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/{route}",
                headers=tier_p_headers,
                json={"decided_by": "   "},
            )
        )
    assert response.status_code == 400, (route, response.text)
    popen.assert_not_called()


def test_h06_worker_spawn_uses_interpreter_module_cwd(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """H-06: approve/apply/rollback spawn current interpreter + module + product cwd."""
    import sys

    from omniagentos.api.routes.improvements import _product_root

    cases = [
        (ImprovementStatus.AWAITING_HUMAN.value, "approve", "apply"),
        (ImprovementStatus.APPROVED.value, "apply", "apply"),
        (ImprovementStatus.APPLIED.value, "rollback", "rollback"),
    ]
    root = str(_product_root())
    for status, route, worker_cmd in cases:
        imp_id = reliability_store.create_improvement("audit", "fix", f"h06-{route}")
        reliability_store.improvements[imp_id].status = status
        # "approve" additionally binds decided_by to the authenticated
        # principal (Round-3 repair: a verified session token derives the
        # fixed "operator" label); apply/rollback have no such binding.
        decided_by = "operator" if route == "approve" else "owner"
        with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
            popen.return_value.pid = 5555
            response = asyncio.run(
                asgi_client_rel.post(
                    f"/api/improvements/{imp_id}/{route}",
                    headers=tier_p_headers,
                    json={"decided_by": decided_by},
                )
            )
        assert response.status_code == 202, (route, response.text)
        argv = list(popen.call_args.args[0])
        kwargs = popen.call_args.kwargs
        assert argv[0] == sys.executable
        assert argv[0] != "python"
        assert argv[1:4] == ["-m", "omniagentos.reliability", worker_cmd]
        assert "--improvement" in argv and argv[argv.index("--improvement") + 1] == imp_id
        assert "--db" in argv
        assert "--repo-root" in argv and argv[argv.index("--repo-root") + 1] == root
        assert kwargs["cwd"] == root
        assert kwargs["start_new_session"] is True
        assert kwargs["stdout"] is not subprocess.DEVNULL
        assert kwargs["stdout"].closed is True
        assert kwargs["stderr"] is subprocess.STDOUT


def test_h06_spawn_failure_returns_500(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """H-06: spawn OSError is not swallowed as success."""
    imp_id = reliability_store.create_improvement("audit", "fix", "spawn-fail")
    reliability_store.improvements[imp_id].status = ImprovementStatus.APPROVED.value
    with patch(
        "omniagentos.api.routes.improvements.subprocess.Popen",
        side_effect=OSError("No such file or directory: broken-python"),
    ) as popen:
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/apply",
                headers=tier_p_headers,
                json={"decided_by": "owner"},
            )
        )
    assert response.status_code == 500
    body = response.json()
    err = body.get("error") or body
    assert err.get("code") == "spawn_failed" or "spawn" in str(body).lower()
    assert popen.call_args.kwargs["stdout"].closed is True


def test_h06_open_failure_returns_500_and_records_failure(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    """H-06: log open failure records spawn failure and fails closed with 500."""
    imp_id = reliability_store.create_improvement("audit", "fix", "open-fail")
    reliability_store.improvements[imp_id].status = ImprovementStatus.APPROVED.value
    with patch(
        "omniagentos.api.routes.improvements.open",
        side_effect=PermissionError("Permission denied: worker.log"),
    ):
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/apply",
                headers=tier_p_headers,
                json={"decided_by": "owner"},
            )
        )
    assert response.status_code == 500
    body = response.json()
    err = body.get("error") or body
    assert err.get("code") == "spawn_failed"
    assert "Permission denied" in str(body)
    from omniagentos.reliability.worker_drive import OUTCOME_SPAWN_FAILED, get_worker_state

    state = get_worker_state(reliability_store, imp_id)
    assert state["outcome"] == OUTCOME_SPAWN_FAILED
    assert "Permission denied" in state["last_spawn_error"]


def test_cas_conflict_on_approve(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    imp_id = reliability_store.create_improvement("audit", "fix", "Test fix")
    reliability_store.improvements[imp_id].status = ImprovementStatus.PROPOSED.value

    response = asyncio.run(
        asgi_client_rel.post(
            f"/api/improvements/{imp_id}/approve",
            headers=tier_p_headers,
            json={"decided_by": "operator"},
        )
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_missing_session_token(asgi_client_rel: httpx.AsyncClient) -> None:
    """All mutating routes require session token."""
    response = asyncio.run(
        asgi_client_rel.post(
            "/api/reliability/audit/run",
            json={"kind": "watch"},
        )
    )
    assert response.status_code == 401


def test_autonomy_get_with_session_token(
    asgi_client_rel: httpx.AsyncClient, auth_headers: dict
) -> None:
    """GET /autonomy needs a session token but no Tier-P token."""
    response = asyncio.run(asgi_client_rel.get("/api/autonomy", headers=auth_headers))
    assert response.status_code == 200
    data = response.json()
    assert "global" in data


def test_autonomy_put_missing_token(asgi_client_rel: httpx.AsyncClient, auth_headers: dict) -> None:
    """PUT /autonomy requires X-Autonomy-Token."""
    response = asyncio.run(
        asgi_client_rel.put(
            "/api/autonomy",
            headers=auth_headers,
            json={"scope_type": "global", "mode": "auto", "max_auto_risk": 1},
        )
    )
    assert response.status_code == 403


def test_v2_get_requires_session_token(asgi_client_rel: httpx.AsyncClient) -> None:
    response = asyncio.run(asgi_client_rel.get("/api/reliability/summary"))
    assert response.status_code == 401


def test_ignore_event_persists_status(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    auth_headers: dict[str, str],
) -> None:
    event_id = reliability_store.insert_reliability_event(
        "run_failed", "warning", "ignore-sig", "ignore-occ", "test"
    )
    response = asyncio.run(
        asgi_client_rel.post(
            f"/api/reliability/events/{event_id}/ignore",
            headers=auth_headers,
            json={},
        )
    )
    assert response.status_code == 200
    assert reliability_store.get_event(event_id).status == EventStatus.IGNORED.value


def test_pull_panel_blocked_improvement(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    imp_id = reliability_store.create_improvement("audit", "fix", "Blocked")
    reliability_store.improvements[imp_id].status = ImprovementStatus.PANEL_BLOCKED.value
    response = asyncio.run(
        asgi_client_rel.post(
            f"/api/improvements/{imp_id}/pull",
            headers=tier_p_headers,
            json={"decided_by": "owner"},
        )
    )
    assert response.status_code == 200
    assert response.json()["status"] == ImprovementStatus.AWAITING_HUMAN.value


def test_apply_worker_owns_transition(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    imp_id = reliability_store.create_improvement("audit", "fix", "Approved")
    reliability_store.improvements[imp_id].status = ImprovementStatus.APPROVED.value
    with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
        popen.return_value.pid = 4242
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/improvements/{imp_id}/apply",
                headers=tier_p_headers,
                json={"decided_by": "owner"},
            )
        )
    assert response.status_code == 202
    assert response.json()["status"] == ImprovementStatus.APPROVED.value
    argv = popen.call_args.args[0]
    assert "--improvement" in argv
    assert argv[argv.index("--improvement") + 1] == imp_id


def test_autonomy_put_persists(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    tier_p_headers: dict[str, str],
) -> None:
    response = asyncio.run(
        asgi_client_rel.put(
            "/api/autonomy",
            headers=tier_p_headers,
            json={
                "scope_type": "kind",
                "scope_id": "fix",
                "mode": "auto",
                "max_auto_risk": 1,
            },
        )
    )
    assert response.status_code == 200
    saved = reliability_store.get_autonomy_setting("kind", "fix")
    assert saved is not None
    assert (saved.mode, saved.max_auto_risk) == ("auto", 1)


def test_agent_request_approve_spawns_company_design(
    asgi_client_rel: httpx.AsyncClient,
    reliability_store: ReliabilityFakeStore,
    auth_headers: dict[str, str],
) -> None:
    request_id = reliability_store.create_agent_request("Add a reliability agent")
    with patch("omniagentos.api.routes.org.subprocess.Popen") as popen:
        response = asyncio.run(
            asgi_client_rel.post(
                f"/api/org/agent-requests/{request_id}/approve",
                headers=auth_headers,
                json={"decided_by": "owner"},
            )
        )
    assert response.status_code == 202
    assert response.json()["status"] == AgentRequestStatus.DESIGNING.value
    assert popen.call_args.args[0] == [
        "python",
        "-m",
        "omniagentos.orgdims.company_requests",
        "design",
        "--request",
        request_id,
    ]


def test_real_reliability_store_v2_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every V2 GET and one approval flow work on a real migrated tmp DB."""
    from omniagentos.sessions import token as session_token

    monkeypatch.setattr(session_token, "TOKEN_PATH", tmp_path / "sessions-token")
    session_value = session_token.load_or_create_token()
    autonomy_path = tmp_path / "autonomy-token"
    autonomy_path.write_text("real-tier-p-token", encoding="utf-8")
    monkeypatch.setattr(autonomy, "AUTONOMY_TOKEN_PATH", autonomy_path)

    store = _api_reliability_store(str(tmp_path / "rf1-smoke.db"))
    assert isinstance(store, SqliteReliabilityStore)

    event_id = store.insert_reliability_event(
        "run_failed",
        "warning",
        "real-smoke-signature",
        "real-smoke-occurrence",
        "test",
    )
    audit_id = store.create_audit("watch", utc_now_iso(), utc_now_iso())
    improvement_id = store.create_improvement("audit", "fix", "Real smoke")
    store.transition_improvement(
        improvement_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.AWAITING_HUMAN.value,
        "test",
    )
    company_id = store.create_org_unit("OmniAgentOS", "company")
    agent_id = store.create_agent("Smoke Agent", org_unit_id=company_id)
    store.create_agent_request("Create a smoke-test agent", from_agent_id="human:test")

    app.dependency_overrides[get_store] = lambda: store
    headers = {"X-Session-Token": session_value}
    tier_p = {
        **headers,
        "X-Autonomy-Token": "real-tier-p-token",
        "X-Omni-Authenticated-Principal": "owner",
    }
    get_paths = [
        "/api/reliability/summary",
        "/api/reliability/events",
        "/api/reliability/audits",
        f"/api/reliability/audits/{audit_id}",
        "/api/reliability/scorecards",
        "/api/improvements",
        f"/api/improvements/{improvement_id}",
        "/api/org/tree",
        "/api/org/agents",
        f"/api/org/agents/{agent_id}",
        f"/api/org/agents/{agent_id}/activity",
        "/api/org/agent-requests",
        "/api/autonomy",
    ]
    try:
        with TestClient(app) as client:
            for path in get_paths:
                response = client.get(path, headers=headers)
                assert response.status_code == 200, f"{path}: {response.text}"

            with patch("omniagentos.api.routes.improvements.subprocess.Popen") as popen:
                popen.return_value.pid = 9001
                approved = client.post(
                    f"/api/improvements/{improvement_id}/approve",
                    headers=tier_p,
                    # decided_by is free-text attribution (Round-4 repair); it
                    # need not equal the authenticated principal.
                    json={"decided_by": "owner"},
                )
            assert approved.status_code == 202, approved.text
            assert approved.json()["status"] == ImprovementStatus.APPROVED.value
            argv = popen.call_args.args[0]
            assert "--improvement" in argv
            assert argv[argv.index("--improvement") + 1] == improvement_id
            assert store.get_event(event_id).status == EventStatus.OPEN.value
    finally:
        app.dependency_overrides.clear()
        store._connection.close()
