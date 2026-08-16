"""Build the {source file -> tests that executed it} map from coverage contexts.

Input is a coverage.py run with per-test dynamic contexts. `pyproject.toml` carries
``dynamic_context = "test_function"`` under ``[tool.coverage.run]``; without it every
line is recorded under the single empty context and the map is unbuildable, so
:func:`coverage_run_config_text` refuses to generate a run config from a pyproject that
lost the setting rather than emitting a config that silently produces an empty map.

Context shapes handled
----------------------
* ``tests.gates.test_engine.test_denies`` — coverage's own ``test_function`` contexts:
  the test function's module ``__name__`` plus its qualname (class included). Resolved
  back to a file by taking the longest dotted prefix that exists on disk as a ``.py``.
* ``tests/gates/test_engine.py::test_denies`` — pytest-cov ``--cov-context=test`` node
  ids, optionally suffixed ``|setup`` / ``|run`` / ``|teardown``.
* ``""`` — coverage's default context, i.e. lines executed at import time, before any
  test started. **This is not evidence that no test needs the file**; it is the absence
  of evidence. Files whose only context is the empty one are dropped from the map, which
  makes them *absent*, which makes the selector force FULL. See
  :class:`CoverageMap.excluded_files`.

Selection granularity is the test **file**, not the node id. A file is a strict superset
of its node ids, it survives test renames between the map build and the selection, and
the failure records a shadow audit compares against are per-node-id but trivially
foldable to files. Finer granularity would be less safe for no measured gain.

Safety rule for unresolvable contexts: if any non-empty context recorded against a source
file cannot be resolved to a test file, that source file is excluded from the map (again:
absent -> FULL). Dropping the unresolvable context instead would silently shrink that
file's test set, which is the one direction of error that loses a real failure.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

#: Where a built map lives. `var/` is gitignored: a map is a machine-local artifact that
#: must be rebuilt when the suite changes, never a committed source of truth.
DEFAULT_MAP_RELPATH = "var/test-selection/coverage_map.json"

#: Directories a resolved context must live under to count as a test.
TEST_ROOTS: tuple[str, ...] = ("tests",)


class CoverageMapError(RuntimeError):
    """The coverage input cannot produce a trustworthy map."""


@dataclass(frozen=True)
class CoverageMap:
    """A built map plus the honesty metadata a consumer needs to distrust it."""

    source_to_tests: Mapping[str, frozenset[str]]
    #: Measured source files deliberately left out of the map, with the reason. Every one
    #: of these forces a FULL run when it changes.
    excluded_files: Mapping[str, str] = field(default_factory=dict)
    #: Contexts that could not be resolved to a test file, for diagnosis.
    unresolved_contexts: tuple[str, ...] = ()
    #: Provenance. `commit` is the tree the map was measured on; a map built on a
    #: different tree than the one being selected for is stale, not wrong-by-definition,
    #: and the shadow harness reports the distance.
    commit: str | None = None
    generated_at: str | None = None
    test_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.source_to_tests

    @property
    def tests(self) -> frozenset[str]:
        out: set[str] = set()
        for group in self.source_to_tests.values():
            out |= group
        return frozenset(out)

    def tests_for(self, source_path: str) -> frozenset[str] | None:
        """Tests known to execute ``source_path``; ``None`` when the file is unknown.

        ``None`` and ``frozenset()`` must never be conflated by a caller: unknown means
        "run everything", and this map never stores an empty set for a known file.
        """
        return self.source_to_tests.get(source_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "commit": self.commit,
            "generated_at": self.generated_at,
            "test_count": self.test_count,
            "source_to_tests": {k: sorted(v) for k, v in sorted(self.source_to_tests.items())},
            "excluded_files": dict(sorted(self.excluded_files.items())),
            "unresolved_contexts": list(self.unresolved_contexts),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CoverageMap:
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise CoverageMapError(
                f"coverage map schema {version!r} != supported {SCHEMA_VERSION!r}; rebuild it"
            )
        raw = payload.get("source_to_tests") or {}
        if not isinstance(raw, Mapping):
            raise CoverageMapError("source_to_tests must be a mapping")
        mapping = {str(k): frozenset(str(t) for t in v) for k, v in raw.items()}
        empties = sorted(k for k, v in mapping.items() if not v)
        if empties:
            raise CoverageMapError(
                "coverage map contains files with an empty test set "
                f"(would read as 'run nothing'): {empties[:5]}"
            )
        return cls(
            source_to_tests=mapping,
            excluded_files=dict(payload.get("excluded_files") or {}),
            unresolved_contexts=tuple(payload.get("unresolved_contexts") or ()),
            commit=payload.get("commit"),
            generated_at=payload.get("generated_at"),
            test_count=int(payload.get("test_count") or 0),
        )

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=1, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> CoverageMap:
        target = Path(path)
        if not target.is_file():
            raise CoverageMapError(f"no coverage map at {target}")
        return cls.from_dict(json.loads(target.read_text(encoding="utf-8")))


def _norm(path: str) -> str:
    out = path.replace("\\", "/")
    while out.startswith("./"):
        out = out[2:]
    return out


def resolve_context(
    context: str,
    repo_root: str | Path,
    test_roots: Iterable[str] = TEST_ROOTS,
) -> str | None:
    """Resolve one coverage context to a repo-relative test file, or ``None``.

    ``None`` means "this context is not attributable to a test file" and callers must
    treat it as loss of information, never as "no test".
    """
    return _resolve_context_cached(
        (context or "").strip(), str(Path(repo_root)), tuple(test_roots)
    )


@lru_cache(maxsize=1 << 16)
def _resolve_context_cached(
    raw: str, repo_root: str, test_roots: tuple[str, ...]
) -> str | None:
    # One context string appears against thousands of source files in a real run; the
    # stat() walk below is the map build's hot loop.
    if not raw:
        return None
    root = Path(repo_root)
    roots = tuple(test_roots)

    def _under_test_root(rel: str) -> str | None:
        for test_root in roots:
            if rel == test_root or rel.startswith(f"{test_root}/"):
                return rel
        return None

    # pytest-cov node-id form: "tests/x/test_y.py::TestC::test_z|run"
    head = raw.split("|", 1)[0]
    if "::" in head or head.endswith(".py"):
        candidate = _norm(head.split("::", 1)[0])
        if candidate.endswith(".py") and (root / candidate).is_file():
            return _under_test_root(candidate)
        return None

    # coverage `test_function` form: dotted module path + qualname. The module part is
    # the test module's `__name__`, which is relative to whichever directory pytest put
    # on sys.path — for `tests/sessions/api/test_x.py` (no `tests/sessions/__init__.py`)
    # that is `api.test_x`, not `tests.sessions.api.test_x`. So: try the repo-relative
    # path first, then fall back to a UNIQUE path suffix among known test files. An
    # ambiguous suffix resolves to nothing, because guessing between two files here
    # would attribute coverage to a test that never ran it.
    parts = raw.split(".")
    index = _test_file_suffix_index(repo_root, test_roots)
    for cut in range(len(parts) - 1, 0, -1):
        candidate = "/".join(parts[:cut]) + ".py"
        if (root / candidate).is_file():
            return _under_test_root(candidate)
        matches = index.get(candidate)
        if matches and len(matches) == 1:
            return next(iter(matches))
    return None


@lru_cache(maxsize=32)
def _test_file_suffix_index(
    repo_root: str, test_roots: tuple[str, ...]
) -> Mapping[str, frozenset[str]]:
    """``path suffix -> test files ending with it``, for every test file in the tree."""
    root = Path(repo_root)
    index: dict[str, set[str]] = {}
    for test_root in test_roots:
        base = root / test_root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            parts = rel.split("/")
            for start in range(1, len(parts)):
                index.setdefault("/".join(parts[start:]), set()).add(rel)
    return {key: frozenset(value) for key, value in index.items()}


def build_map_from_context_pairs(
    pairs: Iterable[tuple[str, Iterable[str]]],
    repo_root: str | Path,
    *,
    commit: str | None = None,
    generated_at: str | None = None,
) -> CoverageMap:
    """Core builder: ``(source file, contexts)`` pairs -> :class:`CoverageMap`.

    Both readers (JSON report and coverage's SQLite data file) funnel through here so
    there is exactly one implementation of the safety rules.
    """
    root = Path(repo_root).resolve()
    mapping: dict[str, set[str]] = {}
    excluded: dict[str, str] = {}
    unresolved: set[str] = set()

    for raw_source, contexts in pairs:
        source = _relative_to_root(raw_source, root)
        if source is None:
            # Measured code outside the repo (site-packages, stdlib). Not selectable and
            # not something a commit can change; ignoring it is not a safety loss.
            continue
        tests: set[str] = set()
        bad: list[str] = []
        for context in contexts:
            if not (context or "").strip():
                continue  # import-time execution: absence of evidence, not evidence.
            resolved = resolve_context(context, root)
            if resolved is None:
                bad.append(context)
                unresolved.add(context)
                continue
            tests.add(resolved)
        if bad:
            excluded[source] = f"unresolved contexts ({len(bad)}), e.g. {bad[0]!r}"
            mapping.pop(source, None)
            continue
        if source in excluded:
            continue
        if not tests:
            excluded[source] = "no test context (import-time execution only)"
            continue
        mapping.setdefault(source, set()).update(tests)

    all_tests: set[str] = set()
    for group in mapping.values():
        all_tests |= group
    return CoverageMap(
        source_to_tests={k: frozenset(v) for k, v in mapping.items()},
        excluded_files=excluded,
        unresolved_contexts=tuple(sorted(unresolved)),
        commit=commit,
        generated_at=generated_at,
        test_count=len(all_tests),
    )


def _relative_to_root(raw: str, root: Path) -> str | None:
    candidate = Path(_norm(raw))
    if not candidate.is_absolute():
        return _norm(str(candidate))
    try:
        return _norm(str(candidate.resolve().relative_to(root)))
    except ValueError:
        return None


def build_map_from_coverage_json(
    payload: Mapping[str, Any] | str | Path,
    repo_root: str | Path,
    *,
    commit: str | None = None,
) -> CoverageMap:
    """Build from ``coverage json --show-contexts`` output (report format 3)."""
    if isinstance(payload, str | Path):
        data = json.loads(Path(payload).read_text(encoding="utf-8"))
    else:
        data = dict(payload)
    meta = data.get("meta") or {}
    if not meta.get("show_contexts"):
        raise CoverageMapError(
            "coverage JSON was written without --show-contexts; it cannot attribute a "
            "line to a test and would build an empty map"
        )
    files = data.get("files") or {}
    if not isinstance(files, Mapping):
        raise CoverageMapError("coverage JSON has no 'files' mapping")

    def _pairs() -> Iterable[tuple[str, Iterable[str]]]:
        for source, entry in files.items():
            contexts: set[str] = set()
            per_line = (entry or {}).get("contexts") or {}
            for names in per_line.values():
                contexts.update(names or [])
            yield source, contexts

    return build_map_from_context_pairs(
        _pairs(), repo_root, commit=commit, generated_at=meta.get("timestamp")
    )


def build_map_from_coverage_data(
    data_file: str | Path,
    repo_root: str | Path,
    *,
    commit: str | None = None,
) -> CoverageMap:
    """Build straight from coverage's SQLite data file (needs coverage installed).

    Preferred for the real suite: ``coverage json --show-contexts`` over ~500 modules
    writes hundreds of MB of per-line context lists, all of which this collapses to a
    per-file set anyway.
    """
    try:
        from coverage import CoverageData  # noqa: PLC0415  (optional build-time dep)
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by the build path
        raise CoverageMapError(
            "coverage.py is not installed in this environment; it is not a declared "
            "dependency of this repo. Build the map with `uv run --with coverage ...`."
        ) from exc

    data = CoverageData(basename=str(data_file))
    data.read()
    measured = list(data.measured_files())
    if not measured:
        raise CoverageMapError(f"coverage data at {data_file} measured no files")

    def _pairs() -> Iterable[tuple[str, Iterable[str]]]:
        for filename in measured:
            contexts: set[str] = set()
            for names in data.contexts_by_lineno(filename).values():
                contexts.update(names or [])
            yield filename, contexts

    return build_map_from_context_pairs(_pairs(), repo_root, commit=commit)


def coverage_run_config_text(
    pyproject_path: str | Path,
    data_file: str | Path,
    *,
    parallel: bool = True,
) -> str:
    """Render a coverage config for the map build, derived from pyproject.

    Kept derived rather than hardcoded so ``[tool.coverage.run]`` stays the single source
    of truth for source/branch/omit, and so losing ``dynamic_context`` there is a loud
    build failure instead of a quietly empty map.
    """
    payload = tomllib.loads(Path(pyproject_path).read_text(encoding="utf-8"))
    run = ((payload.get("tool") or {}).get("coverage") or {}).get("run") or {}
    context_mode = run.get("dynamic_context")
    if context_mode != "test_function":
        raise CoverageMapError(
            f"{pyproject_path} [tool.coverage.run] dynamic_context is {context_mode!r}, "
            "expected 'test_function' — without per-test contexts the coverage map "
            "cannot attribute a line to a test"
        )
    lines = ["[run]", f"dynamic_context = {context_mode}", f"data_file = {data_file}"]
    if parallel:
        # xdist workers are separate processes; each must write its own shard for
        # `coverage combine`, or only the controller's (empty) data survives.
        lines.append("parallel = True")
    sources = run.get("source") or []
    if sources:
        lines.append("source = " + ",".join(str(s) for s in sources))
    if run.get("branch"):
        lines.append("branch = True")
    omit = run.get("omit") or []
    if omit:
        lines.append("omit =")
        lines.extend(f"    {entry}" for entry in omit)
    return "\n".join(lines) + "\n"
