"""The two promises on the tin: **stdlib only**, and **importing it does nothing**.

Both are claims a stranger has to be able to check in a minute, and both are the
kind of claim that decays silently — one convenience import of ``requests``, one
``sys.path`` line added to make a script work from a different directory, and the
package is no longer the thing its README describes. So they are measured rather
than asserted.

The side-effect probe runs in a SUBPROCESS and diffs what the interpreter looked
like before and after the import: ``sys.path``, ``os.environ``, the working
directory and its contents. Measuring in-process would prove nothing, because by
the time a test runs, ``selfloop`` has already been imported by the collector.

The reason this matters more than tidiness: the system this package was extracted
from mutated ``sys.path`` at import and derived a repository root from its own
``__file__``. That single pair of lines was the hardest blocker to ever
pip-installing it — the moment the package moved, the derived root resolved
somewhere arbitrary, and checkpoints and lease files were written into it.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType

import pytest
import selfloop

#: The package's source tree, found through the imported package rather than by
#: walking up from ``__file__`` — the same reason the package itself never
#: guesses a repository root.
PACKAGE_ROOT = Path(selfloop.__file__).resolve().parent
PROTOTYPE_ROOT = PACKAGE_ROOT.parent

MODULE_FILES: list[Path] = sorted(PACKAGE_ROOT.rglob("*.py"))


def _module_name(path: Path) -> str:
    parts = path.relative_to(PACKAGE_ROOT).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(("selfloop", *parts))


MODULE_NAMES: list[str] = [_module_name(path) for path in MODULE_FILES]

#: Modules that would mean this package talks to a network. None of them may be
#: imported anywhere in the tree: a loop that runs unattended on somebody else's
#: machine reaches the world exclusively through the tools its
#: :class:`~selfloop.contracts.ToolRegistry` was granted, and a connector this
#: package opened itself would be authority nobody granted it.
NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "xmlrpc",
        "webbrowser",
    }
)


def test_the_module_inventory_is_not_empty() -> None:
    """Guards every other test in this file against a silently empty scan.

    A suite whose subject list is computed can pass by finding nothing, which is
    the vacuous gate this package refuses everywhere else. It should refuse it
    about itself too.
    """
    assert len(MODULE_FILES) >= 20
    assert "selfloop.receipts" in MODULE_NAMES
    assert "selfloop.adapters.sqlite" in MODULE_NAMES
    assert "selfloop.templates" in MODULE_NAMES


# ---------------------------------------------------------------------------
# stdlib only
# ---------------------------------------------------------------------------


def _imported_roots(path: Path) -> set[str]:
    """Top-level module names imported by *path*, from its AST.

    Read from the source rather than from ``sys.modules`` on purpose: an import
    inside a function body — ``importlib`` in the lazy ``__getattr__`` hooks, or
    ``fcntl`` behind an availability check — is still a dependency, and it is
    exactly the kind that a runtime probe would miss because the branch was never
    taken.
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", MODULE_FILES, ids=MODULE_NAMES)
def test_no_module_imports_anything_outside_the_standard_library(path: Path) -> None:
    """Zero dependencies is a design commitment, not an accident of the moment.

    A loop that runs unattended on somebody else's machine should not be able to
    break because a transitive dependency published a new minor version
    overnight.
    """
    outside = sorted(
        root
        for root in _imported_roots(path)
        if root != "selfloop" and root not in sys.stdlib_module_names
    )
    assert not outside, f"{_module_name(path)} imports non-stdlib module(s) {outside}"


@pytest.mark.parametrize("path", MODULE_FILES, ids=MODULE_NAMES)
def test_no_module_opens_a_network_connection_of_its_own(path: Path) -> None:
    reached = sorted(_imported_roots(path) & NETWORK_MODULES)
    assert not reached, f"{_module_name(path)} imports network module(s) {reached}"


# ---------------------------------------------------------------------------
# No import-time side effects
# ---------------------------------------------------------------------------

#: Runs in a clean interpreter, in an EMPTY working directory, and reports what
#: changed around each import. Everything it measures is something the package
#: promises not to touch.
PROBE = r"""
import importlib
import json
import os
import pkgutil
import sys

cwd = os.getcwd()
before = {
    "path": list(sys.path),
    "env": dict(os.environ),
    "listing": sorted(os.listdir(cwd)),
}

import selfloop

after_root = {
    "path": list(sys.path),
    "heavy": sorted(m for m in sys.modules if m in ("sqlite3", "subprocess")),
}

imported = []
for info in pkgutil.walk_packages(selfloop.__path__, prefix="selfloop."):
    imported.append(info.name)
    importlib.import_module(info.name)

print(json.dumps({
    "before": before,
    "after_root": after_root,
    "after_all": {
        "path": list(sys.path),
        "env": dict(os.environ),
        "listing": sorted(os.listdir(cwd)),
        "cwd": os.getcwd(),
    },
    "cwd": cwd,
    "imported": sorted(imported),
}))
"""


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Import every module in a clean interpreter and report what moved.

    Module-scoped because it costs a process and every assertion below reads the
    same measurement; splitting it into one subprocess per property would say
    nothing extra and would quadruple the cost.
    """
    workspace = tmp_path_factory.mktemp("probe")
    script = workspace / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    empty = workspace / "empty"
    empty.mkdir()

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=empty,
        env={
            **os.environ,
            "PYTHONPATH": str(PROTOTYPE_ROOT),
            # Keeps the probe from littering the source tree with __pycache__.
            # It is about this test's own tidiness; the package writes nothing
            # either way, which is what the assertions below establish.
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, f"the probe failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


def test_importing_the_package_does_not_touch_sys_path(probe: dict) -> None:
    """The line that made the predecessor impossible to pip-install."""
    assert probe["after_root"]["path"] == probe["before"]["path"]


def test_importing_every_module_does_not_touch_sys_path(probe: dict) -> None:
    """Not just the façade: every module in the tree, imported, changes nothing."""
    assert probe["after_all"]["path"] == probe["before"]["path"]


def test_importing_every_module_does_not_write_the_environment(probe: dict) -> None:
    assert probe["after_all"]["env"] == probe["before"]["env"]


def test_importing_every_module_touches_no_files_and_does_not_chdir(
    probe: dict,
) -> None:
    """No lease file, no database, no directory created, no working directory moved."""
    assert probe["before"]["listing"] == []
    assert probe["after_all"]["listing"] == []
    assert probe["after_all"]["cwd"] == probe["cwd"]


def test_the_probe_actually_imported_the_whole_tree(probe: dict) -> None:
    """Non-vacuity again: a walk that found nothing would pass every test above."""
    imported = probe["imported"]
    assert len(imported) >= 20
    for name in ("selfloop.receipts", "selfloop.runtime", "selfloop.adapters.sqlite"):
        assert name in imported


def test_importing_the_facade_stays_cheap(probe: dict) -> None:
    """``import selfloop`` costs four small pure modules and no heavy ones.

    The lazy PEP 562 hooks in ``selfloop/__init__.py`` and
    ``selfloop/adapters/__init__.py`` are what keep that true, and this is the
    measurement that stops a convenience import at the top of one of them from
    quietly reintroducing ``sqlite3`` and ``subprocess`` for every caller.
    """
    assert probe["after_root"]["heavy"] == []


@pytest.mark.parametrize("module", MODULE_NAMES)
def test_every_module_imports_first_in_a_clean_interpreter(module: str) -> None:
    """No module may depend on another having been imported before it.

    ``selfloop.templates`` has a genuine cycle with the two shipped templates —
    resolved only because ``LoopTemplate`` is bound before the import edge is
    traversed — and a cycle that resolves in one order and not another is a
    package that works until somebody imports it differently. This is the check
    that keeps the resolution honest rather than lucky.
    """
    completed = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=PROTOTYPE_ROOT,
        env={**os.environ, "PYTHONPATH": str(PROTOTYPE_ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, f"import {module} failed:\n{completed.stderr}"


def test_the_module_entry_point_does_not_run_the_cli_when_imported() -> None:
    """``python -m selfloop`` runs the CLI; importing ``selfloop.__main__`` must not.

    Without the ``__name__`` guard, anything that merely walks the package —
    this suite, a documentation tool, a bundler — would execute the whole command
    line and exit the interpreter.
    """
    module = import_module("selfloop.__main__")
    assert callable(module.main)


# ---------------------------------------------------------------------------
# Every module declares its surface
# ---------------------------------------------------------------------------


@pytest.fixture(params=MODULE_NAMES)
def module(request: pytest.FixtureRequest) -> ModuleType:
    return import_module(request.param)


def test_every_module_declares_all(module: ModuleType) -> None:
    """``__all__`` is the module's statement about what it is FOR.

    Without one, "the public API" is whatever happens not to start with an
    underscore, which silently includes every name a module imported from
    somewhere else — and a reader cannot tell the difference between an export
    and an implementation detail that leaked.
    """
    exported = getattr(module, "__all__", None)
    assert exported is not None, f"{module.__name__} has no __all__"
    assert isinstance(exported, list)
    assert exported, f"{module.__name__} declares an empty __all__"
    assert all(isinstance(name, str) for name in exported)


def test_no_module_exports_a_name_twice(module: ModuleType) -> None:
    exported = list(module.__all__)
    duplicates = sorted({name for name in exported if exported.count(name) > 1})
    assert not duplicates, f"{module.__name__} exports {duplicates} more than once"


def test_every_exported_name_resolves(module: ModuleType) -> None:
    """Including the lazily-resolved ones, which is the point of checking.

    ``selfloop.run_once`` and the sqlite adapter names are served by PEP 562
    ``__getattr__`` hooks, so a typo in one of those maps is invisible until
    somebody reaches for the name. Here, that is now.
    """
    missing = [name for name in module.__all__ if not hasattr(module, name)]
    assert not missing, f"{module.__name__} exports names it does not define: {missing}"


def test_a_lazy_facade_still_refuses_an_unknown_name() -> None:
    """A ``__getattr__`` hook that returned something for any name would hide typos."""
    with pytest.raises(AttributeError):
        selfloop.not_a_real_export  # noqa: B018 - the attribute access IS the assertion
    assert "run_once" in dir(selfloop)
