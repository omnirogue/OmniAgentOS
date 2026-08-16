"""Pure, bounded recovery escalation policy.

WHY: retrying the current route and escalating to a genuinely different route
have different safety and review meaning.  This module keeps that distinction
explicit, identifies routes by provider plus canonical model (never a marketing
display name), and closes a finite ladder with a typed terminal decision.

The module deliberately performs no I/O and imports no execution, scheduler,
or database lane.  A later package may persist :class:`EscalationCursor`; the
policy itself remains a deterministic value transformation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MAX_LADDER_RUNGS = 256


class EscalationPolicyError(ValueError):
    """Base class for fail-closed policy input errors."""


class InvalidEscalationLadder(EscalationPolicyError):
    """The ladder cannot be evaluated safely."""


class InvalidEscalationCursor(EscalationPolicyError):
    """The cursor does not belong to, or cannot safely index, the ladder."""


class DecisionKind(StrEnum):
    """Typed outcomes from the recovery policy."""

    INITIAL_ROUTE = "initial_route"
    RETRY = "retry"
    ESCALATE = "escalate"
    ESCALATION_EXHAUSTED = "escalation_exhausted"
    INFRASTRUCTURE_EXHAUSTED = "infrastructure_exhausted"
    REVIEWER_UNAVAILABLE = "reviewer_unavailable"
    REVIEWER_RESULT = "reviewer_result"


TERMINAL_KINDS = frozenset(
    {
        DecisionKind.ESCALATION_EXHAUSTED,
        DecisionKind.INFRASTRUCTURE_EXHAUSTED,
    }
)


@dataclass(frozen=True, order=True)
class RouteIdentity:
    """Stable route identity; display-name aliases do not participate."""

    provider: str
    canonical_model: str


@dataclass(frozen=True)
class RouteRung:
    """One ordered route candidate in a finite escalation ladder."""

    index: int
    provider: str
    display_model: str
    canonical_model: str
    effort: str | None = None
    adapter: str | None = None
    name: str | None = None

    @property
    def identity(self) -> RouteIdentity:
        return RouteIdentity(self.provider, self.canonical_model)


@dataclass(frozen=True)
class EscalationLadder:
    """Validated, materialized policy definition."""

    rungs: tuple[RouteRung, ...]
    policy_hash: str
    version: str


@dataclass(frozen=True)
class EscalationSkip:
    """An ineligible forward rung and the deterministic reason it was skipped."""

    rung: RouteRung
    reason: str


@dataclass(frozen=True)
class EscalationCursor:
    """Restart-safe position prepared for a later durable CAS store."""

    policy_hash: str
    rung_index: int
    generation: int
    terminal: DecisionKind | None
    visited: tuple[RouteIdentity, ...]
    skip_reasons: tuple[EscalationSkip, ...] = ()


@dataclass(frozen=True)
class EscalationDecision:
    """One pure policy result."""

    kind: DecisionKind
    route: RouteRung | None
    cursor: EscalationCursor
    reason: str
    skips: tuple[EscalationSkip, ...] = ()
    verdict: str | None = None

    @property
    def is_verdict(self) -> bool:
        return self.kind is DecisionKind.REVIEWER_RESULT and self.verdict is not None


Availability = Collection[RouteIdentity] | Callable[[RouteIdentity], bool] | None


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidEscalationLadder(f"{field} must be a non-empty string")
    return value.strip()


def _normalized_model(value: object, field: str = "model") -> str:
    return _required_text(value, field).casefold()


def _normalized_provider(value: object) -> str:
    return _required_text(value, "provider").casefold()


def canonicalize_model(
    display: str,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """Return a normalized canonical model, resolving bounded alias chains.

    Alias keys are case-insensitive.  Chains are supported because registries
    often retain an old alias that points at a newer alias; cycles fail closed.
    """

    current = _normalized_model(display, "display_model")
    if aliases is None:
        return current
    if not isinstance(aliases, Mapping):
        raise InvalidEscalationLadder("aliases must be a mapping")

    normalized: dict[str, str] = {}
    for raw_alias, raw_target in aliases.items():
        alias = _normalized_model(raw_alias, "alias")
        normalized_target = _normalized_model(raw_target, f"alias target for {alias!r}")
        normalized[alias] = normalized_target

    seen: set[str] = set()
    # The number of distinct aliases is a hard upper bound on useful hops.
    for _ in range(len(normalized) + 1):
        target = normalized.get(current)
        if target is None:
            return current
        if current in seen:
            raise InvalidEscalationLadder("model alias cycle")
        seen.add(current)
        current = target
    raise InvalidEscalationLadder("model alias chain did not terminate")


def _rung_value(raw: RouteRung | Mapping[str, Any], field: str, default: Any = None) -> Any:
    if isinstance(raw, RouteRung):
        return getattr(raw, field)
    if isinstance(raw, Mapping):
        return raw.get(field, default)
    raise InvalidEscalationLadder("each rung must be RouteRung or a mapping")


def _normalize_rung(
    raw: RouteRung | Mapping[str, Any],
    *,
    position: int,
    aliases: Mapping[str, str] | None,
) -> RouteRung:
    raw_index = _rung_value(raw, "index", position)
    if isinstance(raw_index, bool) or not isinstance(raw_index, int):
        raise InvalidEscalationLadder("rung index must be an integer")
    if raw_index != position:
        raise InvalidEscalationLadder(
            f"rung index {raw_index} must equal its ladder position {position}"
        )

    provider = _normalized_provider(_rung_value(raw, "provider"))
    display_model = _required_text(_rung_value(raw, "display_model"), "display_model")
    explicit_canonical = _rung_value(raw, "canonical_model")
    alias_canonical = canonicalize_model(display_model, aliases)
    if aliases is not None and _normalized_model(display_model) in {
        _normalized_model(alias, "alias") for alias in aliases
    }:
        canonical_model = alias_canonical
    elif explicit_canonical is None:
        canonical_model = alias_canonical
    else:
        canonical_model = canonicalize_model(
            _required_text(explicit_canonical, "canonical_model"),
            aliases,
        )

    optional: dict[str, str | None] = {}
    for field in ("effort", "adapter", "name"):
        value = _rung_value(raw, field)
        if value is not None and not isinstance(value, str):
            raise InvalidEscalationLadder(f"{field} must be a string or None")
        optional[field] = value.strip() if isinstance(value, str) and value.strip() else None

    return RouteRung(
        index=position,
        provider=provider,
        display_model=display_model,
        canonical_model=canonical_model,
        effort=optional["effort"],
        adapter=optional["adapter"],
        name=optional["name"],
    )


def build_ladder(
    rungs: Sequence[RouteRung | Mapping[str, Any]],
    *,
    aliases: Mapping[str, str] | None = None,
    version: str = "1",
) -> EscalationLadder:
    """Validate and materialize a finite ladder with a stable SHA-256 hash."""

    if isinstance(rungs, (str, bytes)) or not isinstance(rungs, Sequence):
        raise InvalidEscalationLadder("rungs must be a finite sequence")
    count = len(rungs)
    if count == 0:
        raise InvalidEscalationLadder("escalation ladder must not be empty")
    if count > MAX_LADDER_RUNGS:
        raise InvalidEscalationLadder(
            f"escalation ladder exceeds the {MAX_LADDER_RUNGS}-rung bound"
        )
    policy_version = _required_text(version, "version")

    normalized = tuple(
        _normalize_rung(rungs[position], position=position, aliases=aliases)
        for position in range(count)
    )
    definition = {
        "version": policy_version,
        "rungs": [
            {
                "index": rung.index,
                "provider": rung.provider,
                "display_model": rung.display_model,
                "canonical_model": rung.canonical_model,
                "effort": rung.effort,
                "adapter": rung.adapter,
                "name": rung.name,
            }
            for rung in normalized
        ],
    }
    encoded = json.dumps(
        definition,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return EscalationLadder(
        rungs=normalized,
        policy_hash=hashlib.sha256(encoded).hexdigest(),
        version=policy_version,
    )


def is_terminal(cursor: EscalationCursor) -> bool:
    """Whether the cursor is irreversibly closed."""

    return cursor.terminal in TERMINAL_KINDS


def _validate_cursor(ladder: EscalationLadder, cursor: EscalationCursor) -> None:
    if cursor.policy_hash != ladder.policy_hash:
        raise InvalidEscalationCursor("cursor policy hash does not match ladder")
    if cursor.generation < 0:
        raise InvalidEscalationCursor("cursor generation must be non-negative")
    if not 0 <= cursor.rung_index < len(ladder.rungs):
        raise InvalidEscalationCursor("cursor rung index is outside ladder")
    if not cursor.visited:
        raise InvalidEscalationCursor("cursor must contain a settled route identity")
    if cursor.terminal is not None and cursor.terminal not in TERMINAL_KINDS:
        raise InvalidEscalationCursor("cursor terminal kind is invalid")
    current_identity = ladder.rungs[cursor.rung_index].identity
    if not is_terminal(cursor) and current_identity not in cursor.visited:
        raise InvalidEscalationCursor("active cursor route is not settled")


def initial_route(ladder: EscalationLadder) -> EscalationDecision:
    """Settle the first route without mislabeling initial selection as escalation."""

    first = ladder.rungs[0]
    cursor = EscalationCursor(
        policy_hash=ladder.policy_hash,
        rung_index=0,
        generation=0,
        terminal=None,
        visited=(first.identity,),
    )
    return EscalationDecision(
        kind=DecisionKind.INITIAL_ROUTE,
        route=first,
        cursor=cursor,
        reason="initial route selected",
    )


def _closed_terminal(cursor: EscalationCursor) -> EscalationDecision:
    assert cursor.terminal in TERMINAL_KINDS
    return EscalationDecision(
        kind=cursor.terminal,
        route=None,
        cursor=cursor,
        reason="terminal cursor is closed",
    )


def decide_retry(
    ladder: EscalationLadder,
    cursor: EscalationCursor,
    *,
    failure_class: str | None = None,
) -> EscalationDecision:
    """Retry the settled route without advancing rung or generation."""

    _validate_cursor(ladder, cursor)
    if is_terminal(cursor):
        return _closed_terminal(cursor)
    route = ladder.rungs[cursor.rung_index]
    detail = f" after {failure_class}" if failure_class else ""
    return EscalationDecision(
        kind=DecisionKind.RETRY,
        route=route,
        cursor=cursor,
        reason=f"retry current route{detail}",
    )


def _is_available(identity: RouteIdentity, available: Availability) -> bool:
    if available is None:
        return True
    if callable(available):
        return bool(available(identity))
    return identity in available


def decide_escalate(
    ladder: EscalationLadder,
    cursor: EscalationCursor,
    *,
    failure_class: str | None = None,
    excluded: set[RouteIdentity] | None = None,
    available: Availability = None,
) -> EscalationDecision:
    """Walk forward once to the next distinct eligible route, or close.

    The loop is bounded by the materialized ladder length and never revisits a
    lower rung.  Duplicate canonical identities, exclusions, and unavailable
    infrastructure are recorded rather than silently falling back.
    """

    _validate_cursor(ladder, cursor)
    if is_terminal(cursor):
        return _closed_terminal(cursor)

    excluded_identities = excluded or set()
    visited = set(cursor.visited)
    skips: list[EscalationSkip] = []
    otherwise_eligible = 0
    infrastructure_blocked = 0

    for index in range(cursor.rung_index + 1, len(ladder.rungs)):
        rung = ladder.rungs[index]
        identity = rung.identity
        if identity in visited:
            skips.append(EscalationSkip(rung, "already_visited_identity"))
            continue
        if identity in excluded_identities:
            skips.append(EscalationSkip(rung, "excluded_identity"))
            continue

        otherwise_eligible += 1
        if not _is_available(identity, available):
            infrastructure_blocked += 1
            skips.append(EscalationSkip(rung, "infrastructure_unavailable"))
            continue

        new_cursor = EscalationCursor(
            policy_hash=cursor.policy_hash,
            rung_index=index,
            generation=cursor.generation + 1,
            terminal=None,
            visited=cursor.visited + (identity,),
            skip_reasons=cursor.skip_reasons + tuple(skips),
        )
        detail = f" after {failure_class}" if failure_class else ""
        return EscalationDecision(
            kind=DecisionKind.ESCALATE,
            route=rung,
            cursor=new_cursor,
            reason=f"advanced to a distinct route identity{detail}",
            skips=tuple(skips),
        )

    terminal = (
        DecisionKind.INFRASTRUCTURE_EXHAUSTED
        if otherwise_eligible > 0 and infrastructure_blocked == otherwise_eligible
        else DecisionKind.ESCALATION_EXHAUSTED
    )
    terminal_cursor = EscalationCursor(
        policy_hash=cursor.policy_hash,
        rung_index=len(ladder.rungs) - 1,
        generation=cursor.generation + 1,
        terminal=terminal,
        visited=cursor.visited,
        skip_reasons=cursor.skip_reasons + tuple(skips),
    )
    return EscalationDecision(
        kind=terminal,
        route=None,
        cursor=terminal_cursor,
        reason=(
            "all otherwise eligible routes are infrastructure-unavailable"
            if terminal is DecisionKind.INFRASTRUCTURE_EXHAUSTED
            else "no distinct eligible escalation route remains"
        ),
        skips=tuple(skips),
    )


def decide_reviewer_result(
    ladder: EscalationLadder,
    cursor: EscalationCursor,
    *,
    available: bool,
    verdict: str | None,
) -> EscalationDecision:
    """Represent reviewer infrastructure absence as a non-verdict.

    An available reviewer may return a verdict (including ``None``); this
    function never manufactures approval from missing infrastructure or data.
    """

    _validate_cursor(ladder, cursor)
    if is_terminal(cursor):
        return _closed_terminal(cursor)
    if not available:
        return EscalationDecision(
            kind=DecisionKind.REVIEWER_UNAVAILABLE,
            route=ladder.rungs[cursor.rung_index],
            cursor=cursor,
            reason="reviewer infrastructure unavailable; no verdict",
            verdict=None,
        )
    return EscalationDecision(
        kind=DecisionKind.REVIEWER_RESULT,
        route=ladder.rungs[cursor.rung_index],
        cursor=cursor,
        reason="reviewer result recorded without policy reinterpretation",
        verdict=verdict,
    )
