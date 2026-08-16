"""Shared fixtures and prerequisite gates for the pipeline test suite.

`bare_interpreter` exists because two test modules independently discovered
"an interpreter lacking jsonschema" by probing hardcoded system paths — a
hidden HOST dependency that took down every remote gate: the twin's
/usr/bin/python3 is 3.9, which dies on the tools' `from datetime import UTC`
(3.11+) BEFORE the jsonschema guard the tests exercise, so 4 pipeline tests
failed deterministically on the twin and every remote-gated train was
falsely rejected (measured 2026-08-10: three trains, all innocent).

A qualifying bare interpreter must therefore satisfy BOTH properties the
remedy contract assumes: it can PARSE AND IMPORT the tools far enough to hit
the jsonschema guard (>= 3.11), and it LACKS jsonschema. If no system
interpreter qualifies, one is synthesised: a `venv --without-pip` created
from the current (conforming) interpreter is exactly that pair by
construction — same version, empty site-packages.

NEITHER prerequisite may SKIP when it is absent. A skip is green, and green
is a claim that the property was checked — the twin's false rejections were
possible in the first place because "this host cannot run the check" and
"this host ran the check" were reported with the same colour somewhere in the
chain. Both prerequisite gates therefore call `pytest.fail(..., pytrace=False)`
naming the absent prerequisite, so a host that cannot exercise the remedy
contract says so in red. `test_prereq_failures_are_red.py` forces each of the
two absences and asserts the outcome is a failure, not a skip and not a pass.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_CANDIDATE_BARE_INTERPRETERS = [
    "/opt/homebrew/bin/python3",
    "/usr/bin/python3",
    "/usr/local/bin/python3",
]

_QUALIFY = (
    "import sys\n"
    "raise SystemExit(0 if sys.version_info >= (3, 11) else 3)\n"
)


def _qualifies(cand: str) -> bool:
    """Bare = new enough to reach the jsonschema guard, AND lacking jsonschema."""
    try:
        ver = subprocess.run([cand, "-c", _QUALIFY], capture_output=True, timeout=10)
        if ver.returncode != 0:
            return False
        has = subprocess.run([cand, "-c", "import jsonschema"],
                             capture_output=True, timeout=10)
        return has.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _synthesise_bare_interpreter(venv_parent: Path) -> str | None:
    """A fresh `venv --without-pip` from THIS interpreter has no third-party
    site-packages, so it lacks jsonschema while matching our version. Returns
    None when the synthesis itself fails (no interpreter to hand back)."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv_parent / "v")],
            capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    synth = venv_parent / "v" / "bin" / "python"
    if r.returncode == 0 and synth.exists() and _qualifies(str(synth)):
        return str(synth)
    return None


def _resolve_bare_interpreter(venv_parent: Path) -> str:
    """Discover or synthesise the bare interpreter, or FAIL naming what is absent."""
    for cand in _CANDIDATE_BARE_INTERPRETERS:
        if Path(cand).exists() and _qualifies(cand):
            return cand
    synthesised = _synthesise_bare_interpreter(venv_parent)
    if synthesised is not None:
        return synthesised
    pytest.fail(
        "PREREQUISITE ABSENT: no bare interpreter — Python >= 3.11 that cannot "
        "import jsonschema — is available on this host. None of "
        f"{', '.join(_CANDIDATE_BARE_INTERPRETERS)} qualifies, and synthesising one "
        f"with `{sys.executable} -m venv --without-pip` also failed. The remedy "
        "contract cannot be exercised here. This is RED on purpose: a skip would "
        "report the unrun check with the same colour as a passing one, which is the "
        "shape of defect that made the twin host falsely reject three innocent "
        "trains on 2026-08-10. Fix the host (install/repair `venv`), do not excuse it.",
        pytrace=False,
    )


@pytest.fixture(scope="session")
def bare_interpreter(tmp_path_factory: pytest.TempPathFactory) -> str:
    """An interpreter that reaches the jsonschema guard and lacks jsonschema.

    Fails (never skips) when neither discovery nor synthesis can produce one.
    """
    return _resolve_bare_interpreter(tmp_path_factory.mktemp("bare-venv"))


def _find_conforming_interpreter() -> str | None:
    """The interpreter RUNNING this suite, if it can import jsonschema.

    It is the thing the remedy message is expected to be able to name, and the
    thing every jsonschema-PRESENT assertion in the suite runs under.
    """
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return None
    return sys.executable


#: Resolved once at collection; monkeypatched to None by the falsifier to
#: simulate a suite running under an interpreter without jsonschema.
CONFORMING_INTERPRETER = _find_conforming_interpreter()


def _resolve_conforming_interpreter() -> str:
    """Return the conforming interpreter, or FAIL naming the absent prerequisite."""
    if CONFORMING_INTERPRETER is None:
        pytest.fail(
            "PREREQUISITE ABSENT: the interpreter running this suite "
            f"({sys.executable}) cannot import jsonschema, which is a locked "
            "dependency of this project. An interpreter without jsonschema running "
            "the suite is a BROKEN ENVIRONMENT, not an excused one — the "
            "jsonschema-present half of the admission and remedy contracts cannot "
            "be measured at all here, so nothing may report green. Run the suite "
            "under the project venv (`.venv/bin/python -m pytest pipeline/tests`).",
            pytrace=False,
        )
    return CONFORMING_INTERPRETER


@pytest.fixture(scope="session")
def conforming_interpreter() -> str:
    """The suite's own interpreter, proven to have jsonschema.

    Fails (never skips) when it does not.
    """
    return _resolve_conforming_interpreter()


# --- egress-env hermeticity -------------------------------------------------
#
# Every loop session exports OMNI_NTFY_URL (run-loop.sh ROLE_ENV), so any test
# touching a code path that alerts answers differently depending on the ambient
# shell: green when the variable is absent, red when it points at an endpoint
# that refuses the POST. Five claim-repair tests flip that way on code the
# candidate never touched, which manufactures instrument errors wearing the
# costume of candidate defects.
#
# The scrub is function-scoped and autouse, so a test that opts INTO a transport
# with its own monkeypatch.setenv still wins: the autouse fixture runs first,
# the test's own call runs second, and both are undone at teardown.
# pipeline/tests/test_notify.py depends on exactly that ordering and is the
# regression check for it. Production behaviour is untouched.

#: Spelled the same way test_notify.py spells them, so an opt-in setenv in a
#: test body overrides the same key this deletes.
EGRESS_ENV_VARS = (
    "OMNI_NTFY_URL",
    "OPS_ALERT_SLACK_WEBHOOK_URL",
    "SLACK_WEBHOOK_URL",
)


@pytest.fixture(autouse=True)
def _scrub_egress_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove egress-notification env vars for the duration of each test."""
    for name in EGRESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
