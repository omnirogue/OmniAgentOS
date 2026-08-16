"""Gates and negative mutations for the immutable test-profile snapshot."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from omniagentos.routing import test_profile as tp
from omniagentos.routing.test_profile import (
    REQUIRED_ROLE_COUNT,
    REQUIRED_ROLES,
    TestProfileError,
    load_snapshot,
    production_parity_when_disabled,
    profile_enabled,
    resolve_all_roles,
    resolve_role,
    snapshot_digest,
    to_effective_route,
)

# Canonical seven-role map used for synthetic YAML mutation cases.
_BASE_ROLES: dict[str, dict[str, str]] = {
    "fast_implementer": {"model": "qwen/qwen3-coder-flash", "kind": "implementer"},
    "standard_implementer": {"model": "deepseek/deepseek-v4-pro", "kind": "implementer"},
    "strong_planner": {"model": "x-ai/grok-4.3", "kind": "planner"},
    "verifier": {"model": "deepseek/deepseek-v4-pro", "kind": "reviewer"},
    "strong_synthesizer": {"model": "moonshotai/kimi-k2.6", "kind": "reviewer"},
    "bulk_cheap": {"model": "google/gemini-3.5-flash-lite", "kind": "bulk"},
    "cross_reviewer": {"model": "z-ai/glm-5.2", "kind": "reviewer"},
}


def _write_profile(
    path: Path,
    *,
    roles: dict[str, dict[str, Any]] | None = None,
    budget: dict[str, Any] | None = None,
) -> Path:
    payload = {
        "test_profile": {
            "enabled_by_env": "OMNIAGENTOS_TEST_PROFILE",
            "roles": roles if roles is not None else dict(_BASE_ROLES),
            "budget": budget
            if budget is not None
            else {
                "max_usd_per_run": 3.0,
                "max_usd_per_campaign": 30.0,
                "on_exceeded": "refuse",
            },
        }
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def enable_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = _write_profile(tmp_path / "test-profile.yaml")
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", "1")
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE_CONFIG", str(path))
    return path


@pytest.fixture
def enable_repo_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", "1")
    monkeypatch.delenv("OMNIAGENTOS_TEST_PROFILE_CONFIG", raising=False)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_seven_roles_resolve_from_one_immutable_snapshot(
    enable_repo_profile: None,
) -> None:
    snapshot = load_snapshot(require_enabled=True)
    assert snapshot is not None
    roles = resolve_all_roles()
    assert len(roles) == REQUIRED_ROLE_COUNT == 7
    assert frozenset(roles) == REQUIRED_ROLES
    revisions = {d.profile_revision for d in roles.values()}
    assert revisions == {snapshot.revision}
    assert snapshot.profile_id == "test-profile"
    # Snapshot roles mapping is immutable.
    with pytest.raises(TypeError):
        roles["extra"] = roles["verifier"]  # type: ignore[index]


def test_three_reviewer_lineages(enable_repo_profile: None) -> None:
    snapshot = load_snapshot(require_enabled=True)
    assert snapshot is not None
    assert len(snapshot.reviewer_lineages) >= 3
    reviewer_lineages = {d.model_lineage for d in snapshot.roles.values() if d.kind == "reviewer"}
    assert reviewer_lineages == set(snapshot.reviewer_lineages)
    assert {"deepseek", "kimi", "glm"} <= reviewer_lineages


def test_unknown_model_refuses(
    enable_profile: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    roles = dict(_BASE_ROLES)
    roles["bulk_cheap"] = {
        "model": "totally-unregistered/unknown-model-xyz",
        "kind": "bulk",
    }
    path = _write_profile(tmp_path / "unknown.yaml", roles=roles)
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE_CONFIG", str(path))
    with pytest.raises(TestProfileError, match="unknown or unregistered|profile escape"):
        load_snapshot(require_enabled=True)


def test_strict_model_retained_end_to_end(enable_repo_profile: None) -> None:
    decision = resolve_role("verifier")
    assert decision.strict_model is True
    assert decision.selection_reason == "strict_model"
    route = to_effective_route(decision)
    assert route.selection_reason == "strict_model"
    assert route.effective_model == decision.effective_model
    assert route.profile_id == decision.profile_id
    assert route.profile_revision == decision.profile_revision
    assert route.billing_provider == "openrouter"
    assert route.transport == "api"


def test_disabled_parity_byte_equivalent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_TEST_PROFILE", raising=False)
    assert profile_enabled() is False
    assert load_snapshot() is None
    assert production_parity_when_disabled() is True
    with pytest.raises(TestProfileError, match="disabled"):
        resolve_all_roles()
    with pytest.raises(TestProfileError, match="disabled"):
        resolve_role("verifier")
    # Enabling loads a real snapshot without mutating production routes
    # (this package does not wire router/spawn — load is pure).
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", "1")
    monkeypatch.delenv("OMNIAGENTOS_TEST_PROFILE_CONFIG", raising=False)
    enabled = load_snapshot()
    assert enabled is not None
    assert len(enabled.roles) == 7


def test_anthropic_openrouter_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    roles = dict(_BASE_ROLES)
    roles["cross_reviewer"] = {
        "model": "anthropic/claude-3.5-sonnet",
        "kind": "reviewer",
    }
    path = _write_profile(tmp_path / "anthropic.yaml", roles=roles)
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", "1")
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE_CONFIG", str(path))
    with pytest.raises(
        TestProfileError,
        match="claude|anthropic|POLICY DENY|openrouter",
    ):
        load_snapshot(require_enabled=True)


def test_partial_role_map_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    roles = dict(_BASE_ROLES)
    del roles["cross_reviewer"]
    path = _write_profile(tmp_path / "partial.yaml", roles=roles)
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", "1")
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE_CONFIG", str(path))
    with pytest.raises(TestProfileError, match="partial role map"):
        load_snapshot(require_enabled=True)


def test_profile_escape_refuses(enable_repo_profile: None) -> None:
    with pytest.raises(TestProfileError, match="profile escape"):
        resolve_role("not_a_registered_role")


def test_strict_model_dropped_detected(enable_repo_profile: None) -> None:
    decision = resolve_role("verifier")
    broken = replace(decision, strict_model=False, selection_reason="cheapest")
    with pytest.raises(TestProfileError, match="strict_model dropped"):
        to_effective_route(broken)
    broken_reason = replace(decision, selection_reason="cheapest")
    with pytest.raises(TestProfileError, match="selection_reason must be"):
        to_effective_route(broken_reason)


def test_snapshot_revision_stable(enable_repo_profile: None) -> None:
    first = load_snapshot(require_enabled=True)
    second = load_snapshot(require_enabled=True)
    assert first is not None and second is not None
    assert first.revision == second.revision
    assert snapshot_digest(first) == first.revision
    assert len(first.revision) == 64  # sha256 hex


def test_disabled_profile_diverges_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """disabled-profile-diverges: no alternate routing while disabled."""
    roles = dict(_BASE_ROLES)
    roles["verifier"] = {"model": "z-ai/glm-5.2", "kind": "reviewer"}
    path = _write_profile(tmp_path / "diverge.yaml", roles=roles)
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE_CONFIG", str(path))
    monkeypatch.delenv("OMNIAGENTOS_TEST_PROFILE", raising=False)
    assert load_snapshot() is None
    with pytest.raises(TestProfileError, match="disabled"):
        # Even with a divergent config path set, disabled mode must not resolve.
        resolve_role("verifier")


def test_every_role_is_openrouter_strict(enable_repo_profile: None) -> None:
    for role, decision in resolve_all_roles().items():
        assert decision.billing_provider == "openrouter", role
        assert decision.transport == "api", role
        assert decision.adapter_key == "openrouter", role
        assert decision.strict_model is True, role
        assert decision.selection_reason == "strict_model", role


def test_budget_section_mirrored_on_snapshot(enable_repo_profile: None) -> None:
    snapshot = load_snapshot(require_enabled=True)
    assert snapshot is not None
    assert snapshot.budget is not None
    assert snapshot.budget.max_usd_per_run == 3.0
    assert snapshot.budget.max_usd_per_campaign == 30.0
    assert snapshot.budget.on_exceeded == "refuse"


def test_require_enabled_raises_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", "0")
    with pytest.raises(TestProfileError, match="disabled"):
        load_snapshot(require_enabled=True)


def test_profile_enabled_truth_table(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw in ("", "0", "false", "no", "off", "FALSE"):
        if raw == "":
            monkeypatch.delenv("OMNIAGENTOS_TEST_PROFILE", raising=False)
        else:
            monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", raw)
        assert profile_enabled() is False
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", "1")
    assert profile_enabled() is True


def test_shared_helpers_match_budget_module(monkeypatch: pytest.MonkeyPatch) -> None:
    from omniagentos.budget import simulation as sim

    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", "1")
    assert sim.profile_enabled() is True
    assert sim.profile_enabled is tp.profile_enabled
    assert sim.PROFILE_ENV == tp.PROFILE_ENV
    assert sim.PROFILE_CONFIG_ENV == tp.PROFILE_CONFIG_ENV
