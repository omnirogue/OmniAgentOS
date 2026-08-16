"""A lane must never report success it cannot prove.

The first version of `scripts/testlanes/run_lane.py` computed its status as
`max(exit_codes)`. Python reports a signalled child as a NEGATIVE returncode, so a lane whose
pytest subprocess was SIGKILLed (OOM killer, `kill -9`, a CI timeout) produced
`max([-9, 0]) == 0` -- **a killed run reported SUCCESS** -- and `merge_junit` silently skipped
the missing report part, so the merged artifact looked plausible too.

These tests pin the fix from both ends: the pure status arithmetic, and a real `run_lane()`
call whose pytest subprocesses are stubbed out to imitate a kill.
"""

from __future__ import annotations

import signal
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.testlanes import run_lane as rl


def _write_junit(path: Path, n_tests: int) -> None:
    suite = ET.Element("testsuite", {"name": "stub", "tests": str(n_tests)})
    for i in range(n_tests):
        ET.SubElement(suite, "testcase", {"classname": "stub", "name": f"t{i}", "time": "0.01"})
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------------------
# status arithmetic
# --------------------------------------------------------------------------------------
def test_a_sigkilled_step_is_a_failure_not_a_zero(tmp_path: Path) -> None:
    junit = tmp_path / "part.xml"
    _write_junit(junit, 3)
    killed = rl.Step(name="impacted", returncode=-signal.SIGKILL, junit=junit)
    assert rl.step_exit_code(killed) == 128 + signal.SIGKILL


def test_max_over_a_killed_and_a_clean_step_is_still_a_failure(tmp_path: Path) -> None:
    """The literal regression: max([-9, 0]) == 0."""
    killed_junit = tmp_path / "killed.xml"
    ok_junit = tmp_path / "ok.xml"
    _write_junit(ok_junit, 2)
    steps = [
        rl.Step(name="impacted", returncode=-9, junit=killed_junit),
        rl.Step(name="critical", returncode=0, junit=ok_junit),
    ]
    assert max(step.returncode for step in steps) == 0, "premise of the original bug"
    assert rl.lane_exit_code(steps) != 0
    assert rl.lane_exit_code(steps) == 137


def test_a_clean_step_with_no_report_is_not_trusted(tmp_path: Path) -> None:
    step = rl.Step(name="critical", returncode=0, junit=tmp_path / "never-written.xml")
    assert rl.step_exit_code(step) == rl.EXIT_UNTRUSTWORTHY_REPORT


def test_a_clean_step_with_a_truncated_report_is_not_trusted(tmp_path: Path) -> None:
    junit = tmp_path / "truncated.xml"
    junit.write_text('<?xml version="1.0"?><testsuite name="stub"><testc', encoding="utf-8")
    step = rl.Step(name="critical", returncode=0, junit=junit)
    assert rl.junit_testcase_count(junit) is None
    assert rl.step_exit_code(step) == rl.EXIT_UNTRUSTWORTHY_REPORT


def test_a_critical_step_that_ran_nothing_is_not_trusted(tmp_path: Path) -> None:
    """The critical set is `tests/acceptance` filtered by a marker that is known to select
    18-71 tests. Zero of them running is a broken selection, not a pass."""
    junit = tmp_path / "empty.xml"
    _write_junit(junit, 0)
    step = rl.Step(name="critical", returncode=0, junit=junit, expect_tests=True)
    assert rl.step_exit_code(step) == rl.EXIT_UNTRUSTWORTHY_REPORT


def test_an_impacted_step_deselected_to_nothing_is_allowed(tmp_path: Path) -> None:
    """pytest exits 5 when its markers deselect the whole selection. For the impacted subset
    that is an empty selection, not a failure -- but the report must still exist."""
    junit = tmp_path / "none.xml"
    _write_junit(junit, 0)
    step = rl.Step(name="impacted", returncode=5, junit=junit, expect_tests=False)
    assert rl.step_exit_code(step) == 0

    missing = rl.Step(name="impacted", returncode=5, junit=tmp_path / "gone.xml")
    assert rl.step_exit_code(missing) == rl.EXIT_UNTRUSTWORTHY_REPORT


def test_a_lane_with_no_steps_at_all_is_a_failure() -> None:
    assert rl.lane_exit_code([]) == rl.EXIT_UNTRUSTWORTHY_REPORT


def test_ordinary_pytest_failure_is_propagated(tmp_path: Path) -> None:
    junit = tmp_path / "failed.xml"
    _write_junit(junit, 4)
    assert rl.step_exit_code(rl.Step(name="critical", returncode=1, junit=junit)) == 1


# --------------------------------------------------------------------------------------
# merge_junit no longer hides a missing part
# --------------------------------------------------------------------------------------
def test_merge_junit_reports_missing_parts(tmp_path: Path) -> None:
    good = tmp_path / "good.xml"
    _write_junit(good, 2)
    gone = tmp_path / "gone.xml"
    dest = tmp_path / "merged.xml"

    missing = rl.merge_junit([good, gone], dest)

    assert missing == [gone]
    assert dest.exists(), "the partial merge is still written for the reader that wants it"
    assert len(list(ET.parse(dest).getroot().iter("testcase"))) == 2


# --------------------------------------------------------------------------------------
# end to end through run_lane()
# --------------------------------------------------------------------------------------
def test_run_lane_returns_nonzero_when_a_subprocess_is_killed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No real pytest is spawned: `_run_pytest` is replaced with a stub that imitates a
    SIGKILLed child (negative returncode, no JUnit written) for the critical step."""

    def fake_run_pytest(name: str, args: list[str], junit: Path, expect_tests: bool = True):
        if name == "critical":
            return rl.Step(name=name, returncode=-9, junit=junit, expect_tests=expect_tests)
        _write_junit(junit, 5)
        return rl.Step(name=name, returncode=0, junit=junit, expect_tests=expect_tests)

    monkeypatch.setattr(rl, "_run_pytest", fake_run_pytest)
    rc = rl.run_lane(
        "dev",
        tmp_path / "lane.xml",
        base=None,
        changed_override=["omniagentos/scope/policy.py"],
    )
    assert rc != 0, "a killed critical step must not report SUCCESS"
    assert rc == 137


def test_run_lane_is_green_when_every_step_is_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of the contract: the strict exit logic must not manufacture failures."""

    def fake_run_pytest(name: str, args: list[str], junit: Path, expect_tests: bool = True):
        _write_junit(junit, 5)
        return rl.Step(name=name, returncode=0, junit=junit, expect_tests=expect_tests)

    monkeypatch.setattr(rl, "_run_pytest", fake_run_pytest)
    dest = tmp_path / "lane.xml"
    rc = rl.run_lane(
        "dev", dest, base=None, changed_override=["omniagentos/scope/policy.py"]
    )
    assert rc == 0
    assert len(list(ET.parse(dest).getroot().iter("testcase"))) > 0


# --------------------------------------------------------------------------------------
# xdist safety: the serial bucket never gets -n
# --------------------------------------------------------------------------------------
def test_doctrine_is_run_serially_by_the_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`tests/doctrine`'s revert harness mutates shared fixture files in place. The lane may
    select it, but it must never hand it to `-n`."""
    seen: list[tuple[str, list[str]]] = []

    def fake_run_pytest(name: str, args: list[str], junit: Path, expect_tests: bool = True):
        seen.append((name, list(args)))
        _write_junit(junit, 1)
        return rl.Step(name=name, returncode=0, junit=junit, expect_tests=expect_tests)

    monkeypatch.setattr(rl, "_run_pytest", fake_run_pytest)
    rl.run_lane("dev", tmp_path / "lane.xml", base=None, changed_override=["tests/doctrine/revert.py"])

    serial_steps = [(n, a) for n, a in seen if n == "impacted-serial"]
    assert serial_steps, "a tests/doctrine change produced no serial step"
    for _name, args in serial_steps:
        assert "-n" not in args, f"doctrine was handed to xdist: {args}"
        assert any(a.startswith("tests/doctrine") for a in args)
    for name, args in seen:
        if name == "impacted-parallel":
            assert not any(a.startswith("tests/doctrine") for a in args), (
                "tests/doctrine leaked into the parallel bucket"
            )


def test_an_escalated_lane_still_runs_doctrine_serially(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Escalation replaces the impacted subset with the whole tree. The tree run must
    --ignore tests/doctrine and pay for it in a separate serial step, or escalation would
    reintroduce the very xdist race the partition exists to prevent."""
    seen: list[tuple[str, list[str]]] = []

    def fake_run_pytest(name: str, args: list[str], junit: Path, expect_tests: bool = True):
        seen.append((name, list(args)))
        _write_junit(junit, 1)
        return rl.Step(name=name, returncode=0, junit=junit, expect_tests=expect_tests)

    monkeypatch.setattr(rl, "_run_pytest", fake_run_pytest)
    # omniagentos/db/store.py's closure covers most of the suite -> both lanes escalate.
    rl.run_lane("dev", tmp_path / "lane.xml", base=None, changed_override=["omniagentos/db/store.py"])

    names = [n for n, _ in seen]
    assert "escalated-tree" in names, f"a too-broad closure did not escalate: {names}"
    tree_args = dict(seen)["escalated-tree"]
    assert "--ignore=tests/doctrine" in tree_args
    assert "-n" in tree_args, "the escalated tree run should still be parallel"

    assert "doctrine-serial" in names
    doctrine_args = dict(seen)["doctrine-serial"]
    assert "-n" not in doctrine_args, f"doctrine was handed to xdist: {doctrine_args}"


def test_an_unresolved_file_escalates_pr_but_not_dev(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two triggers are not interchangeable: a hole in the analysis is a coverage
    problem, so the pre-review lane escalates and the pre-commit loop only warns."""
    unresolved = "configs/" + "zz-unreferenced-" + "lane" + ".toml"
    seen: dict[str, list[str]] = {}

    def fake_run_pytest(name: str, args: list[str], junit: Path, expect_tests: bool = True):
        seen[name] = list(args)
        _write_junit(junit, 1)
        return rl.Step(name=name, returncode=0, junit=junit, expect_tests=expect_tests)

    monkeypatch.setattr(rl, "_run_pytest", fake_run_pytest)

    seen.clear()
    rl.run_lane("dev", tmp_path / "dev.xml", base=None, changed_override=[unresolved])
    assert "escalated-tree" not in seen
    assert "critical" in seen

    seen.clear()
    rl.run_lane("pr", tmp_path / "pr.xml", base=None, changed_override=[unresolved])
    assert "escalated-tree" in seen
