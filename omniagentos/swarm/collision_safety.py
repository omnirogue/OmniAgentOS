"""H-35 collision-safety controls: shadow / enforce / rollback, gated by L11.

Production collision safety has two halves:

1. **Scope locks** (``configs/parallelism.yaml`` ``scope_locks.mode``) — cross-lane
   path claims. ``shadow`` writes lock rows and records conflicts; ``enforce``
   refuses conflicting claimants.
2. **Swarm worktree isolation** (``configs/swarm.yaml`` ``worktrees.enabled``) —
   private per-task checkouts so concurrent writers never share a tree.

Both shipped dark (shadow-only locks, worktrees off) while concurrent autonomous
lanes are active. This module is the explicit control surface that:

* resolves the intended mode (``off`` / ``shadow`` / ``enforce``);
* **blocks enforce** until L11 git fencing reports ready (and optional shadow
  soak evidence is present);
* supports **rollback** from enforce → shadow → off without code changes
  (config or env);
* keeps worktree isolation disabled by default until the same gate opens.

L10 implements the controls; L11 owns the fencing contract
(``omniagentos.swarm.scope_wiring`` commit-lock renewal / generation fencing).
Do not flip production to ``enforce`` until that contract is integrated and
shadow evidence is recorded.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml

LOG = logging.getLogger(__name__)

CollisionMode = Literal["off", "shadow", "enforce"]

COLLISION_MODES: tuple[CollisionMode, ...] = ("off", "shadow", "enforce")

#: Explicit collision-safety env (bidirectional override for the unified mode).
COLLISION_MODE_ENV = "OMNIAGENTOS_COLLISION_SAFETY"
#: L11 fencing readiness kill-switch / enable. Truthy only when fencing is live.
FENCING_READY_ENV = "OMNIAGENTOS_L11_FENCING_READY"
#: Optional path to a recorded shadow-soak evidence file (presence + non-empty).
SHADOW_EVIDENCE_ENV = "OMNIAGENTOS_COLLISION_SHADOW_EVIDENCE"

_CONFIG_ENV = "OMNIAGENTOS_PARALLELISM_CONFIG"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

#: Hard default: measurement only. Enforce is never the hard-coded default.
DEFAULT_COLLISION_MODE: CollisionMode = "shadow"

__all__ = [
    "COLLISION_MODE_ENV",
    "COLLISION_MODES",
    "DEFAULT_COLLISION_MODE",
    "FENCING_READY_ENV",
    "SHADOW_EVIDENCE_ENV",
    "CollisionMode",
    "collision_safety_config",
    "effective_collision_mode",
    "enforce_allowed",
    "fencing_ready",
    "intended_collision_mode",
    "rollback_mode",
    "shadow_evidence_recorded",
    "swarm_worktrees_enforceable",
    "verify_l11_fence_capability",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _parallelism_path() -> Path:
    override = os.environ.get(_CONFIG_ENV)
    if override:
        return Path(override)
    return _repo_root() / "configs" / "parallelism.yaml"


def _load_parallelism() -> dict[str, Any]:
    try:
        raw = yaml.safe_load(_parallelism_path().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def collision_safety_config() -> dict[str, Any]:
    """``collision_safety:`` block from ``configs/parallelism.yaml`` (or ``{}``)."""
    section = _load_parallelism().get("collision_safety")
    return dict(section) if isinstance(section, dict) else {}


def _coerce_mode(value: Any) -> CollisionMode | None:
    if value is None:
        return None
    if isinstance(value, bool):
        # bare true → enforce intent; bare false → off (rollback target)
        return "enforce" if value else "off"
    text = str(value).strip().lower()
    if not text:
        return None
    if text in COLLISION_MODES:
        return text  # type: ignore[return-value]
    if text in _TRUTHY:
        return "enforce"
    if text in _FALSY:
        return "off"
    if text in {"rollback", "roll_back"}:
        # Explicit rollback request: step down from enforce to shadow.
        return "shadow"
    return None


def _truthy_config(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in _TRUTHY


def verify_l11_fence_capability() -> bool:
    """Verify that L11 exports a valid, versioned fence contract capability.

    Checks omniagentos.swarm.scope_wiring (or omniagentos.scope) for a
    callable commit_fence_contract(). Returns True ONLY if:
    1. Callable is present and returns a dict/mapping;
    2. Dict contains a valid integer version >= 1 (strict int, not bool, string, or float);
    3. Required contract schema capabilities ('commit_guard', 'commit_claim')
       are declared and enabled.
    """
    try:
        from omniagentos.swarm import scope_wiring

        fn = getattr(scope_wiring, "commit_fence_contract", None)
        if fn is None or not callable(fn):
            import omniagentos.scope as scope_mod

            fn = getattr(scope_mod, "commit_fence_contract", None)
        if fn is None or not callable(fn):
            return False

        contract = fn()
        if not isinstance(contract, dict):
            return False

        version = contract.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            return False

        req_keys = ("commit_guard", "commit_claim")
        capabilities = contract.get("capabilities")

        if capabilities is not None:
            if not isinstance(capabilities, (list, tuple, set, dict)):
                return False
            for key in req_keys:
                if key not in capabilities:
                    return False

        for key in req_keys:
            if key in contract and not contract[key]:
                return False
            if capabilities is None and not contract.get(key):
                return False

        return True
    except Exception:  # noqa: BLE001
        return False


def fencing_ready() -> bool:
    """True only when real versioned L11 fence capability is present AND not disabled by config/env.

    Config and env can ONLY disable fencing readiness (return False); they can
    NEVER fabricate or enable readiness on their own.
    """
    env = os.environ.get(FENCING_READY_ENV, "").strip().lower()
    if env in _FALSY:
        return False

    cfg_val = collision_safety_config().get("fencing_ready")
    if cfg_val is not None and not _truthy_config(cfg_val):
        return False

    return verify_l11_fence_capability()


def shadow_evidence_recorded() -> bool:
    """True when shadow soak evidence is present (optional second gate).

    If neither env nor config points at evidence, this returns True when the
    config does not require evidence (default). When a path is configured, the
    file must exist and be non-empty.
    """
    cfg = collision_safety_config()
    require = cfg.get("require_shadow_evidence")
    if require is None:
        require = False
    path_raw = os.environ.get(SHADOW_EVIDENCE_ENV) or cfg.get("shadow_evidence_path")
    if not path_raw:
        # No evidence path configured: only block when explicitly required.
        return not _truthy_config(require)
    path = Path(str(path_raw)).expanduser()
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def enforce_allowed() -> bool:
    """May collision safety enter ``enforce``? Requires L11 fencing + evidence."""
    cfg = collision_safety_config()
    # allow_enforce can hard-block even when fencing is ready (operator brake).
    if "allow_enforce" in cfg and not _truthy_config(cfg.get("allow_enforce")):
        return False
    if not fencing_ready():
        return False
    if not shadow_evidence_recorded():
        return False
    return True


def intended_collision_mode() -> CollisionMode:
    """Configured/env intent, ignoring the L11 gate.

    Resolution: env ``OMNIAGENTOS_COLLISION_SAFETY`` →
    ``collision_safety.mode`` → scope_locks.mode (same file) → default shadow.
    """
    from_env = _coerce_mode(os.environ.get(COLLISION_MODE_ENV))
    if from_env is not None:
        return from_env
    cfg = collision_safety_config()
    from_cfg = _coerce_mode(cfg.get("mode"))
    if from_cfg is not None:
        return from_cfg
    # Fall back to the existing scope_locks.mode so a host that only set that
    # knob still has a single coherent collision posture.
    try:
        from omniagentos.scope.config import scope_locks_mode

        scope_mode = scope_locks_mode()
        if scope_mode in COLLISION_MODES:
            return scope_mode  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        LOG.debug("scope_locks_mode unavailable for collision intent", exc_info=True)
    return DEFAULT_COLLISION_MODE


def effective_collision_mode() -> CollisionMode:
    """Mode after the L11 enforce gate (and optional rollback).

    ``enforce`` intent with the gate closed **downgrades to shadow** (never
    silently off — measurement continues). That is the fail-closed production
    posture until L11 fencing is integrated.
    """
    intended = intended_collision_mode()
    if intended == "enforce" and not enforce_allowed():
        LOG.warning(
            "collision safety enforce requested but blocked until L11 fencing is "
            "ready and shadow evidence is recorded — operating in shadow"
        )
        return "shadow"
    return intended


def rollback_mode(current: CollisionMode | None = None) -> CollisionMode:
    """Step down one level: enforce → shadow → off.

    Explicit rollback control for operators who need to leave enforce without
    editing code. Does not require L11 readiness (rollback always allowed).
    """
    mode = current if current is not None else effective_collision_mode()
    if mode == "enforce":
        return "shadow"
    if mode == "shadow":
        return "off"
    return "off"


def swarm_worktrees_enforceable() -> bool:
    """May swarm worktree isolation be turned on for production collision safety?

    Worktrees remain opt-in via ``OMNIAGENTOS_SWARM_WORKTREES`` / swarm.yaml, but
    **enforced** isolation (treating worktrees as a required collision barrier)
    is gated the same way as scope enforce: L11 fencing + evidence. Drill-time
    opt-in (env truthy) still works for Phase-2 testing without this gate —
    that path is ``swarm_worktrees_enabled()`` itself.
    """
    return enforce_allowed()
