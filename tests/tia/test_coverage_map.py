"""Map construction, driven by a real coverage artifact.

``fixtures/coverage_contexts.json`` is verbatim output of

    coverage run -m pytest tests     # [run] branch=True, dynamic_context=test_function
    coverage json --show-contexts

over a two-module / three-test project, committed rather than hand-written so the parser
is tested against the format coverage actually emits (including the empty ``""`` context
for import-time lines, which is the case that decides whether a file is selectable).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.tia.build_map import (
    SUBPROCESS_HOOK_TEXT,
    UNMEASURABLE_NODES,
    install_subprocess_hook,
    marker_expression,
    pytest_argv,
)
from scripts.tia.coverage_map import (
    SCHEMA_VERSION,
    CoverageMap,
    CoverageMapError,
    build_map_from_context_pairs,
    build_map_from_coverage_json,
    coverage_run_config_text,
    resolve_context,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "coverage_contexts.json"

#: The tree the fixture was measured on. Recreated (empty files are enough — resolution
#: is by path existence) so context -> file resolution has something to resolve against.
FIXTURE_TREE: tuple[str, ...] = (
    "pkg/__init__.py",
    "pkg/mod_a.py",
    "pkg/mod_b.py",
    "tests/__init__.py",
    "tests/test_alpha.py",
    "tests/sub/__init__.py",
    "tests/sub/test_beta.py",
)


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    for rel in FIXTURE_TREE:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def fixture_map(fixture_root: Path) -> CoverageMap:
    return build_map_from_coverage_json(json.loads(FIXTURE.read_text()), fixture_root)


# --------------------------------------------------------------------------------------
# real coverage output -> map
# --------------------------------------------------------------------------------------
def test_map_attributes_each_source_file_to_the_tests_that_executed_it(
    fixture_map: CoverageMap,
) -> None:
    assert fixture_map.tests_for("pkg/mod_a.py") == frozenset(
        {"tests/test_alpha.py", "tests/sub/test_beta.py"}
    )
    assert fixture_map.tests_for("pkg/mod_b.py") == frozenset({"tests/sub/test_beta.py"})
    assert fixture_map.test_count == 2


def test_import_time_only_file_is_absent_not_mapped_to_nothing(
    fixture_map: CoverageMap,
) -> None:
    """``pkg/__init__.py`` ran at import with no test context.

    Absent (``None``) means "no evidence" and forces FULL. An empty set would mean "no
    tests needed", which is the false negative this whole package exists to prevent.
    """
    assert fixture_map.tests_for("pkg/__init__.py") is None
    assert "pkg/__init__.py" in fixture_map.excluded_files


def test_no_mapped_file_ever_has_an_empty_test_set(fixture_map: CoverageMap) -> None:
    assert all(tests for tests in fixture_map.source_to_tests.values())


def test_json_written_without_show_contexts_is_refused(fixture_root: Path) -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["meta"]["show_contexts"] = False
    with pytest.raises(CoverageMapError, match="show-contexts"):
        build_map_from_coverage_json(payload, fixture_root)


def test_roundtrip_through_disk_preserves_the_map(fixture_map: CoverageMap, tmp_path: Path) -> None:
    target = tmp_path / "map.json"
    fixture_map.save(target)
    reloaded = CoverageMap.load(target)
    assert reloaded.source_to_tests == fixture_map.source_to_tests
    assert reloaded.test_count == fixture_map.test_count


def test_loading_a_map_from_another_schema_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "map.json"
    target.write_text(json.dumps({"schema_version": SCHEMA_VERSION + 1}), encoding="utf-8")
    with pytest.raises(CoverageMapError, match="schema"):
        CoverageMap.load(target)


def test_loading_a_map_with_an_empty_test_set_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "map.json"
    target.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "source_to_tests": {"a.py": []}}),
        encoding="utf-8",
    )
    with pytest.raises(CoverageMapError, match="empty test set"):
        CoverageMap.load(target)


def test_loading_a_missing_map_is_an_error_not_an_empty_map(tmp_path: Path) -> None:
    with pytest.raises(CoverageMapError):
        CoverageMap.load(tmp_path / "nope.json")


# --------------------------------------------------------------------------------------
# context resolution
# --------------------------------------------------------------------------------------
def test_dotted_context_with_a_class_resolves_to_its_file(fixture_root: Path) -> None:
    assert (
        resolve_context("tests.test_alpha.TestAdd.test_method", fixture_root)
        == "tests/test_alpha.py"
    )


def test_dotted_context_in_a_subpackage_resolves(fixture_root: Path) -> None:
    assert resolve_context("tests.sub.test_beta.test_sub", fixture_root) == "tests/sub/test_beta.py"


def test_pytest_cov_node_id_contexts_resolve(fixture_root: Path) -> None:
    assert resolve_context("tests/test_alpha.py::test_add", fixture_root) == "tests/test_alpha.py"
    assert (
        resolve_context("tests/test_alpha.py::TestAdd::test_method|run", fixture_root)
        == "tests/test_alpha.py"
    )


def test_empty_context_resolves_to_nothing(fixture_root: Path) -> None:
    assert resolve_context("", fixture_root) is None
    assert resolve_context("   ", fixture_root) is None


def test_context_outside_the_test_roots_does_not_resolve(fixture_root: Path) -> None:
    """A ``test_*`` function living in production code is not a test file."""
    (fixture_root / "pkg" / "helper.py").write_text("", encoding="utf-8")
    assert resolve_context("pkg.helper.test_helper", fixture_root) is None


def test_context_for_a_file_that_does_not_exist_does_not_resolve(fixture_root: Path) -> None:
    assert resolve_context("tests.gone.test_missing.test_x", fixture_root) is None


def test_a_module_name_that_is_not_repo_relative_resolves_by_unique_suffix(
    tmp_path: Path,
) -> None:
    """Real case: `tests/sessions/api/test_x.py` reports ``__name__`` as ``api.test_x``.

    pytest puts the nearest directory without an ``__init__.py`` on sys.path, so the
    dotted context is relative to ``tests/sessions``, not to the repo. Before this
    fallback, 248 measured source files were dropped from the real map for that reason
    alone.
    """
    target = tmp_path / "tests" / "sessions" / "api" / "test_external_ingest.py"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    assert (
        resolve_context("api.test_external_ingest.test_start", tmp_path)
        == "tests/sessions/api/test_external_ingest.py"
    )


def test_an_ambiguous_module_suffix_resolves_to_nothing(tmp_path: Path) -> None:
    """Two files share the suffix ``api/test_routes.py``; guessing would misattribute."""
    for prefix in ("lab", "sessions"):
        target = tmp_path / "tests" / prefix / "api" / "test_routes.py"
        target.parent.mkdir(parents=True)
        target.write_text("", encoding="utf-8")
    assert resolve_context("api.test_routes.test_cancel_is_idempotent", tmp_path) is None


def test_a_file_with_an_unresolvable_context_is_excluded_not_partially_mapped(
    fixture_root: Path,
) -> None:
    """Dropping the unresolvable context would silently shrink the file's test set."""
    built = build_map_from_context_pairs(
        [("pkg/mod_a.py", ["tests.test_alpha.test_add", "some.vanished.module.test_x"])],
        fixture_root,
    )
    assert built.tests_for("pkg/mod_a.py") is None
    assert "pkg/mod_a.py" in built.excluded_files
    assert "some.vanished.module.test_x" in built.unresolved_contexts


def test_absolute_paths_are_relativised_and_foreign_paths_dropped(fixture_root: Path) -> None:
    built = build_map_from_context_pairs(
        [
            (str(fixture_root / "pkg" / "mod_a.py"), ["tests.test_alpha.test_add"]),
            ("/usr/lib/python3.12/site-packages/whatever.py", ["tests.test_alpha.test_add"]),
        ],
        fixture_root,
    )
    assert built.tests_for("pkg/mod_a.py") == frozenset({"tests/test_alpha.py"})
    assert not [key for key in built.source_to_tests if key.startswith("/")]


# --------------------------------------------------------------------------------------
# the build configuration is derived from pyproject, not restated
# --------------------------------------------------------------------------------------
def test_run_config_is_derived_from_this_repos_pyproject() -> None:
    """Pins the one shared-file edit this package needed.

    Removing ``dynamic_context = "test_function"`` from ``[tool.coverage.run]`` makes the
    map unbuildable, so it must not be removable quietly.
    """
    text = coverage_run_config_text(REPO / "pyproject.toml", "/tmp/x/.coverage")
    assert "dynamic_context = test_function" in text
    assert "source = omniagentos" in text
    assert "branch = True" in text
    assert "parallel = True" in text
    assert "data_file = /tmp/x/.coverage" in text


def test_run_config_refuses_a_pyproject_without_per_test_contexts(tmp_path: Path) -> None:
    target = tmp_path / "pyproject.toml"
    target.write_text("[tool.coverage.run]\nsource = ['omniagentos']\n", encoding="utf-8")
    with pytest.raises(CoverageMapError, match="dynamic_context"):
        coverage_run_config_text(target, tmp_path / ".coverage")


def test_marker_expression_is_derived_from_addopts_not_restated() -> None:
    expression = marker_expression(REPO / "pyproject.toml")
    for marker in ("live_cli", "perf", "live_ollama", "counterfeit_gate", "feature_health"):
        assert marker in expression, f"{marker} would be readmitted by a bare -m"
    assert expression.endswith("and not smoke")


def test_marker_expression_refuses_a_pyproject_without_addopts(tmp_path: Path) -> None:
    target = tmp_path / "pyproject.toml"
    target.write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']\n", encoding="utf-8")
    with pytest.raises(CoverageMapError, match="addopts"):
        marker_expression(target)


def test_build_argv_deselects_every_context_incompatible_test() -> None:
    argv = pytest_argv("not live", workers=4)
    assert argv[-1] != "-n"
    for node, _reason in UNMEASURABLE_NODES:
        assert node in argv, f"{node} would crash the contexts run"
        assert argv[argv.index(node) - 1] == "--deselect"
    assert "-n" in argv and argv[argv.index("-n") + 1] == "4"


def test_the_subprocess_hook_is_a_no_op_when_coverage_is_absent() -> None:
    """A `.pth` runs in every interpreter, so it must survive coverage being uninstalled.

    ``coverage`` is not a declared dependency here: the next ``uv sync`` removes it. An
    unguarded ``import coverage`` in a `.pth` would then break every python start in the
    venv. ``-S`` reproduces "coverage is not importable" exactly.
    """
    assert SUBPROCESS_HOOK_TEXT.startswith("import "), ".pth lines must start with import"
    assert SUBPROCESS_HOOK_TEXT.count("\n") == 1, ".pth lines must be a single line"
    probe = subprocess.run(
        [sys.executable, "-S", "-c", SUBPROCESS_HOOK_TEXT.strip()],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


def test_installing_the_subprocess_hook_writes_that_exact_line(tmp_path: Path) -> None:
    target = install_subprocess_hook(tmp_path)
    assert target.parent == tmp_path
    assert target.read_text(encoding="utf-8") == SUBPROCESS_HOOK_TEXT


def test_every_unmeasurable_node_still_exists_in_the_tree() -> None:
    """A deselect list that has drifted is silently measuring nothing it claims to skip."""
    for node, _reason in UNMEASURABLE_NODES:
        path, _, name = node.partition("::")
        source = (REPO / path).read_text(encoding="utf-8")
        assert (REPO / path).is_file(), f"{path} no longer exists"
        assert f"def {name}(" in source, f"{node} no longer exists"
