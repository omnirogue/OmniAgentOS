"""Verifier-needs-verification tests for the documents_open_defect collection
hook (tests/livesim/conftest.py::pytest_collection_modifyitems and friends).

Built 2026-08-06 per the estate's standing rule that a script written to
mechanically verify agent claims is itself an untested piece of logic and
needs its own coverage -- this hook is exactly that kind of script (it is
the enforcement mechanism for "a test must not silently pin the pre-fix
behaviour as correct"), and it had none until this file.

Every sandbox here loads the REAL hook code from the real conftest.py (via
importlib, using the real file's own __file__ so _REPO/_ISSUES_YAML_PATH
resolve correctly) rather than reimplementing its logic -- a reimplementation
would drift from the real hook and this file would stop meaning anything the
moment it did. Each sandbox then monkeypatches `_ISSUES_YAML_PATH` on the
freshly-imported module object to point at a throwaway YAML file so every
scenario (missing/empty/malformed/valid) is fully isolated from the real
docs/testing/LIVESIM-ISSUES.yaml and from every other test in this file.

Uses pytest's `pytester` fixture (see the `pytest_plugins = ["pytester"]`
line in tests/livesim/conftest.py) to run real, isolated pytest sessions
against generated sandboxes and assert on their exit codes and output.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.livesim

_REAL_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


def _sandbox_conftest_source(yaml_relpath: str = "LIVESIM-ISSUES.yaml") -> str:
    """A minimal conftest.py that imports the REAL tests/livesim/conftest.py
    module (by file path, so it works regardless of sys.path) and re-exports
    just what a bare pytest session needs: the marker registration and the
    real pytest_collection_modifyitems hook. `_ISSUES_YAML_PATH` on the
    imported module is monkeypatched to a sandbox-local file so each pytester
    run is isolated from the real YAML and from other runs in this file."""
    return textwrap.dedent(f'''
        import importlib.util
        from pathlib import Path

        _spec = importlib.util.spec_from_file_location(
            "livesim_real_conftest_under_test", {str(_REAL_CONFTEST)!r}
        )
        _real = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_real)

        # Isolate: point the hook at THIS sandbox's YAML, never the real one.
        _real._ISSUES_YAML_PATH = Path(__file__).parent / {yaml_relpath!r}

        # Re-export exactly what pytest needs to exercise the real hook.
        pytest_collection_modifyitems = _real.pytest_collection_modifyitems
        pytest_terminal_summary = _real.pytest_terminal_summary

        def pytest_configure(config):
            config.addinivalue_line(
                "markers",
                \'documents_open_defect(id="LS-###"): see real conftest\',
            )
            config.addinivalue_line("markers", "livesim: probe marker")
    ''')


VALID_YAML = textwrap.dedent('''
    schema: livesim-issues.v1
    issues:
      - id: LS-100
        title: "open defect, still tracked"
        severity: P3
        kind: product
        status: open
      - id: LS-101
        title: "fix in flight"
        severity: P2
        kind: product
        status: "fix-in-flight: TESTLANE"
      - id: LS-102
        title: "already fixed"
        severity: P3
        kind: product
        status: fixed
''')


def _write_yaml(pytester: pytest.Pytester, content: str, name: str = "LIVESIM-ISSUES.yaml") -> None:
    (pytester.path / name).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Unknown id: a marker referencing an id absent from the YAML must fail
#    collection loudly (LS-TEST hook requirement: never a silent skip).
# ---------------------------------------------------------------------------


def test_unknown_id_fails_collection(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-999-DOES-NOT-EXIST")
        def test_marked_missing_id():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*LS-999-DOES-NOT-EXIST.*does not exist.*"])


# ---------------------------------------------------------------------------
# 2. Raced status + BARE assertion: must fail collection (this is the exact
#    "test pins pre-fix behaviour as correct after a fix landed" miss).
# ---------------------------------------------------------------------------


def test_raced_status_bare_assertion_fails_collection(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-102")  # status: fixed
        def test_still_pins_broken_behaviour():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*LS-102.*status='fixed'.*"])


def test_raced_status_fix_in_flight_bare_assertion_fails_collection(
    pytester: pytest.Pytester,
) -> None:
    """Same as above but for the `fix-in-flight: <lane-id>` status, not just
    the terminal `fixed` -- both prefixes must trip a bare assertion."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-101")  # status: fix-in-flight: TESTLANE
        def test_still_pins_broken_behaviour():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*LS-101.*fix-in-flight: TESTLANE.*"])


# ---------------------------------------------------------------------------
# 3. Raced status + STRICT XFAIL is LEGAL: collection must succeed (the
#    xfail's own XPASS->FAIL is the alarm; the hook must not double-block).
# ---------------------------------------------------------------------------


def test_raced_status_strict_xfail_is_legal(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-101")  # status: fix-in-flight
        @pytest.mark.xfail(reason="LS-101 open, fix in flight", strict=True)
        def test_asserts_fixed_behaviour_ahead_of_landing():
            assert False, "not fixed yet in this sandbox"
        """
    )
    collect_result = pytester.runpytest("--collect-only")
    assert collect_result.ret == pytest.ExitCode.OK, collect_result.stdout.str()
    run_result = pytester.runpytest("-rxX")
    assert run_result.ret == pytest.ExitCode.OK
    run_result.stdout.re_match_lines([r".*1 xfailed.*"])


# ---------------------------------------------------------------------------
# 3b. The THIRD composition state (LS-TEST review round, LS-005's actual
#     shape): verified_fixed_pending_promotion="<receipt>" is legal on a
#     raced status WITHOUT xfail (a plain passing assertion of the fixed
#     behaviour); it is ALSO an error if used when the status is NOT raced
#     (claiming a verified fix the YAML doesn't back up).
#
# 3c. LS-TEST-010 hardening: the flag REQUIRES a non-empty evidence string --
#     bare True/False and the empty string are rejected (an unauditable
#     attestation is exactly the defect class this whole mechanism exists to
#     catch); combining the flag with strict xfail on the same test is
#     rejected (the two claims contradict: "already verified fixed" vs.
#     "currently expected to fail"); and every valid attestation this run
#     collects is named in the terminal summary (verified separately below
#     via `-rA`/full output, since pytester's ExitCode/outcome helpers don't
#     surface a custom terminal_summary section text directly).
# ---------------------------------------------------------------------------


def test_verified_fixed_pending_promotion_on_raced_status_is_legal(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(
            id="LS-101", verified_fixed_pending_promotion="probed-live-2026-08-06"
        )
        def test_plainly_asserts_the_fixed_behaviour():
            assert True  # the fix was independently verified live
        """
    )
    collect_result = pytester.runpytest("--collect-only")
    assert collect_result.ret == pytest.ExitCode.OK, collect_result.stdout.str()
    run_result = pytester.runpytest()
    assert run_result.ret == pytest.ExitCode.OK
    run_result.assert_outcomes(passed=1)


def test_verified_fixed_pending_promotion_on_open_status_fails_collection(
    pytester: pytest.Pytester,
) -> None:
    """The flag must not be usable to silently skip promoting the YAML --
    claiming a verified fix while the YAML still says `open` is itself an
    inconsistency this hook must catch."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(
            id="LS-100", verified_fixed_pending_promotion="probed-live-2026-08-06"
        )  # status: open
        def test_claims_verified_but_yaml_disagrees():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*verified_fixed_pending_promotion=.*claims an independently-verified fix.*"])


@pytest.mark.parametrize("bare_value", ["True", "False", "''"])
def test_verified_fixed_pending_promotion_rejects_bare_boolean_and_empty_string(
    pytester: pytest.Pytester, bare_value: str
) -> None:
    """LS-TEST-010: an unauditable attestation (bare True/False, or an empty
    string that carries no actual evidence) must fail collection loudly --
    the whole point is that the flag REQUIRES a receipt a human can go
    check, not merely a truthy Python value."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked=f"""
        import pytest

        @pytest.mark.documents_open_defect(
            id="LS-101", verified_fixed_pending_promotion={bare_value}
        )
        def test_unauditable_attestation():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*must be a non-empty evidence string.*"])


def test_verified_fixed_pending_promotion_control_leg_pins_broken_behaviour_still_visible(
    pytester: pytest.Pytester,
) -> None:
    """LS-TEST-010's own repro's control leg, ported: this hook cannot
    inspect what an assertion MEANS -- a truthful attestation and an untrue
    one both collect clean and run green once a valid receipt is supplied.
    This is the accepted, documented residual (mitigated by the terminal
    summary naming every attestation, not eliminated) -- this test proves
    the mitigation exists (the attestation appears in `-rA` output), not
    that the underlying limitation is closed, because it cannot be closed by
    a collection hook."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(
            id="LS-101", verified_fixed_pending_promotion="probed-live-2026-08-06"
        )
        def test_still_pins_the_broken_behaviour_but_carries_a_receipt():
            observed_broken = True  # a truthful reviewer would not do this; the hook cannot tell
            assert observed_broken
        """
    )
    result = pytester.runpytest("-rA")
    assert result.ret == pytest.ExitCode.OK
    result.stdout.re_match_lines(
        [r".*verified_fixed_pending_promotion attestations.*",
         r".*test_still_pins_the_broken_behaviour_but_carries_a_receipt.*probed-live-2026-08-06.*"]
    )


def test_verified_fixed_pending_promotion_rejects_combination_with_strict_xfail(
    pytester: pytest.Pytester,
) -> None:
    """LS-TEST-010: the two claims contradict each other ('already verified
    fixed live' vs. 'currently expected to fail') and must not be
    expressible together, even though strict xfail would still govern at
    runtime (incoherent, not dangerous, but should not parse)."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(
            id="LS-101", verified_fixed_pending_promotion="probed-live-2026-08-06"
        )
        @pytest.mark.xfail(reason="LS-101 open", strict=True)
        def test_contradictory_claims():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*is combined with strict.*xfail.*contradict.*"])


def test_terminal_summary_names_every_attestation(pytester: pytest.Pytester) -> None:
    """LS-TEST-010: an attestation must not be able to sit silently -- the
    terminal summary names every flagged test and its receipt, every run,
    whether or not anyone asked for -rA."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(
            id="LS-101", verified_fixed_pending_promotion="probed-live-2026-08-06"
        )
        def test_one():
            assert True

        @pytest.mark.documents_open_defect(
            id="LS-102", verified_fixed_pending_promotion="commit-9d320d68"
        )
        def test_two():
            assert True
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=2)
    result.stdout.re_match_lines(
        [
            r".*verified_fixed_pending_promotion attestations.*",
            r".*test_one.*probed-live-2026-08-06.*",
            r".*test_two.*commit-9d320d68.*",
            r".*2 test\(s\) claim an independently-verified fix.*",
        ]
    )


# ---------------------------------------------------------------------------
# 4. Fail-closed YAML failure modes: missing / empty / malformed must ALL be
#    pytest.UsageError (exit 4), never a silent skip, never an INTERNALERROR.
# ---------------------------------------------------------------------------


def test_missing_yaml_fails_closed(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    # deliberately do NOT write the YAML file at all
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-100")
        def test_marked():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()
    result.stderr.re_match_lines([r".*canonical issues YAML missing.*"])


def test_empty_yaml_fails_closed(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, "")
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-100")
        def test_marked():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()


def test_malformed_yaml_wrong_shape_fails_closed(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, "issues: not-a-list\n")
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-100")
        def test_marked():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()


def test_malformed_yaml_entry_missing_status_fails_closed(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, "issues:\n  - id: LS-100\n")
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-100")
        def test_marked():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()


# ---------------------------------------------------------------------------
# 5. Status vocabulary: any status outside the documented enum is a hard
#    error, including near-miss typos (space instead of hyphen).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_status",
    ["resolved", "closed", "merged", "fix-landed: A1", "fix in flight: A1", "fix-in-review: A1"],
)
def test_unrecognised_status_value_fails_closed(pytester: pytest.Pytester, bad_status: str) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(
        pytester,
        f'issues:\n  - id: LS-100\n    title: "x"\n    status: "{bad_status}"\n',
    )
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-100")
        def test_marked():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "INTERNALERROR" not in result.stdout.str() + result.stderr.str()
    result.stderr.re_match_lines([r".*status value.*outside the documented enum.*"])


def test_duplicate_id_fails_closed(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(
        pytester,
        'issues:\n'
        '  - id: LS-100\n    title: "first"\n    status: open\n'
        '  - id: LS-100\n    title: "duplicate"\n    status: fixed\n',
    )
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-100")
        def test_marked():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*duplicate id.*LS-100.*"])


# ---------------------------------------------------------------------------
# 6. Marker without id: must fail, not silently pass through unchecked.
# ---------------------------------------------------------------------------


def test_marker_without_id_fails_collection(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect()
        def test_marked_no_id():
            assert True
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*missing id=.*"])


# ---------------------------------------------------------------------------
# 7. Strict-xfail test naming an LS id with NO documents_open_defect marker
#    at all must fail collection (this is the exact loophole LS-TEST-005
#    found: the hook's own recommended remedy used to REMOVE a test from
#    the cross-check).
# ---------------------------------------------------------------------------


def test_strict_xfail_naming_ls_id_without_marker_fails_collection(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.xfail(reason="LS-101 open, fix in flight", strict=True)
        def test_orphan_xfail():
            assert False
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*xfail reason names.*LS-101.*no marker at all.*"])


def test_non_strict_xfail_naming_ls_id_without_marker_also_fails_collection(
    pytester: pytest.Pytester,
) -> None:
    """LS-TEST-009-round drift-prevention: the orphan-id scan is NOT gated on
    `strict=True` -- a bare (non-strict) xfail naming an LS id and carrying
    no documents_open_defect marker is exactly "a stale xfail hiding a real
    green test" (this hook's own marker docstring), and must be caught even
    though a non-strict xfail cannot itself self-alarm on XPASS."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.xfail(reason="LS-101 open, not yet fixed")  # strict NOT set
        def test_non_strict_orphan_xfail():
            assert False
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*xfail reason names.*LS-101.*no marker at all.*"])


def test_strict_xfail_partial_id_coverage_fails_collection(pytester: pytest.Pytester) -> None:
    """A strict-xfail test naming TWO ids in its reason but marked for only
    ONE of them must still fail -- partial coverage is still a gap."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_marked="""
        import pytest

        @pytest.mark.documents_open_defect(id="LS-100")
        @pytest.mark.xfail(reason="LS-100/LS-101 open, fix in flight", strict=True)
        def test_partial_orphan_xfail():
            assert False
        """
    )
    result = pytester.runpytest("--collect-only")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.re_match_lines([r".*missing \['LS-101'\].*"])


# ---------------------------------------------------------------------------
# 8. Negative case: a suite with NO documents_open_defect markers at all,
#    and NO strict-xfail tests naming an LS id, must be completely unaffected
#    by the hook -- including when the YAML is missing/broken (the hook must
#    not even open it if there is nothing to check against it).
# ---------------------------------------------------------------------------


def test_unmarked_tests_are_unaffected_even_with_missing_yaml(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_sandbox_conftest_source())
    # NO yaml file written at all.
    pytester.makepyfile(
        test_plain="""
        def test_one():
            assert True

        def test_two():
            assert 1 + 1 == 2
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.OK, result.stdout.str()
    result.assert_outcomes(passed=2)


def test_unmarked_tests_are_unaffected_by_a_raced_marked_sibling_when_deselected(
    pytester: pytest.Pytester,
) -> None:
    """LS-TEST-002 direct proof, inside the real hook (trylast=True): a
    marked test with a RACED status and a bare (non-xfail) assertion would
    normally abort collection -- but if it is deselected via `-m` before our
    hook runs, an unrelated, unmarked test in the same session must still
    run and pass, and the run must exit 0."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_mixed="""
        import pytest

        @pytest.mark.offending
        @pytest.mark.documents_open_defect(id="LS-102")  # status: fixed -- would abort
        def test_offending_but_deselected():
            assert True

        def test_unrelated_and_selected():
            assert True
        """
    )
    result = pytester.runpytest("-m", "not offending")
    assert result.ret == pytest.ExitCode.OK, result.stdout.str()
    result.assert_outcomes(passed=1, deselected=1)


def test_marked_and_selected_still_aborts_when_actually_running(
    pytester: pytest.Pytester,
) -> None:
    """The flip side of the ordering fix: when the offending marked test IS
    selected to run (not deselected), the hook must still abort collection
    -- trylast=True must not have accidentally made the check inert."""
    pytester.makeconftest(_sandbox_conftest_source())
    _write_yaml(pytester, VALID_YAML)
    pytester.makepyfile(
        test_mixed="""
        import pytest

        @pytest.mark.offending
        @pytest.mark.documents_open_defect(id="LS-102")  # status: fixed
        def test_offending_and_selected():
            assert True

        def test_unrelated():
            assert True
        """
    )
    result = pytester.runpytest("-m", "offending")
    assert result.ret == pytest.ExitCode.USAGE_ERROR, result.stdout.str()
