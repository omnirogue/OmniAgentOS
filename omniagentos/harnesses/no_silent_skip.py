"""Fail a pytest session whose *required* selection produced no real result.

``pytest`` exits 0 when every selected test skips, and exits 0 (or 5) when a
marker expression deselects everything. For the release gate's required payload
phases that is a false green: the phase records ``passed`` having executed none
of the behaviour it exists to certify. ``OMNIAGENTOS_REQUIRE_PG=1`` closes only
one source of those skips — an unreachable database — and says nothing about a
missing marker, an absent satellite payload, or a module that skipped itself.

Activate per-phase with ``-p omniagentos.harnesses.no_silent_skip`` and choose a
strictness with ``--no-silent-skip-mode``:

``required``
    The phase's entire selection is one required payload, so *no* skip is
    acceptable — a skipped payload is not evidence that it passed.

``nonempty``
    A broad suite with legitimate optional skips (live providers, extras).
    Individual skips are tolerated, but a session in which nothing at all
    executed is still vacuous, and pytest calls it a success.

Tolerating *some* skips is not a reason to tolerate *all* of them, which is why
the whole-suite ``backend`` phase runs under ``nonempty`` rather than bare: a
conftest error, a mis-set environment, or an ``-m`` typo can skip every test in
the repository and pytest will still exit 0.

The check uses pytest's own report objects rather than parsing terminal output,
so it cannot be defeated by formatting changes or ``-q``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pytest

#: Exit code for "the required selection never actually ran". Distinct from
#: pytest's TESTS_FAILED (1) so gate evidence distinguishes a real failure from
#: an empty one; any non-zero code fails the phase.
NO_RESULT_EXIT_CODE = 2

#: Strictness modes, strictest first.
MODES = ("required", "nonempty")

#: Chosen when the option is omitted. The strict mode is the default on purpose:
#: forgetting to pass a mode must not silently buy the looser check.
DEFAULT_MODE = "required"

_ENV_MARKER = "no_silent_skip"


class RequiredSelectionGuard:
    """Records outcomes and vetoes a success that rests on nothing."""

    def __init__(self, mode: str = DEFAULT_MODE) -> None:
        if mode not in MODES:  # pragma: no cover - argparse choices enforce this
            raise ValueError(f"unknown no_silent_skip mode {mode!r}; expected one of {MODES}")
        self.mode = mode
        self.executed = 0
        self.skipped: list[str] = []
        self.deselected: list[str] = []

    def pytest_deselected(self, items: Any) -> None:
        for item in items:
            nodeid = getattr(item, "nodeid", str(item))
            self.deselected.append(nodeid)

    # --- collection-level skips (pytest.skip(allow_module_level=True), importorskip)
    def pytest_collectreport(self, report: Any) -> None:
        if report.skipped:
            self.skipped.append(f"{report.nodeid or '<collection>'} (collection)")

    # --- setup-level (fixture skip) and call-level skips
    def pytest_runtest_logreport(self, report: Any) -> None:
        # An ``xfail`` that fails as expected reports ``skipped``, and is counted
        # as one: a required payload marked known-broken has not certified the
        # behaviour either. (An ``xpass`` reports ``passed`` and does count — the
        # behaviour did run and did work; only the marker is stale.)
        if report.skipped:
            self.skipped.append(f"{report.nodeid} ({report.when})")
        elif report.when == "call" and (report.passed or report.failed):
            self.executed += 1

    def verdict(self) -> str:
        """Return a refusal reason, or an empty string when the run is real."""
        if self.mode == "required" and self.skipped:
            listed = "\n".join(f"  - {nodeid}" for nodeid in self.skipped[:20])
            more = "" if len(self.skipped) <= 20 else f"\n  ... and {len(self.skipped) - 20} more"
            return (
                f"required selection skipped {len(self.skipped)} test(s); a skipped "
                f"required payload is not evidence that it passed:\n{listed}{more}"
            )
        # Checked in every mode. ``nonempty`` accepts that a broad suite skips
        # *some* tests, but a suite that skipped or deselected every one of them
        # has certified nothing at all, and pytest exits 0 for exactly that.
        if self.executed == 0:
            details = []
            if self.deselected:
                details.append(f"{len(self.deselected)} deselected")
            if self.skipped:
                details.append(f"{len(self.skipped)} skipped")
            detail_str = (
                f" ({', '.join(details)})"
                if details
                else " (nothing was collected or everything was deselected)"
            )
            return (
                f"selection executed no tests{detail_str}; an empty selection cannot "
                f"certify anything (check the marker expression and that the "
                f"payload exists)"
            )
        return ""


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--no-silent-skip-mode",
        choices=list(MODES),
        default=DEFAULT_MODE,
        help=(
            "no_silent_skip strictness: 'required' rejects any skip in a "
            "single-payload selection; 'nonempty' tolerates individual skips but "
            "still rejects a session that executed nothing."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    guard = RequiredSelectionGuard(mode=str(config.getoption("no_silent_skip_mode")))
    config.pluginmanager.register(guard, _ENV_MARKER)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Convert a vacuous success into a failure exit code."""
    # Only ever make a passing run stricter — never mask a genuine failure.
    if exitstatus not in (0, 5):  # 5 == EXIT_NOTESTSCOLLECTED
        return
    guard = session.config.pluginmanager.get_plugin(_ENV_MARKER)
    if guard is None:  # pragma: no cover - registered in pytest_configure
        return
    reason = guard.verdict()
    if not reason:
        return
    session.exitstatus = NO_RESULT_EXIT_CODE
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line(f"no_silent_skip: refusing a vacuous pass — {reason}", red=True)
