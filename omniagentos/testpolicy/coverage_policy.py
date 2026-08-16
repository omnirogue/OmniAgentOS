"""Whole-package coverage policy (M-10, M-22).

Rejects misleading subset artifacts (the audited ".coverage reports 85% over
17 modules / ~3.5% of the package") and evaluates boundary-module presence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.testpolicy.policy_load import load_policy_dict, repo_root


@dataclass(frozen=True)
class CoveragePolicy:
    source_packages: tuple[str, ...]
    min_module_fraction: float
    min_measured_modules: int
    global_line_floor: float
    boundary_modules: tuple[str, ...]
    subsystem_floors: Mapping[str, float]
    reject_subset_artifacts: bool
    subset_rejection_message: str


@dataclass(frozen=True)
class CoverageReportSummary:
    """Normalized view of a coverage report (coverage.py JSON or synthetic)."""

    measured_modules: tuple[str, ...]
    statements: int
    covered: int
    # Optional per-module statement counts for boundary/subsystem checks.
    module_statements: Mapping[str, int] = field(default_factory=dict)
    module_covered: Mapping[str, int] = field(default_factory=dict)

    @property
    def line_rate(self) -> float:
        if self.statements <= 0:
            return 0.0
        return self.covered / self.statements


@dataclass(frozen=True)
class CoverageEvaluation:
    ok: bool
    reasons: tuple[str, ...]
    measured_modules: int
    package_modules: int
    module_fraction: float
    missing_boundaries: tuple[str, ...]
    is_subset: bool
    # M-22: boundaries present in the report but with zero measured statements.
    zero_statement_boundaries: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "measured_modules": self.measured_modules,
            "package_modules": self.package_modules,
            "module_fraction": self.module_fraction,
            "missing_boundaries": list(self.missing_boundaries),
            "zero_statement_boundaries": list(self.zero_statement_boundaries),
            "is_subset": self.is_subset,
        }


def load_coverage_policy(path: str | None = None) -> CoveragePolicy:
    data = load_policy_dict(path)
    cov = data.get("coverage") or {}
    floors = cov.get("subsystem_floors") or {}
    return CoveragePolicy(
        source_packages=tuple(cov.get("source_packages") or ("omniagentos",)),
        min_module_fraction=float(cov.get("min_module_fraction", 0.5)),
        min_measured_modules=int(cov.get("min_measured_modules", 100)),
        global_line_floor=float(cov.get("global_line_floor", 0.8)),
        boundary_modules=tuple(cov.get("boundary_modules") or ()),
        subsystem_floors={str(k): float(v) for k, v in floors.items()},
        reject_subset_artifacts=bool(cov.get("reject_subset_artifacts", True)),
        subset_rejection_message=str(
            cov.get("subset_rejection_message")
            or "Coverage artifact is a subset and must not be reported as package-wide."
        ),
    )


def count_package_modules(package: str = "omniagentos", root: Path | None = None) -> int:
    """Count importable .py modules under the package (excluding __pycache__)."""
    base = (root or repo_root()) / package.replace(".", "/")
    if not base.is_dir():
        return 0
    return sum(1 for path in base.rglob("*.py") if "__pycache__" not in path.parts)


def summary_from_coverage_json(payload: Mapping[str, Any]) -> CoverageReportSummary:
    """Parse coverage.py JSON (`coverage json -o -`) into a summary.

    Accepts either the modern ``files`` map or a simplified synthetic dict used
    by tests: ``{"files": {"omniagentos/foo.py": {"summary": {...}}}}``.
    """
    files = payload.get("files") or {}
    modules: list[str] = []
    module_statements: dict[str, int] = {}
    module_covered: dict[str, int] = {}
    total_stmts = 0
    total_covered = 0
    for file_path, meta in files.items():
        if not isinstance(meta, Mapping):
            continue
        summary_obj = meta.get("summary")
        summary_map: Mapping[str, Any] = summary_obj if isinstance(summary_obj, Mapping) else meta
        stmts = int(summary_map.get("num_statements") or summary_map.get("statements") or 0)
        covered = int(summary_map.get("covered_lines") or summary_map.get("covered") or 0)
        if stmts <= 0 and "executed_lines" in meta:
            # Fallback shape
            executed = meta.get("executed_lines") or []
            missing = meta.get("missing_lines") or []
            covered = len(executed)
            stmts = covered + len(missing)
        mod = _path_to_module(str(file_path))
        if not mod.startswith("omniagentos"):
            continue
        modules.append(mod)
        module_statements[mod] = stmts
        module_covered[mod] = covered
        total_stmts += stmts
        total_covered += covered
    totals = payload.get("totals") or {}
    if totals:
        total_stmts = int(totals.get("num_statements") or total_stmts)
        total_covered = int(totals.get("covered_lines") or total_covered)
    return CoverageReportSummary(
        measured_modules=tuple(sorted(set(modules))),
        statements=total_stmts,
        covered=total_covered,
        module_statements=module_statements,
        module_covered=module_covered,
    )


def summary_from_module_list(
    modules: Iterable[str],
    *,
    statements: int,
    covered: int,
) -> CoverageReportSummary:
    mods = tuple(sorted(set(modules)))
    per = {m: max(1, statements // max(1, len(mods))) for m in mods}
    per_cov = {m: max(0, covered // max(1, len(mods))) for m in mods}
    return CoverageReportSummary(
        measured_modules=mods,
        statements=statements,
        covered=covered,
        module_statements=per,
        module_covered=per_cov,
    )


def evaluate_coverage_report(
    report: CoverageReportSummary,
    policy: CoveragePolicy | None = None,
    *,
    package_module_count: int | None = None,
) -> CoverageEvaluation:
    """Return ok=False for subset / missing-boundary reports (M-10, M-22)."""
    pol = policy or load_coverage_policy()
    package_n = package_module_count
    if package_n is None:
        package_n = 0
        for pkg in pol.source_packages:
            package_n += count_package_modules(pkg)
    measured_n = len(report.measured_modules)
    fraction = (measured_n / package_n) if package_n else 0.0
    reasons: list[str] = []
    is_subset = False

    if pol.reject_subset_artifacts:
        if measured_n < pol.min_measured_modules:
            is_subset = True
            reasons.append(
                f"measured_modules={measured_n} < min_measured_modules={pol.min_measured_modules}"
            )
        if package_n and fraction < pol.min_module_fraction:
            is_subset = True
            reasons.append(
                f"module_fraction={fraction:.3f} < min_module_fraction={pol.min_module_fraction}"
            )
        if is_subset:
            reasons.append(pol.subset_rejection_message)

    missing_boundaries, zero_statement_boundaries = _boundary_presence(report, pol.boundary_modules)
    if missing_boundaries:
        reasons.append("missing boundary modules: " + ", ".join(missing_boundaries))
    # M-22: a boundary that appears only as a zero-statement shell is not measured.
    if zero_statement_boundaries:
        reasons.append("zero-statement boundary modules: " + ", ".join(zero_statement_boundaries))

    # Global line floor only applies to non-subset whole-package reports.
    if not is_subset and report.statements > 0 and report.line_rate < pol.global_line_floor:
        reasons.append(
            f"line_rate={report.line_rate:.3f} < global_line_floor={pol.global_line_floor}"
        )

    ok = not reasons
    return CoverageEvaluation(
        ok=ok,
        reasons=tuple(reasons),
        measured_modules=measured_n,
        package_modules=package_n,
        module_fraction=fraction,
        missing_boundaries=missing_boundaries,
        is_subset=is_subset,
        zero_statement_boundaries=zero_statement_boundaries,
    )


def _boundary_presence(
    report: CoverageReportSummary, boundaries: Iterable[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (missing_boundaries, zero_statement_boundaries).

    A boundary is satisfied only when at least one matching measured module has
    a nonzero statement count (M-22). Presence alone is insufficient.
    """
    measured_set = set(report.measured_modules)
    stmts = report.module_statements or {}
    missing: list[str] = []
    zero: list[str] = []
    for boundary in boundaries:
        matching = [
            mod for mod in measured_set if mod == boundary or mod.startswith(boundary + ".")
        ]
        if not matching:
            missing.append(boundary)
            continue
        total = sum(int(stmts.get(mod, 0)) for mod in matching)
        if total <= 0:
            zero.append(boundary)
    return tuple(missing), tuple(zero)


def _missing_boundaries(measured: Iterable[str], boundaries: Iterable[str]) -> tuple[str, ...]:
    """Backward-compatible helper: modules not present at all (ignores counts)."""
    measured_set = set(measured)
    missing: list[str] = []
    for boundary in boundaries:
        if any(mod == boundary or mod.startswith(boundary + ".") for mod in measured_set):
            continue
        if boundary in measured_set:
            continue
        missing.append(boundary)
    return tuple(missing)


def _path_to_module(path: str) -> str:
    text = path.replace("\\", "/")
    if "omniagentos/" in text:
        text = text[text.index("omniagentos/") :]
    if text.endswith(".py"):
        text = text[:-3]
    if text.endswith("/__init__"):
        text = text[: -len("/__init__")]
    return text.replace("/", ".")
