from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.adapters.kimi_k3_api import FireworksKimiK3Adapter, MoonshotKimiK3Adapter
from omniagentos.adapters.provider_health_gate import (
    ACTION_TERMINAL,
    ProviderHealthGate,
    ProviderHealthGateError,
    snapshot_health,
)
from omniagentos.contracts import AgentInput, HealthStatus, ResultStatus
from omniagentos.db.store import SqliteStore


@pytest.mark.parametrize("provider", ["fireworks", "moonshot"])
def test_paid_provider_snapshot_rows_are_usable(tmp_path: Path, provider: str) -> None:
    path = tmp_path / "provider-health.json"
    path.write_text(
        json.dumps(
            {
                "results": {
                    "fireworks": {
                        "ok": True,
                        "outcome": "configured",
                    },
                    "moonshot": {
                        "ok": False,
                        "outcome": "unprobed",
                        "error": "no non-billing health probe is wired",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    status = snapshot_health(provider, path=path)

    assert isinstance(status, HealthStatus)
    assert "no valid" not in status.detail


def test_down_provider_skips_with_receipt_then_terminally_parks_at_five(tmp_path: Path) -> None:
    db_path = tmp_path / "health.sqlite3"
    gate = ProviderHealthGate(str(db_path))
    try:
        decisions = [
            gate.consult(
                job_id="nightly-k3",
                provider="fireworks",
                health_check=lambda: HealthStatus(healthy=False, detail="probe down"),
            )
            for _ in range(5)
        ]
        assert [decision.action for decision in decisions] == [
            "skip_with_receipt",
            "skip_with_receipt",
            "skip_with_receipt",
            "skip_with_receipt",
            "terminally_parked",
        ]
        assert [decision.consecutive_failures for decision in decisions] == [1, 2, 3, 4, 5]

        store = SqliteStore(str(db_path))
        try:
            rows = store.audit_events_for_target(
                target_type="scheduled_provider_job",
                target_id="nightly-k3:fireworks",
                action_prefix="provider_health.",
            )
            assert len(rows) == 5
            assert rows[0]["action"] == ACTION_TERMINAL
            payload = json.loads(rows[0]["payload_json"])
            assert payload["decision"] == "terminally_parked"
            assert payload["receipt_id"] == decisions[-1].receipt_id
        finally:
            store.close()
    finally:
        gate.close()


def test_healthy_consultation_resets_consecutive_failure_count(tmp_path: Path) -> None:
    gate = ProviderHealthGate(str(tmp_path / "health.sqlite3"))
    try:
        first = gate.consult(
            job_id="scheduled",
            provider="kimi",
            health_check=lambda: False,
        )
        healthy = gate.consult(
            job_id="scheduled",
            provider="kimi",
            health_check=lambda: True,
        )
        after = gate.consult(
            job_id="scheduled",
            provider="kimi",
            health_check=lambda: False,
        )
        assert first.consecutive_failures == 1
        assert healthy.action == "allow"
        assert healthy.consecutive_failures == 0
        assert after.consecutive_failures == 1
    finally:
        gate.close()


def test_terminal_park_is_sticky_and_does_not_recheck_health(tmp_path: Path) -> None:
    db_path = tmp_path / "health.sqlite3"
    gate = ProviderHealthGate(str(db_path))
    health_calls: list[bool] = []

    def down() -> bool:
        health_calls.append(True)
        return False

    try:
        terminal = None
        for _index in range(5):
            terminal = gate.consult(
                job_id="terminal-job",
                provider="fireworks",
                health_check=down,
            )
        assert terminal is not None
        assert terminal.action == "terminally_parked"

        def healthy() -> bool:
            health_calls.append(True)
            return True

        repeated = gate.consult(
            job_id="terminal-job",
            provider="fireworks",
            health_check=healthy,
        )
        assert repeated == terminal
        assert len(health_calls) == 5

        store = SqliteStore(str(db_path))
        try:
            rows = store.audit_events_for_target(
                target_type="scheduled_provider_job",
                target_id="terminal-job:fireworks",
                action_prefix="provider_health.",
            )
            assert len(rows) == 5
        finally:
            store.close()
    finally:
        gate.close()


def test_health_check_exception_is_down_and_receipted(tmp_path: Path) -> None:
    gate = ProviderHealthGate(str(tmp_path / "health.sqlite3"))

    def broken() -> bool:
        raise OSError("snapshot unreadable")

    try:
        decision = gate.consult(job_id="scheduled", provider="fireworks", health_check=broken)
        assert decision.action == "skip_with_receipt"
        assert "health check failed: OSError" in decision.detail
        assert decision.receipt_id.startswith("provider_health_receipt_")
    finally:
        gate.close()


def test_unwritable_receipt_ledger_fails_closed(tmp_path: Path) -> None:
    gate = ProviderHealthGate(str(tmp_path))
    with pytest.raises(ProviderHealthGateError, match="scheduled call is refused"):
        gate.consult(job_id="scheduled", provider="fireworks", health_check=lambda: True)


def test_scheduled_adapter_skips_before_http_with_health_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = ProviderHealthGate(str(tmp_path / "health.sqlite3"))
    adapter = FireworksKimiK3Adapter()
    monkeypatch.setattr(adapter, "provider_health_gate", lambda: gate)
    monkeypatch.setattr(
        adapter,
        "provider_health",
        lambda: HealthStatus(healthy=False, detail="sentinel says down"),
    )
    http_calls: list[bool] = []
    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: http_calls.append(True))
    try:
        result = adapter.run(
            AgentInput(
                run_id="run-scheduled",
                task_id="task-scheduled",
                prompt="work",
                model="kimi-k3",
                metadata={"scheduled_job_id": "nightly-k3"},
            )
        )
        assert result.status is ResultStatus.CANCELLED
        assert result.receipts[0].action == "skip_with_receipt"
        assert result.receipts[0].target == "nightly-k3:fireworks"
        assert http_calls == []
    finally:
        gate.close()
def test_moonshot_adapter_reads_its_own_snapshot_row_not_kimi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M5(health) cross-wire (2026-08-06 review), resolved by Blocker 1.

    Before the Blocker-1 rename, ``MoonshotKimiK3Adapter.name`` was
    ``"kimi"``, so ``adapter.provider_health()`` -> ``snapshot_health(self.name)``
    read the CLI Kimi doctor's row instead of the paid Moonshot snapshot row
    -- the ``moonshot`` row this lane's M6 fix added was dead code from the
    adapter's own perspective.
    """

    calls: list[str] = []

    def fake_snapshot_health(provider: str, *, path: Any = None) -> HealthStatus:
        calls.append(provider)
        return HealthStatus(healthy=True, detail="stub")

    monkeypatch.setattr(
        "omniagentos.adapters.provider_health_gate.snapshot_health",
        fake_snapshot_health,
    )

    adapter = MoonshotKimiK3Adapter()
    assert adapter.name == "moonshot"
    adapter.provider_health()
    assert calls == ["moonshot"]
