"""Trusted pytest inventory output for OmniAgentOS certification.

The certification shell invokes pytest twice with this plugin:

* ``expected`` mode performs collection only and records the complete node-id set.
* ``execution`` mode records the selected node-id set plus one terminal outcome
  for every selected offline test.

The evidence builder compares both artifacts exactly.  JUnit XML alone cannot
report deselected tests, so it is insufficient for proving that the requested
surface was actually exercised in full.

Deselection is the attack this plugin exists to defeat.  ``session.items`` at
:func:`pytest_collection_finish` is the *post-filter* list, so an inventory
built only from it certifies happily when the same tests are removed from both
runs.  The full pre-filter inventory is therefore captured in a ``tryfirst``
:func:`pytest_collection_modifyitems` hook — before ``-k``/``-m``/``--deselect``
and before any third-party filter — and every deselection is recorded
explicitly via :func:`pytest_deselected`.  The recorded selection knobs,
environment, plugin distributions and this module's own resolved path let the
evaluator reject an inventory produced under a hostile selection environment or
by a foreign plugin outside the certified tree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

SCHEMA = "omniagentos.pytest-inventory.v2"

_collected_nodeids: list[str] = []
_selected_nodeids: list[str] = []
_deselected_nodeids: dict[str, list[str]] = {}
_outcomes: dict[str, str] = {}

FORBIDDEN_MARKERS = {
    "live_cli",
    "perf",
    "live_ollama",
    "live",
    "counterfeit_gate",
    "feature_health",
    "e2e",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("omniagentos-certification")
    group.addoption(
        "--certification-inventory",
        action="store",
        default=None,
        help="write the trusted pytest inventory JSON to this path",
    )
    group.addoption(
        "--certification-inventory-mode",
        choices=("expected", "execution"),
        default=None,
        help="identify collection-only versus test-execution inventory",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    del session
    _collected_nodeids.clear()
    _selected_nodeids.clear()
    _deselected_nodeids.clear()
    _outcomes.clear()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Record the inventory BEFORE any filter is allowed to remove an item."""
    del session, config
    _collected_nodeids.extend(item.nodeid for item in items)


def pytest_deselected(items: list[pytest.Item]) -> None:
    """Record every deselection so an unattested/noncanonical deselection cannot certify.

    This is necessary because the exact canonical offline marker is allowed.
    """
    for item in items:
        reasons = []
        for marker in item.iter_markers():
            if marker.name in FORBIDDEN_MARKERS:
                reasons.append(marker.name)
        _deselected_nodeids[item.nodeid] = sorted(list(set(reasons)))


def pytest_collection_finish(session: pytest.Session) -> None:
    _selected_nodeids.extend(item.nodeid for item in session.items)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    nodeid = report.nodeid
    current = _outcomes.get(nodeid)
    if report.failed:
        _outcomes[nodeid] = "failed"
    elif report.skipped or hasattr(report, "wasxfail"):
        if current != "failed":
            _outcomes[nodeid] = "skipped"
    elif report.when == "call" and report.passed and current not in {"failed", "skipped"}:
        _outcomes[nodeid] = "passed"


def _write_inventory(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _selection_options(config: pytest.Config) -> dict[str, Any]:
    """Every knob that can shrink the executed surface, as pytest resolved it."""
    option = config.option
    return {
        "keyword": str(getattr(option, "keyword", "") or ""),
        "markexpr": str(getattr(option, "markexpr", "") or ""),
        "deselect": _string_list(getattr(option, "deselect", None)),
        "ignore": _string_list(getattr(option, "ignore", None)),
        "ignore_glob": _string_list(getattr(option, "ignore_glob", None)),
        "maxfail": int(getattr(option, "maxfail", 0) or 0),
        "last_failed": bool(getattr(option, "lf", False)),
        "failed_first": bool(getattr(option, "failedfirst", False)),
        "collect_only": bool(getattr(option, "collectonly", False)),
    }


def _environment(config: pytest.Config) -> dict[str, Any]:
    return {
        "plugin_autoload_disabled": os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1",
        "pytest_addopts": os.environ.get("PYTEST_ADDOPTS", ""),
        "pytest_plugins": os.environ.get("PYTEST_PLUGINS", ""),
        # Populated only by setuptools entry-point autoloading, so an empty
        # list is positive proof that no external pytest plugin was loaded.
        "plugin_dists": sorted(
            str(dist.project_name) for _plugin, dist in config.pluginmanager.list_plugin_distinfo()
        ),
        "pytest_version": str(pytest.__version__),
    }


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    output = session.config.getoption("--certification-inventory")
    mode = session.config.getoption("--certification-inventory-mode")
    if not output or mode not in {"expected", "execution"}:
        return
    invocation = session.config.invocation_params
    _write_inventory(
        Path(output),
        {
            "schema": SCHEMA,
            "mode": mode,
            "pytest_exit_code": int(exitstatus),
            "collected_nodeids": list(_collected_nodeids),
            "selected_nodeids": list(_selected_nodeids),
            "deselected_nodeids": dict(_deselected_nodeids),
            "outcomes": dict(_outcomes),
            "selection": _selection_options(session.config),
            "environment": _environment(session.config),
            "producer": {
                # Proves which plugin file produced this artifact: a copy that
                # was shadowed by a foreign package on sys.path cannot pass the
                # evaluator's containment check.
                "plugin_file": str(Path(__file__).resolve()),
                "invocation_args": [str(arg) for arg in invocation.args],
                "invocation_dir": str(Path(str(invocation.dir)).resolve()),
            },
        },
    )
