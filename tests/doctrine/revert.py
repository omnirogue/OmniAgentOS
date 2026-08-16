"""Revert-test helper: prove a named test binds to a real behaviour.

The producing checkout is never mutated.  ``revert_test`` builds a bounded
runtime copy in the system temporary directory.  The mutation and both pytest
runs happen in that disposable copy.  A normal return removes it; a SIGKILL can
leave only the bounded copy behind.

The copy deliberately omits Git metadata, caches, and unrelated repository
content.  It contains no symlinks or hard links back to the producer.  This is
not a general sandbox: the named test remains trusted test code, but ordinary
path resolution inside it cannot lead back into the producing checkout.
"""

from __future__ import annotations

import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tests.doctrine._mutate import Mutation, TextReplace, mutated_file
from tests.doctrine._runner import PytestRun, repo_root, run_pytest_node
from tests.doctrine.errors import DoctrineError

# These limits make abandoned SIGKILL residue finite and independently
# inspectable. They are policy bounds, not estimates of today's repository.
_MAX_TARGET_BYTES = 8 * 1024 * 1024
_MAX_SCRATCH_BYTES = 24 * 1024 * 1024
_MAX_SCRATCH_ENTRIES = 2_500
_MAX_TARGET_DEPTH = 32
_SCRATCH_PREFIX = "doctrine-revert-"
_OMITTED_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".venv-hermetic",
        "__pycache__",
        "node_modules",
        "var",
    }
)


@dataclass(frozen=True, slots=True)
class RevertReport:
    """Outcome of a successful revert-test (mutation broke the named test)."""

    target: Path
    nodeid: str
    mutated_failure: PytestRun
    restored_pass: PytestRun

    @property
    def failure_text(self) -> str:
        """Verbatim pytest output while the mutation was applied."""
        return self.mutated_failure.failure_text()

    def summary(self) -> str:
        return (
            f"REVERT-TEST OK  nodeid={self.nodeid!r}  target={self.target}\n"
            f"--- failure with mutation applied (exit {self.mutated_failure.returncode}) ---\n"
            f"{self.failure_text}\n"
            f"--- restored run passed (exit {self.restored_pass.returncode}) ---"
        )


def _scratch_parent(source_root: Path) -> Path:
    """Return an external temporary parent, refusing an in-tree TMPDIR."""
    parent = Path(tempfile.gettempdir()).resolve(strict=True)
    try:
        parent.relative_to(source_root)
    except ValueError:
        return parent
    raise DoctrineError(
        "revert_test refuses a temporary directory inside the producing tree: "
        f"{parent}"
    )


def _relative_target(target: Path, source_root: Path) -> tuple[Path, tuple[str, ...]]:
    """Validate ``target`` and return its canonical path and root-relative parts."""
    if target.is_symlink():
        raise DoctrineError(f"revert_test mutation target must not be a symlink: {target}")
    try:
        canonical = target.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"mutation target does not exist: {target}") from exc
    if not canonical.is_file():
        raise FileNotFoundError(f"mutation target does not exist: {target}")
    if not stat.S_ISREG(canonical.stat().st_mode):
        raise DoctrineError(f"revert_test mutation target must be a regular file: {target}")
    try:
        parts = canonical.relative_to(source_root).parts
    except ValueError as exc:
        raise DoctrineError(
            f"revert_test mutation target is outside pytest cwd {source_root}: {canonical}"
        ) from exc
    if not parts or len(parts) > _MAX_TARGET_DEPTH:
        raise DoctrineError(
            f"revert_test target depth must be 1..{_MAX_TARGET_DEPTH}: {canonical}"
        )
    size = canonical.stat().st_size
    if size > _MAX_TARGET_BYTES:
        raise DoctrineError(
            f"revert_test target is {size} bytes; limit is {_MAX_TARGET_BYTES}"
        )
    return canonical, parts


@dataclass(slots=True)
class _CopyBudget:
    entries: int = 1  # the mkdtemp root itself
    bytes: int = 0

    def add(self, *, size: int = 0) -> None:
        self.entries += 1
        self.bytes += size
        if self.entries > _MAX_SCRATCH_ENTRIES or self.bytes > _MAX_SCRATCH_BYTES:
            raise DoctrineError(
                "revert_test runtime copy exceeds its bounded surface: "
                f"{self.entries}/{_MAX_SCRATCH_ENTRIES} filesystem entries, "
                f"{self.bytes}/{_MAX_SCRATCH_BYTES} bytes"
            )


def _ensure_directory(destination: Path, budget: _CopyBudget) -> None:
    """Create every missing scratch ancestor and charge each to the entry cap."""
    missing: list[Path] = []
    cursor = destination
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        budget.add()


def _copy_path(source: Path, destination: Path, budget: _CopyBudget) -> None:
    """Merge one real file/directory into scratch without producer links."""
    if source.name in _OMITTED_NAMES or source.name.endswith(".pyc"):
        return
    if source.is_symlink():
        raise DoctrineError(f"revert_test runtime source must not be a symlink: {source}")
    if source.is_dir():
        _ensure_directory(destination, budget)
        for child in sorted(source.iterdir(), key=lambda path: path.name):
            _copy_path(child, destination / child.name, budget)
        return
    if not source.is_file() or not stat.S_ISREG(source.stat().st_mode):
        raise DoctrineError(f"revert_test runtime source must be a regular file: {source}")
    if destination.exists():
        return
    _ensure_directory(destination.parent, budget)
    budget.add(size=source.stat(follow_symlinks=False).st_size)
    shutil.copyfile(source, destination, follow_symlinks=False)
    shutil.copymode(source, destination, follow_symlinks=False)
    source_stat = source.stat()
    destination_stat = destination.stat()
    if (source_stat.st_dev, source_stat.st_ino) == (
        destination_stat.st_dev,
        destination_stat.st_ino,
    ):
        raise DoctrineError(f"revert_test runtime copy shares producer inode: {source}")


def _node_path(nodeid: str, source_root: Path) -> Path:
    raw = nodeid.split("::", 1)[0]
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = source_root / candidate
    if candidate.is_symlink():
        raise DoctrineError(f"revert_test nodeid must not name a symlink: {nodeid!r}")
    try:
        canonical = candidate.resolve(strict=True)
        canonical.relative_to(source_root)
    except (FileNotFoundError, ValueError) as exc:
        raise DoctrineError(f"revert_test nodeid is outside pytest cwd: {nodeid!r}") from exc
    if not canonical.is_file() or canonical.is_symlink():
        raise DoctrineError(f"revert_test nodeid must name a real test file: {nodeid!r}")
    return canonical


def _runtime_sources(source_root: Path, target: Path, nodeid: str) -> list[Path]:
    """Select the bounded runtime surface needed by current doctrine callers."""
    node = _node_path(nodeid, source_root)
    candidates = [
        source_root / "omniagentos",
        source_root / "configs",
        source_root / "pyproject.toml",
        source_root / "tests" / "__init__.py",
        source_root / "tests" / "conftest.py",
        source_root / "tests" / "doctrine",
        node.parent,
        target,
    ]
    top_level = target.relative_to(source_root).parts[0]
    if top_level not in {"omniagentos", "tests"}:
        candidates.append(source_root / top_level)

    parent = node.parent
    while parent != source_root:
        for name in ("__init__.py", "conftest.py"):
            candidate = parent / name
            if candidate.is_file():
                candidates.append(candidate)
        parent = parent.parent

    unique: dict[Path, None] = {}
    for candidate in candidates:
        if candidate.exists():
            unique.setdefault(candidate, None)
    return list(unique)


def _materialise_runtime_copy(
    source_root: Path,
    target: Path,
    parts: tuple[str, ...],
    nodeid: str,
) -> tuple[Path, Path]:
    """Create a bounded no-link runtime copy and return ``(root, copied_target)``."""
    scratch = Path(
        tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=_scratch_parent(source_root))
    )
    budget = _CopyBudget()
    try:
        for source in _runtime_sources(source_root, target, nodeid):
            relative = source.relative_to(source_root)
            _copy_path(source, scratch / relative, budget)
        copied_target = scratch.joinpath(*parts)
        if not copied_target.is_file() or copied_target.is_symlink():
            raise DoctrineError(f"revert_test failed to copy mutation target: {target}")
        return scratch, copied_target
    except BaseException:
        shutil.rmtree(scratch)
        raise


@contextmanager
def _isolated_target(
    target: Path,
    source_root: Path,
    nodeid: str,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(scratch_root, copied_target)`` and clean ordinary outcomes."""
    canonical, parts = _relative_target(target, source_root)
    scratch, copied_target = _materialise_runtime_copy(
        source_root,
        canonical,
        parts,
        nodeid,
    )
    try:
        yield scratch, copied_target
    finally:
        # SIGKILL skips this block, leaving only the bounded runtime copy for
        # post-mortem inspection.
        shutil.rmtree(scratch)


def revert_test(
    *,
    target: Path | str,
    mutation: Mutation | TextReplace,
    nodeid: str,
    cwd: Path | str | None = None,
    timeout: float = 120.0,
) -> RevertReport:
    """Mutate an isolated target, demand red, restore there, then demand green.

    Failure output is returned verbatim, matching the original helper contract.
    ``cwd`` is also the isolation root; therefore the target must be contained
    beneath it.  The default remains the repository root.
    """
    path = Path(target)
    source_root = Path(cwd) if cwd is not None else repo_root()
    try:
        source_root = source_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"pytest cwd does not exist: {source_root}") from exc
    if not source_root.is_dir():
        raise NotADirectoryError(f"pytest cwd is not a directory: {source_root}")

    with _isolated_target(path, source_root, nodeid) as (scratch, copied_target):
        runner_env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(scratch),
        }
        with mutated_file(copied_target, mutation):
            broken = run_pytest_node(
                nodeid,
                cwd=scratch,
                env=runner_env,
                timeout=timeout,
            )
            if broken.passed:
                raise DoctrineError(
                    "DECORATION: test still PASSES with the fix/mutation applied — "
                    "it does not bind to the claimed behaviour.\n"
                    f"  nodeid={nodeid!r}\n"
                    f"  target={path}\n"
                    f"  mutation={mutation!r}\n"
                    f"  pytest exit={broken.returncode}\n"
                    f"--- pytest output ---\n{broken.failure_text()}\n"
                    "Do NOT count this test. Report it as decoration."
                )

        restored = run_pytest_node(
            nodeid,
            cwd=scratch,
            env=runner_env,
            timeout=timeout,
        )
        if restored.failed:
            raise DoctrineError(
                "REVERT-TEST incomplete: after restore, the named test still FAILS. "
                "The suite is red for a reason unrelated to the mutation (or restore "
                "did not fully undo the edit).\n"
                f"  nodeid={nodeid!r}\n"
                f"  target={path}\n"
                f"  pytest exit={restored.returncode}\n"
                f"--- pytest output after restore ---\n{restored.failure_text()}\n"
                f"--- earlier failure with mutation (for comparison) ---\n"
                f"{broken.failure_text()}"
            )

    return RevertReport(
        target=path,
        nodeid=nodeid,
        mutated_failure=broken,
        restored_pass=restored,
    )
