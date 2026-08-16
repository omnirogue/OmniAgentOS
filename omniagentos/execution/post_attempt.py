"""Post-attempt bundle: mechanical assess + violation posture (hot-path helper)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.execution.assess import assess
from omniagentos.execution.violations import ViolationContext, plan_violation_response


@dataclass(frozen=True, slots=True)
class PostAttemptResult:
    mechanical_pass: bool
    assess_verdict: str
    assess_reasons: tuple[str, ...]
    violation_actions: tuple[str, ...]
    park_state: str | None
    flags: tuple[str, ...]


def evaluate_post_attempt(
    *,
    working_dir: str | Path,
    expected_files: Sequence[str] = (),
    undeclared_paths: Sequence[str] = (),
    declared_scope: Sequence[str] | None = None,
    lane: str = "swarm",
    holder_id: str = "",
    tier_p: bool = False,
    min_bytes: int = 1,
    use_verify: bool = True,
) -> PostAttemptResult:
    """Run rung-0 assess (+ optional verify_working_tree) + violation plan.

    When ``use_verify=True`` and verify raises, fail closed (non-pass) rather
    than silently skipping undeclared detection.
    """
    scope_verdict: dict[str, Any] | None = None
    merged_undeclared = list(undeclared_paths)
    verify_error: str | None = None
    # Phase 1.4: wire execution/verify.py when owned/expected paths are known
    # and no undeclared list was pre-supplied.
    if use_verify and not merged_undeclared and (declared_scope or expected_files):
        try:
            from omniagentos.execution.verify import verify_working_tree

            scope_paths = list(declared_scope or expected_files)
            result = verify_working_tree(
                working_dir,
                declared=scope_paths,
            )
            if isinstance(result, dict):
                und = result.get("undeclared") or result.get("undeclared_paths") or []
                if und:
                    merged_undeclared.extend(str(p) for p in und)
                scope_verdict = result if "undeclared" in result or "missing" in result else None
                # H-13: inconclusive/degraded observation is never a mechanical pass.
                # verify_working_tree used to return ok=True with empty undeclared when
                # the disposable-index snapshot failed; that must fail closed here too.
                if (
                    result.get("degraded")
                    or result.get("observed_source") == "unobserved"
                    or (result.get("ok") is False and not und and result.get("observation_error"))
                ):
                    detail = (
                        str(result.get("observation_error") or "")
                        or "working-tree observation was inconclusive"
                    )
                    verify_error = f"observation_failed:{detail}"
        except Exception as exc:  # noqa: BLE001 — fail closed, do not soft-swallow
            verify_error = f"{type(exc).__name__}:{exc}"
            scope_verdict = {
                "verify_error": verify_error,
                "undeclared": [],
                "missing": [],
            }
    if merged_undeclared and scope_verdict is None:
        scope_verdict = {
            "missing": list(merged_undeclared),
            "undeclared": list(merged_undeclared),
        }
    assessed = assess(
        working_dir=working_dir,
        expected_files=expected_files,
        min_bytes=min_bytes,
        scope_verdict=scope_verdict,
        execution_level=4 if tier_p else 1,
        max_rung=0,  # mechanical only on the hot path
    )
    verdict = str(assessed.get("verdict") or "needs_review")
    if verify_error:
        # Fail closed: a crashed verify is not a mechanical pass.
        verdict = "fail"
    reasons = tuple(str(r) for r in (assessed.get("reasons") or ()))
    if verify_error:
        reasons = reasons + (f"verify_error:{verify_error}",)
    mechanical_pass = verdict == "pass" and not merged_undeclared and verify_error is None

    flags: list[str] = list(reasons)
    if verify_error:
        flags.append(f"verify_error:{verify_error}")
    actions: tuple[str, ...] = ()
    park: str | None = None
    if merged_undeclared or tier_p or not mechanical_pass or verify_error:
        kind = "tier_p" if tier_p else ("undeclared_write" if merged_undeclared else "policy")
        lane_key = lane if lane in {"swarm", "runner", "longhaul"} else "swarm"
        try:
            plan = plan_violation_response(
                ViolationContext(
                    lane=lane_key,  # type: ignore[arg-type]
                    kind=kind,  # type: ignore[arg-type]
                    holder_id=holder_id or "unknown",
                    paths=tuple(str(p) for p in merged_undeclared),
                    tier_p=tier_p,
                    message="; ".join(reasons) if reasons else (verify_error or kind),
                )
            )
            actions = plan.actions
            park = plan.park_state
            if plan.flag:
                flags.append(plan.flag)
        except ValueError:
            actions = ("notify_operator",)
            park = "waiting_review"
        if verify_error and not actions:
            actions = ("notify_operator", "flag_scope_violation")
            park = park or "waiting_review"

    return PostAttemptResult(
        mechanical_pass=mechanical_pass,
        assess_verdict=verdict,
        assess_reasons=reasons,
        violation_actions=actions,
        park_state=park,
        flags=tuple(flags),
    )
