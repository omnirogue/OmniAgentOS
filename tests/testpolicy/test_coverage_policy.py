"""M-10 / M-22 — whole-package coverage policy rejects subset artifacts."""

from __future__ import annotations

from omniagentos.testpolicy.coverage_policy import (
    CoverageReportSummary,
    count_package_modules,
    evaluate_coverage_report,
    load_coverage_policy,
    summary_from_coverage_json,
    summary_from_module_list,
)
from omniagentos.testpolicy.policy_load import clear_policy_cache


def setup_function() -> None:
    clear_policy_cache()


def test_policy_loads_whole_package_source() -> None:
    policy = load_coverage_policy()
    assert "omniagentos" in policy.source_packages
    assert policy.min_measured_modules >= 100
    assert policy.reject_subset_artifacts is True
    assert len(policy.boundary_modules) >= 5


def test_misleading_subset_artifact_is_rejected() -> None:
    """Recreate the audited .coverage shape: 17 modules, ~85% lines, 3.5% package."""
    policy = load_coverage_policy()
    modules = [
        "omniagentos.orgdims.service",
        "omniagentos.metacog.service",
        "omniagentos.graph_runtime.service",
        "omniagentos.cbm.service",
    ] + [f"omniagentos.subset.extra_{i}" for i in range(13)]
    assert len(modules) == 17
    report = summary_from_module_list(modules, statements=2025, covered=1721)
    package_n = max(count_package_modules(), 500)
    result = evaluate_coverage_report(report, policy, package_module_count=package_n)
    assert result.ok is False
    assert result.is_subset is True
    assert any("subset" in r.lower() or "min_measured" in r for r in result.reasons)


def test_whole_package_report_with_boundaries_passes_structure_gate() -> None:
    policy = load_coverage_policy()
    package_n = 200
    # Build a synthetic whole-package module list including every boundary.
    modules = [f"omniagentos.pkg.mod_{i}" for i in range(package_n)]
    for boundary in policy.boundary_modules:
        modules.append(boundary)
        modules.append(boundary + ".impl")
    report = summary_from_module_list(modules, statements=50_000, covered=42_000)
    result = evaluate_coverage_report(report, policy, package_module_count=package_n)
    assert result.is_subset is False
    assert result.missing_boundaries == ()
    assert result.zero_statement_boundaries == ()
    # May still fail global line floor if rate low; 42000/50000 = 0.84 >= 0.80
    assert result.ok is True, result.reasons


def test_missing_boundary_modules_are_reported() -> None:
    policy = load_coverage_policy()
    modules = [f"omniagentos.happy.path_{i}" for i in range(150)]
    report = summary_from_module_list(modules, statements=10_000, covered=9_000)
    result = evaluate_coverage_report(report, policy, package_module_count=200)
    assert result.ok is False
    assert len(result.missing_boundaries) == len(policy.boundary_modules)


def test_zero_statement_boundary_modules_are_rejected() -> None:
    """M-22: boundary present with 0 measured statements is not satisfied."""
    policy = load_coverage_policy()
    package_n = 200
    modules = [f"omniagentos.pkg.mod_{i}" for i in range(package_n)]
    module_statements: dict[str, int] = {m: 50 for m in modules}
    module_covered: dict[str, int] = {m: 40 for m in modules}
    # Every boundary appears, but with zero statements.
    for boundary in policy.boundary_modules:
        modules.append(boundary)
        module_statements[boundary] = 0
        module_covered[boundary] = 0
    report = CoverageReportSummary(
        measured_modules=tuple(sorted(set(modules))),
        statements=sum(module_statements.values()),
        covered=sum(module_covered.values()),
        module_statements=module_statements,
        module_covered=module_covered,
    )
    result = evaluate_coverage_report(report, policy, package_module_count=package_n)
    assert result.ok is False
    assert result.missing_boundaries == ()
    assert set(result.zero_statement_boundaries) == set(policy.boundary_modules)
    assert any("zero-statement" in r for r in result.reasons)


def test_demo_subset_cli_structured_assertion_exits_zero(tmp_path) -> None:
    """M-10: --demo-subset is a structured self-check (exit 0 = rejection proven)."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "coverage" / "check_coverage_policy.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--demo-subset"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "demo_subset"
    assert payload["status"] == "policy_rejected_subset_as_expected"
    assert payload["checks"]["is_subset"] is True
    assert payload["checks"]["ok_is_false"] is True


def test_summary_from_coverage_json_filters_to_omniagentos() -> None:
    payload = {
        "files": {
            "omniagentos/api/eventbus.py": {"summary": {"num_statements": 40, "covered_lines": 30}},
            "tests/foo.py": {"summary": {"num_statements": 10, "covered_lines": 10}},
        },
        "totals": {"num_statements": 40, "covered_lines": 30},
    }
    summary = summary_from_coverage_json(payload)
    assert summary.measured_modules == ("omniagentos.api.eventbus",)
    assert summary.statements == 40
    assert summary.covered == 30
