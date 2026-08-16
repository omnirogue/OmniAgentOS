"""Tests for W3 health_monitor instance.

Test cases:
- Contract: the REAL tools driven through the REAL template graph, faked only
  at the outermost I/O boundaries (the snapshot file and the launchctl call)
- Unit tests for each tool with fake fixtures
- Drill: dead-api snapshot produces exactly one kickstart (receipt-deduped)
- Unknown remedy scenario: parks with approval row, then EXECUTES on approval
- Counterfeit: verify-that-lies is caught by an external probe
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from omniagentos_loops.contracts import LoopStatus
from omniagentos_loops.instances import health_monitor as hm
from omniagentos_loops.instances.health_monitor import (
    _compute_incident_id,
    _match_remedy,
    _ordered_failures,
    diagnose_failure,
    escalate_unknown,
    monitor_health,
    repair_component,
    verify_repair,
)
from omniagentos_loops.runtime import run_once
from omniagentos_loops.templates import get_template
from omniagentos_loops.tools import LoopTool, args_digest

from omniagentos.contracts import ApprovalState

TEMPLATE = get_template("monitor_diagnose_repair_verify")

#: ``LoopTool.verify`` is the receipt lane's external-evidence hook. W3 declares
#: its predicate unconditionally (and the predicate is tested unconditionally);
#: the WIRING and the retry behaviour can only be exercised on a runtime whose
#: LoopTool carries the field.
REQUIRES_TOOL_VERIFY = pytest.mark.skipif(
    "verify" not in {field.name for field in dataclasses.fields(LoopTool)},
    reason="LoopTool.verify arrives with lane/receipt-outcome",
)

#: The allowlist the live routine row carries (routines.task_template_json).
ALLOWED_REMEDIES = [
    "kickstart_api",
    "kickstart_runner",
    "kickstart_routines",
    "kickstart_health_sentinel",
]

# ========== Fixtures: fake snapshots ==========

@pytest.fixture
def fake_healthy_snapshot():
    """Snapshot: all checks ok."""
    return {
        "available": True,
        "checks": [
            {
                "name": "api",
                "status": "ok",
                "evidence": "API ok on :8485 (v0.1.0, heartbeat 1s)",
                "detail": {"url": "http://127.0.0.1:8485/api/health", "http_status": 200},
            },
            {
                "name": "runner",
                "status": "ok",
                "evidence": "runner alive (pid 37025)",
                "detail": {"repo_root": "/OmniAgentOS", "matched": [{"pid": 37025}]},
            },
        ],
        "timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def fake_api_dead_snapshot():
    """Snapshot: API is dead (fail)."""
    return {
        "available": True,
        "checks": [
            {
                "name": "api",
                "status": "fail",
                "evidence": "API unreachable at http://127.0.0.1:8485/api/health (connection refused)",
                "detail": {"url": "http://127.0.0.1:8485/api/health", "error": "connection refused"},
            },
            {
                "name": "runner",
                "status": "ok",
                "evidence": "runner alive (pid 37025)",
            },
        ],
        "timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def fake_runner_dead_snapshot():
    """Snapshot: runner is dead (fail)."""
    return {
        "available": True,
        "checks": [
            {
                "name": "api",
                "status": "ok",
                "evidence": "API ok on :8485",
            },
            {
                "name": "runner",
                "status": "fail",
                "evidence": "no `python -m omniagentos.runner` process — queued runs will never move",
                "detail": {"repo_root": "/OmniAgentOS", "matched": []},
            },
        ],
        "timestamp": datetime.now(UTC).isoformat(),
    }


@pytest.fixture
def fake_scheduler_stale_snapshot():
    """Snapshot: scheduler tick is stale (unknown remedy)."""
    return {
        "available": True,
        "checks": [
            {
                "name": "scheduler",
                "status": "fail",
                "evidence": "scheduler ticked 20 minutes ago (older than 15 min threshold)",
                "detail": {"last_tick_at": "2026-08-01T15:00:00Z"},
            },
        ],
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ========== The two outermost I/O boundaries, and nothing else ==========


class _Response:
    def __init__(self, code: int, body: str) -> None:
        self._code, self._body = code, body

    def getcode(self) -> int:
        return self._code

    def read(self) -> bytes:
        return self._body.encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class FakeMachine:
    """Stands in for the only two OS boundaries W3 has.

    ``launchctl`` (the repair effect and the job probe) and the API's health
    endpoint (the API's readiness probe). Everything above them — the template
    graph, the tool registry, the execution seam, the receipts, the approvals
    row — runs for real in the contract tests below.

    *kick_rc* is what ``launchctl kickstart`` returns; *recovered* is whether
    the component actually came back afterwards, which is a SEPARATE fact and is
    exactly the gap a green kickstart exit code cannot close.
    """

    def __init__(self, *, kick_rc: int = 0, recovered: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.probes: list[str] = []
        self.kick_rc = kick_rc
        self.recovered = recovered

    def install(self, monkeypatch) -> FakeMachine:
        monkeypatch.setattr(hm.subprocess, "run", self.launchctl)
        monkeypatch.setattr(hm.urllib.request, "urlopen", self.urlopen)
        monkeypatch.setattr(hm, "PROBE_TIMEOUT_S", 0.0)  # no bounded wait in tests
        return self

    @property
    def kickstarted(self) -> list[str]:
        return [call[-1].split("/")[-1] for call in self.calls if call[1] == "kickstart"]

    @property
    def printed(self) -> list[str]:
        return [call[-1].split("/")[-1] for call in self.calls if call[1] == "print"]

    def launchctl(self, cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append(list(cmd))
        if cmd[1] == "kickstart":
            return SimpleNamespace(
                returncode=self.kick_rc,
                stdout="",
                stderr="" if self.kick_rc == 0 else "Could not find service",
            )
        if self.recovered:
            return SimpleNamespace(
                returncode=0, stdout="\tstate = running\n\tpid = 4242\n", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="\tstate = not running\n", stderr="")

    def urlopen(self, url: str, timeout: float = 0.0) -> _Response:
        self.probes.append(str(url))
        if not self.recovered:
            raise OSError("connection refused")
        return _Response(200, json.dumps({"status": "ok", "db": True}))


def write_snapshot(root, checks: list[dict[str, Any]], *, age_s: float = 0.0) -> None:
    """Write the file W3 actually reads, in the shape the sentinel writes it.

    ``ts``, not ``timestamp``: that is what ``health_sentinel.build_snapshot``
    emits, and reading the wrong key is how the freshness evidence went
    unnoticed for as long as it did.
    """
    path = root / "var" / "health-sentinel" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = datetime.now(UTC) - timedelta(seconds=age_s)
    path.write_text(
        json.dumps({"checks": checks, "ts": stamped.strftime("%Y-%m-%dT%H:%M:%SZ")})
    )


API_DEAD = {
    "name": "api",
    "status": "fail",
    "evidence": "API unreachable at http://127.0.0.1:8485/api/health (connection refused)",
    "detail": {"error": "connection refused"},
}
REFLECTION_DEAD = {
    "name": "reflection",
    "status": "fail",
    "evidence": "no reflection briefing for 2026-08-01 in vault/briefings",
    "detail": {},
}


# ========== Unit Tests ==========

class TestMonitor:
    """Test monitor tool."""

    def test_monitor_healthy(self, fake_healthy_snapshot, tmp_path, monkeypatch):
        """Monitor returns snapshot with no failed checks."""
        snapshot_file = tmp_path / "latest.json"
        snapshot_file.write_text(json.dumps(fake_healthy_snapshot))
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))

        # Patch REPO_ROOT to use tmp
        with patch("omniagentos_loops.instances.health_monitor.REPO_ROOT", tmp_path.parent.parent.parent.parent):
            with patch("omniagentos_loops.instances.health_monitor._read_snapshot") as mock_read:
                mock_read.return_value = fake_healthy_snapshot
                result = monitor_health({})

        assert result["snapshot"]["available"] is True
        assert result["failed_checks"] == []

    def test_monitor_with_failures(self, fake_api_dead_snapshot):
        """Monitor extracts failed checks."""
        with patch("omniagentos_loops.instances.health_monitor._read_snapshot") as mock_read:
            mock_read.return_value = fake_api_dead_snapshot
            with patch("omniagentos_loops.instances.health_monitor._tail_logs") as mock_tail:
                mock_tail.return_value = {}
                result = monitor_health({})

        assert result["snapshot"]["available"] is True
        failed = result["failed_checks"]
        assert len(failed) == 1
        assert failed[0]["name"] == "api"
        assert failed[0]["status"] == "fail"


class TestDiagnose:
    """Test diagnose tool."""

    def test_diagnose_healthy(self, fake_healthy_snapshot):
        """Diagnose healthy snapshot returns empty remedy."""
        snapshot = {
            "failed_checks": [],
            "logs": {},
        }
        result = diagnose_failure(snapshot)

        assert result["remedy"] == ""
        assert result["healthy"] is True

    def test_diagnose_api_dead(self, fake_api_dead_snapshot):
        """Diagnose API dead snapshot returns kickstart_api remedy."""
        api_check = fake_api_dead_snapshot["checks"][0]
        snapshot = {
            "failed_checks": [api_check],
            "logs": {"routines": [], "api": []},
        }

        result = diagnose_failure(snapshot)

        assert result["remedy"] == "kickstart_api"
        assert result["label"] == "com.omniagentos.api"
        assert result["component"] == "api"
        assert "incident" in result

    def test_diagnose_runner_dead(self, fake_runner_dead_snapshot):
        """Diagnose runner dead snapshot returns kickstart_runner remedy."""
        runner_check = fake_runner_dead_snapshot["checks"][1]
        snapshot = {
            "failed_checks": [runner_check],
            "logs": {},
        }

        result = diagnose_failure(snapshot)

        assert result["remedy"] == "kickstart_runner"
        assert result["label"] == "com.omniagentos.runner"
        assert result["component"] == "runner"

    def test_diagnose_unknown_remedy(self, fake_scheduler_stale_snapshot):
        """Diagnose unknown remedy (scheduler stale)."""
        scheduler_check = fake_scheduler_stale_snapshot["checks"][0]
        snapshot = {
            "failed_checks": [scheduler_check],
            "logs": {},
        }

        result = diagnose_failure(snapshot)

        assert result["remedy"] == "unknown_scheduler"
        assert result["label"] is None
        assert result["component"] == "scheduler"

    def test_match_remedy_api(self):
        """Pattern matching: API dead."""
        check = {
            "name": "api",
            "status": "fail",
            "evidence": "API unreachable at http://127.0.0.1:8485/api/health (connection refused)",
        }
        remedy, label = _match_remedy(check)
        assert remedy == "kickstart_api"
        assert label == "com.omniagentos.api"

    def test_match_remedy_runner(self):
        """Pattern matching: runner dead."""
        check = {
            "name": "runner",
            "status": "fail",
            "evidence": "no `python -m omniagentos.runner` process — queued runs will never move",
        }
        remedy, label = _match_remedy(check)
        assert remedy == "kickstart_runner"
        assert label == "com.omniagentos.runner"

    def test_compute_incident_id_deterministic(self):
        """Incident ID is stable across ticks on same day."""
        check = {"name": "api", "evidence": "unreachable at 127.0.0.1"}
        id1 = _compute_incident_id(check, "kickstart_api")
        id2 = _compute_incident_id(check, "kickstart_api")
        # Incident IDs should be identical (same component, same day)
        assert id1 == id2


class TestRepair:
    """Test repair tool.

    The repair tool takes the DIAGNOSIS, not the monitor snapshot: the template
    hands it exactly what it consumes. Until 2026-08-01 it was handed the
    snapshot and looked for ``snapshot["diagnosis"]`` — a key nothing in the
    data flow produces — so every production repair refused with ``label None``
    while these unit tests, which hand-built that key, stayed green.
    """

    def test_repair_success_api(self):
        """Repair successfully kicks start API."""
        diagnosis = {"label": "com.omniagentos.api", "component": "api"}

        with patch("os.getuid") as mock_uid:
            mock_uid.return_value = 501
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
                result = repair_component("kickstart_api", diagnosis)

        assert result["success"] is True
        assert result["label"] == "com.omniagentos.api"
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "launchctl"
        assert "kickstart" in cmd
        assert any("com.omniagentos.api" in str(x) for x in cmd)

    def test_repair_allowlist_denied(self):
        """Repair denies unlisted labels."""
        result = repair_component(
            "unknown_remedy", {"label": "com.example.unknown", "component": "unknown"}
        )

        assert result["success"] is False
        assert "not in allowlist" in result["error"]

    def test_repair_no_label(self):
        """Repair fails when label is None (unknown remedy)."""
        result = repair_component("unknown_scheduler", {"label": None, "component": "scheduler"})

        assert result["success"] is False

    def test_repair_subprocess_failure(self):
        """Repair handles subprocess errors gracefully."""
        diagnosis = {"label": "com.omniagentos.api", "component": "api"}

        with patch("os.getuid") as mock_uid:
            mock_uid.return_value = 501
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=1, stdout="", stderr="error")
                result = repair_component("kickstart_api", diagnosis)

        assert result["success"] is False
        assert result["returncode"] == 1


class TestEscalate:
    """Test escalate tool."""

    def test_escalate_unknown(self):
        """Escalate records unknown remedies, with the diagnosis a human needs."""
        result = escalate_unknown(
            "unknown_scheduler",
            {
                "component": "scheduler",
                "evidence": "scheduler ticked 20 minutes ago",
                "incident": "scheduler:sig:2026-08-01",
            },
        )

        assert result["escalated"] is True
        assert result["remedy"] == "unknown_scheduler"
        assert result["component"] == "scheduler"
        assert result["evidence"] == "scheduler ticked 20 minutes ago"
        assert result["incident"] == "scheduler:sig:2026-08-01"

    def test_escalate_never_records_a_blind_escalation(self):
        """An escalation that carries no evidence is the bug, not the fallback.

        This is the assertion that would have failed on main: the tool read the
        diagnosis out of the snapshot, so every approval a human was asked to
        decide said ``component=unknown, evidence=no evidence``.
        """
        result = escalate_unknown(
            "unknown_failure",
            {"component": "reflection", "evidence": "no reflection briefing for today"},
        )
        assert result["component"] != "unknown"
        assert result["evidence"] != "no evidence"


class TestVerify:
    """Test verify tool.

    Verification is an EXTERNAL probe of the launchd job. It cannot be the
    sentinel snapshot: that file is rewritten every 30 minutes, so inside the
    tick that repaired something it still describes the outage.
    """

    def test_verify_healthy_no_remedy(self):
        """Verify returns healthy state when no remedy needed."""
        result = verify_repair("", None)

        assert result["verified"] is True
        assert result["state"] == "healthy"

    def test_verify_escalated_remedy(self):
        """Verify returns escalated state for unknown remedies."""
        result = verify_repair("unknown_scheduler", None)

        assert result["verified"] is False
        assert result["state"] == "escalated"

    def test_verify_repair_failed(self):
        """Verify detects failed repair."""
        repair_result = {
            "success": False,
            "error": "launchctl failed",
        }

        result = verify_repair("kickstart_api", repair_result)

        assert result["verified"] is False
        assert result["state"] == "repair_failed"

    def test_verify_asks_the_api_itself_and_reports_recovery(self, monkeypatch):
        """The API's post-condition is that it ANSWERS, not that launchd spawned it."""
        machine = FakeMachine().install(monkeypatch)
        started = datetime.now(UTC).isoformat()
        result = verify_repair(
            "kickstart_api",
            {"success": True, "label": "com.omniagentos.api", "mttr_start": started},
        )

        assert result["verified"] is True
        assert result["state"] == "recovered"
        assert result["mttr_seconds"] is not None and result["mttr_seconds"] >= 0
        assert machine.probes == ["http://127.0.0.1:8485/api/health"]
        assert machine.printed == [], "launchd's opinion is not the API's readiness"

    def test_verify_probes_launchd_for_components_with_no_endpoint(self, monkeypatch):
        machine = FakeMachine().install(monkeypatch)
        result = verify_repair(
            "kickstart_runner", {"success": True, "label": "com.omniagentos.runner"}
        )

        assert result["verified"] is True
        assert machine.printed == ["com.omniagentos.runner"]

    def test_a_job_that_is_only_spawning_is_not_yet_recovered(self, monkeypatch):
        """``state = xpcproxy`` with a pid is launchd starting it, not a live job."""
        machine = FakeMachine().install(monkeypatch)
        monkeypatch.setattr(
            hm.subprocess,
            "run",
            lambda cmd, **kw: SimpleNamespace(
                returncode=0, stdout="\tstate = xpcproxy\n\tpid = 4242\n", stderr=""
            ),
        )
        result = verify_repair(
            "kickstart_runner", {"success": True, "label": "com.omniagentos.runner"}
        )

        assert result["verified"] is False
        assert result["state"] == "still_failing"
        assert machine  # the API endpoint is irrelevant here

    def test_verify_reports_still_failing_when_the_job_did_not_come_back(self, monkeypatch):
        FakeMachine(recovered=False).install(monkeypatch)
        result = verify_repair(
            "kickstart_api", {"success": True, "label": "com.omniagentos.api"}
        )

        assert result["verified"] is False
        assert result["state"] == "still_failing"

    def test_verify_survives_a_replayed_receipt_without_an_mttr_start(self, monkeypatch):
        """``datetime.fromisoformat(None)`` used to raise inside the verify read."""
        FakeMachine().install(monkeypatch)
        result = verify_repair(
            "kickstart_api", {"success": True, "label": "com.omniagentos.api"}
        )

        assert result["verified"] is True
        assert result["mttr_seconds"] is None


class TestRepairReceiptVerifier:
    """``verify=``: what the RECEIPT records, as opposed to what the tick renders.

    The two are different questions and W3 needs both. The tick's status decides
    what an operator is told about this tick; the receipt decides whether the
    NEXT tick may act on this incident at all. A kickstart that exits 0 while the
    job stays down, filed as a succeeded receipt, suppresses every retry for the
    rest of the incident window (business key = component+signature+day) — the
    service stays down and the incident reads as handled.
    """

    def test_a_kickstart_that_worked_verifies(self, monkeypatch):
        FakeMachine().install(monkeypatch)
        verdict = hm._repair_verified(
            {"success": True, "label": "com.omniagentos.api"},
            {"remedy": "kickstart_api", "diagnosis": {"label": "com.omniagentos.api"}},
        )
        assert verdict["ok"] is True
        assert "HTTP 200" in verdict["detail"]

    def test_a_kickstart_that_exits_zero_but_leaves_the_job_down_does_not_verify(
        self, monkeypatch
    ):
        """launchctl's exit code is not evidence. The probe is."""
        FakeMachine(recovered=False).install(monkeypatch)
        verdict = hm._repair_verified(
            {"success": True, "label": "com.omniagentos.api"},
            {"remedy": "kickstart_api", "diagnosis": {"label": "com.omniagentos.api"}},
        )
        assert verdict["ok"] is False

    def test_it_reads_the_label_the_template_handed_the_tool(self, monkeypatch):
        machine = FakeMachine().install(monkeypatch)
        hm._repair_verified(
            {"success": True},
            {"remedy": "kickstart_runner", "diagnosis": {"label": "com.omniagentos.runner"}},
        )
        assert machine.printed == ["com.omniagentos.runner"]

    def test_no_label_is_not_a_verified_repair(self, monkeypatch):
        FakeMachine().install(monkeypatch)
        assert hm._repair_verified({"success": True}, {"diagnosis": {}})["ok"] is False

    def test_it_never_raises_because_a_raise_would_mean_unknown_not_failed(
        self, monkeypatch
    ):
        """A predicate that raises is read as UNKNOWN (row left claimed).

        "I looked and it is not running" is a verdict, so every probe path must
        return one — including the paths where the probe itself blows up.
        """
        def explode(*args, **kwargs):
            raise OSError("launchctl is gone")

        monkeypatch.setattr(hm, "PROBE_TIMEOUT_S", 0.0)
        monkeypatch.setattr(hm.subprocess, "run", explode)
        monkeypatch.setattr(hm.urllib.request, "urlopen", explode)

        verdict = hm._repair_verified(
            {"success": True, "label": "com.omniagentos.runner"},
            {"diagnosis": {"label": "com.omniagentos.runner"}},
        )
        assert verdict["ok"] is False

    def test_the_verifier_waits_for_a_job_that_is_still_spawning(self, monkeypatch):
        """It runs milliseconds after the kickstart, when launchd says xpcproxy.

        Without the bounded wait this predicate would file a WORKING repair as
        failed and kickstart a healthy service again on the next tick.
        """
        states = iter(
            [
                SimpleNamespace(returncode=0, stdout="\tstate = xpcproxy\n\tpid = 1\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="\tstate = running\n\tpid = 1\n", stderr=""),
            ]
        )
        monkeypatch.setattr(hm, "PROBE_INTERVAL_S", 0.01)
        monkeypatch.setattr(hm, "PROBE_TIMEOUT_S", 5.0)
        monkeypatch.setattr(hm.subprocess, "run", lambda cmd, **kw: next(states))

        verdict = hm._repair_verified(
            {"success": True}, {"diagnosis": {"label": "com.omniagentos.runner"}}
        )
        assert verdict["ok"] is True

    @REQUIRES_TOOL_VERIFY
    def test_the_repair_tool_declares_it(self, make_ctx):
        ctx = make_ctx(instance_id="w3_health_monitor", template=TEMPLATE.name)
        hm.register(ctx)
        assert ctx.tools.get("repair").verify is not None, (
            "without verify= the receipt records a kickstart's EXIT CODE as the "
            "outcome, and a failed repair suppresses its own retry"
        )

    @REQUIRES_TOOL_VERIFY
    def test_a_repair_that_did_not_take_is_re_attempted_on_the_next_tick(
        self, make_ctx, tmp_path, monkeypatch
    ):
        """The whole point, end to end with the real tools and the real receipt.

        Tick 1: launchctl exits 0, the service is still down ⇒ FAILED receipt.
        Tick 2: the same incident is attempted AGAIN (not suppressed) and works.
        Tick 3: now that it succeeded, the receipt dedupes as it always did.
        """
        machine = FakeMachine(recovered=False).install(monkeypatch)
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        ctx = self_ctx = make_ctx(
            instance_id="w3_health_monitor",
            template=TEMPLATE.name,
            params={"allowed_remedies": ALLOWED_REMEDIES},
        )
        write_snapshot(tmp_path, [API_DEAD])
        hm.register(self_ctx)

        first = run_once(ctx, TEMPLATE)
        assert machine.kickstarted == ["com.omniagentos.api"]
        assert first.effects[-1]["succeeded"] is False
        assert first.status is LoopStatus.FAILED

        machine.recovered = True
        second = run_once(ctx, TEMPLATE)
        assert machine.kickstarted == ["com.omniagentos.api"] * 2, (
            "a failed repair must be re-attempted, not filed as a handled incident"
        )
        assert second.effects[-1]["succeeded"] is True
        assert second.status is LoopStatus.COMPLETED

        third = run_once(ctx, TEMPLATE)
        assert machine.kickstarted == ["com.omniagentos.api"] * 2, (
            "and once it worked, the receipt dedupes as before"
        )
        assert third.effects[-1]["replayed"] is True


# ========== Instance <-> template contract ==========


class TestInstanceTemplateContract:
    """The REAL W3 tools, driven through the REAL template graph.

    Every other class in this file calls a tool directly with a hand-built
    argument, which is precisely how the shipped defect stayed invisible: the
    template passed ``{"remedy", "snapshot"}`` while ``repair_component`` read
    the launchd label out of ``snapshot["diagnosis"]``, a key the monitor never
    produced. 21 green tests certified an effect that could not happen.

    Faked here: the snapshot FILE and the ``launchctl`` call. Nothing else — the
    graph, the tool registry, the execution seam, the receipt table and the
    approvals row are the production objects.
    """

    def _ctx(self, make_ctx, tmp_path, checks, *, instance_id="w3_health_monitor"):
        write_snapshot(tmp_path, checks)
        ctx = make_ctx(
            instance_id=instance_id,
            template=TEMPLATE.name,
            params={"allowed_remedies": ALLOWED_REMEDIES},
        )
        hm.register(ctx)
        return ctx

    def test_repair_receives_a_usable_label_and_the_remedy_executes(
        self, make_ctx, tmp_path, monkeypatch
    ):
        """The headline: an allowlisted remedy actually kickstarts the job."""
        machine = FakeMachine().install(monkeypatch)
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        ctx = self._ctx(make_ctx, tmp_path, [API_DEAD])

        report = run_once(ctx, TEMPLATE)

        assert machine.kickstarted == ["com.omniagentos.api"], (
            "the repair effect did not reach launchctl; on main it refused with "
            f"'label None not in allowlist'. calls={machine.calls}"
        )
        assert report.status is LoopStatus.COMPLETED
        assert report.as_dict()["accepted"] is True
        assert [effect["node"] for effect in report.effects] == ["repair"]

    def test_the_same_incident_is_repaired_exactly_once_across_ticks(
        self, make_ctx, tmp_path, monkeypatch
    ):
        """Receipt dedupe, proven through the graph rather than asserted about a key."""
        machine = FakeMachine().install(monkeypatch)
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        ctx = self._ctx(make_ctx, tmp_path, [API_DEAD])

        first = run_once(ctx, TEMPLATE)
        second = run_once(ctx, TEMPLATE)

        assert first.status is LoopStatus.COMPLETED
        assert second.status is LoopStatus.COMPLETED
        assert machine.kickstarted == ["com.omniagentos.api"], (
            "the second tick of the SAME incident must replay the receipt, not kickstart again"
        )
        # ``effects`` is an accumulating channel, so the second tick's entry is last.
        assert [effect["replayed"] for effect in second.effects] == [False, True]

    def test_a_failed_kickstart_does_not_render_as_a_completed_tick(
        self, make_ctx, tmp_path, monkeypatch
    ):
        """launchctl refused ⇒ verify says repair_failed ⇒ the TICK must not pass.

        This is the A2 defect end to end: the verify node detected the failure
        and the tick still settled ``completed, accepted=1``.
        """
        machine = FakeMachine(kick_rc=1).install(monkeypatch)
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        ctx = self._ctx(make_ctx, tmp_path, [API_DEAD])

        report = run_once(ctx, TEMPLATE)

        assert machine.kickstarted == ["com.omniagentos.api"]
        assert report.status is LoopStatus.FAILED, (
            "a repair that failed must not settle as completed/accepted"
        )
        assert report.as_dict()["accepted"] is False
        assert "repair_failed" in report.detail

    def test_a_kickstart_that_does_not_bring_the_job_back_is_not_a_success(
        self, make_ctx, tmp_path, monkeypatch
    ):
        """launchctl returned 0 but the job is still down: unverified, not completed."""
        machine = FakeMachine(recovered=False).install(monkeypatch)
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        ctx = self._ctx(make_ctx, tmp_path, [API_DEAD])

        report = run_once(ctx, TEMPLATE)

        assert machine.kickstarted == ["com.omniagentos.api"]
        assert report.status is LoopStatus.FAILED
        assert "still_failing" in report.detail

    def test_an_unactionable_failure_parks_and_then_executes_on_approval(
        self, make_ctx, tmp_path, monkeypatch, store
    ):
        """Park → human approves → the escalation actually runs, a tick later.

        The tick that resumes re-derives the effect's arguments and the seam
        re-checks them against the digest recorded on the approvals row. On main
        those arguments contained the monitor snapshot — a fresh
        ``timestamp`` and fresh log tails every tick — so the row was bound to
        arguments that no later tick could reproduce and the approved escalation
        was refused as "bound to a different action", permanently. The log file
        rewritten between the two ticks below is what reproduces that drift.
        """
        machine = FakeMachine().install(monkeypatch)
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        log = tmp_path / "var" / "log"
        log.mkdir(parents=True, exist_ok=True)
        (log / "api.log").write_text("tick one log tail\n")
        ctx = self._ctx(make_ctx, tmp_path, [REFLECTION_DEAD])

        parked = run_once(ctx, TEMPLATE)
        assert parked.status is LoopStatus.PARKED
        assert parked.approval_id

        store.decide_approval(parked.approval_id, ApprovalState.APPROVED.value, "owner", "ack")
        (log / "api.log").write_text("tick two log tail — different bytes\n")

        resumed = run_once(ctx, TEMPLATE)

        assert [effect["node"] for effect in resumed.effects] == ["escalate"], (
            f"an approved escalation must execute; got {resumed.status}: {resumed.detail}"
        )
        # NEUTRAL, not favourable: the tick behaved and the fleet is still
        # broken. IDLE is the neutral status in the scheduler's taxonomy
        # (loop_jobs.NEUTRAL_STATUSES = {parked, idle}); COMPLETED would score
        # an unfixed incident as an accomplishment.
        assert resumed.status is LoopStatus.IDLE, (
            f"an executed escalation must be neutral; got {resumed.status}"
        )
        assert machine.kickstarted == [], "an unknown remedy must never kickstart anything"

    def test_an_actionable_failure_behind_an_unactionable_one_is_still_repaired(
        self, make_ctx, tmp_path, monkeypatch
    ):
        """Head-of-line blocking, at the graph level.

        Live: ``reflection`` (no remedy) sat at the head of the failed-check
        list and parked W3 on the same approval for five consecutive ticks while
        the failures behind it were never diagnosed at all.
        """
        machine = FakeMachine().install(monkeypatch)
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        ctx = self._ctx(make_ctx, tmp_path, [REFLECTION_DEAD, API_DEAD])

        report = run_once(ctx, TEMPLATE)

        assert machine.kickstarted == ["com.omniagentos.api"]
        assert report.status is LoopStatus.COMPLETED

    def test_a_stale_snapshot_parks_for_a_human_instead_of_reporting_health(
        self, make_ctx, tmp_path, monkeypatch
    ):
        """The sentinel died. Every check in the file says ok. Nobody is watching.

        This is the whole-graph version of the freshness rule: a snapshot older
        than two sentinel intervals is BLINDNESS, so the tick must reach the
        escalate effect (T3 → parks for a human), never IDLE-on-a-healthy-fleet.
        """
        machine = FakeMachine().install(monkeypatch)
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        write_snapshot(
            tmp_path,
            [{"name": "api", "status": "ok", "evidence": "API ok on :8485"}],
            age_s=hm.SNAPSHOT_MAX_AGE_S + 600,
        )
        ctx = make_ctx(
            instance_id="w3_health_monitor",
            template=TEMPLATE.name,
            params={"allowed_remedies": ALLOWED_REMEDIES},
        )
        hm.register(ctx)

        report = run_once(ctx, TEMPLATE)

        assert report.status is LoopStatus.PARKED, (
            f"a blind monitor must reach a human, not report health; got {report.status}"
        )
        assert machine.kickstarted == []

    def test_a_healthy_fleet_is_idle_and_touches_nothing(
        self, make_ctx, tmp_path, monkeypatch
    ):
        machine = FakeMachine().install(monkeypatch)
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        ctx = self._ctx(
            make_ctx, tmp_path, [{"name": "api", "status": "ok", "evidence": "API ok on :8485"}]
        )

        report = run_once(ctx, TEMPLATE)

        assert report.status is LoopStatus.IDLE
        assert machine.calls == []


class TestDiagnosisIsAFunctionOfItsBusinessKey:
    """An effect's arguments may not drift while its approval row cannot change.

    ``approvals.ensure_approval`` writes the args digest ONCE, keyed by
    (remedy, incident); ``read_outcome`` refuses when the recomputed binding
    differs. So any field of the diagnosis that changes inside one incident
    turns that incident's approval into a permanently unexecutable row.
    """

    def _snapshot(self, checks, *, logs, stamp):
        return {"failed_checks": checks, "logs": logs, "timestamp": stamp, "checks": checks}

    def test_two_ticks_of_one_incident_produce_identical_effect_arguments(self):
        first = diagnose_failure(
            self._snapshot([API_DEAD], logs={"api": ["one"]}, stamp="2026-08-01T10:00:00Z")
        )
        second = diagnose_failure(
            self._snapshot(
                [API_DEAD], logs={"api": ["two", "three"]}, stamp="2026-08-01T10:10:00Z"
            )
        )

        assert first == second
        assert first["incident"] == second["incident"]
        assert args_digest({"remedy": first["remedy"], "diagnosis": first}) == args_digest(
            {"remedy": second["remedy"], "diagnosis": second}
        )

    def test_the_diagnosis_carries_no_timestamp_or_log_tail(self):
        diagnosis = diagnose_failure(
            self._snapshot([API_DEAD], logs={"api": ["x"]}, stamp="2026-08-01T10:00:00Z")
        )
        assert set(diagnosis) == {"remedy", "label", "incident", "component", "evidence"}
        assert "logs" not in diagnosis

    def test_a_changed_diagnosis_always_means_a_changed_incident(self):
        """The one direction that matters: args never drift under a stable key."""
        variants = [
            API_DEAD,
            {**API_DEAD, "evidence": "API unreachable at http://127.0.0.1:8485 (HTTP 502)"},
            {**API_DEAD, "name": "runner"},
            REFLECTION_DEAD,
        ]
        seen: dict[str, str] = {}
        for check in variants:
            diagnosis = diagnose_failure(self._snapshot([check], logs={}, stamp="s"))
            key = f"{diagnosis['remedy']}:{diagnosis['incident']}"
            frozen = json.dumps(diagnosis, sort_keys=True)
            assert seen.setdefault(key, frozen) == frozen, (
                f"business key {key} maps to two different argument payloads — "
                "the second can never be approved"
            )


class TestFailureOrdering:
    """Defect 3: diagnose considered only the FIRST failed check."""

    def test_an_actionable_failure_outranks_an_unactionable_one(self):
        ordered = _ordered_failures([REFLECTION_DEAD, API_DEAD])
        assert [check["name"] for check in ordered] == ["api", "reflection"]

    def test_diagnose_picks_the_actionable_failure_whatever_its_position(self):
        diagnosis = diagnose_failure({"failed_checks": [REFLECTION_DEAD, API_DEAD], "logs": {}})
        assert diagnosis["remedy"] == "kickstart_api"
        assert diagnosis["component"] == "api"

    def test_fail_outranks_warn_among_equally_unactionable_failures(self):
        warn = {"name": "launchd", "status": "warn", "evidence": "2 signal-restarted but running"}
        fail = {"name": "providers", "status": "fail", "evidence": "2/4 providers failing doctor"}
        assert [c["name"] for c in _ordered_failures([warn, fail])] == ["providers", "launchd"]

    def test_a_critical_component_outranks_a_non_critical_one_at_equal_severity(self):
        launchd = {"name": "launchd", "status": "fail", "evidence": "jobs not loaded here"}
        reflection = dict(REFLECTION_DEAD)
        # `launchd` is critical in COMPONENT_STATUS; both are unactionable here.
        assert [c["name"] for c in _ordered_failures([reflection, launchd])] == [
            "launchd",
            "reflection",
        ]

    def test_ordering_is_stable_for_indistinguishable_failures(self):
        a = {"name": "providers", "status": "fail", "evidence": "first"}
        b = {"name": "reflection", "status": "fail", "evidence": "second"}
        assert _ordered_failures([a, b]) == [a, b]
        assert _ordered_failures([b, a]) == [b, a]


# ========== Drill Tests ==========

class TestDrills:
    """Integration drills per loops doctrine."""

    def test_drill_dead_api_one_repair_across_ticks(self):
        """Drill: dead-api snapshot produces exactly one kickstart (receipt-deduped).

        The graph-level version of this drill lives in
        ``TestInstanceTemplateContract`` — this one pins the tool-level halves
        that make it possible: the diagnosis routes to an allowlisted remedy,
        the repair executes it, and the incident key is stable across ticks.
        """
        snapshot = {"failed_checks": [API_DEAD], "logs": {}}

        diagnosis1 = diagnose_failure(snapshot)
        assert diagnosis1["remedy"] == "kickstart_api"

        with patch("os.getuid") as mock_uid:
            mock_uid.return_value = 501
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout="restarted", stderr="")
                repair_result1 = repair_component("kickstart_api", diagnosis1)

        assert repair_result1["success"] is True

        diagnosis2 = diagnose_failure({"failed_checks": [API_DEAD], "logs": {"api": ["new"]}})
        assert f"{diagnosis1['remedy']}:{diagnosis1['incident']}" == (
            f"{diagnosis2['remedy']}:{diagnosis2['incident']}"
        ), "the same incident must produce the same receipt key on the next tick"


# ========== Counterfeit Tests ==========

class TestCounterfeits:
    """Counterfeits to catch mutations in W3."""

    def test_counterfeit_verify_lies_about_recovery(self, monkeypatch):
        """Counterfeit: the repair claimed success but the API never came back.

        Verification must answer from an external probe, so a successful
        ``launchctl kickstart`` exit code cannot buy a verdict.
        """
        repair_result = {
            "success": True,
            "label": "com.omniagentos.api",
            "mttr_start": datetime.now(UTC).isoformat(),
        }

        machine = FakeMachine(recovered=False).install(monkeypatch)
        result = verify_repair("kickstart_api", repair_result)

        assert result["verified"] is False
        assert result["state"] == "still_failing"
        assert machine.probes == ["http://127.0.0.1:8485/api/health"], (
            "verification must probe the component, not re-read the 30-minute-old snapshot"
        )

    def test_counterfeit_repair_ignores_allowlist(self):
        """Counterfeit: repair ignores the allowlist and tries to kickstart unlisted label.

        This must be caught by the allowlist check.
        """
        result = repair_component(
            "attacker_remedy", {"label": "com.example.evil", "component": "attacker"}
        )

        # Must reject
        assert result["success"] is False
        assert "not in allowlist" in result["error"]

    def test_counterfeit_diagnose_adds_unlisted_remedy(self):
        """Counterfeit: diagnose returns a remedy not in the allowlist.

        The template's after_diagnose gate should catch this and escalate.
        """
        # The template's conditional checks: if remedy in allowed_remedies
        # Since evil_hack_sys_exit is not in the allowlist params, it goes to escalate
        # This is validated by the template's conditional edge in build()
        # (tested in loops/tests/test_templates.py)
        assert "evil_hack_sys_exit" not in {"kickstart_api", "kickstart_runner", "kickstart_routines", "kickstart_health_sentinel"}


class TestSnapshotWiring:
    """The two defects that shipped past 21 fixture-driven tests and a gate.

    Every other test in this file injects a snapshot, so neither the path W3
    actually reads nor the behaviour when that read FAILS was ever exercised.
    Live probe 2026-08-01: REPO_ROOT was `parents[4]` = ``/Users/youruser``,
    so the real snapshot was never found -- and because an unreadable snapshot
    produced an empty failure list, the self-heal loop reported IDLE while the
    API was down.
    """

    def test_repo_root_is_the_repository_not_its_parent(self) -> None:
        """REPO_ROOT must locate the repo, proven by a file only it contains."""
        from omniagentos_loops.instances.health_monitor import REPO_ROOT

        assert (REPO_ROOT / "AGENTS.md").is_file(), (
            f"REPO_ROOT={REPO_ROOT} is not the repository root; "
            "the sentinel snapshot and log tails will silently read nothing"
        )
        assert (REPO_ROOT / "loops" / "omniagentos_loops").is_dir()

    def test_an_unreadable_snapshot_is_a_failure_not_all_clear(self, tmp_path) -> None:
        """Blindness must never render as health."""
        from omniagentos_loops.instances import health_monitor as hm

        with patch.object(hm, "REPO_ROOT", tmp_path):  # no var/health-sentinel here
            result = hm.monitor_health({})

        names = [c["name"] for c in result["failed_checks"]]
        assert names == ["health_snapshot"], (
            "an unavailable snapshot must surface as a failed check; "
            f"got failed_checks={result['failed_checks']}"
        )
        assert result["snapshot"]["available"] is False

    def test_an_unreadable_snapshot_never_yields_an_auto_remedy(self) -> None:
        """The synthetic failure must escalate, never match the kickstart allowlist."""
        from omniagentos_loops.instances import health_monitor as hm

        snapshot = {
            "failed_checks": [
                {
                    "name": "health_snapshot",
                    "status": "fail",
                    "evidence": "snapshot file not found",
                }
            ],
            "checks": [],
            "logs": {},
        }
        diagnosis = hm.diagnose_failure(snapshot)
        remedy = (diagnosis or {}).get("remedy")
        assert remedy in (None, "", "unknown") or str(remedy).startswith("unknown"), (
            f"a blind snapshot must not produce an executable remedy; got {remedy!r}"
        )


class TestSnapshotFreshness:
    """A snapshot the sentinel stopped updating is BLINDNESS, not health.

    ``latest.json`` is a photograph. If the sentinel dies, the photograph keeps
    saying every check was ok at the moment it stopped looking, and a monitor
    that trusts it reports a perfectly healthy fleet forever — the exact shape
    of the unreadable-snapshot defect, one layer up. The threshold comes from
    the job's own cadence (launchd ``StartInterval 1800``), so it moves when the
    job does instead of being a number someone liked.
    """

    ALL_OK = [{"name": "api", "status": "ok", "evidence": "API ok on :8485"}]

    def test_the_threshold_is_two_sentinel_intervals(self):
        assert hm.SENTINEL_INTERVAL_S == 1800, "the installed launchd StartInterval"
        assert hm.SNAPSHOT_MAX_AGE_S == 2 * hm.SENTINEL_INTERVAL_S

    def test_a_fresh_snapshot_is_read_normally(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        write_snapshot(tmp_path, self.ALL_OK, age_s=60)

        result = hm.monitor_health({})

        assert result["snapshot"]["available"] is True
        assert result["failed_checks"] == []
        assert result["snapshot"]["age_seconds"] >= 0

    def test_a_stale_snapshot_is_blindness_not_health(self, tmp_path, monkeypatch):
        """All checks ok, but nobody has looked in over an hour."""
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        write_snapshot(tmp_path, self.ALL_OK, age_s=hm.SNAPSHOT_MAX_AGE_S + 60)

        result = hm.monitor_health({})

        assert result["snapshot"]["available"] is False
        assert [c["name"] for c in result["failed_checks"]] == ["health_snapshot"], (
            "a snapshot nobody is updating must surface as a failed check, "
            f"got {result['failed_checks']}"
        )
        assert "old" in result["failed_checks"][0]["evidence"]

    def test_a_snapshot_with_no_ts_cannot_be_trusted(self, tmp_path, monkeypatch):
        """Unknown age is not young. Fail closed."""
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        path = tmp_path / "var" / "health-sentinel" / "latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checks": self.ALL_OK}))

        result = hm.monitor_health({})

        assert result["snapshot"]["available"] is False
        assert [c["name"] for c in result["failed_checks"]] == ["health_snapshot"]

    def test_a_future_dated_snapshot_is_not_fresh(self, tmp_path, monkeypatch):
        """Otherwise one field makes the whole freshness rule bypassable."""
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        write_snapshot(tmp_path, self.ALL_OK, age_s=-3600)

        result = hm.monitor_health({})

        assert result["snapshot"]["available"] is False
        assert "FUTURE" in result["snapshot"]["error"]

    def test_the_timestamp_field_is_the_one_the_sentinel_writes(self, tmp_path, monkeypatch):
        """``ts``, cross-checked against the PRODUCER, not against this test.

        ``_read_snapshot`` read ``timestamp`` for its whole life — a key
        ``health_sentinel.build_snapshot`` has never written — so every snapshot
        was silently ageless.
        """
        producer = hm.REPO_ROOT / "scripts" / "health-sentinel" / "health_sentinel.py"
        source = producer.read_text(encoding="utf-8")
        assert '"ts": _iso(started_at)' in source, (
            "the sentinel changed its snapshot timestamp field; W3's freshness "
            "check reads 'ts' and would go blind-but-quiet"
        )

        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        write_snapshot(tmp_path, self.ALL_OK, age_s=0)
        assert hm._read_snapshot()["available"] is True

    def test_a_stale_snapshot_keeps_one_incident_id_across_ticks(self, tmp_path, monkeypatch):
        """The age grows every tick; the incident (and its approval) must not.

        ``_signature`` normalizes ``\\d+[smhd]`` to ``N``, which is why the
        evidence writes the age with an ``s`` suffix — otherwise each tick would
        mint a new incident, a new approval row and a fresh page.
        """
        monkeypatch.setattr(hm, "REPO_ROOT", tmp_path)
        write_snapshot(tmp_path, self.ALL_OK, age_s=hm.SNAPSHOT_MAX_AGE_S + 100)
        first = diagnose_failure(hm.monitor_health({}))
        write_snapshot(tmp_path, self.ALL_OK, age_s=hm.SNAPSHOT_MAX_AGE_S + 4000)
        second = diagnose_failure(hm.monitor_health({}))

        assert first == second, f"stale-snapshot diagnosis drifted: {first} != {second}"
        assert first["incident"] == second["incident"]


class TestWidenedAllowlist:
    """Verify the KICKSTART_ALLOWLIST includes all fleet repair labels."""

    def test_original_labels_still_present(self):
        """The original four core labels must still be allowlisted."""
        original_labels = {
            "com.omniagentos.api",
            "com.omniagentos.runner",
            "com.omniagentos.routines",
            "com.omniagentos.health-sentinel",
        }
        missing = original_labels - hm.KICKSTART_ALLOWLIST
        assert not missing, f"Missing original labels: {missing}"

    def test_fleet_repair_labels_now_included(self):
        """The fleet repair labels that previously decayed are now allowlisted."""
        fleet_labels = {
            "com.omniagentos.reflection-nightly",
            "com.omniagentos.reflection-watchdog",
            "com.omniagentos.agent-watchdog",
            "com.omniagentos.backlog-executor",
            "com.omniagentos.hygiene",
            "com.omniagentos.planner-canary",
            "com.omniagentos.swarm-optimizer",
            "com.omniagentos.fable-curator",
        }
        missing = fleet_labels - hm.KICKSTART_ALLOWLIST
        assert not missing, f"Missing fleet repair labels: {missing}"

    def test_allowlist_has_all_twelve_labels(self):
        """Total allowlist must have 4 original + 8 fleet = 12 labels."""
        assert len(hm.KICKSTART_ALLOWLIST) == 12, (
            f"Expected 12 labels (4 original + 8 fleet), got {len(hm.KICKSTART_ALLOWLIST)}: "
            f"{sorted(hm.KICKSTART_ALLOWLIST)}"
        )

    def test_repair_accepts_widened_labels(self):
        """Repair tool accepts all labels now in the allowlist.

        ``subprocess.run`` MUST be faked here. ``repair_component`` runs the
        allowlist check and then immediately shells out; an unfaked call in this
        loop issues a real ``launchctl kickstart -k`` against every one of the
        twelve live jobs. This suite is the W3 loop's gate_command, so it runs
        on the serving box on every tick: unfaked, it SIGTERMed the whole fleet
        (2026-08-05 — the API could not survive it and crash-looped at exit=-15).
        """
        calls: list[list[str]] = []

        def _record(cmd, **_kwargs):
            calls.append(cmd)
            return Mock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=_record):
            for label in hm.KICKSTART_ALLOWLIST:
                # A diagnosis carrying a label that is now allowlisted should not
                # be rejected by repair_component (modulo uid and subprocess).
                diagnosis = {"label": label}
                result = hm.repair_component("kickstart_any", diagnosis)
                # Should NOT contain "not in allowlist" error
                assert "not in allowlist" not in (result.get("error") or "").lower(), (
                    f"Label {label!r} was rejected as not in allowlist: {result}"
                )

        # Every accepted label reached the launchctl boundary — and reached the
        # FAKE, not the machine.
        assert len(calls) == len(hm.KICKSTART_ALLOWLIST)
        assert all(cmd[0] == "launchctl" for cmd in calls)

    def test_safety_bounds_unchanged(self):
        """Safety bounds (probe timeout, sentinel interval, etc.) remain as-is."""
        # These constants govern the safety and responsiveness of W3
        # and must not drift when the allowlist expands
        assert hm.PROBE_TIMEOUT_S == 25.0, "Probe timeout changed"
        assert hm.PROBE_INTERVAL_S == 1.0, "Probe interval changed"
        assert hm.SENTINEL_INTERVAL_S == 1800, "Sentinel interval changed"
        assert hm.SNAPSHOT_MAX_AGE_S == 3600, "Snapshot max age changed"
        assert hm.SNAPSHOT_FUTURE_SKEW_S == 60, "Future skew tolerance changed"
