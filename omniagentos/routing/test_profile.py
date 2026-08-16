"""Immutable test-profile snapshot and fail-closed role validator.

Production consumers (router/spawn/scheduler) are not wired here. This module
loads ``configs/test-profile.yaml``, validates exactly seven OpenRouter-eligible
role routes, freezes them into an immutable snapshot, and refuses profile
escape, unknown identity, Anthropic-through-OpenRouter, and partial role maps.

When ``OMNIAGENTOS_TEST_PROFILE`` is absent/false, loaders return ``None`` and
resolvers raise so callers keep production routing unchanged (disabled parity).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from omniagentos.contracts import EffectiveRoute, ReasoningEffort
from omniagentos.routing.api_policy import (
    ApiRoutePolicyError,
    assert_api_route_allowed,
    model_lineage,
)

PROFILE_ENV = "OMNIAGENTOS_TEST_PROFILE"
PROFILE_CONFIG_ENV = "OMNIAGENTOS_TEST_PROFILE_CONFIG"
PROFILE_ID = "test-profile"
REQUIRED_ROLE_COUNT = 7
MIN_REVIEWER_LINEAGES = 3
BILLING_PROVIDER = "openrouter"
TRANSPORT = "api"
ADAPTER_KEY = "openrouter"
SELECTION_REASON = "strict_model"
LINEAGE_UNKNOWN = "unknown"

REQUIRED_ROLES: frozenset[str] = frozenset(
    {
        "fast_implementer",
        "standard_implementer",
        "strong_planner",
        "verifier",
        "strong_synthesizer",
        "bulk_cheap",
        "cross_reviewer",
    }
)
REVIEWER_ROLES: frozenset[str] = frozenset({"verifier", "strong_synthesizer", "cross_reviewer"})
DEFAULT_KIND_BY_ROLE: Mapping[str, str] = MappingProxyType(
    {
        "fast_implementer": "implementer",
        "standard_implementer": "implementer",
        "strong_planner": "planner",
        "verifier": "reviewer",
        "strong_synthesizer": "reviewer",
        "bulk_cheap": "bulk",
        "cross_reviewer": "reviewer",
    }
)


class TestProfileError(RuntimeError):
    """Invalid, incomplete, or disabled test-profile configuration."""

    __test__ = False  # not a pytest test class


@dataclass(frozen=True, slots=True)
class RoleDecision:
    """One resolved role; maps cleanly onto contracts.EffectiveRoute fields."""

    role: str
    requested_model: str
    effective_model: str
    model_lineage: str
    billing_provider: str
    transport: str
    adapter_key: str
    effort: str | None
    profile_id: str
    profile_revision: str
    selection_reason: str
    strict_model: bool
    kind: str


@dataclass(frozen=True, slots=True)
class TestProfileBudget:
    max_usd_per_run: float
    max_usd_per_campaign: float
    on_exceeded: str


@dataclass(frozen=True, slots=True)
class TestProfileSnapshot:
    """Immutable validated profile; role map is a MappingProxyType."""

    profile_id: str
    revision: str
    enabled_by_env: str
    roles: Mapping[str, RoleDecision]
    reviewer_lineages: frozenset[str]
    budget: TestProfileBudget | None
    source_path: Path


def profile_enabled() -> bool:
    """True only when the test-profile env flag is an affirmative value."""
    raw = (os.environ.get(PROFILE_ENV) or "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def profile_config_path() -> Path:
    """Resolved path of the test-profile YAML (env override or repo default)."""
    override = (os.environ.get(PROFILE_CONFIG_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "configs" / "test-profile.yaml"


def load_snapshot(*, require_enabled: bool = False) -> TestProfileSnapshot | None:
    """Load and validate the profile snapshot.

    When disabled and ``require_enabled`` is False, return ``None`` (disabled
    parity — callers must keep production routing). When enabled or
    ``require_enabled`` is True, load+validate or raise ``TestProfileError``.
    """
    if not profile_enabled():
        if require_enabled:
            raise TestProfileError(
                f"test profile is disabled ({PROFILE_ENV} absent/false); "
                "production routing must be used unchanged"
            )
        return None
    return _load_and_validate(profile_config_path())


def resolve_all_roles() -> Mapping[str, RoleDecision]:
    """Resolve the full 7-role map from one snapshot; refuse if incomplete."""
    snapshot = load_snapshot(require_enabled=True)
    assert snapshot is not None  # require_enabled raises when disabled
    if len(snapshot.roles) != REQUIRED_ROLE_COUNT:
        raise TestProfileError(
            f"partial role map: expected {REQUIRED_ROLE_COUNT} roles, got {len(snapshot.roles)}"
        )
    missing = REQUIRED_ROLES - frozenset(snapshot.roles)
    if missing:
        raise TestProfileError(f"partial role map: missing required roles {sorted(missing)}")
    return snapshot.roles


def resolve_role(role: str) -> RoleDecision:
    """Resolve one role from the validated snapshot; refuse escape."""
    name = str(role or "").strip()
    if not name:
        raise TestProfileError("empty role name is not resolvable")
    roles = resolve_all_roles()
    decision = roles.get(name)
    if decision is None:
        raise TestProfileError(
            f"profile escape refused: role {name!r} is outside the frozen "
            f"registry (known: {sorted(roles)})"
        )
    return decision


def snapshot_digest(snapshot: TestProfileSnapshot) -> str:
    """Return the immutable content revision of a validated snapshot."""
    return snapshot.revision


def to_effective_route(decision: RoleDecision) -> EffectiveRoute:
    """Build ``EffectiveRoute`` retaining strict_model selection identity."""
    if not decision.strict_model:
        raise TestProfileError(
            f"strict_model dropped for role {decision.role!r}; refusing conversion"
        )
    if decision.selection_reason != SELECTION_REASON:
        raise TestProfileError(
            f"selection_reason must be {SELECTION_REASON!r} for role "
            f"{decision.role!r}, got {decision.selection_reason!r}"
        )
    effort: ReasoningEffort | None = None
    if decision.effort is not None:
        try:
            effort = ReasoningEffort(decision.effort)
        except ValueError as exc:
            raise TestProfileError(
                f"invalid effort {decision.effort!r} for role {decision.role!r}"
            ) from exc
    return EffectiveRoute(
        role=decision.role,
        requested_model=decision.requested_model,
        effective_model=decision.effective_model,
        model_lineage=decision.model_lineage,
        billing_provider=decision.billing_provider,
        transport=decision.transport,
        adapter_key=decision.adapter_key,
        effort=effort,
        profile_id=decision.profile_id,
        profile_revision=decision.profile_revision,
        selection_reason=decision.selection_reason,
    )


def production_parity_when_disabled() -> bool:
    """Document disabled-mode contract: no alternate routing is produced."""
    if profile_enabled():
        return False
    return load_snapshot() is None


def _load_and_validate(path: Path) -> TestProfileSnapshot:
    try:
        raw_text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except (OSError, yaml.YAMLError) as exc:
        raise TestProfileError(f"invalid test profile {path}: {exc}") from exc

    if not isinstance(data, dict) or "test_profile" not in data:
        raise TestProfileError(f"test profile {path} missing top-level test_profile key")

    section = data["test_profile"]
    if not isinstance(section, dict):
        raise TestProfileError(f"test_profile in {path} must be a mapping")

    enabled_by_env = str(section.get("enabled_by_env") or PROFILE_ENV).strip()
    roles_raw = section.get("roles")
    if not isinstance(roles_raw, dict) or not roles_raw:
        raise TestProfileError(f"test profile {path} has no roles map")

    role_names = {str(k).strip() for k in roles_raw}
    if len(roles_raw) != REQUIRED_ROLE_COUNT:
        raise TestProfileError(
            f"partial role map: expected exactly {REQUIRED_ROLE_COUNT} roles, "
            f"got {len(roles_raw)} ({sorted(role_names)})"
        )
    missing = REQUIRED_ROLES - role_names
    if missing:
        raise TestProfileError(f"partial role map: missing required roles {sorted(missing)}")
    extra = role_names - REQUIRED_ROLES
    if extra:
        raise TestProfileError(f"profile escape refused: unexpected roles {sorted(extra)}")

    draft_roles: dict[str, dict[str, Any]] = {}
    for role_name, entry in roles_raw.items():
        name = str(role_name).strip()
        draft_roles[name] = _validate_role_entry(name, entry)

    revision = _canonical_revision(draft_roles)
    decisions: dict[str, RoleDecision] = {}
    for name, draft in sorted(draft_roles.items()):
        decisions[name] = RoleDecision(
            role=name,
            requested_model=draft["requested_model"],
            effective_model=draft["effective_model"],
            model_lineage=draft["model_lineage"],
            billing_provider=draft["billing_provider"],
            transport=draft["transport"],
            adapter_key=draft["adapter_key"],
            effort=draft["effort"],
            profile_id=PROFILE_ID,
            profile_revision=revision,
            selection_reason=SELECTION_REASON,
            strict_model=True,
            kind=draft["kind"],
        )

    reviewer_lineages = frozenset(
        d.model_lineage for d in decisions.values() if d.kind == "reviewer"
    )
    if len(reviewer_lineages) < MIN_REVIEWER_LINEAGES:
        raise TestProfileError(
            f"three_reviewer_lineages gate failed: need ≥{MIN_REVIEWER_LINEAGES} "
            f"distinct reviewer lineages, got {sorted(reviewer_lineages)}"
        )

    budget = _parse_budget(section.get("budget"), path)
    return TestProfileSnapshot(
        profile_id=PROFILE_ID,
        revision=revision,
        enabled_by_env=enabled_by_env,
        roles=MappingProxyType(decisions),
        reviewer_lineages=reviewer_lineages,
        budget=budget,
        source_path=path,
    )


def _validate_role_entry(role: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise TestProfileError(f"role {role!r} entry must be a mapping")
    model = str(entry.get("model") or "").strip()
    if not model:
        raise TestProfileError(f"role {role!r} has empty model id")

    # Explicit overrides are accepted only when they match the OpenRouter policy.
    billing = str(entry.get("billing_provider") or BILLING_PROVIDER).strip().lower()
    transport = str(entry.get("transport") or TRANSPORT).strip().lower()
    adapter = str(entry.get("adapter_key") or ADAPTER_KEY).strip().lower()
    if billing != BILLING_PROVIDER:
        raise TestProfileError(
            f"role {role!r} billing_provider must be {BILLING_PROVIDER!r}, got {billing!r}"
        )
    if transport != TRANSPORT:
        raise TestProfileError(f"role {role!r} transport must be {TRANSPORT!r}, got {transport!r}")
    if adapter != ADAPTER_KEY:
        raise TestProfileError(
            f"role {role!r} adapter_key must be {ADAPTER_KEY!r}, got {adapter!r}"
        )

    lineage = model_lineage(model)
    if not lineage or lineage == LINEAGE_UNKNOWN:
        raise TestProfileError(
            f"unknown or unregistered model identity for role {role!r}: {model!r}"
        )
    try:
        assert_api_route_allowed(model, path="openrouter")
    except ApiRoutePolicyError as exc:
        raise TestProfileError(
            f"profile escape refused for role {role!r} model {model!r}: {exc}"
        ) from exc

    # Anthropic/claude must never resolve to OpenRouter (also covered by policy).
    if lineage in {"claude", "anthropic"} and billing == BILLING_PROVIDER:
        raise TestProfileError(
            f"anthropic-openrouter-resolves refused for role {role!r}: "
            f"lineage {lineage!r} cannot use OpenRouter"
        )

    kind_raw = entry.get("kind")
    if kind_raw is None or str(kind_raw).strip() == "":
        kind = DEFAULT_KIND_BY_ROLE.get(role, "implementer")
    else:
        kind = str(kind_raw).strip().lower()
    if role in REVIEWER_ROLES and kind != "reviewer":
        raise TestProfileError(f"role {role!r} must be kind=reviewer, got {kind!r}")

    effort_raw = entry.get("effort")
    effort: str | None
    if effort_raw is None or str(effort_raw).strip() == "":
        effort = None
    else:
        effort = str(effort_raw).strip().lower()

    return {
        "requested_model": model,
        "effective_model": model,
        "model_lineage": lineage,
        "billing_provider": billing,
        "transport": transport,
        "adapter_key": adapter,
        "effort": effort,
        "kind": kind,
    }


def _canonical_revision(draft_roles: Mapping[str, Mapping[str, Any]]) -> str:
    payload = {
        name: {
            "requested_model": draft["requested_model"],
            "effective_model": draft["effective_model"],
            "model_lineage": draft["model_lineage"],
            "billing_provider": draft["billing_provider"],
            "transport": draft["transport"],
            "adapter_key": draft["adapter_key"],
            "effort": draft["effort"],
            "kind": draft["kind"],
            "selection_reason": SELECTION_REASON,
            "strict_model": True,
        }
        for name, draft in sorted(draft_roles.items())
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _parse_budget(raw: Any, path: Path) -> TestProfileBudget | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TestProfileError(f"budget in {path} must be a mapping")
    try:
        max_run = float(raw["max_usd_per_run"])
        max_campaign = float(raw["max_usd_per_campaign"])
        on_exceeded = str(raw["on_exceeded"]).strip().lower()
    except (KeyError, TypeError, ValueError) as exc:
        raise TestProfileError(f"invalid budget in {path}: {exc}") from exc
    if max_run <= 0 or max_campaign <= 0 or max_campaign < max_run:
        raise TestProfileError("budget limits must be positive and coherent")
    if on_exceeded != "refuse":
        raise TestProfileError("budget.on_exceeded must be 'refuse'")
    return TestProfileBudget(max_run, max_campaign, on_exceeded)


def _decision_dict(decision: RoleDecision) -> dict[str, Any]:
    return asdict(decision)


__all__ = [
    "ADAPTER_KEY",
    "BILLING_PROVIDER",
    "MIN_REVIEWER_LINEAGES",
    "PROFILE_CONFIG_ENV",
    "PROFILE_ENV",
    "PROFILE_ID",
    "REQUIRED_ROLE_COUNT",
    "REQUIRED_ROLES",
    "REVIEWER_ROLES",
    "SELECTION_REASON",
    "TRANSPORT",
    "RoleDecision",
    "TestProfileBudget",
    "TestProfileError",
    "TestProfileSnapshot",
    "load_snapshot",
    "production_parity_when_disabled",
    "profile_config_path",
    "profile_enabled",
    "resolve_all_roles",
    "resolve_role",
    "snapshot_digest",
    "to_effective_route",
]
