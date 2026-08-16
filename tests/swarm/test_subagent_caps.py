"""Hard delegation caps are enforced at the unified worker spawn boundary."""

from __future__ import annotations

import logging
import threading

import pytest

import omniagentos.swarm.spawn as spawn_module
from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.swarm.spawn import (
    DEFAULT_MAX_CONCURRENT_DELEGATIONS,
    DEFAULT_MAX_TOTAL_DELEGATIONS,
    DELEGATION_CONSTRAINTS_IN_PROMPT_ENV,
    ROLE_PACK_FOOTER,
    ROLE_PACK_HEADER,
    ROLE_PACK_MODE_ENV,
    DelegationLimitReached,
    delegation_caps,
    delegation_constraints_in_prompt,
    delegation_truncation_count,
    parse_delegation_cap,
)
from tests.swarm.test_spawn import FakeSupervisor, make_request, make_spawner


@pytest.fixture(autouse=True)
def _advertise_caps_in_worker_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the worker-facing advisory ON for this file only.

    The advisory ships default-OFF (adding bytes to every brief is a prompt
    change other gates assert byte-identity against), so the tests that read it
    out of a prompt must opt in explicitly. Enforcement below does NOT depend on
    this flag — the caps bite either way.
    """

    monkeypatch.setenv(DELEGATION_CONSTRAINTS_IN_PROMPT_ENV, "1")


LIMIT_NOTICE = "[Delegation limit reached — synthesize what you have."


def _silence_cbm_allocation_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop CBM's per-spawn allocation stamp for placement assertions.

    The stamp is a volatile prefix written by a DIFFERENT package after prompt
    assembly; leaving it in would make these tests read as if the delegation
    passengers were the thing at the head. Disabling it here isolates the
    delegation package's own placement, exactly as the role-pack suite does.
    """

    def fail_allocate(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("CBM deliberately unavailable in delegation placement tests")

    monkeypatch.setattr(CognitiveBudgetService, "allocate", fail_allocate)


def test_advisory_is_off_by_default_and_takes_an_explicit_opt_in() -> None:
    """Default OFF, and only the exact affirmative spellings turn it on."""

    assert delegation_constraints_in_prompt({}) is False
    for disabled in ("", "   ", "0", "off", "no", "ture", "enforce"):
        assert (
            delegation_constraints_in_prompt({DELEGATION_CONSTRAINTS_IN_PROMPT_ENV: disabled})
            is False
        )
    for enabled in ("1", "true", "  TRUE  ", "yes", "on"):
        assert (
            delegation_constraints_in_prompt({DELEGATION_CONSTRAINTS_IN_PROMPT_ENV: enabled})
            is True
        )


def test_delegation_cap_parsing_is_strict() -> None:
    assert parse_delegation_cap("7", default=20) == 7
    for invalid in (None, "", "  ", "-3", "0", "3.5", "2k", 3):
        assert parse_delegation_cap(invalid, default=20) == 20


def test_delegation_cap_configuration_uses_environment_overrides() -> None:
    assert delegation_caps(
        {
            "OMNIAGENTOS_MAX_TOTAL_DELEGATIONS": "9",
            "OMNIAGENTOS_MAX_CONCURRENT_DELEGATIONS": "4",
        }
    ) == (9, 4)
    assert delegation_caps(
        {
            "OMNIAGENTOS_MAX_TOTAL_DELEGATIONS": "invalid",
            "OMNIAGENTOS_MAX_CONCURRENT_DELEGATIONS": "-1",
        }
    ) == (DEFAULT_MAX_TOTAL_DELEGATIONS, DEFAULT_MAX_CONCURRENT_DELEGATIONS)
    # A concurrent cap cannot advertise more work than the total run budget.
    assert delegation_caps(
        {
            "OMNIAGENTOS_MAX_TOTAL_DELEGATIONS": "2",
            "OMNIAGENTOS_MAX_CONCURRENT_DELEGATIONS": "9",
        }
    ) == (2, 2)


def test_total_cap_truncates_excess_and_marks_final_worker_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(spawn_module, "MAX_TOTAL_DELEGATIONS", 2)
    monkeypatch.setattr(spawn_module, "MAX_CONCURRENT_DELEGATIONS", 2)
    spawner, supervisor, _, _, _, _ = make_spawner(tmp_path)
    before = delegation_truncation_count()

    spawner.spawn(make_request(attempt_id="swa_one"))
    spawner.spawn(make_request(attempt_id="swa_two"))
    with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.spawn"):
        with pytest.raises(DelegationLimitReached, match="max_total_delegations=2"):
            spawner.spawn(make_request(attempt_id="swa_three"))

    assert len(supervisor.calls) == 2
    assert delegation_truncation_count() == before + 1
    assert "cap_name=max_total_delegations" in caplog.text
    assert "cap_value=2" in caplog.text
    assert "current_count=2" in caplog.text
    assert "truncated=1" in caplog.text
    final_prompt = str(supervisor.calls[1]["prompt"])
    assert "Delegation limit reached — synthesize what you have" in final_prompt
    assert "max 2 total for this run, max 2 concurrent in flight" in final_prompt


class _BlockingSupervisor(FakeSupervisor):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def spawn(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        self.started.set()
        assert self.release.wait(timeout=5)
        return "ses_blocking"


def test_concurrent_cap_hard_truncates_without_queueing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(spawn_module, "MAX_TOTAL_DELEGATIONS", 20)
    monkeypatch.setattr(spawn_module, "MAX_CONCURRENT_DELEGATIONS", 1)
    supervisor = _BlockingSupervisor()
    spawner, _, _, _, _, _ = make_spawner(tmp_path, supervisor=supervisor)
    before = delegation_truncation_count()
    thread = threading.Thread(target=lambda: spawner.spawn(make_request(attempt_id="swa_one")))
    thread.start()
    assert supervisor.started.wait(timeout=5)

    with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.spawn"):
        with pytest.raises(DelegationLimitReached, match="max_concurrent_delegations=1"):
            spawner.spawn(make_request(attempt_id="swa_two"))

    supervisor.release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(supervisor.calls) == 1
    assert delegation_truncation_count() == before + 1
    assert "cap_name=max_concurrent_delegations" in caplog.text
    assert "cap_value=1" in caplog.text
    assert "current_count=1" in caplog.text
    assert "truncated=1" in caplog.text


def test_worker_prompt_uses_the_same_numbers_as_the_enforcer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spawn_module, "MAX_TOTAL_DELEGATIONS", 7)
    monkeypatch.setattr(spawn_module, "MAX_CONCURRENT_DELEGATIONS", 3)
    spawner, supervisor, _, _, _, _ = make_spawner(tmp_path)

    spawner.spawn(make_request())

    prompt = str(supervisor.calls[0]["prompt"])
    assert (
        "[Delegation constraints: max 7 total for this run, max 3 concurrent in flight]" in prompt
    )


def test_advisory_is_spliced_in_behind_the_role_contract(tmp_path) -> None:
    """Placement, at the unit: the contract keeps the head, the advisory follows.

    The role contract is a stable HEAD segment — it must open the prompt handed
    to the adapter. The advisory is a passenger and rides BEHIND it.
    """

    spawner, _, _, _, _, _ = make_spawner(tmp_path)
    header = ROLE_PACK_HEADER.format(job_role="implementer")
    packed = f"{header}\ncontract body\n{ROLE_PACK_FOOTER}\n\nTHE-BRIEF"

    result = spawner._with_delegation_constraints(packed)

    assert result.startswith(header)
    advisory = spawner._delegation_constraints_prompt()
    assert result == f"{header}\ncontract body\n{ROLE_PACK_FOOTER}\n\n{advisory}THE-BRIEF"
    assert result.index(ROLE_PACK_FOOTER) < result.index("[Delegation constraints:")
    assert result.index("[Delegation constraints:") < result.index("THE-BRIEF")


def test_advisory_opens_the_prompt_when_there_is_no_contract_to_sit_behind(tmp_path) -> None:
    spawner, _, _, _, _, _ = make_spawner(tmp_path)

    result = spawner._with_delegation_constraints("THE-BRIEF")

    assert result == spawner._delegation_constraints_prompt() + "THE-BRIEF"


def test_prompt_is_byte_identical_when_the_advisory_is_off(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The autouse opt-in is what adds bytes; without it nothing is touched."""

    monkeypatch.delenv(DELEGATION_CONSTRAINTS_IN_PROMPT_ENV, raising=False)
    spawner, _, _, _, _, _ = make_spawner(tmp_path)
    packed = f"{ROLE_PACK_HEADER.format(job_role='implementer')}\n{ROLE_PACK_FOOTER}\n\nTHE-BRIEF"

    assert spawner._with_delegation_constraints(packed) == packed
    assert spawner._with_delegation_constraints("THE-BRIEF") == "THE-BRIEF"


def test_enforced_role_contract_still_opens_the_worker_prompt(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: turning the advisory on must not displace the contract."""

    monkeypatch.setenv(ROLE_PACK_MODE_ENV, "enforce")
    spawner, supervisor, _, _, _, _ = make_spawner(tmp_path)

    spawner.spawn(make_request())

    prompt = str(supervisor.calls[0]["prompt"])
    header = ROLE_PACK_HEADER.format(job_role="implementer")
    assert prompt.count("[Delegation constraints:") == 1
    assert prompt.index(header) < prompt.index("[Delegation constraints:")
    assert prompt.index(ROLE_PACK_FOOTER) < prompt.index("[Delegation constraints:")


def test_final_slot_notice_rides_behind_the_role_contract(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OTHER delegation passenger obeys the same placement rule.

    The last admitted worker is handed a "limit reached — synthesize" notice.
    That notice is emitted at admission and used to be prepended to the finished
    prompt, i.e. AFTER the role contract had been placed at the head — so on
    exactly the spawn that trips a cap, the worker read the notice before it read
    who it was. It now rides behind the contract like the standing advisory.
    """

    monkeypatch.setenv(ROLE_PACK_MODE_ENV, "enforce")
    monkeypatch.setattr(spawn_module, "MAX_TOTAL_DELEGATIONS", 1)
    monkeypatch.setattr(spawn_module, "MAX_CONCURRENT_DELEGATIONS", 1)
    _silence_cbm_allocation_stamp(monkeypatch)
    spawner, supervisor, _, _, _, _ = make_spawner(tmp_path)

    spawner.spawn(make_request())

    prompt = str(supervisor.calls[0]["prompt"])
    header = ROLE_PACK_HEADER.format(job_role="implementer")
    # The notice is still delivered — placement changed, delivery did not.
    assert prompt.count(LIMIT_NOTICE) == 1
    # HEAD: the contract opens the prompt handed to the adapter.
    assert prompt.startswith(header)
    assert prompt.index(ROLE_PACK_FOOTER) < prompt.index(LIMIT_NOTICE)
    # Both passengers sit between the contract and the brief, and the notice —
    # the urgent one — keeps its historical position ahead of the advisory.
    assert prompt.index(LIMIT_NOTICE) < prompt.index("[Delegation constraints:")
    assert prompt.index("[Delegation constraints:") < prompt.index("RAW-BRIEF-SENTINEL")


def test_final_slot_notice_opens_the_prompt_when_there_is_no_contract(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the role pack off there is nothing to sit behind: legacy shape kept."""

    monkeypatch.setattr(spawn_module, "MAX_TOTAL_DELEGATIONS", 1)
    monkeypatch.setattr(spawn_module, "MAX_CONCURRENT_DELEGATIONS", 1)
    _silence_cbm_allocation_stamp(monkeypatch)
    spawner, supervisor, _, _, _, _ = make_spawner(tmp_path)

    spawner.spawn(make_request())

    prompt = str(supervisor.calls[0]["prompt"])
    assert ROLE_PACK_FOOTER not in prompt
    assert prompt.startswith(LIMIT_NOTICE)
    assert prompt.index(LIMIT_NOTICE) < prompt.index("[Delegation constraints:")
