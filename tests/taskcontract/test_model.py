"""TaskContract hash, JSON round-trip, validation."""

from __future__ import annotations

import pytest

from omniagentos.taskcontract import (
    AcceptanceCriterion,
    Budgets,
    LeaseFields,
    RiskClass,
    TaskContract,
    TaskContractError,
)


def _contract(**overrides: object) -> TaskContract:
    base = dict(
        objective="Ship fan-in package",
        acceptance_criteria=(
            AcceptanceCriterion(id="AC-1", condition="tests pass", evidence_required=True),
            AcceptanceCriterion(id="AC-2", condition="typed APIs", evidence_required=False),
        ),
        read_set=("omniagentos/fanin/",),
        write_set=("omniagentos/fanin/", "tests/fanin/"),
        risk_class=RiskClass.R1,
        budgets=Budgets(max_tokens=10_000, max_cost_usd=2.5),
        lease=LeaseFields(
            lease_id="L1", holder="worker-a", expires_at="2026-07-25T12:00:00Z", generation=3
        ),
        task_id="tsk_1",
        lineage_id="lin_1",
        version=1,
    )
    base.update(overrides)
    return TaskContract(**base)  # type: ignore[arg-type]


def test_json_roundtrip() -> None:
    c1 = _contract()
    c2 = TaskContract.from_json(c1.to_json())
    assert c2.objective == c1.objective
    assert c2.acceptance_criteria == c1.acceptance_criteria
    assert c2.read_set == c1.read_set
    assert c2.write_set == c1.write_set
    assert c2.risk_class is RiskClass.R1
    assert c2.budgets.max_tokens == 10_000
    assert c2.lease.holder == "worker-a"
    assert c1.contract_hash() == c2.contract_hash()


def test_contract_hash_stable_and_independent_of_lease() -> None:
    c1 = _contract()
    c2 = _contract(
        lease=LeaseFields(
            lease_id="L2",
            holder="worker-b",
            expires_at="2099-01-01T00:00:00Z",
            generation=99,
        )
    )
    assert c1.contract_hash() == c2.contract_hash()
    c3 = _contract(objective="Ship something else")
    assert c1.contract_hash() != c3.contract_hash()


def test_dict_roundtrip_and_risk() -> None:
    c = _contract(risk_class=RiskClass.R3)
    data = c.to_dict()
    assert data["risk_class"] == "R3"
    assert data["acceptance_criteria"][0]["evidence_required"] is True
    restored = TaskContract.from_dict(data)
    assert restored.risk_class is RiskClass.R3


def test_validation_errors() -> None:
    with pytest.raises(TaskContractError, match="objective"):
        TaskContract.from_dict(
            {
                "objective": "  ",
                "acceptance_criteria": [{"id": "a", "condition": "ok"}],
                "risk_class": "R0",
            }
        )
    with pytest.raises(TaskContractError, match="criterion"):
        TaskContract.from_dict({"objective": "x", "acceptance_criteria": [], "risk_class": "R0"})
    with pytest.raises(TaskContractError, match="unique"):
        TaskContract.from_dict(
            {
                "objective": "x",
                "acceptance_criteria": [
                    {"id": "a", "condition": "one"},
                    {"id": "a", "condition": "two"},
                ],
                "risk_class": "R0",
            }
        )
    with pytest.raises(TaskContractError, match="risk_class"):
        TaskContract.from_dict(
            {
                "objective": "x",
                "acceptance_criteria": [{"id": "a", "condition": "ok"}],
                "risk_class": "R9",
            }
        )


def test_path_sets_drop_blanks_preserve_order() -> None:
    c = TaskContract.from_dict(
        {
            "objective": "x",
            "acceptance_criteria": [{"id": "a", "condition": "ok"}],
            "risk_class": "R0",
            "read_set": [" a ", "", "b"],
            "write_set": ["w1", "  ", "w2"],
        }
    )
    assert c.read_set == ("a", "b")
    assert c.write_set == ("w1", "w2")


def test_new_fields_roundtrip() -> None:
    c = TaskContract(
        objective="Ship it with new parameters",
        acceptance_criteria=(AcceptanceCriterion(id="AC-1", condition="tests pass"),),
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R1,
        out_of_scope_paths=("out/path1", "out/path2"),
        non_goals=("no hacky code", "no database drop"),
        security_requirements=("no credential exposure",),
        performance_requirements=("runs under 500ms",),
        retry_limit=3,
        escalation_path="escalate_to_owner",
        handoff_role="senior_steward",
    )
    data = c.to_dict()
    assert data["out_of_scope_paths"] == ["out/path1", "out/path2"]
    assert data["non_goals"] == ["no hacky code", "no database drop"]
    assert data["security_requirements"] == ["no credential exposure"]
    assert data["performance_requirements"] == ["runs under 500ms"]
    assert data["retry_limit"] == 3
    assert data["escalation_path"] == "escalate_to_owner"
    assert data["handoff_role"] == "senior_steward"

    restored = TaskContract.from_dict(data)
    assert restored.out_of_scope_paths == ("out/path1", "out/path2")
    assert restored.non_goals == ("no hacky code", "no database drop")
    assert restored.security_requirements == ("no credential exposure",)
    assert restored.performance_requirements == ("runs under 500ms",)
    assert restored.retry_limit == 3
    assert restored.escalation_path == "escalate_to_owner"
    assert restored.handoff_role == "senior_steward"


def test_contract_hash_backwards_compatible_with_literal_pin() -> None:
    # Build a contract with none of the new fields set.
    c = TaskContract(
        objective="Verify stable hash",
        acceptance_criteria=(AcceptanceCriterion(id="AC-1", condition="tests pass"),),
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R0,
    )
    # Ensure to_dict() does not contain any of the seven new keys
    data = c.to_dict()
    for key in [
        "out_of_scope_paths",
        "non_goals",
        "security_requirements",
        "performance_requirements",
        "retry_limit",
        "escalation_path",
        "handoff_role",
    ]:
        assert key not in data

    # Pin the exact SHA-256 of the hash payload as a literal. This value was
    # computed by running this very fixture against the code as it stood BEFORE
    # the seven optional fields were added -- a genuine backwards-compatibility
    # pin, not a value back-fitted to the current implementation.
    #
    # It matters because contract_hash() is an identity check: TaskContractStore
    # enforces UNIQUE(contract_hash, lane, ref_id) and validate_transition raises
    # "contract_hash mismatch" when a stored hash does not match a recomputed
    # one. Contracts already persisted in real databases were hashed without
    # these keys, so emitting them unconditionally would have orphaned every
    # existing row. If this assertion fails, the hashed payload changed and old
    # contracts can no longer transition.
    expected_hash = "0d7514ff86ed9995bd674d40997907014b348d8461d349dc3cbc9978a3e18001"
    assert c.contract_hash() == expected_hash
