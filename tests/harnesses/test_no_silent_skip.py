"""A required gate selection that only skips must not exit 0.

These run real ``pytest`` subprocesses over generated files. Asserting the exit
code of an actual pytest session is the point: the defect being regressed is a
property of pytest's own exit-code contract, which no in-process fake reproduces.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from omniagentos.harnesses.no_silent_skip import NO_RESULT_EXIT_CODE
from omniagentos.harnesses.release_gate import (
    NO_SILENT_SKIP_PLUGIN,
    default_phase_specs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``pytest.ExitCode.USAGE_ERROR`` — what argparse produces for an unknown value.
PYTEST_USAGE_ERROR = int(pytest.ExitCode.USAGE_ERROR)


def _run_pytest(target: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *extra,
            str(target),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _write(tmp_path: Path, name: str, body: str) -> Path:
    target = tmp_path / name
    target.write_text(textwrap.dedent(body), encoding="utf-8")
    return target


ALL_SKIP = """
    import pytest

    @pytest.mark.skip(reason="payload unavailable")
    def test_required_behaviour():
        assert False
"""

MODULE_SKIP = """
    import pytest
    pytest.skip("required payload not installed", allow_module_level=True)

    def test_required_behaviour():
        assert False
"""

REAL = """
    def test_required_behaviour():
        assert True
"""


def test_baseline_pytest_exits_zero_when_everything_skips(tmp_path: Path) -> None:
    """Establishes the defect: plain pytest calls an all-skipped run a success."""
    result = _run_pytest(_write(tmp_path, "test_all_skip.py", ALL_SKIP))
    assert result.returncode == 0, "premise changed: plain pytest no longer passes on all-skip"


@pytest.mark.parametrize(
    ("name", "body"),
    [
        pytest.param("test_all_skip.py", ALL_SKIP, id="every-test-skipped"),
        pytest.param("test_module_skip.py", MODULE_SKIP, id="whole-module-skipped"),
    ],
)
def test_plugin_fails_a_selection_that_only_skips(tmp_path: Path, name: str, body: str) -> None:
    """Asserted as the guard's own exit code, not merely as "non-zero".

    Bare pytest already exits 5 for a module-level skip (nothing is collected),
    so ``!= 0`` would be satisfied with the guard deleted — true for the wrong
    reason. The baseline below therefore pins only that pytest's own code is
    *not* ``NO_RESULT_EXIT_CODE``, and the result must carry the guard's.
    """
    target = _write(tmp_path, name, body)
    baseline = _run_pytest(target)
    assert baseline.returncode != NO_RESULT_EXIT_CODE, (
        f"premise changed: bare pytest already returns {NO_RESULT_EXIT_CODE} for "
        f"{name}, so this test could no longer attribute the refusal to the guard"
    )

    result = _run_pytest(target, "-p", NO_SILENT_SKIP_PLUGIN)
    assert result.returncode == NO_RESULT_EXIT_CODE, (
        f"skipped required selection not refused by the guard "
        f"(got {result.returncode})\n{result.stdout}\n{result.stderr}"
    )
    assert "no_silent_skip" in result.stdout + result.stderr


def test_plugin_fails_a_selection_that_matches_nothing(tmp_path: Path) -> None:
    """An empty marker selection is a false green too, not just an all-skip.

    Asserted as the guard's *own* exit code rather than as "non-zero", because
    plain pytest already exits 5 (EXIT_NOTESTSCOLLECTED) for a selection that
    matches nothing — so ``!= 0`` is satisfied whether or not the plugin does
    anything, and would keep passing if the guard were deleted. Demanding
    ``NO_RESULT_EXIT_CODE`` distinguishes the guard's refusal from pytest's
    incidental one, and the baseline below pins that they differ.
    """
    target = _write(tmp_path, "test_real.py", REAL)
    baseline = _run_pytest(target, "-m", "no_such_marker_exists")
    assert baseline.returncode != NO_RESULT_EXIT_CODE, (
        f"premise changed: bare pytest already returns {NO_RESULT_EXIT_CODE}, so this "
        f"test could no longer tell the guard apart from pytest's own exit"
    )

    result = _run_pytest(target, "-p", NO_SILENT_SKIP_PLUGIN, "-m", "no_such_marker_exists")
    assert result.returncode == NO_RESULT_EXIT_CODE, (
        f"empty selection not refused by the guard (got {result.returncode})\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "no_silent_skip" in result.stdout + result.stderr


def test_plugin_still_passes_a_genuinely_executed_selection(tmp_path: Path) -> None:
    """The guard must not turn real passes into failures."""
    result = _run_pytest(_write(tmp_path, "test_real.py", REAL), "-p", NO_SILENT_SKIP_PLUGIN)
    assert result.returncode == 0, f"guard broke a real pass\n{result.stdout}\n{result.stderr}"


def test_plugin_preserves_a_genuine_failure(tmp_path: Path) -> None:
    """A real failure must keep pytest's own exit code, not be relabelled."""
    failing = _write(
        tmp_path,
        "test_failing.py",
        """
        def test_required_behaviour():
            assert False
        """,
    )
    result = _run_pytest(failing, "-p", NO_SILENT_SKIP_PLUGIN)
    assert result.returncode == 1, f"expected TESTS_FAILED(1), got {result.returncode}"


def test_plugin_refuses_a_payload_that_is_merely_expected_to_fail(tmp_path: Path) -> None:
    """``xfail`` is a skip for certification purposes, and deliberately so.

    A required payload marked known-broken exits 0 under plain pytest, which is
    the same false green as an outright skip: the behaviour the phase exists to
    certify did not work. Pinning it here makes that a decision rather than an
    artefact of pytest reporting ``xfail`` as ``skipped``.
    """
    xfailing = _write(
        tmp_path,
        "test_xfail.py",
        """
        import pytest

        @pytest.mark.xfail(reason="known broken")
        def test_required_behaviour():
            assert False
        """,
    )
    baseline = _run_pytest(xfailing)
    assert baseline.returncode == 0, "premise changed: plain pytest no longer passes on xfail"

    result = _run_pytest(xfailing, "-p", NO_SILENT_SKIP_PLUGIN)
    assert result.returncode == NO_RESULT_EXIT_CODE, (
        f"known-broken required payload not refused by the guard "
        f"(got {result.returncode})\n{result.stdout}\n{result.stderr}"
    )


MIXED = """
    import pytest

    def test_runs():
        assert True

    @pytest.mark.skip(reason="optional extra, legitimately unavailable")
    def test_optional():
        assert False
"""


def test_nonempty_mode_tolerates_a_suite_that_skips_only_some_tests(tmp_path: Path) -> None:
    """The premise that makes ``nonempty`` usable for the whole backend suite.

    ``required`` would fail this run, and that is why the backend phase cannot
    use it: optional extras and unavailable live providers skip legitimately.
    """
    target = _write(tmp_path, "test_mixed.py", MIXED)

    strict = _run_pytest(target, "-p", NO_SILENT_SKIP_PLUGIN, "--no-silent-skip-mode", "required")
    assert strict.returncode == NO_RESULT_EXIT_CODE, (
        "premise changed: 'required' no longer rejects an optional skip, so "
        "'nonempty' would have nothing to be looser about"
    )

    result = _run_pytest(target, "-p", NO_SILENT_SKIP_PLUGIN, "--no-silent-skip-mode", "nonempty")
    assert result.returncode == 0, (
        f"nonempty mode broke a suite with legitimate optional skips\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("name", "body", "extra"),
    [
        pytest.param("test_all_skip.py", ALL_SKIP, (), id="every-test-skipped"),
        pytest.param("test_module_skip.py", MODULE_SKIP, (), id="whole-module-skipped"),
        pytest.param(
            "test_real.py", REAL, ("-m", "no_such_marker_exists"), id="everything-deselected"
        ),
    ],
)
def test_nonempty_mode_still_refuses_a_run_that_executed_nothing(
    tmp_path: Path, name: str, body: str, extra: tuple[str, ...]
) -> None:
    """Tolerating *some* skips must not mean tolerating *all* of them.

    This is the hole the whole-suite backend phase had while it ran bare: a
    conftest error, an unset environment variable, or a mistyped marker can skip
    or deselect every test in the repository, and pytest reports that as success.
    A phase permitted to skip optionally is not thereby permitted to skip
    entirely.
    """
    target = _write(tmp_path, name, body)
    result = _run_pytest(
        target, "-p", NO_SILENT_SKIP_PLUGIN, "--no-silent-skip-mode", "nonempty", *extra
    )
    assert result.returncode == NO_RESULT_EXIT_CODE, (
        f"a run that executed nothing was certified (exit {result.returncode})\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "executed no tests" in result.stdout + result.stderr


def test_an_unrecognised_mode_is_rejected_rather_than_silently_loosened(tmp_path: Path) -> None:
    """A misspelt mode must not degrade into "no checking at all".

    Run against a payload that genuinely *executes*. An all-skip payload would
    make this vacuous: with the validation deleted, an unknown mode falls through
    to the non-``required`` branch, the zero-executed rule still fires, and the
    run exits ``NO_RESULT_EXIT_CODE`` — non-zero for a reason that has nothing to
    do with the mode being rejected. A real payload leaves ``USAGE_ERROR`` as the
    only way the run can be non-zero, so this fails if the validation is removed.
    """
    target = _write(tmp_path, "test_real.py", REAL)

    baseline = _run_pytest(target, "-p", NO_SILENT_SKIP_PLUGIN, "--no-silent-skip-mode", "nonempty")
    assert baseline.returncode == 0, (
        f"premise changed: this payload no longer passes under a valid mode, so a "
        f"non-zero result could not be attributed to the mode being rejected\n"
        f"{baseline.stdout}\n{baseline.stderr}"
    )

    result = _run_pytest(target, "-p", NO_SILENT_SKIP_PLUGIN, "--no-silent-skip-mode", "lenient")
    assert result.returncode == PYTEST_USAGE_ERROR, (
        f"an unknown strictness mode was accepted (got {result.returncode}, "
        f"expected pytest's usage error)\n{result.stdout}\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "invalid choice: 'lenient'" in combined, (
        f"the run failed, but not because the mode was refused:\n{combined}"
    )


def test_production_marker_selection_passes_when_payload_executes(tmp_path: Path) -> None:
    """Production marker-plus-multi-test-tree argv shape must exit 0 when payload passes."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    (tests_dir / "test_payload.py").write_text(
        textwrap.dedent("""
            import pytest

            @pytest.mark.smoke
            @pytest.mark.s19b_live_restart
            def test_live_restart_payload():
                assert True
        """),
        encoding="utf-8",
    )
    (tests_dir / "test_unrelated.py").write_text(
        textwrap.dedent("""
            import pytest

            @pytest.mark.smoke
            def test_other_smoke():
                assert True

            def test_unmarked():
                assert True
        """),
        encoding="utf-8",
    )

    result = _run_pytest(
        tests_dir,
        "-p",
        NO_SILENT_SKIP_PLUGIN,
        "--no-silent-skip-mode",
        "required",
        "-m",
        "smoke and s19b_live_restart",
    )
    assert result.returncode == 0, (
        f"marker selection failed despite payload executing:\n{result.stdout}\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "1 passed" in combined
    assert "deselected" in combined


def test_production_marker_selection_refused_when_payload_missing(tmp_path: Path) -> None:
    """Missing or non-matching required marker payload must exit NO_RESULT_EXIT_CODE."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_unrelated.py").write_text(
        textwrap.dedent("""
            import pytest

            @pytest.mark.smoke
            def test_other_smoke():
                assert True
        """),
        encoding="utf-8",
    )

    result = _run_pytest(
        tests_dir,
        "-p",
        NO_SILENT_SKIP_PLUGIN,
        "--no-silent-skip-mode",
        "required",
        "-m",
        "smoke and s19b_live_restart",
    )
    assert result.returncode == NO_RESULT_EXIT_CODE, (
        f"missing payload was not refused:\n{result.stdout}\n{result.stderr}"
    )
    assert "executed no tests" in result.stdout + result.stderr


def test_production_marker_selection_refused_when_payload_skips(tmp_path: Path) -> None:
    """Skipped required payload must exit NO_RESULT_EXIT_CODE even if selected."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_skipped_payload.py").write_text(
        textwrap.dedent("""
            import pytest

            @pytest.mark.smoke
            @pytest.mark.s19b_live_restart
            @pytest.mark.skip(reason="payload skipped")
            def test_live_restart_payload():
                assert False
        """),
        encoding="utf-8",
    )

    result = _run_pytest(
        tests_dir,
        "-p",
        NO_SILENT_SKIP_PLUGIN,
        "--no-silent-skip-mode",
        "required",
        "-m",
        "smoke and s19b_live_restart",
    )
    assert result.returncode == NO_RESULT_EXIT_CODE, (
        f"skipped payload was not refused:\n{result.stdout}\n{result.stderr}"
    )
    assert "skipped" in (result.stdout + result.stderr).lower()


def test_every_pytest_phase_loads_the_guard_at_the_right_strictness() -> None:
    """No pytest phase may certify a run that executed nothing."""
    specs = {s.name: s.argv for s in default_phase_specs(python="py", repo_root=REPO_ROOT)}

    for name in ("api_contracts", "live_restart", "load_contention"):
        argv = specs[name]
        assert NO_SILENT_SKIP_PLUGIN in argv, (
            f"phase {name!r} can certify a skipped payload: {argv}"
        )
        assert "required" in argv, (
            f"single-payload phase {name!r} must reject any skip, not merely an empty run: {argv}"
        )

    backend = specs["backend"]
    assert NO_SILENT_SKIP_PLUGIN in backend, (
        f"whole-suite backend phase can certify a run in which every test "
        f"skipped, which pytest exits 0 for: {backend}"
    )
    assert "nonempty" in backend, (
        f"backend has legitimate optional skips, so it must be 'nonempty' rather "
        f"than 'required': {backend}"
    )
