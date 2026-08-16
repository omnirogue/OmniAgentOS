from __future__ import annotations

from typing import cast

import pytest

from omniagentos.contracts import ActionClass, HarnessType
from omniagentos.policy import (
    AutonomyTier,
    PolicyError,
    PolicyMode,
    evaluate_action,
    is_hard_stop,
    load_policy,
    sandbox_for_tools,
    validate_tools,
)


@pytest.fixture
def cfg():  # type: ignore[no-untyped-def]
    return load_policy()


@pytest.fixture
def supervised_cfg():  # type: ignore[no-untyped-def]
    """The legacy gating posture: the configured action_classes floors apply."""
    return load_policy().model_copy(update={"mode": PolicyMode.SUPERVISED})


def test_load_policy_mirrors_configuration(cfg) -> None:  # type: ignore[no-untyped-def]
    assert set(cfg.action_classes) == set(ActionClass)
    assert cfg.tools.known == ["generate", "file_read", "file_write", "shell"]
    assert set(cfg.tools.sandbox_mapping) == {"read_only", "workspace_write"}
    # AUTO-APPROVE F1: 72h TTL so daily check-in does not race expiry.
    assert cfg.approval_expiry_hours == 72
    assert cfg.stale_worker_seconds == 30
    assert cfg.autonomy == AutonomyTier.HANDS_OFF


def test_configured_mode_is_auto(cfg) -> None:  # type: ignore[no-untyped-def]
    """The deliberate full-auto flip landed (swarm Phase 0, A0.1): the shipped
    configs/policy.yaml runs ``mode: auto`` -- unattended operation with the
    hard-stop overlay (irreversible only; money/delete/secret escalate via
    broker + classifier). SUPERVISED behavior stays covered by the
    ``supervised_cfg`` fixture matrix below."""
    assert cfg.mode == PolicyMode.AUTO


@pytest.mark.parametrize(
    ("action_class", "requires_approval", "always_human"),
    [
        (ActionClass.READ_ONLY, False, False),
        (ActionClass.SANDBOXED_CREATION, False, False),
        (ActionClass.INTERNAL_REVERSIBLE, False, False),
        (ActionClass.EXTERNAL_REVERSIBLE, True, False),
        (ActionClass.CONSEQUENTIAL, True, True),
        (ActionClass.IRREVERSIBLE, True, True),
    ],
)
def test_action_class_matrix_supervised(
    supervised_cfg, action_class: ActionClass, requires_approval: bool, always_human: bool
) -> None:  # type: ignore[no-untyped-def]
    """SUPERVISED mode keeps the legacy floors (regression-safe)."""
    decision = evaluate_action(action_class, supervised_cfg)

    assert decision.requires_approval is requires_approval
    assert decision.always_human is always_human
    assert action_class.value in decision.reason


@pytest.mark.parametrize(
    ("action_class", "requires_approval", "always_human"),
    [
        # AUTO mode (Grok product): irreversible only. Finance via broker.
        (ActionClass.READ_ONLY, False, False),
        (ActionClass.SANDBOXED_CREATION, False, False),
        (ActionClass.INTERNAL_REVERSIBLE, False, False),
        (ActionClass.EXTERNAL_REVERSIBLE, False, False),
        (ActionClass.CONSEQUENTIAL, False, False),
        (ActionClass.IRREVERSIBLE, True, True),
    ],
)
def test_action_class_matrix_auto(
    cfg, action_class: ActionClass, requires_approval: bool, always_human: bool
) -> None:  # type: ignore[no-untyped-def]
    """AUTO mode: only irreversible hard-stops; finance gated by broker."""
    decision = evaluate_action(action_class, cfg)

    assert decision.requires_approval is requires_approval
    assert decision.always_human is always_human


def test_is_hard_stop_is_exactly_irreversible() -> None:
    """Frozen contract for the provisioning package: only irreversible hard-stops."""
    assert is_hard_stop(ActionClass.IRREVERSIBLE) is True
    assert is_hard_stop("irreversible") is True
    for other in set(ActionClass) - {ActionClass.IRREVERSIBLE}:
        assert is_hard_stop(other) is False
    # Fails closed: an unknown class is treated as a hard-stop (never auto-granted).
    assert is_hard_stop("bogus-class") is True


def test_unknown_action_class_fails_closed(cfg) -> None:  # type: ignore[no-untyped-def]
    unknown = cast(ActionClass, "unknown")

    with pytest.raises(PolicyError, match="Unknown action class.*action denied"):
        evaluate_action(unknown, cfg)


@pytest.mark.parametrize(
    "tools_allowed",
    [[], ["generate"], ["file_read"], ["file_write"], ["shell"], ["generate", "shell"]],
)
def test_validate_tools_accepts_known_tools(cfg, tools_allowed: list[str]) -> None:  # type: ignore[no-untyped-def]
    assert validate_tools(tools_allowed, cfg) is None


def test_validate_tools_names_unknown_tool_and_known_list(cfg) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PolicyError) as error:
        validate_tools(["generate", "network"], cfg)

    message = str(error.value)
    assert "network" in message
    for known_tool in cfg.tools.known:
        assert known_tool in message


TOOL_CASES = [
    ([], "read_only"),
    (["generate"], "read_only"),
    (["file_read"], "read_only"),
    (["file_write"], "workspace_write"),
    (["shell"], "workspace_write"),
    (["file_write", "shell"], "workspace_write"),
    (["generate", "file_write"], "workspace_write"),
]
HARNESSES = [
    HarnessType.CLI_CLAUDE,
    HarnessType.CLI_CODEX,
    HarnessType.CLI_GROK,
    cast(HarnessType, "unknown"),
]


@pytest.mark.parametrize(("tools_allowed", "level"), TOOL_CASES)
@pytest.mark.parametrize("harness", HARNESSES)
def test_full_sandbox_mapping_matrix(
    cfg, harness: HarnessType, tools_allowed: list[str], level: str
) -> None:  # type: ignore[no-untyped-def]
    sandbox = sandbox_for_tools(harness, tools_allowed, cfg)
    harness_name = harness.value if isinstance(harness, HarnessType) else str(harness)

    assert sandbox.level == level
    assert sandbox.detail == cfg.tools.sandbox_mapping[level].get(harness_name, "")


def test_sandbox_rejects_unknown_tool(cfg) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PolicyError, match="Unknown tool 'network'"):
        sandbox_for_tools(HarnessType.CLI_CODEX, ["network"], cfg)
