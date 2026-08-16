"""Governance: deterministic risk classification + protected-path tiers (§6, §5b).

This module is the immutable safety core of the self-improving system. Two jobs:

  1. ``classify_risk(entries, declared_paths, ...)`` — a DETERMINISTIC classifier
     over the authoritative sandbox ``git diff`` (name-status + content). It only
     ever RAISES the LLM-suggested risk, never lowers it (taxonomy note). Protected
     paths (Tier P/S), declared-vs-actual mismatch, symlink/rename targets that
     resolve into Tier P, module-shadowing, and keyword guards force the level up.

  2. ``load_governance_config(path)`` — parses ``configs/governance.yaml`` (Tier P)
     with HARDCODED validation floors (panel ≥ 3 distinct families, observation
     window ≥ 6h, thresholds bounded, ``allow_majority_l1`` default false). The
     floors live here in code; editing the yaml below a floor is REFUSED.

Governance is deterministic and only-raises: given the same diff it always yields
the same level, and it is impossible for a proposal (or a judge) to talk it down.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.policy.protected_paths import (
    get_protected_paths,
)
from omniagentos.policy.protected_paths import (
    path_tier as _registry_path_tier,
)
from omniagentos.reliability.taxonomy import ChangeRisk

# Protected path tiers (§6) come solely from configs/protected-paths.yaml via
# omniagentos.policy.protected_paths — no second independently editable list here.
# Immutable floors are enforced by that loader, not re-declared as editable sets.

# Keyword guards (§6): any of these in a diff's content or path forces L4. Conservative
# stem prefixes (deletion/payments/permissions/authentication all match) — the
# classifier only raises, so a false positive is the SAFE direction.
_KEYWORD_RE = re.compile(
    r"(?i)(billing|payment|auth|security|permission|delet|credential|secret\b)"
)


# --- Diff model (shared with W3 sandbox, which builds these off the real worktree)


@dataclass
class DiffEntry:
    """One changed file in the authoritative sandbox diff.

    ``resolved_paths`` are the realpath-resolved effective targets of this change
    (the literal path is added separately). For a symlink or rename that resolves
    into a protected tree, the resolved target is what triggers protection (B2).
    """

    status: str  # git name-status letter: A|M|D|R|C|T (R/C may carry a score suffix)
    path: str  # repo-relative path (new path for renames)
    old_path: str | None = None  # rename/copy source
    resolved_paths: list[str] = field(default_factory=list)  # realpath-resolved targets
    content: str = ""  # unified diff body for this file (keyword scan)
    is_symlink: bool = False


@dataclass
class RiskResult:
    """Outcome of ``classify_risk`` — the level plus an auditable reason list."""

    level: int
    reasons: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    tier: str | None = None  # 'P' | 'S' | None (most severe tier touched)
    tier_p: bool = False
    tier_s: bool = False
    declared_mismatch: bool = False
    keyword_hit: bool = False


# --- Path helpers


def _norm(path: str | None) -> str:
    """Normalize a repo-relative path for matching (never resolves symlinks)."""
    if not path:
        return ""
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def path_tier(path: str) -> str | None:
    """Return 'P', 'S', or None for a path. Tier P is checked first (it wins).

    Authority is the checked-in protected-paths registry (fail-closed loader).
    """
    return _registry_path_tier(path)


def _clamp_risk(level: int) -> int:
    try:
        lvl = int(level)
    except (TypeError, ValueError):
        return ChangeRisk.L1.value
    return max(ChangeRisk.L1.value, min(ChangeRisk.L4.value, lvl))


def classify_risk(
    entries: list[DiffEntry],
    declared_paths: list[str] | None,
    *,
    suggested_level: int = 1,
    repo_root: str | Path | None = None,
) -> RiskResult:
    """Deterministically classify the risk of a diff (§5, B2, m1).

    The classifier only ever RAISES ``suggested_level``:

      * Tier P touched (directly, via a symlink/rename resolving into Tier P, or by
        shadowing a Tier-P module) ⇒ L4.
      * ``declared_paths`` given and the diff touches an undeclared path ⇒ L4.
      * A keyword guard (billing/payment/auth/security/permission/delete/…) fires ⇒ L4.
      * Tier S touched (or a Tier-S module shadowed) ⇒ at least L3.

    ``declared_paths=None`` disables the declared-vs-actual check (use when there is
    no file diff to reconcile, e.g. a pure config/skill/agent proposal). An empty
    list means "nothing declared" — any diff path is then a mismatch (safe default).
    """
    reasons: list[str] = []
    protected: list[str] = []
    level = _clamp_risk(suggested_level)
    tier_p = tier_s = mismatch = keyword = False

    declared: set[str] | None = None
    if declared_paths is not None:
        declared = {_norm(p) for p in declared_paths}

    for entry in entries:
        literals = [_norm(entry.path)]
        if entry.old_path:
            literals.append(_norm(entry.old_path))

        # Declared-vs-actual: any literal diff path outside the declared set ⇒ L4 (B2).
        if declared is not None:
            for lp in literals:
                if lp and lp not in declared:
                    mismatch = True
                    reasons.append(f"undeclared-path:{lp}")

        # Tier check over EVERY effective path: literal + realpath-resolved targets.
        effective = list(literals) + [_norm(p) for p in (entry.resolved_paths or [])]
        for p in effective:
            tier = path_tier(p)
            if tier == "P":
                tier_p = True
                protected.append(p)
                reasons.append(f"tier-P:{p}")
            elif tier == "S":
                tier_s = True
                reasons.append(f"tier-S:{p}")

        # Module shadowing: a NEW .py file whose stem matches a protected module,
        # placed somewhere its literal path is not itself protected (B2).
        primary = _norm(entry.path)
        if entry.status.upper().startswith("A") and primary.endswith(".py"):
            if path_tier(primary) is None:
                stem = Path(primary).stem
                registry = get_protected_paths()
                if stem in registry.protected_p_modules:
                    tier_p = True
                    protected.append(primary)
                    reasons.append(f"shadow-P:{stem}")
                elif stem in registry.protected_s_modules:
                    tier_s = True
                    reasons.append(f"shadow-S:{stem}")

        # Keyword guard on content + path.
        haystack = f"{entry.content or ''}\n{primary}"
        if _KEYWORD_RE.search(haystack):
            keyword = True
            reasons.append(f"keyword-guard:{primary}")

    if tier_s:
        level = max(level, ChangeRisk.L3.value)
    if tier_p or mismatch or keyword:
        level = ChangeRisk.L4.value

    tier = "P" if tier_p else ("S" if tier_s else None)
    return RiskResult(
        level=level,
        reasons=reasons,
        protected_paths=protected,
        tier=tier,
        tier_p=tier_p,
        tier_s=tier_s,
        declared_mismatch=mismatch,
        keyword_hit=keyword,
    )


def build_diff_entries(
    repo_root: str | Path,
    base_ref: str,
    head_ref: str = "HEAD",
) -> list[DiffEntry]:
    """Build DiffEntry list from a real git range, realpath-resolving each target.

    Shared helper: W3's sandbox uses this to produce the authoritative diff that
    ``classify_risk`` consumes. Renames/copies are followed (``--find-renames``);
    every new path is realpath-resolved against the worktree so a symlink or a
    rename that lands in a protected tree is captured in ``resolved_paths`` (B2).
    """
    root = Path(repo_root).resolve()
    name_status = _git(["diff", "--name-status", "-M", "-C", f"{base_ref}", f"{head_ref}"], root)
    entries: list[DiffEntry] = []
    for line in name_status.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        old_path = None
        if status[:1] in ("R", "C") and len(parts) >= 3:
            old_path = parts[1].strip()
            path = parts[2].strip()
        else:
            path = parts[-1].strip()
        content = _git(["diff", f"{base_ref}", f"{head_ref}", "--", path], root, check=False)
        entry = DiffEntry(
            status=status,
            path=path,
            old_path=old_path,
            content=content,
        )
        _resolve_entry_targets(entry, root)
        entries.append(entry)
    return entries


def _resolve_entry_targets(entry: DiffEntry, root: Path) -> None:
    """Populate ``resolved_paths`` for a diff entry by realpath-resolving its paths."""
    resolved: list[str] = []
    for p in filter(None, (entry.path, entry.old_path)):
        abs_p = root / p
        if abs_p.is_symlink():
            entry.is_symlink = True
        real = Path(os.path.realpath(abs_p))
        try:
            rel = real.relative_to(root)
            resolved.append(str(rel))
        except ValueError:
            # Target escapes the repo (e.g. ~/Library/LaunchAgents) — keep absolute.
            resolved.append(str(real))
    entry.resolved_paths = resolved


def _git(args: list[str], cwd: Path, *, check: bool = True) -> str:
    """Run a read-only git command and return stdout (used only for diff building)."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


# --- Governance config loader with HARDCODED floors (§6, B3)


class GovernanceConfigError(ValueError):
    """A governance.yaml value violates a hardcoded floor (or is malformed)."""


# Floors — hardcoded here so an edit to configs/governance.yaml can never relax them.
_FLOOR_PANEL_MIN_FAMILIES = 3
_FLOOR_OBSERVATION_MIN_HOURS = 6
_FLOOR_KPI_THRESHOLD_MIN = 0.0
_FLOOR_KPI_THRESHOLD_MAX = 1.0
_MAX_AUTO_RISK_HARD_CAP = 2  # schema ceiling; operating cap is configurable ≤ this
_DEFAULT_ALLOW_MAJORITY_L1 = False


@dataclass
class GovernanceConfig:
    """Validated governance knobs. All fields have passed the hardcoded floors."""

    allow_majority_l1: bool = _DEFAULT_ALLOW_MAJORITY_L1
    panel_families: list[str] = field(default_factory=lambda: ["anthropic", "openai", "xai"])
    panel_fallback_families: list[str] = field(default_factory=lambda: ["google", "moonshot"])
    observation_windows_hours: dict[str, int] = field(
        default_factory=lambda: {"L1": 24, "L2": 48, "L3": 48, "L4": 72}
    )
    kpi_thresholds: dict[str, float] = field(
        default_factory=lambda: {"regression_fraction": 0.15, "divergence_fraction": 0.10}
    )
    max_auto_risk_cap: int = 1
    raw: dict[str, Any] = field(default_factory=dict)

    def observation_hours(self, risk_level: int) -> int:
        """Observation-window hours for a risk level (falls back to the longest known)."""
        key = f"L{_clamp_risk(risk_level)}"
        if key in self.observation_windows_hours:
            return int(self.observation_windows_hours[key])
        return max(self.observation_windows_hours.values())

    def regression_fraction(self) -> float:
        return float(self.kpi_thresholds.get("regression_fraction", 0.15))

    def divergence_fraction(self) -> float:
        return float(self.kpi_thresholds.get("divergence_fraction", 0.10))


def _validate_config(cfg: GovernanceConfig) -> GovernanceConfig:
    """Enforce the hardcoded floors; raise GovernanceConfigError on any violation."""
    distinct = list(dict.fromkeys(cfg.panel_families))
    if len(distinct) < _FLOOR_PANEL_MIN_FAMILIES:
        raise GovernanceConfigError(
            f"panel must have ≥ {_FLOOR_PANEL_MIN_FAMILIES} distinct families, got {distinct}"
        )
    for key, hours in cfg.observation_windows_hours.items():
        try:
            h = int(hours)
        except (TypeError, ValueError) as exc:
            raise GovernanceConfigError(f"observation window {key} not an int: {hours!r}") from exc
        if h < _FLOOR_OBSERVATION_MIN_HOURS:
            raise GovernanceConfigError(
                f"observation window {key}={h}h below floor {_FLOOR_OBSERVATION_MIN_HOURS}h"
            )
    for key, val in cfg.kpi_thresholds.items():
        try:
            v = float(val)
        except (TypeError, ValueError) as exc:
            raise GovernanceConfigError(f"kpi threshold {key} not a number: {val!r}") from exc
        if not (_FLOOR_KPI_THRESHOLD_MIN <= v <= _FLOOR_KPI_THRESHOLD_MAX):
            raise GovernanceConfigError(
                f"kpi threshold {key}={v} outside "
                f"[{_FLOOR_KPI_THRESHOLD_MIN}, {_FLOOR_KPI_THRESHOLD_MAX}]"
            )
    if not isinstance(cfg.allow_majority_l1, bool):
        raise GovernanceConfigError("allow_majority_l1 must be a boolean")
    if not (0 <= int(cfg.max_auto_risk_cap) <= _MAX_AUTO_RISK_HARD_CAP):
        raise GovernanceConfigError(
            f"max_auto_risk_cap must be in [0, {_MAX_AUTO_RISK_HARD_CAP}], "
            f"got {cfg.max_auto_risk_cap}"
        )
    return cfg


def load_governance_config(path: str | Path | None = None) -> GovernanceConfig:
    """Load + validate configs/governance.yaml. Missing file ⇒ hardcoded defaults.

    The returned config has passed every floor. Governance knobs (quorum flags,
    panel families/fallbacks, observation windows, KPI thresholds, risk maps) live
    ONLY here (Tier P). A yaml that dips below a floor is REFUSED (B3).
    """
    if path is None:
        return _validate_config(GovernanceConfig())
    p = Path(path)
    if not p.exists():
        return _validate_config(GovernanceConfig())

    import yaml  # repo already vendors PyYAML

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise GovernanceConfigError(f"malformed governance.yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise GovernanceConfigError("governance.yaml must be a mapping")

    quorum = data.get("quorum", {}) or {}
    panel = data.get("panel", {}) or {}
    defaults = GovernanceConfig()
    cfg = GovernanceConfig(
        allow_majority_l1=bool(quorum.get("allow_majority_l1", _DEFAULT_ALLOW_MAJORITY_L1)),
        panel_families=list(panel.get("families", defaults.panel_families)),
        panel_fallback_families=list(
            panel.get("fallback_families", defaults.panel_fallback_families)
        ),
        observation_windows_hours=dict(
            data.get("observation_windows_hours", defaults.observation_windows_hours)
        ),
        kpi_thresholds=dict(data.get("kpi_thresholds", defaults.kpi_thresholds)),
        max_auto_risk_cap=int(data.get("max_auto_risk_cap", defaults.max_auto_risk_cap)),
        raw=data,
    )
    return _validate_config(cfg)


__all__ = [
    "DiffEntry",
    "RiskResult",
    "classify_risk",
    "build_diff_entries",
    "path_tier",
    "GovernanceConfig",
    "GovernanceConfigError",
    "load_governance_config",
]
