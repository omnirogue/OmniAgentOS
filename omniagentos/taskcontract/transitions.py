"""Authoritative TaskContract state-machine transitions (M-16).

TaskContract is the validated intent object; this module defines the
persisted lifecycle states and legal transitions used across supported lanes
(swarm, runner, longhaul, sessions). Ledger identity is the contract_hash.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from omniagentos.taskcontract.models import TaskContractError

__all__ = [
    "ContractState",
    "SUPPORTED_LANES",
    "can_transition",
    "validate_transition",
]


class ContractState(StrEnum):
    """Persisted lifecycle of an adopted TaskContract."""

    DRAFT = "draft"
    ADOPTED = "adopted"
    ACTIVE = "active"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    CLOSED = "closed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


# Legal directed edges. Unknown transitions fail closed.
_ALLOWED: dict[ContractState, frozenset[ContractState]] = {
    ContractState.DRAFT: frozenset(
        {ContractState.ADOPTED, ContractState.REJECTED, ContractState.SUPERSEDED}
    ),
    ContractState.ADOPTED: frozenset(
        {
            ContractState.ACTIVE,
            ContractState.REJECTED,
            ContractState.SUPERSEDED,
            ContractState.CLOSED,
        }
    ),
    ContractState.ACTIVE: frozenset(
        {
            ContractState.VERIFYING,
            ContractState.VERIFIED,
            ContractState.REJECTED,
            ContractState.SUPERSEDED,
            ContractState.CLOSED,
        }
    ),
    ContractState.VERIFYING: frozenset(
        {
            ContractState.VERIFIED,
            ContractState.ACTIVE,  # re-open for repair under same contract
            ContractState.REJECTED,
            ContractState.CLOSED,
        }
    ),
    ContractState.VERIFIED: frozenset({ContractState.CLOSED, ContractState.SUPERSEDED}),
    ContractState.CLOSED: frozenset(),
    ContractState.REJECTED: frozenset({ContractState.SUPERSEDED}),
    ContractState.SUPERSEDED: frozenset(),
}

SUPPORTED_LANES: frozenset[str] = frozenset(
    {
        "swarm",
        "runner",
        "longhaul",
        "session",
        "sessions",
        "intake",
        "graph",
        "test",
    }
)


def can_transition(from_state: str | ContractState, to_state: str | ContractState) -> bool:
    """Return True when ``from_state → to_state`` is a legal transition."""
    try:
        src = ContractState(str(from_state))
        dst = ContractState(str(to_state))
    except ValueError:
        return False
    if src == dst:
        return True  # idempotent no-op
    return dst in _ALLOWED.get(src, frozenset())


def validate_transition(
    from_state: str | ContractState,
    to_state: str | ContractState,
    *,
    contract_hash: str | None = None,
    expected_hash: str | None = None,
) -> ContractState:
    """Validate and return the destination state, or raise TaskContractError.

    When both hashes are supplied they must match — this is the ledger
    identity/adoption check that prevents adopting a mutated contract body
    under a previously recorded hash.
    """
    try:
        src = ContractState(str(from_state))
        dst = ContractState(str(to_state))
    except ValueError as exc:
        raise TaskContractError(
            f"invalid contract state transition {from_state!r} → {to_state!r}"
        ) from exc

    if expected_hash is not None and contract_hash is not None:
        if str(expected_hash) != str(contract_hash):
            raise TaskContractError(
                "contract_hash mismatch: adoption/transition identity check failed"
            )

    if src == dst:
        return dst
    if dst not in _ALLOWED.get(src, frozenset()):
        raise TaskContractError(f"illegal TaskContract transition {src.value} → {dst.value}")
    return dst


def assert_supported_lane(lane: str) -> str:
    text = str(lane or "").strip().lower()
    if not text:
        raise TaskContractError("lane must be non-empty")
    if text not in SUPPORTED_LANES:
        raise TaskContractError(f"unsupported TaskContract lane: {lane!r}")
    return text


def transition_table() -> Mapping[str, tuple[str, ...]]:
    """Stable view of the transition graph for tests and operators."""
    return {src.value: tuple(sorted(d.value for d in dsts)) for src, dsts in _ALLOWED.items()}
