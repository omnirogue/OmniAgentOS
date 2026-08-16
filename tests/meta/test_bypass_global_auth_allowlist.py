"""Pin the collection-time assumption behind ``_bypass_global_auth``.

The root autouse fixture can only install its override when importing a test
module (or one of its collected conftests) has already loaded the ASGI app.
This deliberately keeps ordinary tests from importing the application tree.
The small exception set is safe because it does not exercise a route guarded
by ``require_session_token``; see the matching explanation in
``tests/conftest.py``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"

# These two tests generate or inspect a production ``api/main.py`` source
# fixture rather than importing the live app. They are app-touching for this
# corpus check, but cannot make an authenticated request.
_APP_SOURCE_GENERATOR_MARKERS = {
    "tests/scripts/test_openapi_drift_check.py": "from omniagentos.api.main import app",
    "tests/scripts/test_reachability_gate_framework_routes.py": "omniagentos/api/main.py",
}

_EXPECTED_WITHOUT_COLLECTION_IMPORT = {
    "tests/entrypoints/test_api_lifespan_indexes_vault.py",
    "tests/entrypoints/test_api_lifespan_mints_token.py",
    "tests/entrypoints/test_api_lifespan_seeds.py",
    "tests/entrypoints/test_api_startup_recovers_stale_swarms.py",
    "tests/entrypoints/test_api_startup_refuses_incoherent_sim.py",
    "tests/scripts/test_openapi_drift_check.py",
    "tests/scripts/test_reachability_gate_framework_routes.py",
    "tests/skills/test_integration.py",
    "tests/test_prod_imports.py",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports_api(node: ast.AST) -> bool:
    """Whether an import executes ``omniagentos.api.__init__``.

    The package's ``__init__`` re-exports ``app`` from ``api.main``, so even a
    top-level import of ``omniagentos.api.deps`` has loaded ``api.main`` before
    the function-scoped autouse fixture starts.
    """
    if isinstance(node, ast.Import):
        return any(
            alias.name == "omniagentos.api" or alias.name.startswith("omniagentos.api.")
            for alias in node.names
        )
    return isinstance(node, ast.ImportFrom) and node.module is not None and (
        node.module == "omniagentos.api" or node.module.startswith("omniagentos.api.")
    )


def _imports_app(node: ast.AST) -> bool:
    """Whether an import names the live ASGI application, at any scope."""
    if isinstance(node, ast.Import):
        return any(alias.name == "omniagentos.api.main" for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return False
    return node.module == "omniagentos.api.main" or (
        node.module == "omniagentos.api" and any(alias.name == "app" for alias in node.names)
    )


def _top_level_imports_api(path: Path) -> bool:
    return any(_imports_api(node) for node in _tree(path).body)


def _fixture_names_importing_app(conftest_paths: Iterable[Path]) -> set[str]:
    """Find active fixtures whose body imports the app after autouse starts."""
    names: set[str] = set()
    for conftest_path in conftest_paths:
        for node in ast.walk(_tree(conftest_path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(
                isinstance(decorator, ast.Attribute) and decorator.attr == "fixture"
                or isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "fixture"
                for decorator in node.decorator_list
            ):
                continue
            if any(_imports_app(descendant) for descendant in ast.walk(node)):
                names.add(node.name)
    return names


def _test_uses_fixture_importing_app(path: Path, conftest_paths: Iterable[Path]) -> bool:
    app_fixtures = _fixture_names_importing_app(conftest_paths)
    if not app_fixtures:
        return False
    tree = _tree(path)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "usefixtures"
            and any(
                isinstance(argument, ast.Constant) and argument.value in app_fixtures
                for argument in node.args
            )
        ):
            return True
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith(
            "test_"
        ):
            continue
        if any(
            argument.arg in app_fixtures for argument in (*node.args.args, *node.args.kwonlyargs)
        ):
            return True
    return False


def _applicable_conftests(path: Path) -> list[Path]:
    conftests: list[Path] = []
    directory = path.parent
    while True:
        conftest = directory / "conftest.py"
        if conftest.is_file():
            conftests.append(conftest)
        if directory == _TESTS_ROOT:
            break
        directory = directory.parent
    return conftests


def _is_app_touching(path: Path, conftest_paths: Iterable[Path]) -> bool:
    tree = _tree(path)
    relative_path = path.relative_to(_REPO_ROOT).as_posix()
    marker = _APP_SOURCE_GENERATOR_MARKERS.get(relative_path)
    return (
        any(_imports_app(node) for node in ast.walk(tree))
        or _test_uses_fixture_importing_app(path, conftest_paths)
        or marker is not None and marker in path.read_text(encoding="utf-8")
    )


def _imports_app_during_collection(path: Path, conftest_paths: Iterable[Path]) -> bool:
    return _top_level_imports_api(path) or any(
        _top_level_imports_api(conftest_path) for conftest_path in conftest_paths
    )


def test_app_touching_files_without_collection_import_are_allowlisted() -> None:
    """A new fixture-only app test must explicitly join the documented exception set."""
    observed: set[str] = set()
    for path in _TESTS_ROOT.rglob("test_*.py"):
        conftest_paths = _applicable_conftests(path)
        if _is_app_touching(path, conftest_paths) and not _imports_app_during_collection(
            path, conftest_paths
        ):
            observed.add(path.relative_to(_REPO_ROOT).as_posix())

    assert observed == _EXPECTED_WITHOUT_COLLECTION_IMPORT
