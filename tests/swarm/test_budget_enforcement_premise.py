"""A pytest session runs ADVISORY unless a test asks for enforcement.

2026-08-05. ``scripts/launch-env.sh`` began exporting
``OMNIAGENTOS_BUDGET_ENFORCEMENT=block`` on 2026-08-04 ("Operator decision"), and
``omniagentos.budget.policy.blocks()`` reads that variable at call time from
PRODUCTION code paths — admission in ``swarm/scheduler.py``, the improve
dispatcher, the session reaper, the provider CLI cap. Every test in this repo
that wants block mode sets it with ``monkeypatch.setenv``; the rest were written
against the code default (advisory) and never pinned it. So from 2026-08-04 the
same suite gave different answers depending on whether the shell that launched
pytest had sourced ``launch-env.sh``.

That is not hypothetical: it is layer 2 of the chronic in-gate
"COUNTERFEIT GATE CONTROL FAILED" refusal. ``merge-gate.sh`` runs from an
operator shell, so its "hermetic" counterfeit worker inherited ``block``; the
simulation campaigns' recovery attempts were then refused admission, two
``tests/simharness/test_orchestration.py`` nodes in the corpus's ``must_fail``
union went red, and the gate refused the candidate for a defect in its own
instrument. The identical corpus run from a plain worktree passed 85/85.

``tests/conftest.py`` already pins ``OMNIAGENTOS_DB``, ``OMNIAGENTOS_VAR``,
``OMNIAGENTOS_LEDGER_DIR``/``_VAULT_DIR``, ``OMNIAGENTOS_KNOWLEDGE`` and the
startup-seeding flags for exactly this reason — an operator's exported runtime
posture is not a test premise. Enforcement mode now joins that list.

DETECTION SCOPE, stated plainly: this test can only go red in a session that
actually inherited a live ``block`` — which is precisely the launch-env-sourced
shell the merge gate runs under, and the only place the defect ever fired. The
deterministic negative control for the pin itself (ambient ``block`` staged,
premise held) is
``tests/simharness/test_env_premises.py::test_an_ambient_block_never_reaches_a_campaign_that_did_not_ask_for_it``.
"""

from __future__ import annotations

import os

from omniagentos.budget import policy as budget_policy


def test_the_session_default_is_advisory_not_the_operator_shells_posture() -> None:
    raw = os.environ.get(budget_policy.ENFORCEMENT_ENV)
    assert budget_policy.enforcement_mode() == budget_policy.ADVISORY, (
        f"{budget_policy.ENFORCEMENT_ENV}={raw!r} leaked into the pytest session. "
        "Production admission/reaper/CLI-cap paths now BLOCK inside tests that "
        "were written against the advisory default, and the same suite gives a "
        "different answer depending on which shell launched it — restore the "
        "session-wide pin in tests/conftest.py"
    )


def test_a_test_that_asks_for_block_still_gets_it(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The pin is a DEFAULT, not a ceiling: function-scoped opt-in still wins."""
    monkeypatch.setenv(budget_policy.ENFORCEMENT_ENV, "block")
    assert budget_policy.blocks(), (
        "the session pin is overriding a test's explicit opt-in — every "
        "budget-enforcement test in the suite is now asserting nothing"
    )
