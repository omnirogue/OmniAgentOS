"""How the gate resolves a name to a symbol — the half that decides caller vs coincidence.

Each test here is a probe an adversarial reviewer ran against this gate and against main,
and each names the wrong answer it pins down. Two kinds of wrong answer matter, and they
are not symmetric:

  FALSE-GREEN — the gate says "wired" about something nothing reaches. That is the defect
  this gate exists to catch, so a false-GREEN is the gate lying about its own subject.
  FALSE-RED — the gate refuses working code. Cheap to notice, expensive in trust: it
  trains authors to reach for devtasks/REACHABILITY-EXEMPT.txt, and an exemption written to
  silence a wrong refusal is indistinguishable, later, from one recording a real decision.

So resolution has to be exact in both directions: follow the import graph the way Python
follows it (relative imports, package re-exports, aliases), and refuse to guess anywhere
else (an attribute chain nothing imported, a module whose name merely shares a prefix, a
mention inside a docstring or an `if TYPE_CHECKING:` block).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def reachability_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Reachability Gate Test")
    _git(repo, "config", "user.email", "reachability-gate@example.com")
    gate = repo / "scripts" / "reachability-gate.py"
    gate.parent.mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "reachability-gate.py", gate)
    gate.chmod(0o755)
    (repo / "omniagentos").mkdir()
    (repo / "omniagentos" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "candidate")
    return repo


def _commit_candidate(repo: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate")


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/reachability-gate.py", "candidate", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------------------------
# Following the import graph — false-REDs, the ones that manufacture exemption lines
# --------------------------------------------------------------------------------------


def test_caller_through_a_package_reexport_counts(reachability_repo: Path) -> None:
    """The live ``catalog_stats`` shape, which the repo uses 34 times.

    ``filesearch/__init__.py`` does ``from .catalog import stats as catalog_stats`` and the
    route does ``from omniagentos.filesearch import catalog_stats; catalog_stats()``. The
    caller never names the defining module, so a resolver that only compares the written
    import path finds nothing and refuses a function that is plainly called.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/filesearch/catalog.py": "def stats() -> dict:\n    return {}\n",
            "omniagentos/filesearch/__init__.py": (
                "from omniagentos.filesearch.catalog import stats as catalog_stats\n\n"
                "__all__ = ['catalog_stats']\n"
            ),
            "omniagentos/api/routes/filesearch.py": (
                "from omniagentos.filesearch import catalog_stats\n\n"
                "def _report() -> dict:\n"
                "    return catalog_stats()\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"stats() is called through its package re-export.\nstdout:\n{result.stdout}"
    )


def test_caller_via_relative_import_counts(reachability_repo: Path) -> None:
    """``from .sibling import symbol`` is an import like any other."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/notifications/service.py": (
                "def serialize_notification(row: dict) -> dict:\n    return row\n"
            ),
            "omniagentos/notifications/dal.py": (
                "from .service import serialize_notification\n\n"
                "def _rows() -> list:\n"
                "    return [serialize_notification({})]\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, f"A relative import is an import.\nstdout:\n{result.stdout}"


def test_caller_via_parent_relative_import_counts(reachability_repo: Path) -> None:
    """``from ..pkg.mod import symbol`` — the level must be counted, not ignored."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/notifications/service.py": (
                "def build_board_lookup() -> dict:\n    return {}\n"
            ),
            "omniagentos/api/routes/boards.py": (
                "from ...notifications.service import build_board_lookup\n\n"
                "def _lookup() -> dict:\n"
                "    return build_board_lookup()\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"A parent-relative import is an import.\nstdout:\n{result.stdout}"
    )


def test_symbol_used_as_a_first_class_value_is_wired(reachability_repo: Path) -> None:
    """``HANDLERS = {'ping': handle_ping}`` is wiring, and a dispatcher will call it.

    Requiring a literal ``symbol(...)`` call site refuses every table-driven dispatch in
    the repo. The evidence that matters is that production code holds a reference to the
    function, not the syntax by which it is eventually invoked.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/handlers/ping.py": (
                "def handle_ping(payload: dict) -> str:\n    return 'pong'\n"
            ),
            "omniagentos/handlers/registry.py": (
                "from omniagentos.handlers.ping import handle_ping\n\n"
                "HANDLERS = {'ping': handle_ping}\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"A function placed in a dispatch table is wired.\nstdout:\n{result.stdout}"
    )


def test_symbol_passed_as_an_argument_is_wired(reachability_repo: Path) -> None:
    """Handing the function to something else is also wiring."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/handlers/ping.py": (
                "def handle_ping(payload: dict) -> str:\n    return 'pong'\n"
            ),
            "omniagentos/handlers/registry.py": (
                "from omniagentos.handlers.ping import handle_ping\n\n"
                "def _install(bus) -> None:\n"
                "    bus.subscribe('ping', handle_ping)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"A function handed to a registrar is wired.\nstdout:\n{result.stdout}"
    )


def test_caller_through_a_lazy_importlib_module_counts(reachability_repo: Path) -> None:
    """``vault = importlib.import_module("omniagentos.vault"); vault.render_run_note``.

    Twenty-two production sites import this way to break import cycles. The module is
    named by a string literal, so the binding is every bit as static as an import
    statement — and ``wrapper/durable.py`` reaches ``render_run_note`` through one, with no
    other call site anywhere.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/vault/run_note.py": (
                "def render_run_note(run: dict) -> str:\n    return 'note'\n"
            ),
            "omniagentos/vault/__init__.py": (
                "from omniagentos.vault.run_note import render_run_note\n\n"
                "__all__ = ['render_run_note']\n"
            ),
            "omniagentos/wrapper/durable.py": (
                "import importlib\n\n"
                "def _note(run: dict) -> str:\n"
                "    vault = importlib.import_module('omniagentos.vault')\n"
                "    return vault.render_run_note(run)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"A lazily imported module is still an import.\nstdout:\n{result.stdout}"
    )


def test_dynamic_import_of_the_right_module_without_touching_the_symbol_refuses(
    reachability_repo: Path,
) -> None:
    """Importing the defining module is not using what it defines.

    This is the shape that matters most for the lazy-import mechanism: the module resolves
    correctly, and the gate must STILL refuse, because the symbol is never reached on it.
    Recognising the import without gating on the attribute would hand every module a way to
    vouch for everything it exports.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/vault/run_note.py": (
                "def render_run_note(run: dict) -> str:\n    return 'note'\n\n\n"
                "def write_note(path: str) -> str:\n    return path\n"
            ),
            "omniagentos/wrapper/durable.py": (
                "import importlib\n\n"
                "def _note(path: str) -> str:\n"
                "    vault = importlib.import_module('omniagentos.vault.run_note')\n"
                "    return vault.write_note(path)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"Only write_note is reached on that module.\nstdout:\n{result.stdout}"
    )
    assert "render_run_note()" in result.stdout
    assert "write_note()" not in result.stdout


def test_dynamic_import_of_a_computed_module_name_is_not_a_caller(
    reachability_repo: Path,
) -> None:
    """``import_module(f"omniagentos.{name}")`` names no module the gate can read.

    A literal is a fact; an f-string is a runtime value. Guessing which module it will
    produce is the word-matching this gate was rewritten to stop doing, so the non-literal
    form resolves to nothing and fails closed.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/vault/run_note.py": (
                "def render_run_note(run: dict) -> str:\n    return 'note'\n"
            ),
            "omniagentos/wrapper/durable.py": (
                "import importlib\n\n"
                "def _note(run: dict, name: str) -> str:\n"
                "    mod = importlib.import_module(f'omniagentos.vault.{name}')\n"
                "    return mod.render_run_note(run)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"A computed module name is not a resolvable import.\nstdout:\n{result.stdout}"
    )
    assert "render_run_note()" in result.stdout


def test_dynamic_import_of_an_unrelated_module_is_not_a_caller(
    reachability_repo: Path,
) -> None:
    """The literal has to name the right module; nothing here is guessed."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/vault/run_note.py": (
                "def render_run_note(run: dict) -> str:\n    return 'note'\n"
            ),
            "omniagentos/wrapper/durable.py": (
                "import importlib\n\n"
                "def _note(run: dict) -> str:\n"
                "    other = importlib.import_module('omniagentos.other')\n"
                "    return other.render_run_note(run)\n"
            ),
            "omniagentos/other.py": "VALUE = 1\n",
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"A different module's attribute is a different symbol.\nstdout:\n{result.stdout}"
    )
    assert "render_run_note()" in result.stdout


# --------------------------------------------------------------------------------------
# Refusing to guess — false-GREENs, the ones that make the gate lie about its own subject
# --------------------------------------------------------------------------------------


def test_bare_import_of_a_reexport_is_still_not_a_caller(reachability_repo: Path) -> None:
    """Chain-following must not turn a re-export into a caller.

    ``__init__`` re-exports were how three unwired functions read as wired. Resolving the
    chain tells us WHICH symbol a name refers to; it says nothing about whether anyone
    used it.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/filesearch/catalog.py": "def stats() -> dict:\n    return {}\n",
            "omniagentos/filesearch/__init__.py": (
                "from omniagentos.filesearch.catalog import stats as catalog_stats\n\n"
                "__all__ = ['catalog_stats']\n"
            ),
            "omniagentos/api/routes/filesearch.py": (
                "from omniagentos.filesearch import catalog_stats  # noqa: F401\n\n"
                "DOCS = 'call catalog_stats() yourself'\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"Importing a re-export and never using it is not a call.\nstdout:\n{result.stdout}"
    )
    assert "stats()" in result.stdout


def test_attribute_chain_without_an_import_is_not_a_caller(
    reachability_repo: Path,
) -> None:
    """``self.storage.read()`` does not call ``storage.py:read``.

    The tree holds ~2100 attribute chains whose last-but-one element happens to match some
    module's stem — it is the house style for injected collaborators. Accepting them made
    a module vouch for itself.
    """
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/employee_transcripts/storage.py": (
                "def read(key: str) -> str:\n    return key\n"
            ),
            "omniagentos/employee_transcripts/service.py": (
                "class Service:\n"
                "    def __init__(self, storage) -> None:\n"
                "        self.storage = storage\n\n"
                "    def _fetch(self, key: str) -> str:\n"
                "        return self.storage.read(key)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"An injected collaborator is not the module.\nstdout:\n{result.stdout}"
    )
    assert "read()" in result.stdout


def test_module_prefix_match_needs_a_dot_boundary(reachability_repo: Path) -> None:
    """``omniagentos.search_helpers`` is not inside ``omniagentos.search``."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/search.py": "def probe() -> str:\n    return 'search'\n",
            "omniagentos/search_helpers.py": "def probe() -> str:\n    return 'helper'\n",
            "omniagentos/caller.py": (
                "from omniagentos.search_helpers import probe\n\n"
                "def _go() -> str:\n"
                "    return probe()\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"search_helpers.probe is not search.probe.\nstdout:\n{result.stdout}"
    )
    assert "omniagentos/search.py" in result.stdout
    assert "omniagentos/search_helpers.py" not in result.stdout


def test_docstring_mention_in_the_defining_module_is_not_a_caller(
    reachability_repo: Path,
) -> None:
    """The defining module got a regex pass while everything else got the AST."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/reporting.py": (
                "def compute_total(rows: list) -> int:\n"
                "    return len(rows)\n\n\n"
                "def _helper() -> int:\n"
                '    """See compute_total() for the real arithmetic."""\n'
                "    return 0\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, f"A docstring is not a call site.\nstdout:\n{result.stdout}"
    assert "compute_total()" in result.stdout


def test_same_named_method_in_the_defining_module_is_not_a_caller(
    reachability_repo: Path,
) -> None:
    """``handle.read()`` is a method on `handle`, not the module-level ``read``."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/employee_transcripts/storage.py": (
                "def read(key: str) -> str:\n"
                "    return key\n\n\n"
                "class Handle:\n"
                "    def _proxy(self, key: str) -> str:\n"
                "        return self.store.read(key)\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"A method that shares the name is not the function.\nstdout:\n{result.stdout}"
    )
    assert "read()" in result.stdout


def test_same_module_call_is_still_a_caller(reachability_repo: Path) -> None:
    """The positive control for the two refusals above: a real same-module call passes."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/reporting.py": (
                "def compute_total(rows: list) -> int:\n"
                "    return len(rows)\n\n\n"
                "def _summary(rows: list) -> str:\n"
                "    return f'total={compute_total(rows)}'\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"A real call in the defining module wires the symbol.\nstdout:\n{result.stdout}"
    )


def test_docstring_mention_in_an_init_is_not_a_caller(reachability_repo: Path) -> None:
    """``__init__.py`` got the same regex pass, with the same result."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/scanner/engine.py": "def scan_content() -> str:\n    return 'ok'\n",
            "omniagentos/scanner/__init__.py": (
                '"""Scanning package.\n\n'
                "Call scan_content() to scan a document.\n"
                '"""\n\n'
                "from omniagentos.scanner.engine import scan_content\n\n"
                "__all__ = ['scan_content']\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, (
        f"A package docstring is not a call site.\nstdout:\n{result.stdout}"
    )
    assert "scan_content()" in result.stdout


def test_reference_inside_a_type_checking_block_is_not_wiring(
    reachability_repo: Path,
) -> None:
    """``if TYPE_CHECKING:`` never executes; nothing inside it wires anything."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/search_module.py": "def fetch() -> str:\n    return 'data'\n",
            "omniagentos/caller.py": (
                "from typing import TYPE_CHECKING\n\n"
                "if TYPE_CHECKING:\n"
                "    from omniagentos.search_module import fetch\n\n"
                "    LOOKUP = {'fetch': fetch}\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1, f"Type-checking-only code runs never.\nstdout:\n{result.stdout}"
    assert "fetch()" in result.stdout
