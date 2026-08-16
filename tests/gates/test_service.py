"""GateService envelope + G8 release grant checks."""

from __future__ import annotations

from omniagentos.gates.service import DEFAULT_POLICY_VERSION, GateService
from omniagentos.gates.types import GateId


def test_default_policy_version_is_grok_gates() -> None:
    service = GateService()
    assert service.policy_version == "grok-gates/1"
    assert DEFAULT_POLICY_VERSION == "grok-gates/1"
    decision = service.g0_intake()
    assert decision.policy_version == "grok-gates/1"


def test_gate_decisions_have_a_complete_envelope() -> None:
    service = GateService(policy_version="2026.07")
    decision = service.g3_tool({"tool": "read"})

    assert decision.gate_id is GateId.G3
    assert decision.decision == "allow"
    assert decision.evidence == {"tool": "read"}
    assert decision.next_state
    assert decision.policy_version == "2026.07"


def test_g0_through_g6_return_decisions() -> None:
    service = GateService()
    decisions = [
        service.g0_intake(),
        service.g1_plan(),
        service.g2_dispatch(),
        service.g3_tool(),
        service.g4_budget(),
        service.g5_local_verify(),
        service.g6_independent_review(),
    ]
    assert [d.gate_id for d in decisions] == [
        GateId.G0,
        GateId.G1,
        GateId.G2,
        GateId.G3,
        GateId.G4,
        GateId.G5,
        GateId.G6,
    ]
    assert all(d.decision == "allow" for d in decisions)
    assert all(d.policy_version == "grok-gates/1" for d in decisions)
    assert [d.next_state for d in decisions] == [
        "intake_accepted",
        "plan_accepted",
        "dispatched",
        "tool_authorized",
        "budget_ok",
        "locally_verified",
        "independent_reviewed",
    ]


def test_g8_release_denies_grant_id_without_store() -> None:
    """Bare grant_id is not proof — consequential requires live lookup (fail closed)."""
    service = GateService()
    decision = service.g8_release({"consequential": True, "grant_id": "grn_1"})
    assert decision.gate_id is GateId.G8
    assert decision.decision == "deny"
    assert decision.next_state == "release_blocked"
    assert decision.evidence.get("reason") == "grant_lookup_unavailable"


def test_g8_release_allows_with_live_grant_store() -> None:
    import tempfile
    from pathlib import Path

    from omniagentos.db.migrate import migrate
    from omniagentos.db.store import SqliteStore
    from omniagentos.grants import GrantsStore

    service = GateService()
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "g.db")
        migrate(db)
        gs = GrantsStore(SqliteStore(db))
        # Without a real grant row, must deny
        d = service.g8_release({"consequential": True, "grant_id": "missing", "grant_store": gs})
        assert d.decision == "deny"
        assert d.evidence.get("reason") == "grant_not_found"


def test_g8_release_denies_consequential_without_grant_id() -> None:
    service = GateService()
    decision = service.g8_release({"consequential": True})
    assert decision.decision == "deny"
    assert decision.next_state == "release_blocked"
    assert decision.evidence.get("reason") == "missing_grant_id"


def test_g8_release_allows_non_consequential_without_grant_id() -> None:
    service = GateService()
    decision = service.g8_release({"consequential": False})
    assert decision.decision == "allow"
    assert decision.next_state == "released"


def test_g8_release_treats_send_kind_as_consequential() -> None:
    service = GateService()
    denied = service.g8_release({"kind": "send"})
    assert denied.decision == "deny"
    # grant_id without store is also deny (fail closed)
    denied2 = service.g8_release({"kind": "send", "grant_id": "grn_9"})
    assert denied2.decision == "deny"
    assert denied2.evidence.get("reason") == "grant_lookup_unavailable"
