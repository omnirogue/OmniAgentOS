"""Pytest plugin: surface required-suite skips into the default CI signal (M-41).

Hooks are registered via ``tests/conftest.py`` (``pytest_plugins``). They:

* record every skip with nodeid/reason/markers;
* at session end, match against ``configs/coverage_policy.yaml`` required_suites;
* print ``REQUIRED_SUITE_SKIPPED`` / ``REQUIRED_SUITE_ABSENT`` lines to the
  terminal reporter so they appear in default pytest/CI output;
* detect vanished suites (path exists on disk or was expected, but never
  collected) when collection is broad enough that absence is meaningful.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from omniagentos.testpolicy.skip_policy import (
    SkipRecord,
    load_required_suites,
    surface_required_skips,
)

_STORE_KEY = "_omniagentos_required_suite_surface"


class _RequiredSuiteSurfaceStore:
    def __init__(self) -> None:
        self.skips: list[SkipRecord] = []
        self.collected: list[dict[str, Any]] = []
        self.last_report: Any = None
        self.enabled: bool = True


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("omniagentos-testpolicy")
    group.addoption(
        "--surface-required-suites",
        action="store_true",
        default=None,
        help="Force surface required-suite skips/absences (M-41).",
    )
    group.addoption(
        "--no-surface-required-suites",
        action="store_true",
        default=False,
        help="Disable required-suite skip surfacing for this run.",
    )


def pytest_configure(config: pytest.Config) -> None:
    store = _RequiredSuiteSurfaceStore()
    # Default ON for the normal suite; opt-out via flag or env.
    disable_env = os.environ.get("OMNIAGENTOS_SURFACE_REQUIRED_SUITES", "").strip().lower()
    if config.getoption("--no-surface-required-suites") or disable_env in {
        "0",
        "false",
        "no",
        "off",
    }:
        store.enabled = False
    elif config.getoption("--surface-required-suites") or disable_env in {
        "1",
        "true",
        "yes",
        "on",
    }:
        store.enabled = True
    # else leave default True
    setattr(config, _STORE_KEY, store)
    config.addinivalue_line(
        "markers",
        "required_suite(name): optional marker tying a test to a required suite id",
    )


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    store: _RequiredSuiteSurfaceStore | None = getattr(config, _STORE_KEY, None)
    if store is None or not store.enabled:
        return
    collected: list[dict[str, Any]] = []
    for item in items:
        markers = tuple(sorted({m.name for m in item.iter_markers()}))
        path = ""
        try:
            path = str(Path(str(item.fspath)).as_posix())
        except Exception:  # noqa: BLE001
            path = item.nodeid.split("::", 1)[0]
        # Prefer repo-relative path for glob matching.
        for prefix in ("tests/", "/tests/"):
            if prefix in path:
                path = path[path.index("tests/") :]
                break
        collected.append({"nodeid": item.nodeid, "path": path or item.nodeid, "markers": markers})
    store.collected = collected


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    outcome = yield
    report = outcome.get_result()
    if not report.skipped:
        return
    config = item.config
    store: _RequiredSuiteSurfaceStore | None = getattr(config, _STORE_KEY, None)
    if store is None or not store.enabled:
        return
    reason = _skip_reason(report, call)
    markers = tuple(sorted({m.name for m in item.iter_markers()}))
    path = item.nodeid.split("::", 1)[0]
    store.skips.append(SkipRecord(nodeid=item.nodeid, reason=reason, path=path, markers=markers))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    store: _RequiredSuiteSurfaceStore | None = getattr(config, _STORE_KEY, None)
    if store is None or not store.enabled:
        return
    # Absence checks only when collection is broad (full default suite or
    # explicit opt-in). Focused path runs would otherwise spam ABSENT for every
    # suite outside the selected subtree.
    check_absent = _should_check_absent(config, store)
    report = surface_required_skips(
        store.skips,
        collected_items=store.collected,
        check_absent=check_absent,
    )
    store.last_report = report
    if not report.surfaced_messages:
        return
    tw = getattr(config, "pluginmanager", None)
    terminal = None
    if tw is not None:
        terminal = config.pluginmanager.get_plugin("terminalreporter")
    lines = ["", "=== required suite surface (M-41) ===", *report.surfaced_messages, ""]
    if terminal is not None:
        for line in lines:
            terminal.write_line(line)
    else:
        for line in lines:
            print(line)
    # Optional hard-fail for CI when required suites vanish (not for ordinary skips).
    fail_absent = os.environ.get("OMNIAGENTOS_FAIL_REQUIRED_SUITE_ABSENT", "").strip() in {
        "1",
        "true",
        "yes",
    }
    if fail_absent and report.missing_required_runs and exitstatus == 0:
        session.exitstatus = 1


def _should_check_absent(config: pytest.Config, store: _RequiredSuiteSurfaceStore) -> bool:
    force = os.environ.get("OMNIAGENTOS_SURFACE_REQUIRED_ABSENT", "").strip().lower()
    if force in {"1", "true", "yes", "on"}:
        return True
    if force in {"0", "false", "no", "off"}:
        return False
    if config.getoption("--surface-required-suites"):
        return True
    # Broad collection: no path args, or only the default testpaths root.
    args = [str(a) for a in (config.args or [])]
    if not args:
        return True
    broad_tokens = {"", ".", "tests", "tests/", "tests\\"}
    if all(a.rstrip("/") in {"tests", "."} or a in broad_tokens for a in args):
        return True
    # If every required suite path appears under the selected roots, check absent.
    try:
        suites = load_required_suites()
    except Exception:  # noqa: BLE001
        return False
    # When the run collected enough items that at least one suite matched, still
    # report absences for other configured suites only if collection is large.
    if len(store.collected) >= 50:
        return True
    del suites
    return False


def _skip_reason(report: pytest.TestReport, call: pytest.CallInfo[Any]) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        return str(longrepr[2])
    if hasattr(longrepr, "reprcrash") and longrepr.reprcrash is not None:  # type: ignore[union-attr]
        msg = getattr(longrepr.reprcrash, "message", None)  # type: ignore[union-attr]
        if msg:
            return str(msg)
    if call.excinfo is not None:
        try:
            return str(call.excinfo.value)
        except Exception:  # noqa: BLE001
            pass
    text = str(longrepr) if longrepr is not None else ""
    # pytest formats skips as "Skipped: reason"
    if "Skipped:" in text:
        return text.split("Skipped:", 1)[-1].strip()
    return text or "skipped"
