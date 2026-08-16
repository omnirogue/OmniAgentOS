"""Counterfeit-test helper: prove a named test binds to the *right* behaviour.

A revert-test shows the suite notices *some* change. A counterfeit-test shows
it notices the *realistic fake* of the improvement — the thing an agent ships
when it wants green without doing the work.

Protocol
--------
1. Apply a counterfeit mutation (favourable constant, grep-for-name, etc.).
2. Run the named test — it **must fail** (catch the fake).
3. Restore the file.
4. Optionally re-run to confirm green after restore (default: yes).

If step 2 still passes, the suite cannot detect that fake. Raise
:class:`DoctrineError` — same severity as a decorative revert-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.doctrine._mutate import Mutation, TextReplace, mutated_file
from tests.doctrine._runner import PytestRun, run_pytest_node
from tests.doctrine.errors import DoctrineError


@dataclass(frozen=True, slots=True)
class CounterfeitReport:
    """Outcome of a successful counterfeit-test (the suite caught the fake)."""

    target: Path
    nodeid: str
    counterfeit_failure: PytestRun
    restored_pass: PytestRun | None

    @property
    def failure_text(self) -> str:
        """Verbatim pytest output while the counterfeit was applied."""
        return self.counterfeit_failure.failure_text()

    def summary(self) -> str:
        restore_line = (
            f"restored run passed (exit {self.restored_pass.returncode})"
            if self.restored_pass is not None
            else "restore re-check skipped"
        )
        return (
            f"COUNTERFEIT-TEST OK  nodeid={self.nodeid!r}  target={self.target}\n"
            f"--- failure with counterfeit applied "
            f"(exit {self.counterfeit_failure.returncode}) ---\n"
            f"{self.failure_text}\n"
            f"--- {restore_line} ---"
        )


def counterfeit_test(
    *,
    target: Path | str,
    counterfeit: Mutation | TextReplace,
    nodeid: str,
    cwd: Path | str | None = None,
    timeout: float = 120.0,
    recheck_restored: bool = True,
) -> CounterfeitReport:
    """Apply ``counterfeit`` to ``target`` and demand ``nodeid`` fails.

    Parameters
    ----------
    target:
        File to temporarily fake.
    counterfeit:
        Mutation that *looks* like the improvement (wrong constant, partial
        redirect, grep-for-name, hardcoded width, …).
    nodeid:
        Exact pytest node that should catch the fake.
    cwd:
        Working directory for pytest (defaults to repo root).
    timeout:
        Seconds allowed for each pytest invocation.
    recheck_restored:
        When True (default), re-run ``nodeid`` after restore and require pass.

    Returns
    -------
    CounterfeitReport
        Includes the verbatim failure text from the counterfeit run.

    Raises
    ------
    DoctrineError
        If the test still passes under the counterfeit (suite is blind to that
        fake), or fails after restore when recheck is enabled.
    """
    path = Path(target)
    work = Path(cwd) if cwd is not None else None

    with mutated_file(path, counterfeit):
        faked = run_pytest_node(nodeid, cwd=work, timeout=timeout)
        if faked.passed:
            raise DoctrineError(
                "COUNTERFEIT UNCAUGHT: test still PASSES under a known fake of "
                "the improvement — the suite does not bind to the right behaviour.\n"
                f"  nodeid={nodeid!r}\n"
                f"  target={path}\n"
                f"  counterfeit={counterfeit!r}\n"
                f"  pytest exit={faked.returncode}\n"
                f"--- pytest output ---\n{faked.failure_text()}\n"
                "Do NOT count this test as evidence of the claim."
            )

    restored: PytestRun | None = None
    if recheck_restored:
        restored = run_pytest_node(nodeid, cwd=work, timeout=timeout)
        if restored.failed:
            raise DoctrineError(
                "COUNTERFEIT-TEST incomplete: after restore, the named test "
                "still FAILS.\n"
                f"  nodeid={nodeid!r}\n"
                f"  target={path}\n"
                f"  pytest exit={restored.returncode}\n"
                f"--- pytest output after restore ---\n{restored.failure_text()}\n"
                f"--- earlier failure with counterfeit ---\n{faked.failure_text()}"
            )

    return CounterfeitReport(
        target=path,
        nodeid=nodeid,
        counterfeit_failure=faked,
        restored_pass=restored,
    )
