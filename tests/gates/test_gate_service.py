from omniagentos.gates.service import GateService
from omniagentos.gates.types import GateId


def test_gate_decisions_have_a_complete_envelope() -> None:
    service = GateService(policy_version="2026.07")
    decision = service.g3_tool({"tool": "read"})

    assert decision.gate_id is GateId.G3
    assert decision.decision == "allow"
    assert decision.evidence == {"tool": "read"}
    assert decision.next_state
    assert decision.policy_version == "2026.07"


def test_every_exposed_gate_returns_a_decision() -> None:
    service = GateService()
    assert [
        service.g0_intake(),
        service.g2_dispatch(),
        service.g5_local_verify(),
        service.g6_independent_review(),
    ]
