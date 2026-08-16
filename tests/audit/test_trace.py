"""W1.4 — Trace audit rule engine.

Claims under test:

* Ten independent rules each return pass/fail/inconclusive with supporting
  event ids; no rule reads another's result.
* A missing required check in a complete trace is FAIL (never "unknown").
* INCONCLUSIVE is reachable only via a declared TraceGap, and the aggregate
  fails closed on it (accepted is False).
* Observed violations still FAIL even when a covering gap is declared.
* A reusable clean segment makes all ten rules PASS; each category has a
  targeted violation that fails with a stable machine-checkable ``code``.

Hermetic: pure in-memory dicts. No network, no DB, no model calls.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omniagentos.audit.trace import (
    RULES,
    AuditReport,
    CategoryResult,
    Expectations,
    TraceGap,
    TraceSegment,
    Verdict,
    audit,
    rule_boundaries,
    rule_prompt_injection,
    rule_retries,
    rule_routing,
    rule_tools,
    rule_verification,
)

CATEGORY_NAMES: tuple[str, ...] = (
    "routing",
    "prompt_injection",
    "tools",
    "context",
    "timeouts",
    "boundaries",
    "retries",
    "verification",
    "evidence",
    "cost_latency",
)

DIGEST_SYSTEM = "a" * 64
DIGEST_CONTEXT = "b" * 64
DIGEST_BAD = "c" * 64


def _event(
    eid: int,
    action: str,
    *,
    actor: str = "worker",
    payload: Mapping[str, Any] | None = None,
    ts: str = "2026-01-01T00:00:00Z",
) -> dict[str, Any]:
    return {
        "id": eid,
        "ts": ts,
        "type": "audit.event",
        "actor": actor,
        "action": action,
        "target_type": "",
        "target_id": "",
        "payload_json": dict(payload or {}),
        "trace_id": "tr_w14",
    }


def _expectations() -> Expectations:
    return Expectations(
        risk_class="R1",
        expected_topology="solo",
        expected_model="gpt-test",
        expected_prompt_digests={
            "system:base": DIGEST_SYSTEM,
            "context:task": DIGEST_CONTEXT,
        },
        required_tools=frozenset({"read_file", "run_tests"}),
        prohibited_tools=frozenset({"shell_exec"}),
        required_context_ids=frozenset({"ctx_arch", "ctx_task"}),
        hard_timeout_s=300.0,
        idle_timeout_s=60.0,
        write_set=frozenset({"omniagentos/audit", "tests/audit"}),
        max_retries=2,
        worker_actor="worker",
        cost_budget=1.0,
        latency_budget_s=120.0,
    )


def _clean_events() -> list[dict[str, Any]]:
    """Fully-compliant trace that makes all ten rules PASS."""
    return [
        _event(
            1,
            "routing.decision",
            actor="router",
            payload={
                "topology": "solo",
                "model": "gpt-test",
                "risk_class": "R1",
            },
            ts="2026-01-01T00:00:00Z",
        ),
        _event(
            2,
            "dispatch.spawn",
            actor="scheduler",
            payload={"topology": "solo", "model": "gpt-test"},
            ts="2026-01-01T00:00:01Z",
        ),
        _event(
            3,
            "prompt.assembly",
            actor="promptshape",
            payload={
                "digests": {
                    "system:base": DIGEST_SYSTEM,
                    "context:task": DIGEST_CONTEXT,
                }
            },
            ts="2026-01-01T00:00:02Z",
        ),
        _event(
            4,
            "tools.assigned",
            actor="scheduler",
            payload={"tools": ["read_file", "run_tests"]},
            ts="2026-01-01T00:00:03Z",
        ),
        _event(
            5,
            "context.ack",
            actor="worker",
            payload={"acknowledged": ["ctx_arch", "ctx_task"]},
            ts="2026-01-01T00:00:04Z",
        ),
        _event(
            6,
            "timeouts.armed",
            actor="scheduler",
            payload={"hard_timeout_s": 300.0, "idle_timeout_s": 60.0},
            ts="2026-01-01T00:00:05Z",
        ),
        _event(
            7,
            "scope.lock",
            actor="worker",
            payload={"path": "omniagentos/audit/trace.py"},
            ts="2026-01-01T00:00:06Z",
        ),
        _event(
            8,
            "file.write",
            actor="worker",
            payload={"path": "tests/audit/test_trace.py"},
            ts="2026-01-01T00:00:07Z",
        ),
        _event(
            9,
            "claim.pass",
            actor="worker",
            payload={"verdict": "pass", "artifact_id": "art_clean_001"},
            ts="2026-01-01T00:00:08Z",
        ),
        _event(
            10,
            "budget.usage",
            actor="meter",
            payload={"cost": 0.25, "latency_s": 12.0},
            ts="2026-01-01T00:00:09Z",
        ),
        _event(
            11,
            "work.completed",
            actor="worker",
            payload={},
            ts="2026-01-01T00:00:10Z",
        ),
        _event(
            12,
            "review.verdict",
            actor="reviewer",
            payload={"verdict": "pass", "artifact_id": "art_review_001"},
            ts="2026-01-01T00:00:11Z",
        ),
    ]


def _clean_segment() -> TraceSegment:
    return TraceSegment(events=tuple(_clean_events()), gaps=())


def _with_events(
    *extra: Mapping[str, Any],
    drop_ids: set[int] | None = None,
    replace: Mapping[int, Mapping[str, Any]] | None = None,
) -> TraceSegment:
    drop_ids = drop_ids or set()
    replace = dict(replace or {})
    events: list[Mapping[str, Any]] = []
    for event in _clean_events():
        eid = int(event["id"])
        if eid in drop_ids:
            continue
        if eid in replace:
            events.append(dict(replace[eid]))
        else:
            events.append(event)
    events.extend(extra)
    return TraceSegment(events=tuple(events), gaps=())


# ---------------------------------------------------------------------------
# Spec-named pre-set tests
# ---------------------------------------------------------------------------


def test_each_category_reports_independently_over_the_same_segment() -> None:
    # The same clean segment is fed to every rule; each returns its own category
    # without consulting another rule's result.
    segment = _clean_segment()
    exp = _expectations()
    results = [rule(segment, exp) for _, rule in RULES]
    assert len(results) == 10
    names = [r.category for r in results]
    assert names == list(CATEGORY_NAMES)
    assert all(r.verdict is Verdict.PASS for r in results), (
        f"clean segment must PASS every category; got "
        f"{[(r.category, r.verdict, r.code) for r in results if r.verdict is not Verdict.PASS]}"
    )
    # Independence: mutating expectations for one category must not change
    # another rule's category field when re-run in isolation.
    only_tools = Expectations(required_tools=frozenset({"missing_tool"}))
    tools_result = rule_tools(segment, only_tools)
    routing_result = rule_routing(segment, only_tools)
    assert tools_result.category == "tools"
    assert routing_result.category == "routing"
    assert tools_result.verdict is Verdict.FAIL
    # Routing still has decision+dispatch evidence even with empty topology expectation.
    assert routing_result.verdict is Verdict.PASS


def test_a_missing_required_verification_event_is_a_fail_not_unknown() -> None:
    segment = _with_events(drop_ids={12})
    result = rule_verification(segment, _expectations())
    assert result.verdict is Verdict.FAIL, "missing verification on a complete trace is FAIL"
    assert result.code == "verification.missing"
    report = audit(segment, _expectations())
    assert report.verdict is Verdict.FAIL
    assert report.accepted is False


def test_inconclusive_is_only_reachable_from_a_declared_trace_gap() -> None:
    # Complete trace, missing verification → FAIL, never inconclusive.
    complete = _with_events(drop_ids={12})
    assert rule_verification(complete, _expectations()).verdict is Verdict.FAIL

    # Same missing evidence with a declared gap covering verification → INCONCLUSIVE.
    gapped = TraceSegment(
        events=complete.events,
        gaps=(
            TraceGap(
                kind="missing_segment_boundary",
                detail="verification tail not yet sealed",
                categories=frozenset({"verification"}),
            ),
        ),
    )
    result = rule_verification(gapped, _expectations())
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.code == "verification.trace_gap"


def test_the_aggregate_fails_closed_on_any_inconclusive_category() -> None:
    # All other categories PASS; verification is INCONCLUSIVE via gap.
    events = tuple(e for e in _clean_events() if int(e["id"]) != 12)
    segment = TraceSegment(
        events=events,
        gaps=(
            TraceGap(
                kind="sequencing_hole",
                detail="review events not in segment",
                categories=frozenset({"verification"}),
            ),
        ),
    )
    report = audit(segment, _expectations())
    assert report.by_category("verification").verdict is Verdict.INCONCLUSIVE
    assert report.verdict is Verdict.INCONCLUSIVE
    assert report.accepted is False, "INCONCLUSIVE must fail closed for acceptance"


def test_boundary_rule_flags_a_write_outside_the_contract_write_set() -> None:
    segment = _with_events(
        _event(
            99,
            "file.write",
            payload={"path": "configs/policy.yaml"},
            ts="2026-01-01T00:00:12Z",
        )
    )
    result = rule_boundaries(segment, _expectations())
    assert result.verdict is Verdict.FAIL
    assert result.code == "boundaries.write_outside_write_set"
    assert 99 in result.event_ids


def test_retry_rule_fails_an_unbounded_or_evidence_free_retry() -> None:
    # Evidence-free retry.
    free = _with_events(
        _event(20, "task.retry", payload={}, ts="2026-01-01T00:00:12Z"),
    )
    free_result = rule_retries(free, _expectations())
    assert free_result.verdict is Verdict.FAIL
    assert free_result.code == "retries.evidence_free_retry"

    # Unbounded: more retries than max_retries.
    unbounded = _with_events(
        _event(
            21,
            "task.retry",
            payload={"failure": "art_err_1", "attempt": 1},
            ts="2026-01-01T00:00:12Z",
        ),
        _event(
            22,
            "task.retry",
            payload={"failure": "art_err_2", "attempt": 2},
            ts="2026-01-01T00:00:13Z",
        ),
        _event(
            23,
            "task.retry",
            payload={"failure": "art_err_3", "attempt": 3},
            ts="2026-01-01T00:00:14Z",
        ),
    )
    unbounded_result = rule_retries(unbounded, _expectations())
    assert unbounded_result.verdict is Verdict.FAIL
    assert unbounded_result.code == "retries.unbounded"


def test_prompt_rule_compares_layer_digests_when_present() -> None:
    # Mismatched digest for a layer present in both.
    bad = _with_events(
        replace={
            3: _event(
                3,
                "prompt.assembly",
                actor="promptshape",
                payload={
                    "digests": {
                        "system:base": DIGEST_BAD,
                        "context:task": DIGEST_CONTEXT,
                    }
                },
                ts="2026-01-01T00:00:02Z",
            )
        }
    )
    mismatch = rule_prompt_injection(bad, _expectations())
    assert mismatch.verdict is Verdict.FAIL
    assert mismatch.code == "prompt_injection.layer_digest_mismatch"

    # Layer expected but absent from recorded digests.
    missing_layer = _with_events(
        replace={
            3: _event(
                3,
                "prompt.assembly",
                actor="promptshape",
                payload={"digests": {"system:base": DIGEST_SYSTEM}},
                ts="2026-01-01T00:00:02Z",
            )
        }
    )
    missing = rule_prompt_injection(missing_layer, _expectations())
    assert missing.verdict is Verdict.FAIL
    assert missing.code == "prompt_injection.layer_missing"

    # No assembly event at all.
    none = _with_events(drop_ids={3})
    no_ev = rule_prompt_injection(none, _expectations())
    assert no_ev.verdict is Verdict.FAIL
    assert no_ev.code == "prompt_injection.no_prompt_evidence"


def test_results_cite_the_event_ids_that_support_them() -> None:
    segment = _clean_segment()
    exp = _expectations()
    report = audit(segment, exp)
    routing = report.by_category("routing")
    assert routing.verdict is Verdict.PASS
    assert 1 in routing.event_ids and 2 in routing.event_ids, (
        "routing PASS must cite decision and dispatch event ids"
    )
    prompt = report.by_category("prompt_injection")
    assert 3 in prompt.event_ids
    tools = report.by_category("tools")
    assert 4 in tools.event_ids
    verification = report.by_category("verification")
    assert 12 in verification.event_ids
    # Empty event_ids only when the verdict rests on "there were no such events"
    # (e.g. zero retries is a legitimate PASS with ()).
    retries = report.by_category("retries")
    assert retries.verdict is Verdict.PASS
    assert retries.event_ids == ()


# ---------------------------------------------------------------------------
# Aggregate shape + gap posture
# ---------------------------------------------------------------------------


def test_audit_returns_exactly_the_ten_section_10_2_categories() -> None:
    report = audit(_clean_segment(), _expectations())
    assert len(report.categories) == 10
    assert tuple(c.category for c in report.categories) == CATEGORY_NAMES
    assert [name for name, _ in RULES] == list(CATEGORY_NAMES)
    assert report.verdict is Verdict.PASS
    assert report.accepted is True


def test_a_declared_gap_does_not_launder_an_observed_violation() -> None:
    # Write outside write_set is an observed violation; a covering gap must not
    # turn it into inconclusive.
    segment = TraceSegment(
        events=tuple(
            [
                *_clean_events(),
                _event(
                    99,
                    "file.write",
                    payload={"path": "/etc/passwd"},
                    ts="2026-01-01T00:00:12Z",
                ),
            ]
        ),
        gaps=(
            TraceGap(
                kind="sequencing_hole",
                detail="boundaries partial",
                categories=frozenset({"boundaries"}),
            ),
        ),
    )
    result = rule_boundaries(segment, _expectations())
    assert result.verdict is Verdict.FAIL, "observed violation must stay FAIL under a covering gap"
    assert result.code == "boundaries.write_outside_write_set"
    report = audit(segment, _expectations())
    assert report.verdict is Verdict.FAIL
    assert report.accepted is False


def test_a_gap_scoped_to_one_category_does_not_blind_the_others() -> None:
    # Drop tool evidence and declare a tools-only gap. Routing (and others with
    # evidence) must still PASS; tools alone is INCONCLUSIVE.
    segment = TraceSegment(
        events=tuple(e for e in _clean_events() if int(e["id"]) != 4),
        gaps=(
            TraceGap(
                kind="missing_segment_boundary",
                detail="tool assignment not in segment",
                categories=frozenset({"tools"}),
            ),
        ),
    )
    exp = _expectations()
    tools = rule_tools(segment, exp)
    routing = rule_routing(segment, exp)
    assert tools.verdict is Verdict.INCONCLUSIVE
    assert tools.code == "tools.trace_gap"
    assert routing.verdict is Verdict.PASS
    report = audit(segment, exp)
    assert report.by_category("tools").verdict is Verdict.INCONCLUSIVE
    assert report.by_category("routing").verdict is Verdict.PASS
    assert report.verdict is Verdict.INCONCLUSIVE
    assert report.accepted is False


def test_payload_json_string_and_mapping_are_both_accepted() -> None:
    # Rules must not require callers to pre-decode payload_json.
    import json

    events: list[dict[str, Any]] = []
    for event in _clean_events():
        row = dict(event)
        row["payload_json"] = json.dumps(event["payload_json"])
        events.append(row)
    report = audit(events, _expectations())
    assert report.verdict is Verdict.PASS


def test_trace_segment_coerce_accepts_bare_sequences() -> None:
    bare: Sequence[Mapping[str, Any]] = _clean_events()
    coerced = TraceSegment.coerce(bare)
    assert isinstance(coerced, TraceSegment)
    assert coerced.gaps == ()
    again = TraceSegment.coerce(coerced)
    assert again is coerced


# ---------------------------------------------------------------------------
# One violation per category — the quality bar
# ---------------------------------------------------------------------------


class TestViolationsAreRejected:
    def _assert_category_fail(
        self,
        segment: TraceSegment,
        category: str,
        code: str,
        exp: Expectations | None = None,
    ) -> CategoryResult:
        exp = exp or _expectations()
        report = audit(segment, exp)
        result = report.by_category(category)
        assert result.verdict is Verdict.FAIL, (
            f"{category}: expected FAIL got {result.verdict} code={result.code!r}"
        )
        assert result.code == code, f"{category}: expected code {code!r} got {result.code!r}"
        assert report.verdict is Verdict.FAIL
        assert report.accepted is False
        return result

    def test_routing_model_mismatch(self) -> None:
        segment = _with_events(
            replace={
                2: _event(
                    2,
                    "dispatch.spawn",
                    actor="scheduler",
                    payload={"topology": "solo", "model": "wrong-model"},
                    ts="2026-01-01T00:00:01Z",
                )
            }
        )
        self._assert_category_fail(segment, "routing", "routing.model_mismatch")

    def test_prompt_injection_layer_digest_mismatch(self) -> None:
        segment = _with_events(
            replace={
                3: _event(
                    3,
                    "prompt.assembly",
                    actor="promptshape",
                    payload={
                        "digests": {
                            "system:base": DIGEST_BAD,
                            "context:task": DIGEST_CONTEXT,
                        }
                    },
                    ts="2026-01-01T00:00:02Z",
                )
            }
        )
        self._assert_category_fail(
            segment, "prompt_injection", "prompt_injection.layer_digest_mismatch"
        )

    def test_tools_prohibited_tool_used(self) -> None:
        segment = _with_events(
            replace={
                4: _event(
                    4,
                    "tools.assigned",
                    actor="scheduler",
                    payload={"tools": ["read_file", "run_tests", "shell_exec"]},
                    ts="2026-01-01T00:00:03Z",
                )
            }
        )
        self._assert_category_fail(segment, "tools", "tools.prohibited_tool_used")

    def test_context_unacknowledged_item(self) -> None:
        segment = _with_events(
            replace={
                5: _event(
                    5,
                    "context.ack",
                    actor="worker",
                    payload={"acknowledged": ["ctx_arch"]},  # ctx_task missing
                    ts="2026-01-01T00:00:04Z",
                )
            }
        )
        self._assert_category_fail(segment, "context", "context.unacknowledged_item")

    def test_timeouts_hard_timeout_exceeded(self) -> None:
        segment = _with_events(
            replace={
                11: _event(
                    11,
                    "work.completed",
                    actor="worker",
                    payload={},
                    # arm at 00:00:05 with hard=300s; complete far beyond that
                    ts="2026-01-01T01:00:00Z",
                )
            }
        )
        self._assert_category_fail(segment, "timeouts", "timeouts.hard_timeout_exceeded")

    def test_boundaries_write_outside_write_set(self) -> None:
        segment = _with_events(
            _event(
                99,
                "file.write",
                payload={"path": "secrets/keys.pem"},
                ts="2026-01-01T00:00:12Z",
            )
        )
        self._assert_category_fail(segment, "boundaries", "boundaries.write_outside_write_set")

    def test_retries_evidence_free_retry(self) -> None:
        segment = _with_events(
            _event(20, "task.retry", payload={"reason": ""}, ts="2026-01-01T00:00:12Z"),
        )
        self._assert_category_fail(segment, "retries", "retries.evidence_free_retry")

    def test_verification_not_independent(self) -> None:
        segment = _with_events(
            replace={
                12: _event(
                    12,
                    "review.verdict",
                    actor="worker",  # same as worker_actor
                    payload={"verdict": "pass", "artifact_id": "art_review_001"},
                    ts="2026-01-01T00:00:11Z",
                )
            }
        )
        self._assert_category_fail(segment, "verification", "verification.not_independent")

    def test_evidence_claim_without_reference(self) -> None:
        segment = _with_events(
            replace={
                9: _event(
                    9,
                    "claim.pass",
                    actor="worker",
                    payload={"verdict": "pass", "detail": "looks fine to me"},
                    ts="2026-01-01T00:00:08Z",
                )
            }
        )
        self._assert_category_fail(segment, "evidence", "evidence.claim_without_reference")

    def test_cost_latency_exceedance_absorbed(self) -> None:
        segment = _with_events(
            replace={
                10: _event(
                    10,
                    "budget.usage",
                    actor="meter",
                    payload={"cost": 9.99, "latency_s": 12.0, "exceeded": True},
                    ts="2026-01-01T00:00:09Z",
                )
            }
        )
        # No escalation event follows the exceedance.
        self._assert_category_fail(segment, "cost_latency", "cost_latency.exceedance_absorbed")


# ---------------------------------------------------------------------------
# Prefix matching for write_set (component boundary)
# ---------------------------------------------------------------------------


class TestBoundaryPathPrefix:
    def test_write_set_prefix_is_component_aware(self) -> None:
        # a/b covers a/b/c but NOT a/bc
        exp = Expectations(write_set=frozenset({"a/b"}))
        ok = TraceSegment(events=(_event(1, "file.write", payload={"path": "a/b/c"}),))
        bad = TraceSegment(events=(_event(1, "file.write", payload={"path": "a/bc"}),))
        assert rule_boundaries(ok, exp).verdict is Verdict.PASS
        bad_result = rule_boundaries(bad, exp)
        assert bad_result.verdict is Verdict.FAIL
        assert bad_result.code == "boundaries.write_outside_write_set"

    def test_empty_write_set_with_observed_write_is_a_violation(self) -> None:
        exp = Expectations(write_set=frozenset())
        segment = TraceSegment(
            events=(_event(1, "file.write", payload={"path": "anywhere/file.py"}),)
        )
        result = rule_boundaries(segment, exp)
        assert result.verdict is Verdict.FAIL
        assert result.code == "boundaries.write_outside_write_set"


# ---------------------------------------------------------------------------
# Type smoke for public surfaces
# ---------------------------------------------------------------------------


class TestPublicTypes:
    def test_audit_report_by_category_and_accepted(self) -> None:
        report = audit(_clean_segment(), _expectations())
        assert isinstance(report, AuditReport)
        assert report.accepted is True
        for name in CATEGORY_NAMES:
            cr = report.by_category(name)
            assert isinstance(cr, CategoryResult)
            assert cr.category == name

    def test_zero_retries_is_a_legitimate_pass(self) -> None:
        # Clean segment has no retry events.
        result = rule_retries(_clean_segment(), _expectations())
        assert result.verdict is Verdict.PASS
        assert result.event_ids == ()

    def test_rule_functions_accept_bare_event_lists(self) -> None:
        events = _clean_events()
        exp = _expectations()
        for name, rule in RULES:
            result = rule(events, exp)
            assert result.category == name
            assert result.verdict is Verdict.PASS, (name, result.code, result.detail)
