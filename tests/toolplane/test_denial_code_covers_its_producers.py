"""``denial_code`` must have an answer for every deny reason the gate actually produces.

Refs #203.

``_DENIAL_CODES`` is a first-match-wins prefix table in ``toolplane/session.py``; the reason
strings it reduces are written in ``api/routes/sessions.py``. Nothing coupled the two, so the
Task fan-out gate shipped seven deny reasons on 2026-07-27 and the table was extended on
2026-08-04 without them — every one of them reduced to the generic ``"denied"``, including
``task-fanout:task-gate-error``, which is a *crashed* gate rather than a refusal.

The guard here DISCOVERS the producers from the route module's AST rather than listing them.
A hand-written list of the reasons known today has the same failure mode as the table it is
meant to protect: it goes stale silently. This one breaks the moment a new
``{"decision": "deny", ..., "reason": ...}`` literal lands without a code.

Non-literal reasons (``verdict.reason`` and friends) cannot be resolved statically. They are
reported by name instead of being dropped, so "the sweep found nothing" can never be confused
with "the sweep could not look".
"""

from __future__ import annotations

import ast
from pathlib import Path

import omniagentos
from omniagentos.toolplane.session import denial_code

#: The module that decides, and therefore the module that writes deny reasons.
ROUTE_MODULE = Path(omniagentos.__file__).parent / "api" / "routes" / "sessions.py"

#: What ``denial_code`` returns when no prefix matched. A reason reaching this is one the
#: ledger cannot tell apart from any other unclassified refusal.
GENERIC = "denied"


def _static_prefix(node: ast.expr) -> str | None:
    """The leading constant text of a string literal or f-string, if there is any.

    ``f"task-fanout:{why}"`` yields ``"task-fanout:"`` — which is exactly what a
    ``str.startswith`` table has to match on, so it is the right thing to test.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str) and head.value:
            return head.value
    return None


def _deny_reasons(tree: ast.Module) -> tuple[list[tuple[int, str]], list[int]]:
    """Every ``{"decision": "deny", ..., "reason": ...}`` dict literal in the module.

    Returns ``(resolvable, unresolvable)`` as ``[(lineno, prefix)]`` and ``[lineno]``.
    """
    resolvable: list[tuple[int, str]] = []
    unresolvable: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        pairs: dict[str, ast.expr] = {
            key.value: value
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        decision = pairs.get("decision")
        if not (isinstance(decision, ast.Constant) and decision.value == "deny"):
            continue
        reason = pairs.get("reason")
        if reason is None:
            continue
        prefix = _static_prefix(reason)
        if prefix is None:
            unresolvable.append(node.lineno)
        else:
            resolvable.append((node.lineno, prefix))

    return resolvable, unresolvable


def test_the_sweep_actually_finds_the_producers() -> None:
    """Guard the guard: a walk that silently matched nothing would pass every assertion below."""
    tree = ast.parse(ROUTE_MODULE.read_text(encoding="utf-8"))
    resolvable, _ = _deny_reasons(tree)

    assert resolvable, f"found no deny-reason literals in {ROUTE_MODULE} — the AST walk is broken"
    # The route has always denied on an unknown session; if that is gone the walk drifted.
    assert any(prefix == "unknown-session" for _, prefix in resolvable), (
        f"the walk missed the unknown-session denial; found {sorted(p for _, p in resolvable)}"
    )


def test_every_deny_reason_the_route_writes_reduces_to_a_specific_code() -> None:
    """No deny reason may land in the generic bucket — that is what erases the difference."""
    tree = ast.parse(ROUTE_MODULE.read_text(encoding="utf-8"))
    resolvable, unresolvable = _deny_reasons(tree)

    collapsed = [
        f"{ROUTE_MODULE.name}:{lineno} {prefix!r} -> {GENERIC!r}"
        for lineno, prefix in sorted(resolvable)
        if denial_code(prefix) == GENERIC
    ]
    assert not collapsed, (
        "deny reasons with no prefix in toolplane.session._DENIAL_CODES:\n  "
        + "\n  ".join(collapsed)
        + f"\n(non-literal reasons not checkable here, at lines {unresolvable})"
    )


def test_a_crashed_fanout_gate_is_not_recorded_as_an_ordinary_refusal() -> None:
    """The point of the codes: an instrument failure must not read like a normal denial.

    ``api/routes/sessions.py`` turns an exception inside the Task gate into
    ``allowed, why = False, "task-gate-error"`` — a fail-CLOSED deny. In the ledger that has to
    stay distinguishable from the flag simply being dark, and from the grant saying no.
    """
    crashed = denial_code("task-fanout:task-gate-error")
    dark = denial_code("task-fanout-disabled")
    refused = denial_code("task-fanout:budget_exhausted")

    assert crashed != GENERIC, "a crashed spawn gate is recorded as an ordinary denial"
    assert crashed != dark, "a crashed gate is indistinguishable from a dark feature flag"
    assert crashed != refused, "a crashed gate is indistinguishable from an exhausted budget"


def test_every_consume_child_slot_outcome_keeps_its_own_meaning() -> None:
    """The ``why`` values are a closed set in swarm/insession.py; none may read as a crash."""
    crashed = denial_code("task-fanout:task-gate-error")
    for why in (
        "no_grant",
        "grant_voided",
        "grant_expired",
        "budget_exhausted",
        "attempt_ended",
    ):
        code = denial_code(f"task-fanout:{why}")
        assert code != GENERIC, f"task-fanout:{why} falls into the generic bucket"
        assert code != crashed, f"task-fanout:{why} is recorded as a gate crash"
