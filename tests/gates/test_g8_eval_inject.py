"""G8 /gates/eval injects GrantsStore and fails closed on dead grant_id."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.db.migrate import migrate


def test_gates_eval_g8_denies_dead_grant(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "g.db"
    migrate(str(db))
    monkeypatch.setenv("OMNIAGENTOS_DB", str(db))
    # default_db_path may still point elsewhere; patch GrantsStore inject path via env
    # used by default_db_path if it reads OMNIAGENTOS_DB
    from omniagentos import contracts

    monkeypatch.setattr(contracts, "default_db_path", lambda: str(db))

    client = TestClient(app)
    res = client.post(
        "/api/grok/gates/eval",
        json={
            "gate": "g8",
            "evidence": {"consequential": True, "grant_id": "gnt_dead_missing"},
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["decision"] == "deny"
    assert body["evidence"].get("reason") in {
        "grant_not_found",
        "grant_lookup_unavailable",
        "grant_not_live",
    }


def test_g8_service_denies_without_store() -> None:
    from omniagentos.gates.service import GateService

    d = GateService().g8_release({"consequential": True, "grant_id": "anything"})
    assert d.decision == "deny"
    assert d.evidence.get("reason") == "grant_lookup_unavailable"
