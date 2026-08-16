"""Load and validate integration-branch role / promotion policy.

Mirrors the ``ModelStage`` / ``load_config`` idiom in
``omniagentos.improvement_chain``: frozen stages, a single YAML policy file,
and load-time invariants that refuse unsafe configurations.

CLI (bash consumer):

    python -m omniagentos.integration.config get <dotted.path>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from omniagentos.formation.lineage import lineage_for_model

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "configs" / "integration.yaml"

# Same vocabulary as improvement_chain._stage.
_EFFORT_VOCABULARY = frozenset({None, "low", "medium", "high", "xhigh", "max"})
_PROMOTION_MODES = frozenset({"off", "report", "enforce"})
_REQUIRED_ROLES = frozenset({"coder", "lane_reviewer", "aggregate_reviewer"})


@dataclass(frozen=True)
class IntegrationStage:
    harness: str
    model: str
    effort: str | None
    can_merge_to_main: bool


@dataclass(frozen=True)
class IntegrationConfig:
    stages: Mapping[str, IntegrationStage]
    branch_prefix: str
    protected_branches: tuple[str, ...]
    batch_state_file: str
    batch_worktree_root: str
    reviewer_lineage_required: str
    prose_fallback: bool
    promotion_mode: str
    promotion_pause_s: int
    gate_targets: tuple[str, ...]


def _stage(raw: Any, name: str) -> IntegrationStage:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be a mapping")
    harness = str(raw.get("harness") or "").strip()
    model = str(raw.get("model") or "").strip()
    effort_raw = raw.get("effort")
    effort = str(effort_raw).strip().lower() if effort_raw is not None else None
    if not harness or not model:
        raise ValueError(f"{name} requires harness and model")
    if effort not in _EFFORT_VOCABULARY:
        raise ValueError(f"{name}.effort has unsupported value {effort!r}")
    return IntegrationStage(
        harness=harness,
        model=model,
        effort=effort,
        can_merge_to_main=bool(raw.get("can_merge_to_main", False)),
    )


def _positive_int(raw: Any, default: int, name: str) -> int:
    value = default if raw is None else int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _resolve_promotion_mode(raw_mode: Any) -> str:
    """YAML mode, overridden by ``OMNIAGENTOS_AUTO_PROMOTE`` when set."""
    env = os.environ.get("OMNIAGENTOS_AUTO_PROMOTE")
    if env is not None and str(env).strip() != "":
        mode = str(env).strip().lower()
    else:
        mode = str(raw_mode or "report").strip().lower()
    if mode not in _PROMOTION_MODES:
        raise ValueError(
            f"promotion.mode has unsupported value {mode!r}; "
            f"allowed: {sorted(_PROMOTION_MODES)}"
        )
    return mode


def load_integration_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
) -> IntegrationConfig:
    """Load and validate the central integration-branch policy."""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("integration config must be a mapping")

    body = raw.get("integration", raw)
    if not isinstance(body, Mapping):
        raise ValueError("integration must be a mapping")

    roles_raw = body.get("roles") or {}
    if not isinstance(roles_raw, Mapping) or not roles_raw:
        raise ValueError("integration.roles must be a non-empty mapping")

    stages: dict[str, IntegrationStage] = {}
    for role, role_raw in roles_raw.items():
        role_name = str(role).strip()
        if not role_name:
            raise ValueError("integration.roles contains an empty role name")
        stages[role_name] = _stage(role_raw, f"roles.{role_name}")

    missing = sorted(_REQUIRED_ROLES - set(stages))
    if missing:
        raise ValueError(f"integration.roles missing required role(s): {missing}")

    # Load-time invariant: no LLM stage may claim merge authority.
    # Merging belongs to deterministic code (merge-gate.sh + friends).
    for role_name, stage in stages.items():
        if stage.can_merge_to_main:
            raise ValueError(
                f"roles.{role_name}.can_merge_to_main must be false "
                "(an LLM stage claiming merge authority is a config error; "
                "merging belongs to deterministic code)"
            )

    reviewer_lineage_required = str(
        body.get("reviewer_lineage_required") or "anthropic"
    ).strip().lower()
    if not reviewer_lineage_required:
        raise ValueError("reviewer_lineage_required must be non-empty")

    aggregate = stages["aggregate_reviewer"]
    aggregate_lineage = lineage_for_model(aggregate.model)
    if aggregate_lineage != reviewer_lineage_required:
        raise ValueError(
            f"aggregate_reviewer model {aggregate.model!r} resolves to lineage "
            f"{aggregate_lineage!r}, but reviewer_lineage_required is "
            f"{reviewer_lineage_required!r}"
        )

    coder_lineage = lineage_for_model(stages["coder"].model)
    lane_reviewer_lineage = lineage_for_model(stages["lane_reviewer"].model)
    if aggregate_lineage == coder_lineage:
        raise ValueError(
            f"aggregate_reviewer lineage {aggregate_lineage!r} must differ from "
            f"coder lineage {coder_lineage!r}"
        )
    if aggregate_lineage == lane_reviewer_lineage:
        raise ValueError(
            f"aggregate_reviewer lineage {aggregate_lineage!r} must differ from "
            f"lane_reviewer lineage {lane_reviewer_lineage!r}"
        )

    branch_prefix = str(body.get("branch_prefix") or "integration/batch").strip()
    if not branch_prefix:
        raise ValueError("branch_prefix must be non-empty")

    protected_raw = body.get("protected_branches") or ["main"]
    if not isinstance(protected_raw, list) or not protected_raw:
        raise ValueError("protected_branches must be a non-empty list")
    protected_branches = tuple(str(item).strip() for item in protected_raw if str(item).strip())
    if not protected_branches:
        raise ValueError("protected_branches must contain at least one branch name")

    batch = body.get("batch") or {}
    if not isinstance(batch, Mapping):
        raise ValueError("batch must be a mapping")
    batch_state_file = str(batch.get("state_file") or "").strip()
    batch_worktree_root = str(batch.get("worktree_root") or "").strip()
    if not batch_state_file or not batch_worktree_root:
        raise ValueError("batch.state_file and batch.worktree_root are required")

    verdicts = body.get("verdicts") or {}
    if not isinstance(verdicts, Mapping):
        raise ValueError("verdicts must be a mapping")
    prose_fallback = bool(verdicts.get("prose_fallback", True))

    promotion = body.get("promotion") or {}
    if not isinstance(promotion, Mapping):
        raise ValueError("promotion must be a mapping")
    promotion_mode = _resolve_promotion_mode(promotion.get("mode"))
    promotion_pause_s = _positive_int(promotion.get("pause_s"), 1800, "promotion.pause_s")
    gate_raw = promotion.get("gate_targets") or []
    if not isinstance(gate_raw, list) or not gate_raw:
        raise ValueError("promotion.gate_targets must be a non-empty list")
    gate_targets = tuple(str(item).strip() for item in gate_raw if str(item).strip())
    if not gate_targets:
        raise ValueError("promotion.gate_targets must contain at least one target")

    return IntegrationConfig(
        stages=MappingProxyType(stages),
        branch_prefix=branch_prefix,
        protected_branches=protected_branches,
        batch_state_file=batch_state_file,
        batch_worktree_root=batch_worktree_root,
        reviewer_lineage_required=reviewer_lineage_required,
        prose_fallback=prose_fallback,
        promotion_mode=promotion_mode,
        promotion_pause_s=promotion_pause_s,
        gate_targets=gate_targets,
    )


def _load_raw(path: Path) -> Mapping[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("integration config must be a mapping")
    return raw


def _get_dotted(data: Any, dotted: str) -> Any:
    """Resolve a dotted path against a nested mapping/sequence."""
    cur: Any = data
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        raise KeyError("empty path")
    for part in parts:
        if isinstance(cur, Mapping):
            if part not in cur:
                raise KeyError(part)
            cur = cur[part]
            continue
        if isinstance(cur, (list, tuple)):
            try:
                idx = int(part)
            except ValueError as exc:
                raise KeyError(part) from exc
            cur = cur[idx]
            continue
        raise KeyError(part)
    return cur


def get_config_value(dotted: str, *, path: str | Path = DEFAULT_CONFIG_PATH) -> Any:
    """Return the raw YAML value at ``dotted`` (bash-friendly CLI helper)."""
    raw = _load_raw(Path(path))
    # Prefer paths under the top-level `integration` key when present.
    try:
        return _get_dotted(raw, dotted)
    except KeyError:
        if "integration" in raw and not dotted.startswith("integration."):
            return _get_dotted(raw, f"integration.{dotted}")
        raise


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.integration.config",
        description="Read integration.yaml values for shell consumers.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to integration.yaml (default: repo configs/integration.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    get_p = sub.add_parser("get", help="Print the raw value at a dotted path")
    get_p.add_argument("path", help="Dotted path, e.g. branch_prefix or roles.coder.model")
    args = parser.parse_args(argv)

    if args.command == "get":
        try:
            value = get_config_value(args.path, path=args.config)
        except KeyError as exc:
            print(f"unknown path: {args.path} ({exc})", file=sys.stderr)
            return 2
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        print(_format_value(value))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
