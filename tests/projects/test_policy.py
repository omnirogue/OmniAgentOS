from __future__ import annotations

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.policy import PolicyError, load_policy
from omniagentos.projects import (
    assert_grants_monotonic,
    evaluate_action_for_project,
    resolve_project_policy,
)


def test_no_grants_returns_global_policy() -> None:
    cfg = load_policy()
    resolved = resolve_project_policy(cfg, [])
    assert resolved == dict(cfg.action_classes)


def test_project_cannot_relax_an_approval_gated_class() -> None:
    cfg = load_policy()
    # external_reversible globally requires approval. Overlays are MONOTONIC:
    # a grant that asks to relax it is a no-op -- the global floor holds (F1).
    resolved = resolve_project_policy(
        cfg, [{"action_class": "external_reversible", "requires_approval": False}]
    )
    assert cfg.action_classes[ActionClass.EXTERNAL_REVERSIBLE].requires_approval is True
    assert resolved[ActionClass.EXTERNAL_REVERSIBLE].requires_approval is True


def test_no_grant_can_relax_any_action_class(  # regression for every class (F1)
) -> None:
    cfg = load_policy()
    for action_class, base in cfg.action_classes.items():
        resolved = resolve_project_policy(
            cfg,
            [
                {
                    "action_class": action_class.value,
                    "requires_approval": False,
                    "always_human": False,
                }
            ],
        )
        effective = resolved[action_class]
        # A grant can never lower a floor: OR semantics preserve every global bit.
        assert effective.requires_approval >= base.requires_approval
        assert effective.always_human >= base.always_human
        if base.requires_approval:
            assert effective.requires_approval is True
        if base.always_human:
            assert effective.always_human is True


def test_assert_grants_monotonic_rejects_relaxation() -> None:
    cfg = load_policy()
    with pytest.raises(PolicyError):
        assert_grants_monotonic(
            cfg, [{"action_class": "external_reversible", "requires_approval": False}]
        )
    with pytest.raises(PolicyError):
        assert_grants_monotonic(
            cfg,
            [{"action_class": "consequential", "requires_approval": True, "always_human": False}],
        )
    # Tightening (and unknown-class detection) are fine / raise as expected.
    assert_grants_monotonic(cfg, [{"action_class": "read_only", "requires_approval": True}])
    with pytest.raises(PolicyError):
        assert_grants_monotonic(cfg, [{"action_class": "nope"}])


def test_hard_human_class_cannot_be_relaxed() -> None:
    cfg = load_policy()
    # A grant that tries to auto-approve consequential must be ignored: the global
    # always_human floor holds, and always_human forces requires_approval True.
    resolved = resolve_project_policy(
        cfg,
        [{"action_class": "consequential", "requires_approval": False, "always_human": False}],
    )
    assert resolved[ActionClass.CONSEQUENTIAL].always_human is True
    assert resolved[ActionClass.CONSEQUENTIAL].requires_approval is True


def test_project_can_tighten_a_class() -> None:
    cfg = load_policy()
    resolved = resolve_project_policy(
        cfg, [{"action_class": "read_only", "requires_approval": True}]
    )
    assert cfg.action_classes[ActionClass.READ_ONLY].requires_approval is False
    assert resolved[ActionClass.READ_ONLY].requires_approval is True


def test_evaluate_action_for_project_respects_overlay() -> None:
    cfg = load_policy()
    # A tightening overlay is honored: read_only (globally auto) can be raised.
    decision = evaluate_action_for_project(
        "read_only",
        cfg,
        [{"action_class": "read_only", "requires_approval": True}],
    )
    assert decision.requires_approval is True

    # A relaxing overlay is a no-op: the global approval floor holds (F1).
    gated = evaluate_action_for_project(
        "external_reversible",
        cfg,
        [{"action_class": "external_reversible", "requires_approval": False}],
    )
    assert gated.requires_approval is True

    hard = evaluate_action_for_project("consequential", cfg, [])
    assert hard.always_human is True


def test_unknown_action_class_fails_closed() -> None:
    cfg = load_policy()
    with pytest.raises(PolicyError):
        evaluate_action_for_project("nope", cfg, [])
    with pytest.raises(PolicyError):
        resolve_project_policy(cfg, [{"action_class": "nope"}])
