"""The simulation campaign's environment premises are PINNED, never inherited.

2026-08-05, layer 2 of the in-gate counterfeit CONTROL failure. ``SimulationCampaign``
declares its whole runtime environment in one ``isolated_env`` dict — DB, var,
ledger, vault, sim mode, swarm execution, target cap, formations path — precisely
so a campaign's premises do not depend on the shell that launched pytest. Budget
ENFORCEMENT was the one premise left out: the campaign had a
``budget_enforcement_block`` opt-in that SET ``block``, and no branch that pinned
the other side, so an ambient ``OMNIAGENTOS_BUDGET_ENFORCEMENT=block`` was
inherited by every campaign that had not asked for it.

``scripts/launch-env.sh`` started exporting exactly that value on 2026-08-04
("Operator decision"), which is why the merge gate — run from a launch-env shell
— saw ``test_attempt_timeout_is_closed_and_escalated`` and
``test_malformed_provider_json_does_not_kill_run`` go red while an isolated
worktree run of the identical corpus passed 85/85. Both nodes sit in the
counterfeit corpus's ``must_fail`` union, so the whole gate refused with
"COUNTERFEIT GATE CONTROL FAILED". In block mode the recovery attempt is never
admitted: the run ends ``failed`` with its second scripted response unconsumed,
and the timeout campaign never terminalizes inside its 15s join.

A campaign is a MECHANISM SIMULATION: the enforcement posture it runs under is
part of the scenario definition, so it is stated, both ways, in ``isolated_env``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.budget import policy as budget_policy
from tests.simharness.runner import SimulationCampaign


def test_an_ambient_block_never_reaches_a_campaign_that_did_not_ask_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEGATIVE CONTROL for the leak: ambient ``block``, campaign says advisory.

    Exports the value a launch-env shell exports BEFORE the campaign is entered
    — the exact shape the gate ran under — and proves the campaign pins it back
    to the code default it was written against.
    """
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    assert budget_policy.blocks(), "ambient leak not staged — this proves nothing"

    with SimulationCampaign(
        tmp_path / "env-premise", monkeypatch, scenario="env-premise", target_cap=1
    ):
        assert budget_policy.enforcement_mode() == budget_policy.ADVISORY, (
            "the campaign inherited the operator shell's budget enforcement "
            "posture; every scenario whose premise is advisory now depends on "
            "which shell launched pytest"
        )
        assert not budget_policy.blocks()


def test_a_campaign_that_asks_for_block_still_gets_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in still works — pinning the default must not disarm it."""
    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)

    with SimulationCampaign(
        tmp_path / "env-premise-block",
        monkeypatch,
        scenario="env-premise-block",
        target_cap=1,
        budget_enforcement_block=True,
    ):
        assert budget_policy.blocks(), (
            "budget_enforcement_block=True no longer reaches budget.policy — "
            "the admission-blocking scenarios are now asserting nothing"
        )
