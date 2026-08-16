"""Abstract and in-memory CAS stores for pure escalation cursors.

Durable schema and SQL intentionally belong to P1-RECOVERY-CURSOR.  This module
defines the persistence boundary now so the pure policy does not later need to
learn about a database.

Callers of three-valued ``get_cursor`` / ``cas_advance`` must handle ``None``
explicitly — bare truthiness collapsing miss with a falsey cursor is a defect.
Success returns a cursor; absence/conflict returns explicit ``None``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from omniagentos.routing.escalation import (
    DecisionKind,
    EscalationCursor,
    EscalationSkip,
    RouteIdentity,
    is_terminal,
)


class CursorAlreadyExists(ValueError):
    """An initial cursor was supplied for an existing scope."""


class InvalidCursorAdvance(ValueError):
    """A proposed cursor violates the store's monotonic invariants."""


@dataclass(frozen=True)
class AttemptIdentityRecord:
    """Shape reserved for a future escalation_attempt_identity row."""

    scope_id: str
    generation: int
    rung_index: int
    provider: str
    canonical_model: str
    decision_kind: str
    terminal: str | None
    policy_hash: str


class EscalationCursorStore(Protocol):
    """Storage interface a later durable implementation must satisfy."""

    def get_cursor(self, scope_id: str) -> EscalationCursor | None: ...

    def put_initial(self, scope_id: str, cursor: EscalationCursor) -> EscalationCursor: ...

    def cas_advance(
        self,
        scope_id: str,
        expected_generation: int,
        new_cursor: EscalationCursor,
    ) -> EscalationCursor | None: ...


def _scope(scope_id: str) -> str:
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ValueError("scope_id must be a non-empty string")
    return scope_id


def _validate_cursor_shape(cursor: EscalationCursor) -> None:
    if cursor.generation < 0:
        raise InvalidCursorAdvance("generation must be non-negative")
    if cursor.rung_index < 0:
        raise InvalidCursorAdvance("rung index must be non-negative")
    if not cursor.policy_hash:
        raise InvalidCursorAdvance("policy hash must be non-empty")
    if not cursor.visited:
        raise InvalidCursorAdvance("visited identities must not be empty")


def _identity_from_visited(cursor: EscalationCursor) -> RouteIdentity:
    if 0 <= cursor.rung_index < len(cursor.visited):
        return cursor.visited[cursor.rung_index]
    return cursor.visited[-1]


def _attempt_from_cursor(
    scope_id: str,
    cursor: EscalationCursor,
    *,
    is_initial: bool = False,
) -> AttemptIdentityRecord:
    identity = _identity_from_visited(cursor)
    if cursor.terminal is not None:
        decision_kind = str(cursor.terminal)
    elif is_initial or cursor.generation == 0:
        decision_kind = DecisionKind.INITIAL_ROUTE.value
    else:
        decision_kind = DecisionKind.ESCALATE.value
    terminal = str(cursor.terminal) if cursor.terminal is not None else None
    return AttemptIdentityRecord(
        scope_id=scope_id,
        generation=cursor.generation,
        rung_index=cursor.rung_index,
        provider=identity.provider,
        canonical_model=identity.canonical_model,
        decision_kind=decision_kind,
        terminal=terminal,
        policy_hash=cursor.policy_hash,
    )


class InMemoryEscalationCursorStore:
    """Thread-safe reference store with durable-store-compatible CAS rules.

    ``get_cursor`` and ``cas_advance`` are three-valued: success returns a
    cursor, absence/conflict returns explicit ``None``.  Callers must not use
    bare truthiness alone to mean "absent".
    """

    def __init__(self) -> None:
        self._cursors: dict[str, EscalationCursor] = {}
        self._transcripts: dict[str, list[AttemptIdentityRecord]] = {}
        self._lock = Lock()

    def get_cursor(self, scope_id: str) -> EscalationCursor | None:
        key = _scope(scope_id)
        with self._lock:
            return self._cursors.get(key)

    def put_initial(self, scope_id: str, cursor: EscalationCursor) -> EscalationCursor:
        key = _scope(scope_id)
        _validate_cursor_shape(cursor)
        with self._lock:
            if key in self._cursors:
                raise CursorAlreadyExists(f"cursor already exists for scope {key!r}")
            self._cursors[key] = cursor
            self._transcripts[key] = [
                _attempt_from_cursor(key, cursor, is_initial=True)
            ]
        return cursor

    def cas_advance(
        self,
        scope_id: str,
        expected_generation: int,
        new_cursor: EscalationCursor,
    ) -> EscalationCursor | None:
        key = _scope(scope_id)
        _validate_cursor_shape(new_cursor)
        with self._lock:
            current = self._cursors.get(key)
            if current is None or current.generation != expected_generation:
                return None
            if new_cursor.generation != expected_generation + 1:
                raise InvalidCursorAdvance("generation must advance by exactly one")
            if new_cursor.policy_hash != current.policy_hash:
                raise InvalidCursorAdvance("policy hash cannot change")
            if new_cursor.rung_index < current.rung_index:
                raise InvalidCursorAdvance("rung index cannot move backward")
            if new_cursor.visited[: len(current.visited)] != current.visited:
                raise InvalidCursorAdvance(
                    "settled route transcript must be append-only"
                )

            if is_terminal(current):
                # Terminal closed: only idempotent same-terminal reclose is ok.
                if new_cursor.terminal is None:
                    raise InvalidCursorAdvance(
                        "terminal cursor cannot advance or reopen"
                    )
                if new_cursor.terminal != current.terminal:
                    raise InvalidCursorAdvance(
                        "terminal cursor cannot change terminal kind"
                    )
            else:
                if new_cursor.visited[: len(current.visited)] != current.visited:
                    raise InvalidCursorAdvance(
                        "settled route transcript must be append-only"
                    )

            self._cursors[key] = new_cursor
            self._transcripts.setdefault(key, []).append(
                _attempt_from_cursor(key, new_cursor, is_initial=False)
            )
            return new_cursor

    def attempt_transcript(self, scope_id: str) -> list[AttemptIdentityRecord]:
        key = _scope(scope_id)
        with self._lock:
            rows = self._transcripts.get(key, [])
            return list(rows)


# Concise compatibility names.
MemoryEscalationCursorStore = InMemoryEscalationCursorStore
InMemoryEscalationStore = InMemoryEscalationCursorStore


def cursor_to_record(cursor: EscalationCursor) -> dict[str, object]:
    """Return the JSON-compatible shape reserved for a future durable row."""

    def identity_record(identity: RouteIdentity) -> dict[str, str]:
        return {
            "provider": identity.provider,
            "canonical_model": identity.canonical_model,
        }

    def skip_record(skip: EscalationSkip) -> dict[str, object]:
        return {
            "rung_index": skip.rung.index,
            "provider": skip.rung.provider,
            "display_model": skip.rung.display_model,
            "canonical_model": skip.rung.canonical_model,
            "reason": skip.reason,
        }

    return {
        "policy_hash": cursor.policy_hash,
        "rung_index": cursor.rung_index,
        "generation": cursor.generation,
        "terminal": str(cursor.terminal) if cursor.terminal is not None else None,
        "visited": [identity_record(identity) for identity in cursor.visited],
        "skip_reasons": [skip_record(skip) for skip in cursor.skip_reasons],
    }


def cursor_from_record(record: Mapping[str, object]) -> EscalationCursor:
    """Rehydrate the stable subset of the future persistence shape.

    Skip transcript rehydration is intentionally deferred because it embeds
    rung metadata owned by the policy definition; settled route identities are
    sufficient to continue safely without repeating an attempt.
    """

    raw_visited = record.get("visited")
    if not isinstance(raw_visited, list):
        raise ValueError("visited must be a list")
    visited: list[RouteIdentity] = []
    for item in raw_visited:
        if not isinstance(item, Mapping):
            raise ValueError("visited entries must be mappings")
        provider = item.get("provider")
        canonical_model = item.get("canonical_model")
        if not isinstance(provider, str) or not isinstance(canonical_model, str):
            raise ValueError("visited identity fields must be strings")
        visited.append(RouteIdentity(provider, canonical_model))

    raw_terminal = record.get("terminal")
    terminal = DecisionKind(raw_terminal) if isinstance(raw_terminal, str) else None

    def integer_field(name: str, default: int) -> int:
        raw = record.get(name, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError(f"{name} must be an integer")
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc

    cursor = EscalationCursor(
        policy_hash=str(record.get("policy_hash", "")),
        rung_index=integer_field("rung_index", -1),
        generation=integer_field("generation", -1),
        terminal=terminal,
        visited=tuple(visited),
    )
    _validate_cursor_shape(cursor)
    return cursor


# Alternate names used by migration-shape docs.
cursor_to_dict = cursor_to_record
cursor_from_dict = cursor_from_record
