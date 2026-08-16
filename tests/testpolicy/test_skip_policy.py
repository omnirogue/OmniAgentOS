"""M-41 — required suite skips must be surfaced, not silent."""

from __future__ import annotations

import textwrap

from omniagentos.testpolicy.policy_load import clear_policy_cache
from omniagentos.testpolicy.skip_policy import (
    RequiredSuite,
    SkipRecord,
    load_required_suites,
    surface_required_skips,
)


def setup_function() -> None:
    clear_policy_cache()


def test_required_suites_are_configured() -> None:
    suites = load_required_suites()
    ids = {s.id for s in suites}
    assert "knowledge_pg_role_boundary" in ids
    assert "sandbox_exec_guardrails" in ids
    assert "restart_recovery" in ids


def test_postgres_skip_is_surfaced() -> None:
    report = surface_required_skips(
        [
            SkipRecord(
                nodeid="tests/knowledge/test_schema.py::test_roles",
                reason="PostgreSQL unavailable",
            )
        ]
    )
    assert report.has_surfaced_skips
    assert any(sid == "knowledge_pg_role_boundary" for sid, _ in report.matched_skips)
    assert any("REQUIRED_SUITE_SKIPPED" in msg for msg in report.surfaced_messages)
    assert any("knowledge_pg_role_boundary" in msg for msg in report.surfaced_messages)


def test_unrelated_skip_is_not_required_surface() -> None:
    report = surface_required_skips(
        [
            SkipRecord(
                nodeid="tests/adapters/test_gemini.py::test_optional",
                reason="CLI not installed",
            )
        ]
    )
    assert report.matched_skips == ()


def test_absent_required_suite_collection_is_surfaced() -> None:
    report = surface_required_skips(
        skips=[],
        collected_nodeids=[
            "tests/api/test_health.py::test_ok",
            "tests/policy/test_shell.py::test_rm",
        ],
    )
    # knowledge suite never collected
    assert "knowledge_pg_role_boundary" in report.missing_required_runs
    assert any("REQUIRED_SUITE_ABSENT" in m for m in report.surfaced_messages)


def test_markers_any_filters_collection_for_absence() -> None:
    """M-41: suite with markers_any is absent unless a matching marker was collected."""
    suite = RequiredSuite(
        id="pg_marked",
        description="marker-gated pg suite",
        path_globs=("tests/knowledge/**",),
        skip_reasons_expected=("PostgreSQL unavailable",),
        env_requirement="postgres",
        surface_when_skipped=True,
        markers_any=("postgres",),
    )
    # Path matches knowledge, but markers do not include postgres → absent.
    report = surface_required_skips(
        skips=[],
        collected_items=[
            {
                "nodeid": "tests/knowledge/test_schema.py::test_roles",
                "path": "tests/knowledge/test_schema.py",
                "markers": ("unit",),
            }
        ],
        suites=[suite],
    )
    assert "pg_marked" in report.missing_required_runs

    # Same path with the required marker → present (no ABSENT).
    report_ok = surface_required_skips(
        skips=[],
        collected_items=[
            {
                "nodeid": "tests/knowledge/test_schema.py::test_roles",
                "path": "tests/knowledge/test_schema.py",
                "markers": ("postgres", "unit"),
            }
        ],
        suites=[suite],
    )
    assert report_ok.missing_required_runs == ()


def test_markers_any_filters_skip_match() -> None:
    suite = RequiredSuite(
        id="sandbox_marked",
        description="marker-gated sandbox",
        path_globs=("tests/runner/**",),
        skip_reasons_expected=("sandbox-exec",),
        env_requirement="sandbox_exec",
        surface_when_skipped=True,
        markers_any=("live_sandbox",),
    )
    # Skip has path+reason but wrong markers → not matched.
    no_match = surface_required_skips(
        [
            SkipRecord(
                nodeid="tests/runner/test_guardrail_ac_policy.py::test_live",
                reason="sandbox-exec unavailable",
                markers=("unit",),
            )
        ],
        suites=[suite],
        check_absent=False,
    )
    assert no_match.matched_skips == ()

    match = surface_required_skips(
        [
            SkipRecord(
                nodeid="tests/runner/test_guardrail_ac_policy.py::test_live",
                reason="sandbox-exec unavailable",
                markers=("live_sandbox",),
            )
        ],
        suites=[suite],
        check_absent=False,
    )
    assert any(sid == "sandbox_marked" for sid, _ in match.matched_skips)


def test_off_list_reason_is_surfaced_not_suppressed() -> None:
    """A required suite disabled for an UNEXPECTED reason is the case M-41 exists for.

    ``skip_reasons_expected`` names the benign, environment-driven reasons a
    suite is allowed to vanish for. Using it as a precondition for reporting
    inverts the guard: the routine skip gets a line and the anomalous one —
    ``@pytest.mark.skip(reason="flaky")`` on a security/recovery suite — goes
    silent. ``REQUIRED_SUITE_ABSENT`` is not a backstop, because a skipped test
    is still collected.
    """
    suite = RequiredSuite(
        id="restart_recovery",
        description="restart / recovery paths",
        path_globs=("tests/reliability/**",),
        skip_reasons_expected=("restart", "recovery"),
        env_requirement="none",
        surface_when_skipped=True,
    )
    nodeid = "tests/reliability/test_restart.py::test_resumes_after_crash"
    collected = [{"nodeid": nodeid, "path": "tests/reliability/test_restart.py", "markers": ()}]

    for reason in (
        "temporarily disabled while we debug",
        "flaky in CI",
        "",
    ):
        report = surface_required_skips(
            [SkipRecord(nodeid=nodeid, reason=reason, path="tests/reliability/test_restart.py")],
            collected_items=collected,
            suites=[suite],
            check_absent=True,
        )
        # The suite went dark; something must say so.
        assert report.surfaced_messages, f"no signal at all for reason={reason!r}"
        assert any(sid == "restart_recovery" for sid, _ in report.matched_skips), (
            f"off-list reason {reason!r} was suppressed instead of surfaced"
        )
        # And it must be distinguishable from a routine environment skip.
        assert any("REQUIRED_SUITE_SKIPPED_UNEXPECTED" in m for m in report.surfaced_messages), (
            f"off-list reason {reason!r} was not flagged as unexpected"
        )
        assert report.unexpected_skips, f"unexpected_skips empty for reason={reason!r}"

    # Control: an on-list environment reason stays the routine signal.
    routine = surface_required_skips(
        [
            SkipRecord(
                nodeid=nodeid,
                reason="recovery harness unavailable",
                path="tests/reliability/test_restart.py",
            )
        ],
        collected_items=collected,
        suites=[suite],
        check_absent=True,
    )
    assert any(sid == "restart_recovery" for sid, _ in routine.matched_skips)
    assert routine.unexpected_skips == ()
    assert not any("REQUIRED_SUITE_SKIPPED_UNEXPECTED" in m for m in routine.surfaced_messages)
    assert any(m.startswith("REQUIRED_SUITE_SKIPPED ") for m in routine.surfaced_messages)


def _child_pytest_env(policy_path: str) -> dict[str, str]:
    import os
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["OMNIAGENTOS_COVERAGE_POLICY"] = policy_path
    # Ensure omniagentos is importable when cwd is a temp dir.
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(repo) + (os.pathsep + prev if prev else "")
    return env


def test_pytest_plugin_surfaces_skip_in_real_session(tmp_path) -> None:
    """M-41: real pytest hook records a required-suite skip and prints the signal."""
    import subprocess
    import sys

    policy = tmp_path / "coverage_policy.yaml"
    policy.write_text(
        textwrap.dedent(
            """\
            version: 1
            coverage:
              source_packages: [omniagentos]
              min_module_fraction: 0.5
              min_measured_modules: 100
              global_line_floor: 0.8
              boundary_modules: []
              subsystem_floors: {}
              reject_subset_artifacts: true
              subset_rejection_message: subset
            required_suites:
              - id: demo_pg
                description: demo required suite
                path_globs:
                  - test_demo_required.py
                markers_any: []
                skip_reasons_expected:
                  - PostgreSQL unavailable
                env_requirement: postgres
                surface_when_skipped: true
            scale: {}
            classification: {}
            """
        ),
        encoding="utf-8",
    )
    test_file = tmp_path / "test_demo_required.py"
    test_file.write_text(
        textwrap.dedent(
            """\
            import pytest

            def test_needs_pg():
                pytest.skip("PostgreSQL unavailable")
            """
        ),
        encoding="utf-8",
    )
    env = _child_pytest_env(str(policy))
    env["OMNIAGENTOS_SURFACE_REQUIRED_ABSENT"] = "0"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "omniagentos.testpolicy.pytest_plugin",
            "--surface-required-suites",
            "-q",
            str(test_file),
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "1 skipped" in combined or "skipped" in combined.lower()
    assert "REQUIRED_SUITE_SKIPPED" in combined
    assert "demo_pg" in combined


def test_pytest_plugin_surfaces_vanished_suite(tmp_path) -> None:
    """M-41: vanished suite (never collected) is ABSENT under broad surface."""
    import subprocess
    import sys

    policy = tmp_path / "coverage_policy.yaml"
    policy.write_text(
        textwrap.dedent(
            """\
            version: 1
            coverage:
              source_packages: [omniagentos]
              min_module_fraction: 0.5
              min_measured_modules: 100
              global_line_floor: 0.8
              boundary_modules: []
              subsystem_floors: {}
              reject_subset_artifacts: true
              subset_rejection_message: subset
            required_suites:
              - id: vanished_suite
                description: never collected
                path_globs:
                  - tests/knowledge/**
                markers_any: []
                skip_reasons_expected: []
                env_requirement: postgres
                surface_when_skipped: true
            scale: {}
            classification: {}
            """
        ),
        encoding="utf-8",
    )
    test_file = tmp_path / "test_only_api.py"
    test_file.write_text(
        textwrap.dedent(
            """\
            def test_ok():
                assert True
            """
        ),
        encoding="utf-8",
    )
    env = _child_pytest_env(str(policy))
    env["OMNIAGENTOS_SURFACE_REQUIRED_ABSENT"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "omniagentos.testpolicy.pytest_plugin",
            "--surface-required-suites",
            "-q",
            str(test_file),
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "1 passed" in combined or "passed" in combined.lower()
    assert "REQUIRED_SUITE_ABSENT" in combined
    assert "vanished_suite" in combined
