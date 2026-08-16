"""Build the coverage map: run the suite with per-test contexts, then reduce it.

    uv pip install coverage            # not a declared dependency of this repo
    python -m scripts.tia.build_map --run --workers 8

Three things this module knows that a bare `coverage run -m pytest` does not:

1. **xdist workers are separate processes.** Only the controller is measured unless a
   ``.pth`` in site-packages calls :func:`coverage.process_startup` in every interpreter
   and the config sets ``parallel = True``. ``--run`` installs that hook (opt out with
   ``--no-install-hook`` and accept a map of the controller's own coverage, i.e. nearly
   nothing).
2. **The marker expression is derived, never duplicated.** It is read out of
   ``[tool.pytest.ini_options] addopts`` and composed with ``and not smoke``; pytest's
   ``-m`` is store-not-append, so passing a bare ``-m`` would silently *readmit* the
   live/perf/counterfeit lanes that pyproject excludes.
3. **One test cannot be measured with contexts at all.**
   ``tests/lab/eval/test_grader.py::test_protected_grader_scores_out_of_process_and_returns_no_expected``
   monkeypatches ``sqlite3.connect`` process-wide to prove the parent never opens the
   protected DB; coverage flushes its data to SQLite on every context switch, trips that
   guard, and kills the xdist worker (observed: worker crash + session INTERNALERROR).
   It is deselected for the build and recorded in ``unmeasured_tests`` — the code it
   alone covers therefore stays *absent* from the map, which forces FULL. That is the
   safe direction, and it is written down rather than silently absorbed.

Excluded from the build for the same reason as the fast lane: ``smoke`` (spawns real
processes and ports; a coverage-instrumented fleet of them on a loaded box is noise, not
signal). Anything a build excludes is simply not in the map, and anything not in the map
forces FULL, so exclusions cost selectivity and never safety.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import sysconfig
import tomllib
from collections.abc import Sequence
from pathlib import Path

from scripts.tia.coverage_map import (
    DEFAULT_MAP_RELPATH,
    CoverageMap,
    CoverageMapError,
    build_map_from_coverage_data,
    coverage_run_config_text,
)

#: Node ids that cannot run under coverage contexts, with the reason (see module docs).
#: All five replace or wrap ``sqlite3.connect`` process-wide. Coverage stores its data in
#: SQLite and reconnects on every context switch, so it walks straight into the patch:
#: two of these killed an xdist worker outright (AssertionError from the guard, and
#: "Cannot operate on a closed database"), and the counting ones would silently count
#: coverage's own connections into their assertions.
UNMEASURABLE_NODES: tuple[tuple[str, str], ...] = (
    (
        "tests/lab/eval/test_grader.py::test_protected_grader_scores_out_of_process_and_returns_no_expected",
        "sqlite3.connect patched to raise; coverage's context flush trips the guard",
    ),
    (
        "tests/reflection/test_perrun.py::test_close_failures_inside_analyze_run_are_logged",
        "sqlite3.connect wrapped so close() raises; coverage's next flush hits a closed DB",
    ),
    (
        "tests/improve/test_dispatcher.py::test_single_writer_workers_emit_files_only",
        "sqlite3.connect patched to explode",
    ),
    (
        "tests/swarm/test_idle_hotloop.py::test_ultra_cap_idle_poll_reuses_connection_and_waits",
        "counts sqlite3.connect calls; coverage's own connections would be counted in",
    ),
    (
        "tests/orchestrator/test_executor.py::test_repeated_attach_reuses_and_closes_runner_owned_supervisor",
        "counts sqlite3.connect calls; coverage's own connections would be counted in",
    ),
)

SUBPROCESS_HOOK_FILENAME = "zz_tia_coverage_subprocess.pth"
#: A `.pth` runs in EVERY interpreter that uses this venv, so the plain
#: ``import coverage; coverage.process_startup()`` form turns a later `uv sync` (which
#: drops coverage — it is not a declared dependency) into an ImportError on every python
#: start. Guarded, it degrades to a no-op instead. `.pth` lines must be one line and
#: begin with `import`; `process_startup()` itself no-ops unless COVERAGE_PROCESS_START
#: is set, so leaving the file installed costs nothing.
SUBPROCESS_HOOK_TEXT = (
    'import importlib.util; (importlib.util.find_spec("coverage") '
    'and __import__("coverage").process_startup())\n'
)

BUILD_DIR_RELPATH = "var/test-selection"


def marker_expression(pyproject_path: str | Path, *, exclude_smoke: bool = True) -> str:
    """Derive the run's ``-m`` expression from pyproject instead of restating it.

    Restating it is how the fast lane silently readmitted nine live/perf tests
    (see the Makefile's note); the same trap applies here.
    """
    payload = tomllib.loads(Path(pyproject_path).read_text(encoding="utf-8"))
    addopts = ((payload.get("tool") or {}).get("pytest") or {}).get("ini_options", {}).get(
        "addopts", ""
    )
    tokens = shlex.split(addopts) if isinstance(addopts, str) else list(addopts)
    base = ""
    for index, token in enumerate(tokens):
        if token == "-m" and index + 1 < len(tokens):
            base = tokens[index + 1]
        elif token.startswith("-m") and len(token) > 2:
            base = token[2:]
    if not base:
        raise CoverageMapError(
            f"{pyproject_path} declares no addopts -m expression; refusing to guess one"
        )
    return f"({base}) and not smoke" if exclude_smoke else base


DESELECT_NODES: tuple[str, ...] = tuple(node for node, _reason in UNMEASURABLE_NODES)


def pytest_argv(
    marker: str,
    workers: int,
    deselect: Sequence[str] = DESELECT_NODES,
) -> list[str]:
    argv = [sys.executable, "-m", "pytest", "-q", "-m", marker]
    if workers > 1:
        argv += ["-n", str(workers)]
    for node in deselect:
        argv += ["--deselect", node]
    return argv


def install_subprocess_hook(site_packages: str | Path | None = None) -> Path:
    """Install the ``.pth`` that starts coverage in every child interpreter."""
    target_dir = Path(site_packages or sysconfig.get_paths()["purelib"])
    target = target_dir / SUBPROCESS_HOOK_FILENAME
    target.write_text(SUBPROCESS_HOOK_TEXT, encoding="utf-8")
    return target


def _git_head(repo_root: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def run_suite(repo_root: Path, build_dir: Path, workers: int, install_hook: bool) -> int:
    data_file = build_dir / ".coverage"
    build_dir.mkdir(parents=True, exist_ok=True)
    for stale in build_dir.glob(".coverage*"):
        stale.unlink()
    config_path = build_dir / "coverage-run.cfg"
    config_path.write_text(
        coverage_run_config_text(repo_root / "pyproject.toml", data_file, parallel=workers > 1),
        encoding="utf-8",
    )
    if install_hook and workers > 1:
        print(f"[build_map] subprocess hook -> {install_subprocess_hook()}")
    env = dict(os.environ)
    env["COVERAGE_PROCESS_START"] = str(config_path)
    env["COVERAGE_RCFILE"] = str(config_path)
    argv = pytest_argv(marker_expression(repo_root / "pyproject.toml"), workers)
    if workers <= 1:
        argv = [sys.executable, "-m", "coverage", "run", *argv[2:]]
    print(f"[build_map] {shlex.join(argv)}")
    proc = subprocess.run(argv, cwd=repo_root, env=env, check=False)
    combine = subprocess.run(
        [sys.executable, "-m", "coverage", "combine", "--keep"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"[build_map] combine rc={combine.returncode} {combine.stdout.strip()[:400]}")
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the TIA coverage map.")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--run", action="store_true", help="run the suite under coverage first")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-install-hook", action="store_true")
    parser.add_argument("--data-file", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    build_dir = repo_root / BUILD_DIR_RELPATH
    data_file = Path(args.data_file) if args.data_file else build_dir / ".coverage"
    out_path = Path(args.out) if args.out else repo_root / DEFAULT_MAP_RELPATH

    if args.run:
        rc = run_suite(repo_root, build_dir, args.workers, not args.no_install_hook)
        print(f"[build_map] suite rc={rc} (a red suite still produces a usable map)")

    try:
        cmap = build_map_from_coverage_data(data_file, repo_root, commit=_git_head(repo_root))
    except CoverageMapError as exc:
        print(f"build_map: {exc}", file=sys.stderr)
        return 2
    cmap.save(out_path)
    print(
        f"[build_map] {out_path}: {len(cmap.source_to_tests)} source files -> "
        f"{cmap.test_count} test files; {len(cmap.excluded_files)} measured files excluded "
        f"(absent => FULL); {len(cmap.unresolved_contexts)} unresolved contexts"
    )
    return 0


def loaded_map(repo_root: str | Path) -> CoverageMap:
    """Convenience for callers that just want the built map."""
    return CoverageMap.load(Path(repo_root) / DEFAULT_MAP_RELPATH)


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
