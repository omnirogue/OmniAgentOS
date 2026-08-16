"""The actor→employee map the sweep hands to the attribution matcher.

Production consumes the mapping exactly as a dict (``sweep.py`` passes
``ingest.ACTOR_EMPLOYEE_MAP`` straight into ``match_task_detailed``), so these
tests pin the dict's contents — the known human identities resolve, and bot
identities are absent so agent work is never attributed to a person.
"""

from __future__ import annotations

import pytest

from omniagentos.team.ingest import ACTOR_EMPLOYEE_MAP


@pytest.mark.parametrize(
    ("actor", "employee_id"),
    [
        ("owner@initech.example", "emp_owner"),
        ("example-org", "emp_owner"),
        ("alice-dev", "emp_alice"),
        ("bob-dev", "emp_bob"),
    ],
)
def test_actor_mapping(actor: str, employee_id: str) -> None:
    assert ACTOR_EMPLOYEE_MAP[actor] == employee_id


@pytest.mark.parametrize(
    "actor",
    [
        "B2-contenttype",
        "Implementer Loop",
        "GrokOmni Fusion",
        "initech-bot",
        # The fleet GitHub App: its work is the fleet's, never a person's.
        "omniagentos-bot[bot]",
        "4580856+omniagentos-bot[bot]@users.noreply.github.com",
    ],
)
def test_bot_identities_are_not_employees(actor: str) -> None:
    assert actor not in ACTOR_EMPLOYEE_MAP
