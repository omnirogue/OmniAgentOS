from __future__ import annotations

import pytest

from omniagentos.scheduler.routines import (
    RoutineValidationError,
    should_auto_pause,
    validate_routine,
)
from tests.routines.conftest import valid_routine_payload


def test_valid_payload_passes() -> None:
    validate_routine(valid_routine_payload())


@pytest.mark.parametrize(
    "command",
    [
        "true",
        "true && true",
        "echo ok",
        "ls -la",
        "python -c 'raise SystemExit(0)'",
        "pytest tests || true",
        "pytest tests --collect-only",
        "pytest --override-ini=addopts=--collect-only tests",
        "pytest -o addopts=--collect-only tests",
        "/private/tmp/pytest tests",
        "pytest /private/tmp/test_pass.py",
        "pytest ../other-project/tests",
        "pytest @/private/tmp/pytest-args",
        "ruff check . --exit-zero",
        "ruff check --config unsafe-fixes=true .",
        "npm test",
        "make test",
    ],
)
def test_rejects_vacuous_or_shell_composed_gate_commands(command: str) -> None:
    payload = valid_routine_payload(gate_config={"command": command, "expected_exit_code": 0})
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("objective verifier" in error for error in excinfo.value.errors)


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/routines",
        "python -m pytest tests/routines",
        "ruff check omniagentos",
        "mypy omniagentos",
        "pyright omniagentos",
        "git diff --check",
    ],
)
def test_accepts_recognized_objective_verifiers(command: str) -> None:
    validate_routine(
        valid_routine_payload(gate_config={"command": command, "expected_exit_code": 0})
    )


def test_rejects_treating_verifier_failure_as_success() -> None:
    payload = valid_routine_payload(
        gate_config={"command": "pytest tests/routines", "expected_exit_code": 1}
    )
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("must be 0" in error for error in excinfo.value.errors)


def test_rejects_missing_gate() -> None:
    payload = valid_routine_payload()
    del payload["gate_type"]
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("gate_type" in e for e in excinfo.value.errors)


def test_rejects_unknown_gate_type() -> None:
    payload = valid_routine_payload(gate_type="vibes")
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("gate_type" in e for e in excinfo.value.errors)


def test_accepts_merge_candidate_gate_with_bound_shas() -> None:
    validate_routine(
        valid_routine_payload(
            gate_type="merge_candidate",
            gate_config={
                "command": "pytest tests/routines",
                "expected_exit_code": 0,
                "candidate_sha": "a" * 40,
                "merge_base_sha": "b" * 40,
            },
        )
    )


def test_rejects_merge_candidate_gate_without_candidate_binding() -> None:
    payload = valid_routine_payload(
        gate_type="merge_candidate",
        gate_config={"command": "pytest tests/routines", "expected_exit_code": 0},
    )
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("candidate_sha" in error for error in excinfo.value.errors)
    assert any("merge_base_sha" in error for error in excinfo.value.errors)


def test_rejects_missing_hard_cap() -> None:
    payload = valid_routine_payload()
    del payload["hard_cap_type"]
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("hard_cap_type" in e for e in excinfo.value.errors)


def test_rejects_zero_hard_cap_value() -> None:
    payload = valid_routine_payload(hard_cap_value=0)
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("hard_cap_value" in e for e in excinfo.value.errors)


def test_rejects_non_integer_max_iterations() -> None:
    payload = valid_routine_payload(hard_cap_type="max_iterations", hard_cap_value=2.5)
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("whole number" in e for e in excinfo.value.errors)


def test_budget_hard_cap_allows_fractional_value() -> None:
    validate_routine(valid_routine_payload(hard_cap_type="budget_usd", hard_cap_value=12.5))


def test_rejects_missing_trigger() -> None:
    payload = valid_routine_payload()
    del payload["trigger_type"]
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("trigger_type" in e for e in excinfo.value.errors)


def test_cron_trigger_requires_cron_expression() -> None:
    payload = valid_routine_payload(trigger_config={})
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("trigger_config.cron" in e for e in excinfo.value.errors)


def test_event_trigger_requires_event_name() -> None:
    payload = valid_routine_payload(trigger_type="event", trigger_config={})
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("trigger_config.event" in e for e in excinfo.value.errors)


def test_event_trigger_with_event_name_passes() -> None:
    validate_routine(
        valid_routine_payload(trigger_type="event", trigger_config={"event": "goal.metric"})
    )


def test_metric_threshold_gate_requires_operator_and_threshold() -> None:
    payload = valid_routine_payload(gate_type="metric_threshold", gate_config={"metric": "roas"})
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    messages = excinfo.value.errors
    assert any("operator" in e for e in messages)
    assert any("threshold" in e for e in messages)


def test_metric_threshold_gate_with_full_config_passes() -> None:
    validate_routine(
        valid_routine_payload(
            gate_type="metric_threshold",
            gate_config={"metric": "roas", "operator": ">=", "threshold": 1.5},
        )
    )


def test_rejects_missing_notification_target() -> None:
    payload = valid_routine_payload(notification_target={})
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("notification_target.channel" in e for e in excinfo.value.errors)


def test_email_notification_requires_target_address() -> None:
    payload = valid_routine_payload(notification_target={"channel": "email"})
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("notification_target.target" in e for e in excinfo.value.errors)


def test_email_notification_with_address_passes() -> None:
    validate_routine(
        valid_routine_payload(notification_target={"channel": "email", "target": "ops@example.com"})
    )


def test_rejects_missing_task_template() -> None:
    payload = valid_routine_payload(task_template={})
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    assert any("task_template" in e for e in excinfo.value.errors)


def test_collects_every_violation_not_just_first() -> None:
    payload = {"name": "broken"}
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine(payload)
    # trigger, task_template, gate, hard_cap, notification: 5 independent failures.
    assert len(excinfo.value.errors) >= 5


def test_disabled_draft_may_omit_engine_fields() -> None:
    """LOOPS1-E2 draft exemption: status=disabled omits template/trigger/gate/cap/notify."""
    validate_routine({"name": "draft-only", "status": "disabled"})


def test_disabled_draft_still_validates_bogus_scope() -> None:
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine({"name": "draft", "status": "disabled", "scope": "nope"})
    assert any("scope" in e for e in excinfo.value.errors)


def test_active_sparse_payload_still_rejected() -> None:
    """cf-draft-exemption-on-active: exemption is status-keyed only."""
    with pytest.raises(RoutineValidationError) as excinfo:
        validate_routine({"name": "active-sparse", "status": "active"})
    assert len(excinfo.value.errors) >= 5


def test_should_auto_pause_requires_minimum_sample() -> None:
    assert should_auto_pause(1, 0) is False
    assert should_auto_pause(2, 0) is False


def test_should_auto_pause_below_floor() -> None:
    assert should_auto_pause(4, 1) is True  # 25% acceptance


def test_should_auto_pause_at_or_above_floor() -> None:
    assert should_auto_pause(4, 2) is False  # exactly 50% acceptance meets the floor
    assert should_auto_pause(4, 3) is False  # 75% acceptance, well clear of the floor
