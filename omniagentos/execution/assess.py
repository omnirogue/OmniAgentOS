"""Post-execution assessment ladder: mechanical evidence, assessor, retry, panel."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.contracts import AssessmentVerdict, DeclaredScope
from omniagentos.path_containment import inode_relative_parts_anchored

__all__ = ["Assessor", "AssessmentContext", "AssessmentResult", "assess", "fix_scope_after_fail"]


@dataclass(frozen=True, slots=True)
class AssessmentContext:
    working_dir: Path
    expected_files: tuple[str, ...]
    scope_verdict: Mapping[str, Any] | None
    more_budget: bool = False
    execution_level: int | None = None
    keyword_advisory: bool = False


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    verdict: AssessmentVerdict = AssessmentVerdict.NEEDS_REVIEW
    reasons: tuple[str, ...] = ()


type Assessor = Callable[[AssessmentContext], AssessmentResult | dict[str, Any] | str]


def _verdict(value: Any) -> AssessmentVerdict:
    if isinstance(value, AssessmentVerdict):
        return value
    try:
        return AssessmentVerdict(str(value).strip().lower())
    except ValueError:
        return AssessmentVerdict.NEEDS_REVIEW


def _result(value: AssessmentResult | Mapping[str, Any] | str | Any) -> dict[str, Any]:
    if isinstance(value, AssessmentResult):
        return {"verdict": value.verdict.value, "reasons": list(value.reasons)}
    if isinstance(value, Mapping):
        verdict = _verdict(value.get("verdict", value.get("result", "needs_review")))
        reasons = value.get("reasons", value.get("reason", ()))
        if isinstance(reasons, str):
            reasons = [reasons]
        return {"verdict": verdict.value, "reasons": [str(item) for item in (reasons or ())]}
    return {"verdict": _verdict(value).value, "reasons": []}


def _mechanical(
    working_dir: Path,
    expected_files: tuple[str, ...],
    min_bytes: int,
    scope_verdict: Mapping[str, Any] | None,
) -> dict[str, Any]:
    missing: list[str] = []
    undersized: list[str] = []
    for expected in expected_files:
        candidate = working_dir / expected
        if inode_relative_parts_anchored(candidate.resolve(), working_dir.resolve()) is None:
            missing.append(expected)
            continue
        if not candidate.is_file():
            missing.append(expected)
        elif candidate.stat().st_size < min_bytes:
            undersized.append(expected)
    violations = list(
        (scope_verdict or {}).get("undeclared") or (scope_verdict or {}).get("missing") or ()
    )
    reasons: list[str] = []
    if missing:
        reasons.append(f"missing expected files: {', '.join(missing)}")
    if undersized:
        reasons.append(f"expected files below {min_bytes} bytes: {', '.join(undersized)}")
    if violations:
        reasons.append(f"scope violations observed: {', '.join(str(item) for item in violations)}")
    return {
        "pass": not missing and not undersized,
        "missing": missing,
        "undersized": undersized,
        "violations": violations,
        "reasons": reasons,
    }


def _manifest_grade(manifest: Any, expected_files: tuple[str, ...]) -> dict[str, Any] | None:
    if manifest is None:
        return None
    try:
        from omniagentos.workmodes.manifest import grade_manifest, load_manifest

        loaded = load_manifest(str(manifest)) if isinstance(manifest, (str, Path)) else manifest
        grade = grade_manifest(loaded, expected_files=expected_files or None)
        return {"verdict": grade.verdict.value, "reasons": list(grade.reasons)}
    except Exception:  # optional protocol must never break ordinary code assessment
        return None


def _workmodes_verify_hook(artifact_manifest: Any) -> dict[str, Any] | None:
    """Partial verification hook: run bridged workmodes validators when requested.

    Gated by ``OMNIAGENTOS_MCP_BRIDGE_MODE`` (off → shadow → enforce). When the
    mode is ``off`` or the manifest carries no ``workmodes_checks``, this is a
    no-op and the assess ladder is unchanged. Shadow records findings without
    flipping the mechanical verdict; enforce can fail the mechanical step.
    """
    if artifact_manifest is None:
        return None
    try:
        from omniagentos.toolplane.mcp_mode import mcp_bridge_mode
        from omniagentos.toolplane.workmodes_bridge import run_assess_validators

        mode = mcp_bridge_mode()
        if mode == "off":
            return None
        checks = None
        if isinstance(artifact_manifest, Mapping):
            checks = artifact_manifest.get("workmodes_checks")
        else:
            checks = getattr(artifact_manifest, "workmodes_checks", None)
        if not checks:
            return None
        if not isinstance(checks, (list, tuple)):
            checks = [checks]
        return run_assess_validators(checks, mode=mode)
    except Exception:  # optional bridge must never break ordinary code assessment
        return None


def _call_panel(judges: Any, context: AssessmentContext) -> dict[str, Any]:
    target = getattr(judges, "review", judges)
    try:
        return _result(target(context))
    except TypeError:
        return _result(target())


def assess(
    *,
    working_dir: str | Path,
    expected_files: Iterable[str] = (),
    min_bytes: int = 1,
    scope_verdict: Mapping[str, Any] | None = None,
    assessor: Assessor | None = None,
    judges: Any = None,
    execution_level: int | None = None,
    keyword_advisory: bool = False,
    max_rung: int = 3,
    artifact_manifest: Any = None,
    parent_scope: DeclaredScope | Mapping[str, Any] | Iterable[str] | None = None,
    observed_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Run rungs 0--3, returning evidence even when scope is only observed."""
    root = Path(working_dir)
    expected = tuple(str(path) for path in expected_files)
    max_rung = max(0, min(3, int(max_rung)))
    mechanical = _mechanical(root, expected, max(0, int(min_bytes)), scope_verdict)
    manifest = _manifest_grade(artifact_manifest, expected)
    if manifest is not None:
        mechanical["manifest"] = manifest
        if manifest["verdict"] == AssessmentVerdict.FAIL.value:
            mechanical["pass"] = False
            mechanical["reasons"].extend(manifest["reasons"])
    # Partial verification hook (workmodes validators via MCP bridge, flag-gated).
    workmodes_verify = _workmodes_verify_hook(artifact_manifest)
    if workmodes_verify is not None:
        mechanical["workmodes_verify"] = workmodes_verify
        if (
            workmodes_verify.get("mode") == "enforce"
            and workmodes_verify.get("verdict") == AssessmentVerdict.FAIL.value
        ):
            mechanical["pass"] = False
            mechanical["reasons"].extend(list(workmodes_verify.get("reasons") or ()))
    result: dict[str, Any] = {
        "verdict": AssessmentVerdict.PASS.value
        if mechanical["pass"]
        else AssessmentVerdict.FAIL.value,
        "rung": 0,
        "reasons": list(mechanical["reasons"]),
        "mechanical": mechanical,
        "assessor": None,
        "judges": None,
        "fix_scope": None,
        "keyword_advisory": keyword_advisory,
        "execution_level": execution_level,
    }
    if not mechanical["pass"]:
        if parent_scope is not None and observed_paths is not None:
            result["fix_scope"] = fix_scope_after_fail(parent_scope, observed_paths)
        return result

    context = AssessmentContext(
        root, expected, scope_verdict, False, execution_level, keyword_advisory
    )
    if assessor is not None and max_rung >= 1:
        assessed = _result(assessor(context))
        result["assessor"] = {"attempts": [assessed]}
        result["rung"] = 1
        result["verdict"] = assessed["verdict"]
        result["reasons"].extend(assessed["reasons"])
        if assessed["verdict"] != AssessmentVerdict.PASS.value and max_rung >= 2:
            retry = _result(
                assessor(
                    AssessmentContext(
                        root, expected, scope_verdict, True, execution_level, keyword_advisory
                    )
                )
            )
            result["assessor"]["attempts"].append(retry)
            result["rung"] = 2
            result["verdict"] = retry["verdict"]
            result["reasons"].extend(retry["reasons"])

    needs_panel = (
        judges is not None
        and max_rung >= 3
        and (
            execution_level is not None
            and execution_level >= 3
            or result["verdict"] != AssessmentVerdict.PASS.value
        )
    )
    if needs_panel:
        judged = _call_panel(judges, context)
        result["judges"] = judged
        result["rung"] = 3
        result["verdict"] = judged["verdict"]
        result["reasons"].extend(judged["reasons"])
    if (
        result["verdict"] == AssessmentVerdict.FAIL.value
        and parent_scope is not None
        and observed_paths is not None
    ):
        result["fix_scope"] = fix_scope_after_fail(parent_scope, observed_paths)
    return result


def fix_scope_after_fail(
    parent_scope: DeclaredScope | Mapping[str, Any] | Iterable[str], observed_paths: Iterable[str]
) -> list[str]:
    """Return only declared paths which were actually observed; never widen scope."""
    if isinstance(parent_scope, Mapping):
        parent: list[str] = []
        for field in (
            "files_to_modify",
            "files_to_create",
            "files_to_delete",
            "create_roots",
            "must_modify",
        ):
            parent.extend(str(item) for item in (parent_scope.get(field) or ()))
    elif isinstance(parent_scope, DeclaredScope) or any(
        hasattr(parent_scope, field)
        for field in (
            "files_to_modify",
            "files_to_create",
            "files_to_delete",
            "create_roots",
            "must_modify",
        )
    ):
        parent = []
        for field in (
            "files_to_modify",
            "files_to_create",
            "files_to_delete",
            "create_roots",
            "must_modify",
        ):
            parent.extend(str(item) for item in (getattr(parent_scope, field, None) or ()))
    elif isinstance(parent_scope, str):
        parent = [parent_scope]
    else:
        parent = [str(item) for item in parent_scope]
    observed = {str(path).replace("\\", "/").removeprefix("./") for path in observed_paths}
    return list(
        dict.fromkeys(
            path for path in parent if path.replace("\\", "/").removeprefix("./") in observed
        )
    )
