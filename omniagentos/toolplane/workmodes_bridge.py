"""Register workmodes validators as toolplane/MCP-reachable checks.

Also provides the assess-time verification hook used by ``execution/assess.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omniagentos.toolplane.mcp_types import McpToolDescriptor

__all__ = [
    "VALIDATOR_TOOL_NAMES",
    "run_assess_validators",
    "run_workmodes_tool",
    "workmodes_tool_descriptors",
]

VALIDATOR_TOOL_NAMES: tuple[str, ...] = ("validate_ad_copy",)


def workmodes_tool_descriptors() -> list[McpToolDescriptor]:
    return [
        McpToolDescriptor(
            name="validate_ad_copy",
            description="Validate ad-copy variants against a platform profile.",
            input_schema={
                "type": "object",
                "properties": {
                    "platform": {"type": "string"},
                    "variants": {"type": "object"},
                    "disclaimers": {"type": "array", "items": {"type": "string"}},
                    "banned": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["platform", "variants"],
            },
            risk="low",
            source="workmodes",
        ),
    ]


def run_workmodes_tool(name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(args or {})
    if name != "validate_ad_copy":
        return {"ok": False, "error": "unknown_tool", "detail": f"unknown workmodes tool: {name}"}
    try:
        from omniagentos.workmodes.protocols import validate_ad_copy

        platform = str(payload.get("platform") or "")
        variants_raw = payload.get("variants") or {}
        if not isinstance(variants_raw, Mapping):
            return {"ok": False, "error": "invalid_args", "detail": "variants must be an object"}
        variants: dict[str, Sequence[str]] = {
            str(k): [str(v) for v in (vals if isinstance(vals, Sequence) else [vals])]
            for k, vals in variants_raw.items()
        }
        report = validate_ad_copy(
            platform,
            variants,
            disclaimers=list(payload.get("disclaimers") or ()),
            banned=list(payload.get("banned") or ()),
        )
        findings = [
            {
                "kind": f.kind,
                "slot": f.slot,
                "detail": f.detail,
            }
            for f in report.findings
        ]
        return {
            "ok": bool(report.ok),
            "content": {
                "platform": report.platform,
                "ok": bool(report.ok),
                "findings": findings,
                "violations": [
                    {"kind": f.kind, "slot": f.slot, "detail": f.detail} for f in report.violations
                ],
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "validator_failed", "detail": str(exc)}


def run_assess_validators(
    checks: Sequence[Mapping[str, Any]] | None,
    *,
    mode: str,
) -> dict[str, Any] | None:
    """Run verify-time workmodes checks for the assess ladder.

    * ``off`` — return None (no change to baseline).
    * ``shadow`` / ``enforce`` — run checks and return a structured record.
      Only ``enforce`` maps a failing check into a FAIL verdict for the caller.
    """
    if mode == "off" or not checks:
        return None
    results: list[dict[str, Any]] = []
    any_fail = False
    for check in checks:
        name = str(check.get("tool") or check.get("name") or "validate_ad_copy")
        args = check.get("args") if isinstance(check.get("args"), Mapping) else check
        out = run_workmodes_tool(name, args if isinstance(args, Mapping) else {})
        results.append({"tool": name, "result": out})
        if not out.get("ok"):
            any_fail = True
    verdict = "fail" if any_fail and mode == "enforce" else "pass"
    if any_fail and mode == "shadow":
        verdict = "pass"  # recorded failure, verdict not enforced
    reasons: list[str] = []
    if any_fail:
        reasons.append("workmodes validator reported findings")
    return {
        "verdict": verdict,
        "reasons": reasons,
        "mode": mode,
        "checks": results,
        "any_fail": any_fail,
    }


# Optional injection seam for tests.
ValidatorFn = Callable[[str, Mapping[str, Any]], dict[str, Any]]
