"""Falsifiers for this suite's own prerequisite gates (conftest.py).

Both gates — `bare_interpreter` and `conforming_interpreter` — used to SKIP when
their prerequisite was absent. A skip is green, and green is a claim that the
property was checked; that is how a host which could not run a check ended up
indistinguishable from a host that ran it and passed, and it is the shape of
defect behind the twin's three false train rejections on 2026-08-10.

These tests FORCE each absence and assert the outcome is a FAILURE: not a skip,
and not a pass. Both `Failed` and `Skipped` are caught deliberately — catching
only `Failed` would let a regression to `pytest.skip` escape the test body and
mark this falsifier itself as skipped, i.e. green. The explicit type assertion
is what makes a regression red.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

Failed = pytest.fail.Exception
Skipped = pytest.skip.Exception


@pytest.fixture
def conftest_module(request: pytest.FixtureRequest):
    """The loaded pipeline/tests/conftest.py.

    Found through the plugin manager rather than imported by name, so this does
    not depend on pytest's import mode or on the tests directory being a package.
    """
    target = Path(__file__).resolve().parent / "conftest.py"
    for plugin in request.config.pluginmanager.get_plugins():
        path = getattr(plugin, "__file__", None)
        if path and Path(path).resolve() == target:
            return plugin
    raise AssertionError(f"{target} is not loaded as a conftest plugin")


class TestBareInterpreterAbsenceIsRed:
    def test_synthesis_failure_fails_and_does_not_skip(self, conftest_module, monkeypatch, tmp_path):
        """Force the prerequisite absent: a subprocess layer that always returns
        non-zero disqualifies every system candidate AND fails the
        `venv --without-pip` synthesis, which is the last resort."""
        def _always_fails(*args, **kwargs):
            return subprocess.CompletedProcess(args[0] if args else [], 1, b"", b"forced")

        monkeypatch.setattr(subprocess, "run", _always_fails)

        with pytest.raises((Failed, Skipped)) as excinfo:
            conftest_module._resolve_bare_interpreter(tmp_path)

        assert excinfo.type is Failed, (
            "an absent bare interpreter must FAIL the run, not skip it: "
            f"got {excinfo.type.__name__}")
        message = str(excinfo.value)
        assert "PREREQUISITE ABSENT" in message
        assert "jsonschema" in message and "3.11" in message, (
            f"the failure must name the absent prerequisite: {message!r}")


class TestConformingInterpreterAbsenceIsRed:
    def test_missing_conforming_interpreter_fails_and_does_not_skip(self, conftest_module,
                                                                    monkeypatch):
        """Simulate a suite running under an interpreter without jsonschema."""
        monkeypatch.setattr(conftest_module, "CONFORMING_INTERPRETER", None)

        with pytest.raises((Failed, Skipped)) as excinfo:
            conftest_module._resolve_conforming_interpreter()

        assert excinfo.type is Failed, (
            "an interpreter without jsonschema running the suite is a broken "
            f"environment, not an excused one: got {excinfo.type.__name__}")
        message = str(excinfo.value)
        assert "PREREQUISITE ABSENT" in message
        assert "jsonschema" in message, (
            f"the failure must name the absent prerequisite: {message!r}")

    def test_present_conforming_interpreter_is_returned(self, conftest_module):
        """Control: with the prerequisite present the gate returns, so the test
        above is measuring the absence and not a gate that always fails."""
        assert conftest_module._resolve_conforming_interpreter() == \
            conftest_module.CONFORMING_INTERPRETER
