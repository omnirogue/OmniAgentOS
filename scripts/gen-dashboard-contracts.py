#!/usr/bin/env python3
"""Generate dashboard TypeScript contract declarations from Python authorities.

Source of truth (never hand-mirror in components):

* ``omniagentos.contracts.Events`` / ``MissionEvents``
* ``omniagentos.contracts.ExecutionRef`` / ``EffectiveRoute`` / ``CostObservation``
* ``omniagentos.swarm.contracts.SwarmPlanDecision`` / ``PLAN_DISPOSITIONS``
* ``omniagentos.swarm.contracts.SWARM_EVENT_ACTIONS`` / ``SWARM_GROUP_EVENT_ACTIONS``

Default product target (documented only; this tool must be pointed at a path):

    dashboard/src/lib/generated/

P0-CONTRACT.v1a does not write into ``dashboard/**``. Callers and tests MUST pass
``--output`` to a temporary directory. Use ``--check`` to verify an existing
tree without rewriting (exit 1 on drift).

Writes are atomic (temp file + ``os.replace``) and deterministic (stable key
order, trailing newline, no timestamps).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import types
import typing
from collections.abc import Sequence
from enum import Enum
from pathlib import Path

# Ensure repo root is importable when invoked as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omniagentos.contracts import (  # noqa: E402
    PROVIDER_REQUEST_STATES,
    CostObservation,
    CostQuality,
    EffectiveRoute,
    Events,
    ExecutionRef,
    MissionEvents,
    ProviderCallStage,
    ReasoningEffort,
)
from omniagentos.swarm.contracts import (  # noqa: E402
    PLAN_DISPOSITIONS,
    SWARM_EVENT_ACTIONS,
    SWARM_EVENT_KIND,
    SWARM_GROUP_EVENT_ACTIONS,
    FormationBinding,
    PlanIssue,
    SwarmPlan,
    SwarmPlanDecision,
    SwarmTaskSpec,
)

# Documented eventual product location — never written unless the caller
# explicitly passes this path as --output.
DEFAULT_OUTPUT_DOC = "dashboard/src/lib/generated"
DASHBOARD_ROOT = "dashboard"

# Nested / companion models emitted as named interfaces (no parallel schema).
_NESTED_MODELS: dict[str, type] = {
    "FormationBinding": FormationBinding,
    "SwarmTaskSpec": SwarmTaskSpec,
    "SwarmPlan": SwarmPlan,
    "PlanIssue": PlanIssue,
}

_GENERATED_BANNER = (
    "/**\n"
    " * GENERATED FILE — do not edit by hand.\n"
    " * Source: scripts/gen-dashboard-contracts.py\n"
    " * Authorities: omniagentos.contracts + omniagentos.swarm.contracts\n"
    " */\n"
    "\n"
)


def _ts_string_union(values: Sequence[str]) -> str:
    if not values:
        return "never"
    return " | ".join(f'"{v}"' for v in values)


def _ts_const_array(name: str, values: Sequence[str], exported_type: str | None = None) -> str:
    lines = [f"export const {name} = ["]
    for value in values:
        lines.append(f'  "{value}",')
    lines.append("] as const;")
    if exported_type:
        lines.append(f"export type {exported_type} = (typeof {name})[number];")
    lines.append("")
    return "\n".join(lines)


def _enum_values(enum_cls: type[Enum]) -> tuple[str, ...]:
    return tuple(str(m.value) for m in enum_cls)


def _annotation_to_ts(ann: object) -> str:
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)

    if ann is str or ann is int or ann is float or ann is bool:
        return {str: "string", int: "number", float: "number", bool: "boolean"}[ann]  # type: ignore[index]
    if ann is type(None):
        return "null"

    # Closed Enum / StrEnum vocabularies → string-literal unions (never bare string).
    if isinstance(ann, type) and issubclass(ann, Enum):
        values = _enum_values(ann)
        return _ts_string_union(values) if values else "string"

    # Named nested models already emitted as interfaces.
    if isinstance(ann, type) and ann.__name__ in _NESTED_MODELS:
        return ann.__name__

    if origin is list:
        inner = _annotation_to_ts(args[0]) if args else "unknown"
        return f"{inner}[]"
    if origin is dict:
        return "Record<string, unknown>"

    # Optional / Union / PEP604 unions
    if origin is typing.Union or origin is types.UnionType:
        parts = [_annotation_to_ts(a) for a in args] if args else ["unknown"]
        seen: list[str] = []
        for p in parts:
            if p not in seen:
                seen.append(p)
        return " | ".join(seen)

    if origin is typing.Literal:
        lits: list[str] = []
        for a in args:
            if isinstance(a, str):
                lits.append(f'"{a}"')
            elif isinstance(a, bool):
                lits.append("true" if a else "false")
            else:
                lits.append(str(a))
        return " | ".join(lits) if lits else "string"

    text = str(ann)
    if text in _NESTED_MODELS:
        return text
    if text.startswith("list[") and text.endswith("]"):
        inner_name = text[5:-1].strip()
        if inner_name in _NESTED_MODELS:
            return f"{inner_name}[]"
        return f"{_annotation_to_ts(inner_name)}[]"

    if "ReasoningEffort" in text:
        return _ts_string_union(_enum_values(ReasoningEffort))
    if "CostQuality" in text:
        return _ts_string_union(_enum_values(CostQuality))
    if "ProviderCallStage" in text:
        return _ts_string_union(_enum_values(ProviderCallStage))

    if text in {"str", "int", "float", "bool"}:
        return {"str": "string", "int": "number", "float": "number", "bool": "boolean"}[text]
    if "None" in text and "str" in text:
        return "string | null"
    if "int" in text and "None" in text:
        return "number | null"
    if text.endswith("str") or "str |" in text:
        return "string"
    return "unknown"


def _model_field_lines(model: type) -> list[str]:
    """Render a TS interface from a Pydantic model field map.

    Generated shapes are response/serialized models (``model_dump``). Pydantic
    defaults still appear as keys, so every field is a required property; only
    nullability is expressed in the TypeScript type (``string | null``), never
    via an optional ``?`` marker for defaulted fields.
    """
    lines: list[str] = []
    for name, field in model.model_fields.items():
        ts_type = _annotation_to_ts(field.annotation)
        lines.append(f"  {name}: {ts_type};")
    return lines


def _render_interface(name: str, model: type) -> str:
    body = "\n".join(_model_field_lines(model))
    return f"export interface {name} {{\n{body}\n}}\n"


def render_events_ts() -> str:
    parts = [
        _GENERATED_BANNER,
        "// Frozen Wave-0 Events.ALL — never mutate; additive kinds live below.\n",
        _ts_const_array("FROZEN_EVENT_TYPES", Events.ALL, "FrozenEventType"),
        _ts_const_array("MISSION_EVENT_TYPES", MissionEvents.ALL, "MissionEventType"),
        _ts_const_array("SWARM_EVENT_ACTIONS", SWARM_EVENT_ACTIONS, "SwarmEventAction"),
        _ts_const_array(
            "SWARM_GROUP_EVENT_ACTIONS", SWARM_GROUP_EVENT_ACTIONS, "SwarmGroupEventAction"
        ),
        f'export const SWARM_EVENT_KIND = "{SWARM_EVENT_KIND}" as const;\n',
        # PLAN_DISPOSITIONS is the single Python authority (from get_args).
        _ts_const_array("PLAN_DISPOSITIONS", PLAN_DISPOSITIONS, "PlanDisposition"),
    ]
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts)


def render_models_ts() -> str:
    cost_qualities = _enum_values(CostQuality)
    stages = _enum_values(ProviderCallStage)
    efforts = _enum_values(ReasoningEffort)
    parts: list[str] = [
        _GENERATED_BANNER,
        _ts_const_array("COST_QUALITIES", cost_qualities, "CostQuality"),
        _ts_const_array("PROVIDER_CALL_STAGES", stages, "ProviderCallStage"),
        _ts_const_array("PROVIDER_REQUEST_STATES", PROVIDER_REQUEST_STATES, "ProviderRequestState"),
        _ts_const_array("REASONING_EFFORTS", efforts, "ReasoningEffort"),
    ]
    # Nested swarm plan shapes first so SwarmPlanDecision can reference them.
    for name in ("FormationBinding", "SwarmTaskSpec", "SwarmPlan", "PlanIssue"):
        parts.append(_render_interface(name, _NESTED_MODELS[name]))
        parts.append("\n")
    for name, model in (
        ("ExecutionRef", ExecutionRef),
        ("EffectiveRoute", EffectiveRoute),
        ("CostObservation", CostObservation),
        ("SwarmPlanDecision", SwarmPlanDecision),
    ):
        parts.append(_render_interface(name, model))
        parts.append("\n")
    body = "".join(parts)
    return body if body.endswith("\n") else body + "\n"


def render_index_ts() -> str:
    body = _GENERATED_BANNER + 'export * from "./events";\n' + 'export * from "./models";\n'
    return body if body.endswith("\n") else body + "\n"


def build_outputs() -> dict[str, str]:
    """Return relative-path → file body mapping (deterministic)."""
    bodies = {
        "events.ts": render_events_ts(),
        "models.ts": render_models_ts(),
        "index.ts": render_index_ts(),
    }
    return {name: (body if body.endswith("\n") else body + "\n") for name, body in bodies.items()}


def _under_dashboard(path: Path) -> bool:
    dash = (_ROOT / DASHBOARD_ROOT).resolve()
    try:
        path.resolve().relative_to(dash)
        return True
    except ValueError:
        return False


def assert_output_path_allowed(output_dir: Path) -> None:
    """Refuse any write/check target under dashboard/** unless override is set."""
    if not _under_dashboard(output_dir):
        return
    if os.environ.get("OMNIAGENTOS_ALLOW_DASHBOARD_CONTRACT_WRITE") == "1":
        return
    raise PermissionError(
        f"refusing path under {DASHBOARD_ROOT}/** without "
        "OMNIAGENTOS_ALLOW_DASHBOARD_CONTRACT_WRITE=1 "
        "(pass a temporary --output path outside dashboard/)"
    )


def _atomic_write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = body if body.endswith("\n") else body + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_outputs(output_dir: Path, outputs: dict[str, str] | None = None) -> list[Path]:
    assert_output_path_allowed(output_dir)
    bodies = outputs if outputs is not None else build_outputs()
    written: list[Path] = []
    for rel, body in sorted(bodies.items()):
        target = output_dir / rel
        _atomic_write(target, body)
        written.append(target)
    return written


def check_outputs(output_dir: Path, outputs: dict[str, str] | None = None) -> list[str]:
    """Return a list of drift descriptions (empty ⇒ in sync).

    Reports missing expected files, content drift, and unexpected ``*.ts``
    siblings so stale generated TypeScript cannot survive a --check.
    """
    bodies = outputs if outputs is not None else build_outputs()
    problems: list[str] = []
    expected_names = set(bodies)
    for rel, expected in sorted(bodies.items()):
        path = output_dir / rel
        expected_body = expected if expected.endswith("\n") else expected + "\n"
        if not path.is_file():
            problems.append(f"missing: {rel}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected_body:
            problems.append(f"drift: {rel}")
    if output_dir.is_dir():
        for path in sorted(output_dir.glob("*.ts")):
            if path.name not in expected_names:
                problems.append(f"unexpected: {path.name}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate dashboard TS contracts from Python authorities. "
            f"Documented product target: {DEFAULT_OUTPUT_DOC} "
            "(pass --output explicitly; this task/tests use a temp path). "
            f"All paths under {DASHBOARD_ROOT}/** are protected."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help=(
            "Output directory for generated TS files. Required. "
            f"Product target is {DEFAULT_OUTPUT_DOC}; tests must use a temp path "
            f"outside {DASHBOARD_ROOT}/."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify existing output matches generated content (no write).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    output_dir = args.output.expanduser()
    if not output_dir.is_absolute():
        output_dir = (_ROOT / output_dir).resolve()
    else:
        output_dir = output_dir.resolve()

    try:
        assert_output_path_allowed(output_dir)
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    bodies = build_outputs()
    if args.check:
        problems = check_outputs(output_dir, bodies)
        if problems:
            print("contract drift detected:", file=sys.stderr)
            for item in problems:
                print(f"  {item}", file=sys.stderr)
            return 1
        print(f"ok: {output_dir} matches generated contracts")
        return 0

    written = write_outputs(output_dir, bodies)
    for path in written:
        try:
            rel = path.relative_to(_ROOT)
        except ValueError:
            rel = path
        print(f"wrote {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
