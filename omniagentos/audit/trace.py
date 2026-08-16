"""Trace audit rule engine — ten independent pure rules over a fetched segment.

§10.3 posture (applied identically inside every rule):

1. Observed violation → FAIL (a declared gap never launders this).
2. Evidence missing + covering gap → INCONCLUSIVE (``<category>.trace_gap``).
3. Evidence missing + complete trace → FAIL (``<category>.no_evidence`` or more specific).
4. Otherwise PASS, citing the supporting event ids.

This module is pure: stdlib + ``omniagentos.contracts`` only. No DB, no I/O,
no scheduler, no swarm.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from omniagentos.contracts import Events

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class TraceGap:
    """A declared incompleteness in the trace that blinds selected categories."""

    kind: str
    detail: str
    categories: frozenset[str] = frozenset()
    # empty categories → blinds *all* audit categories


@dataclass(frozen=True)
class TraceSegment:
    """Already-fetched event rows plus any declared gaps."""

    events: tuple[Mapping[str, Any], ...]
    gaps: tuple[TraceGap, ...] = ()

    @classmethod
    def coerce(cls, segment: TraceSegment | Sequence[Mapping[str, Any]]) -> TraceSegment:
        if isinstance(segment, TraceSegment):
            return segment
        return cls(events=tuple(segment), gaps=())

    def blinds(self, category: str) -> TraceGap | None:
        """Return the first gap that covers ``category`` (empty set = all)."""
        for gap in self.gaps:
            if not gap.categories or category in gap.categories:
                return gap
        return None


@dataclass(frozen=True)
class Expectations:
    """What the trace is audited against. Partial construction is intentional."""

    risk_class: str = ""
    expected_topology: str = ""
    expected_model: str = ""
    expected_prompt_digests: Mapping[str, str] = field(default_factory=dict)
    required_tools: frozenset[str] = frozenset()
    prohibited_tools: frozenset[str] = frozenset()
    required_context_ids: frozenset[str] = frozenset()
    hard_timeout_s: float | None = None
    idle_timeout_s: float | None = None
    write_set: frozenset[str] = frozenset()
    max_retries: int | None = None
    worker_actor: str = ""
    cost_budget: float | None = None
    latency_budget_s: float | None = None


@dataclass(frozen=True)
class CategoryResult:
    category: str
    verdict: Verdict
    code: str
    detail: str
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AuditReport:
    verdict: Verdict
    categories: tuple[CategoryResult, ...]

    @property
    def accepted(self) -> bool:
        # Fail-closed: INCONCLUSIVE is NOT accepted.
        return self.verdict is Verdict.PASS

    def by_category(self, name: str) -> CategoryResult:
        for result in self.categories:
            if result.category == name:
                return result
        raise KeyError(name)


# ---------------------------------------------------------------------------
# Private helpers (payload, ids, posture)
# ---------------------------------------------------------------------------


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Decode ``payload_json`` whether it arrives as a string or a mapping.

    Never raises. Unparseable input yields ``{}``.
    """
    raw = event.get("payload_json", event.get("payload", {}))
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        return {}
    return {}


def _event_id(event: Mapping[str, Any]) -> int | None:
    raw = event.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _ids(*events: Mapping[str, Any]) -> tuple[int, ...]:
    out: list[int] = []
    for event in events:
        eid = _event_id(event)
        if eid is not None:
            out.append(eid)
    return tuple(out)


def _action(event: Mapping[str, Any]) -> str:
    return str(event.get("action") or "").lower()


def _actor(event: Mapping[str, Any]) -> str:
    return str(event.get("actor") or "")


def _action_has(event: Mapping[str, Any], *needles: str) -> bool:
    action = _action(event)
    return all(n in action for n in needles)


def _action_has_any(event: Mapping[str, Any], *needles: str) -> bool:
    action = _action(event)
    return any(n in action for n in needles)


def _result(
    category: str,
    verdict: Verdict,
    code: str,
    detail: str,
    event_ids: tuple[int, ...] = (),
) -> CategoryResult:
    if verdict is not Verdict.PASS and not code:
        code = f"{category}.unspecified"
    return CategoryResult(
        category=category,
        verdict=verdict,
        code=code if verdict is not Verdict.PASS else (code or ""),
        detail=detail,
        event_ids=event_ids,
    )


def _pass(
    category: str,
    detail: str,
    event_ids: tuple[int, ...] = (),
) -> CategoryResult:
    return _result(category, Verdict.PASS, "", detail, event_ids)


def _fail(
    category: str,
    code: str,
    detail: str,
    event_ids: tuple[int, ...] = (),
) -> CategoryResult:
    return _result(category, Verdict.FAIL, code, detail, event_ids)


def _missing(
    segment: TraceSegment,
    category: str,
    code: str,
    detail: str,
    event_ids: tuple[int, ...] = (),
) -> CategoryResult:
    """Evidence missing: gap → INCONCLUSIVE; complete → FAIL."""
    gap = segment.blinds(category)
    if gap is not None:
        return _result(
            category,
            Verdict.INCONCLUSIVE,
            f"{category}.trace_gap",
            f"declared gap kind={gap.kind}: {gap.detail}",
            event_ids,
        )
    return _fail(category, code, detail, event_ids)


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Accept trailing Z.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _path_components(path: str) -> tuple[str, ...]:
    cleaned = path.strip().replace("\\", "/").strip("/")
    if not cleaned:
        return ()
    return tuple(part for part in cleaned.split("/") if part and part != ".")


def _path_covered(path: str, write_set: frozenset[str]) -> bool:
    """Prefix match on normalized path components: ``a/b`` covers ``a/b/c``, not ``a/bc``."""
    target = _path_components(path)
    if not target:
        return False
    for allowed in write_set:
        prefix = _path_components(allowed)
        if not prefix:
            continue
        if len(target) >= len(prefix) and target[: len(prefix)] == prefix:
            return True
    return False


def _tools_from_payload(payload: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for key in ("tools", "assigned_tools", "used_tools", "tool_names"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            found.add(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                if isinstance(item, str) and item:
                    found.add(item)
                elif isinstance(item, Mapping):
                    name = item.get("name") or item.get("tool") or item.get("id")
                    if isinstance(name, str) and name:
                        found.add(name)
    for key in ("tool", "tool_name", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value and key != "name":
            found.add(value)
        elif key == "name" and isinstance(value, str) and value and "tool" in str(payload):
            found.add(value)
    return found


def _failure_evidence(payload: Mapping[str, Any]) -> bool:
    """True when retry payload carries non-empty failure/error/evidence/reason."""
    for key in ("failure", "error", "evidence", "reason"):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, Mapping) and value:
            return True
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) > 0:
            return True
    return False


def _claim_has_reference(payload: Mapping[str, Any]) -> bool:
    for key in ("artifact_id", "artifact", "evidence_event_id", "evidence"):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if isinstance(value, Mapping) and value:
            return True
    return False


# Events vocabulary is imported for callers/tests that stamp rows with
# Events.AUDIT; rules themselves match on action + payload, not type.
AUDIT_EVENT_TYPE: str = Events.AUDIT


# ---------------------------------------------------------------------------
# Rules — each independent; never call another rule
# ---------------------------------------------------------------------------


def rule_routing(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "routing"

    decisions = [
        e
        for e in segment.events
        if _action_has_any(e, "routing", "route")
        and any(k in _payload(e) for k in ("topology", "model", "risk_class"))
    ]
    dispatches = [e for e in segment.events if _action_has_any(e, "dispatch", "spawn")]

    # Observed mismatches first (even with a covering gap).
    if decisions and dispatches:
        decision = decisions[0]
        d_payload = _payload(decision)
        for dispatch in dispatches:
            p = _payload(dispatch)
            cited = _ids(decision, dispatch)
            d_model = str(d_payload.get("model") or "")
            p_model = str(p.get("model") or "")
            if d_model and p_model and d_model != p_model:
                return _fail(
                    category,
                    "routing.model_mismatch",
                    f"decision model={d_model!r} dispatch model={p_model!r}",
                    cited,
                )
            d_topo = str(d_payload.get("topology") or "")
            p_topo = str(p.get("topology") or "")
            if d_topo and p_topo and d_topo != p_topo:
                return _fail(
                    category,
                    "routing.topology_mismatch",
                    f"decision topology={d_topo!r} dispatch topology={p_topo!r}",
                    cited,
                )
        # Risk class / expected topology & model against the recorded decision.
        if expectations.risk_class:
            got = str(d_payload.get("risk_class") or "")
            if got and got != expectations.risk_class:
                return _fail(
                    category,
                    "routing.risk_class_mismatch",
                    f"expected risk_class={expectations.risk_class!r} got={got!r}",
                    _ids(decision),
                )
        if expectations.expected_model:
            got_model = str(d_payload.get("model") or "")
            if got_model and got_model != expectations.expected_model:
                return _fail(
                    category,
                    "routing.model_mismatch",
                    f"expected model={expectations.expected_model!r} got={got_model!r}",
                    _ids(decision),
                )
        if expectations.expected_topology:
            got_topo = str(d_payload.get("topology") or "")
            if got_topo and got_topo != expectations.expected_topology:
                return _fail(
                    category,
                    "routing.topology_mismatch",
                    f"expected topology={expectations.expected_topology!r} got={got_topo!r}",
                    _ids(decision),
                )
        return _pass(
            category,
            "routing decision matches dispatch",
            _ids(decision, *dispatches),
        )

    if not decisions:
        return _missing(
            segment,
            category,
            "routing.no_decision",
            "no routing-decision event in segment",
        )
    return _missing(
        segment,
        category,
        "routing.no_dispatch",
        "routing decision present but no dispatch/spawn event",
        _ids(*decisions),
    )


def rule_prompt_injection(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "prompt_injection"

    assemblies: list[Mapping[str, Any]] = []
    for event in segment.events:
        payload = _payload(event)
        digests = payload.get("digests") if "digests" in payload else payload.get("hashes")
        if isinstance(digests, Mapping):
            assemblies.append(event)
            continue
        if _action_has_any(event, "prompt", "assembly", "prompt_assembly"):
            assemblies.append(event)

    if not assemblies:
        return _missing(
            segment,
            category,
            "prompt_injection.no_prompt_evidence",
            "no prompt-assembly event with digests/hashes",
        )

    event = assemblies[0]
    payload = _payload(event)
    raw_digests = payload.get("digests") if "digests" in payload else payload.get("hashes")
    recorded: dict[str, str] = {}
    if isinstance(raw_digests, Mapping):
        recorded = {str(k): str(v) for k, v in raw_digests.items()}

    expected = dict(expectations.expected_prompt_digests or {})
    cited = _ids(event)

    for layer, expected_digest in expected.items():
        if layer not in recorded:
            return _fail(
                category,
                "prompt_injection.layer_missing",
                f"expected layer {layer!r} absent from recorded digests",
                cited,
            )
        if recorded[layer] != expected_digest:
            return _fail(
                category,
                "prompt_injection.layer_digest_mismatch",
                f"layer {layer!r} digest mismatch",
                cited,
            )

    # Empty expected digests still requires an assembly event (satisfied above).
    return _pass(category, "prompt layer digests consistent", cited)


def rule_tools(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "tools"

    tool_events: list[Mapping[str, Any]] = []
    observed: set[str] = set()
    for event in segment.events:
        payload = _payload(event)
        tools = _tools_from_payload(payload)
        if tools or _action_has_any(event, "tool"):
            tool_events.append(event)
            observed |= tools

    # Prohibited tools are observed violations.
    prohibited_hits = sorted(observed & set(expectations.prohibited_tools))
    if prohibited_hits:
        cited = _ids(
            *(
                e
                for e in tool_events
                if _tools_from_payload(_payload(e)) & set(expectations.prohibited_tools)
            )
        )
        return _fail(
            category,
            "tools.prohibited_tool_used",
            f"prohibited tools used: {prohibited_hits}",
            cited,
        )

    if expectations.required_tools:
        missing = sorted(set(expectations.required_tools) - observed)
        if missing:
            if not tool_events and not observed:
                return _missing(
                    segment,
                    category,
                    "tools.required_tool_missing",
                    f"required tools missing: {missing}",
                )
            return _fail(
                category,
                "tools.required_tool_missing",
                f"required tools missing: {missing}",
                _ids(*tool_events),
            )
        return _pass(category, "required tools present; prohibited absent", _ids(*tool_events))

    # No required tools asserted: need some tool evidence only if prohibited also empty?
    # Spec: no tool evidence code when evidence missing on complete trace for tools category.
    # With empty required/prohibited, a complete trace with no tool events still PASSes
    # (nothing to check) — but only when there is no positive obligation.
    if expectations.prohibited_tools and not tool_events:
        # Prohibited list present but no tool log at all → cannot confirm absence on a
        # complete trace? Spec says "prohibited tools absent from the log" — empty log
        # means absent → PASS. Keep pass.
        return _pass(category, "no prohibited tools observed", ())

    if not tool_events and (expectations.required_tools or expectations.prohibited_tools):
        return _missing(
            segment,
            category,
            "tools.no_tool_evidence",
            "no tool assignment/use events in segment",
        )

    return _pass(
        category,
        "tool obligations satisfied",
        _ids(*tool_events),
    )


def rule_context(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "context"

    ack_events: list[Mapping[str, Any]] = []
    acknowledged: set[str] = set()
    for event in segment.events:
        payload = _payload(event)
        is_ack = _action_has(event, "context", "ack") or _action_has_any(
            event, "context_ack", "acknowledge"
        )
        raw_ack = payload.get("acknowledged")
        items: list[str] = []
        if isinstance(raw_ack, Sequence) and not isinstance(raw_ack, (str, bytes)):
            items = [str(x) for x in raw_ack if str(x)]
        elif isinstance(raw_ack, str) and raw_ack:
            items = [raw_ack]
        if is_ack or items:
            ack_events.append(event)
            acknowledged.update(items)
            # Also accept payload context_ids on an ack-shaped action.
            if is_ack:
                for key in ("context_ids", "ids", "items"):
                    value = payload.get(key)
                    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                        acknowledged.update(str(x) for x in value if str(x))

    required = set(expectations.required_context_ids)
    if required:
        missing = sorted(required - acknowledged)
        if missing:
            if not ack_events:
                return _missing(
                    segment,
                    category,
                    "context.no_context_evidence",
                    "no context acknowledgment events",
                )
            return _fail(
                category,
                "context.unacknowledged_item",
                f"unacknowledged context items: {missing}",
                _ids(*ack_events),
            )
        return _pass(category, "required context items acknowledged", _ids(*ack_events))

    if not ack_events and required:
        return _missing(
            segment,
            category,
            "context.no_context_evidence",
            "no context acknowledgment events",
        )

    # No required context → PASS with empty obligations.
    return _pass(category, "no required context items", _ids(*ack_events))


def rule_timeouts(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "timeouts"

    arm_events: list[Mapping[str, Any]] = []
    for event in segment.events:
        payload = _payload(event)
        has_hard = any(k in payload for k in ("hard_timeout_s", "hard_timeout", "timeout_hard_s"))
        has_idle = any(k in payload for k in ("idle_timeout_s", "idle_timeout", "timeout_idle_s"))
        if has_hard and has_idle:
            arm_events.append(event)
        elif _action_has_any(event, "timeout") and (
            "arm" in _action(event) or has_hard or has_idle
        ):
            arm_events.append(event)

    if not arm_events:
        return _missing(
            segment,
            category,
            "timeouts.not_armed",
            "no hard+idle timeout arming event",
        )

    arm = arm_events[0]
    payload = _payload(arm)
    hard = payload.get("hard_timeout_s", payload.get("hard_timeout", payload.get("timeout_hard_s")))
    idle = payload.get("idle_timeout_s", payload.get("idle_timeout", payload.get("timeout_idle_s")))
    try:
        hard_s = float(hard) if hard is not None else None
    except (TypeError, ValueError):
        hard_s = None
    try:
        idle_s = float(idle) if idle is not None else None
    except (TypeError, ValueError):
        idle_s = None

    if hard_s is None or idle_s is None:
        return _missing(
            segment,
            category,
            "timeouts.not_armed",
            "timeout arm event missing hard or idle value",
            _ids(arm),
        )

    # Prefer expectations when provided; otherwise use armed values.
    hard_limit = (
        float(expectations.hard_timeout_s) if expectations.hard_timeout_s is not None else hard_s
    )
    idle_limit = (
        float(expectations.idle_timeout_s) if expectations.idle_timeout_s is not None else idle_s
    )

    arm_ts = _parse_ts(arm.get("ts"))
    completions = [
        e for e in segment.events if _action_has_any(e, "complete", "completed", "finish", "done")
    ]
    # Observed hard-timeout exceedance.
    if arm_ts is not None and completions:
        for completion in completions:
            cts = _parse_ts(completion.get("ts"))
            if cts is None:
                continue
            elapsed = (cts - arm_ts).total_seconds()
            if elapsed > hard_limit:
                return _fail(
                    category,
                    "timeouts.hard_timeout_exceeded",
                    f"completion {elapsed}s after arm exceeds hard={hard_limit}s",
                    _ids(arm, completion),
                )

    # Idle stretches between consecutive timestamped events.
    stamped: list[tuple[datetime, Mapping[str, Any]]] = []
    for event in segment.events:
        ts = _parse_ts(event.get("ts"))
        if ts is not None:
            stamped.append((ts, event))
    stamped.sort(key=lambda pair: pair[0])
    for i in range(1, len(stamped)):
        gap_s = (stamped[i][0] - stamped[i - 1][0]).total_seconds()
        if gap_s > idle_limit:
            return _fail(
                category,
                "timeouts.idle_timeout_exceeded",
                f"idle stretch {gap_s}s exceeds idle={idle_limit}s",
                _ids(stamped[i - 1][1], stamped[i][1]),
            )

    return _pass(category, "timeouts armed and honored", _ids(arm, *completions))


def rule_boundaries(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "boundaries"

    write_events: list[tuple[Mapping[str, Any], str]] = []
    for event in segment.events:
        if not _action_has_any(event, "write", "lock", "scope"):
            # Also accept payload path with explicit write-ish type.
            payload_probe = _payload(event)
            if "path" not in payload_probe and "path_rel" not in payload_probe:
                continue
            if not _action_has_any(event, "write", "lock", "scope", "file"):
                continue
        payload = _payload(event)
        path = payload.get("path", payload.get("path_rel", payload.get("target_path", "")))
        if isinstance(path, str) and path.strip():
            write_events.append((event, path.strip()))

    if not write_events:
        # Empty write_set with no writes: nothing to violate → PASS.
        # Spec note: empty write_set WITH observed write is a violation (handled below).
        if expectations.write_set:
            return _missing(
                segment,
                category,
                "boundaries.no_write_evidence",
                "write_set non-empty but no write/lock events",
            )
        return _pass(category, "no write events and empty write_set", ())

    for event, path in write_events:
        if not _path_covered(path, expectations.write_set):
            return _fail(
                category,
                "boundaries.write_outside_write_set",
                f"path {path!r} outside write_set",
                _ids(event),
            )

    return _pass(
        category,
        "all writes inside write_set",
        _ids(*(e for e, _ in write_events)),
    )


def rule_retries(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "retries"

    retries = [e for e in segment.events if _action_has_any(e, "retry")]
    # Zero retries on a complete trace is a legitimate PASS.
    if not retries:
        return _pass(category, "no retries observed", ())

    # Evidence-free retry is an observed violation.
    for event in retries:
        if not _failure_evidence(_payload(event)):
            return _fail(
                category,
                "retries.evidence_free_retry",
                "retry event lacks non-empty failure/error/evidence/reason",
                _ids(event),
            )

    count = len(retries)
    # Also honor an explicit count in payload if present on a summary event.
    for event in retries:
        payload = _payload(event)
        if "retry_count" in payload or "attempt" in payload:
            try:
                count = max(count, int(payload.get("retry_count", payload.get("attempt", count))))
            except (TypeError, ValueError):
                pass

    if expectations.max_retries is not None and count > expectations.max_retries:
        return _fail(
            category,
            "retries.unbounded",
            f"retry count {count} exceeds max_retries={expectations.max_retries}",
            _ids(*retries),
        )

    # Explicit unbounded markers on a retry event are observed violations.
    for event in retries:
        payload = _payload(event)
        if payload.get("unbounded") is True or payload.get("max_retries") == -1:
            return _fail(
                category,
                "retries.unbounded",
                "retry marked unbounded",
                _ids(event),
            )

    return _pass(category, "retries bounded with failure evidence", _ids(*retries))


def rule_verification(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "verification"

    reviews = [
        e
        for e in segment.events
        if _action_has_any(e, "review", "verdict", "verify", "verification")
    ]
    if not reviews:
        return _missing(
            segment,
            category,
            "verification.missing",
            "no independent review/verdict event",
        )

    worker = expectations.worker_actor
    work_actors = {
        _actor(e)
        for e in segment.events
        if _actor(e) and not _action_has_any(e, "review", "verdict", "verify", "verification")
    }

    for event in reviews:
        actor = _actor(event)
        if worker:
            if actor and actor != worker:
                return _pass(
                    category,
                    f"independent verification by {actor!r}",
                    _ids(event),
                )
            # Same as worker → not independent (observed violation).
            if actor and actor == worker:
                return _fail(
                    category,
                    "verification.not_independent",
                    f"verification actor {actor!r} equals worker_actor",
                    _ids(event),
                )
        else:
            # worker_actor blank: must differ from actors of work events.
            if actor and (not work_actors or actor not in work_actors):
                return _pass(
                    category,
                    f"independent verification by {actor!r}",
                    _ids(event),
                )
            if actor and actor in work_actors:
                return _fail(
                    category,
                    "verification.not_independent",
                    f"verification actor {actor!r} also performed work events",
                    _ids(event),
                )

    # Reviews exist but none cleared independence.
    return _fail(
        category,
        "verification.not_independent",
        "review/verdict event(s) not independent of the worker",
        _ids(*reviews),
    )


def rule_evidence(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "evidence"
    _ = expectations  # category is self-contained; expectations unused

    claims: list[Mapping[str, Any]] = []
    for event in segment.events:
        payload = _payload(event)
        action = _action(event)
        verdict_val = str(payload.get("verdict") or payload.get("result") or "").lower()
        kind = str(payload.get("kind") or payload.get("claim_kind") or "").lower()
        is_claim = "claim" in action or payload.get("is_claim") is True
        if kind == "claim" and verdict_val in {"pass", "fail"}:
            is_claim = True
        if payload.get("claim") is True and verdict_val in {"pass", "fail"}:
            is_claim = True
        if is_claim:
            claims.append(event)

    if not claims:
        return _missing(
            segment,
            category,
            "evidence.no_claims",
            "no pass/fail claim events in segment",
        )

    for event in claims:
        if not _claim_has_reference(_payload(event)):
            return _fail(
                category,
                "evidence.claim_without_reference",
                "claim lacks artifact/event reference",
                _ids(event),
            )

    return _pass(category, "all claims reference artifacts/events", _ids(*claims))


def rule_cost_latency(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> CategoryResult:
    segment = TraceSegment.coerce(segment)
    category = "cost_latency"

    budget_events: list[Mapping[str, Any]] = []
    exceedances: list[Mapping[str, Any]] = []
    escalations: list[Mapping[str, Any]] = []

    for event in segment.events:
        payload = _payload(event)
        action = _action(event)
        if _action_has_any(event, "budget", "cost", "latency") or any(
            k in payload for k in ("cost", "latency_s", "cost_budget", "latency_budget_s", "budget")
        ):
            budget_events.append(event)
        exceeded = bool(payload.get("exceeded") or payload.get("budget_exceeded"))
        if exceeded or "exceed" in action:
            exceedances.append(event)
            budget_events.append(event)
        if _action_has_any(event, "escalat"):
            escalations.append(event)

    # Detect numeric exceedance against expectations when recorded usage is present.
    for event in budget_events:
        payload = _payload(event)
        if expectations.cost_budget is not None and "cost" in payload:
            try:
                if float(payload["cost"]) > float(expectations.cost_budget):
                    if event not in exceedances:
                        exceedances.append(event)
            except (TypeError, ValueError):
                pass
        if expectations.latency_budget_s is not None and "latency_s" in payload:
            try:
                if float(payload["latency_s"]) > float(expectations.latency_budget_s):
                    if event not in exceedances:
                        exceedances.append(event)
            except (TypeError, ValueError):
                pass

    if exceedances:
        # Each exceedance must be followed (by id or ts order) by an escalation.
        for exc in exceedances:
            exc_id = _event_id(exc) or -1
            exc_ts = _parse_ts(exc.get("ts"))
            followed = False
            for esc in escalations:
                esc_id = _event_id(esc)
                if esc_id is not None and exc_id >= 0 and esc_id > exc_id:
                    followed = True
                    break
                esc_ts = _parse_ts(esc.get("ts"))
                if exc_ts is not None and esc_ts is not None and esc_ts >= exc_ts:
                    followed = True
                    break
            if not followed:
                return _fail(
                    category,
                    "cost_latency.exceedance_absorbed",
                    "budget exceedance without subsequent escalation",
                    _ids(exc),
                )
        return _pass(
            category,
            "budget exceedances escalated",
            _ids(*exceedances, *escalations),
        )

    if not budget_events:
        return _missing(
            segment,
            category,
            "cost_latency.no_budget_evidence",
            "no cost/latency/budget events in segment",
        )

    return _pass(category, "budgets observed without absorbed exceedance", _ids(*budget_events))


# ---------------------------------------------------------------------------
# Registry + aggregate
# ---------------------------------------------------------------------------


RULES: tuple[tuple[str, Callable[..., CategoryResult]], ...] = (
    ("routing", rule_routing),
    ("prompt_injection", rule_prompt_injection),
    ("tools", rule_tools),
    ("context", rule_context),
    ("timeouts", rule_timeouts),
    ("boundaries", rule_boundaries),
    ("retries", rule_retries),
    ("verification", rule_verification),
    ("evidence", rule_evidence),
    ("cost_latency", rule_cost_latency),
)


def audit(
    segment: TraceSegment | Sequence[Mapping[str, Any]],
    expectations: Expectations,
) -> AuditReport:
    """Run all ten rules and fold: any FAIL → FAIL; else any INCONCLUSIVE → INCONCLUSIVE."""
    segment = TraceSegment.coerce(segment)
    results: list[CategoryResult] = []
    for _name, rule in RULES:
        results.append(rule(segment, expectations))

    if any(r.verdict is Verdict.FAIL for r in results):
        overall = Verdict.FAIL
    elif any(r.verdict is Verdict.INCONCLUSIVE for r in results):
        overall = Verdict.INCONCLUSIVE
    else:
        overall = Verdict.PASS

    return AuditReport(verdict=overall, categories=tuple(results))
