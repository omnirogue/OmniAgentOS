"""The selector. Its entire job is to be safe first and narrow second.

The failure mode this module is written against is not "selected too much", it is
"selected too little and the suite went green anyway". Every rule below therefore
resolves ambiguity toward FULL:

* an empty / missing / stale-schema coverage map -> FULL;
* a changed path that is absent from the map -> FULL (absence is *no information*, and
  no information is not the same as no tests);
* a changed path that changes how the whole run is configured — conftest, pyproject,
  pytest/tox/setup config, lockfiles and requirement pins, migrations, plugin
  registries, CI config -> FULL, without consulting the map at all;
* an empty change set -> FULL, because "nothing changed" reaching a selector means the
  change detector failed, not that the suite is unnecessary;
* a critical-pattern set that resolves to nothing -> FULL, because the always-run
  guarantee has evidently stopped working.

And on top of any subset: :data:`ALWAYS_RUN_PATTERNS`, the tests whose whole purpose is
to catch the class of defect that a per-file impact analysis cannot see — doctrine,
counterfeits, gates, acceptance, certification, path/secret security. Those are unioned
into every subset selection by :func:`_get_critical_tests`, which reads the filesystem.
If it ever returns an empty set again, :func:`select_tests` degrades to FULL and
``tests/tia/test_selector_safety.py`` goes red. That binding is the point: the previous
two attempts at this module shipped ``_get_critical_tests`` as ``return set()`` and their
tests passed either way.

Selection granularity is the test **file** (see ``coverage_map`` for why).

Residual risks, written down rather than discovered later
---------------------------------------------------------
* **Test-to-test edges are invisible.** Coverage contexts record which test executed a
  *source* line; nothing records that ``tests/a/test_x.py`` imports a helper from
  ``tests/b/test_y.py``. A changed test file therefore selects itself and no dependent.
  Non-test modules under ``tests/`` (harnesses, fixtures) are absent from the map and so
  force FULL, which covers the common shape; direct test-module imports are not covered.
  The static analysis in ``scripts/testlanes/impacted.py`` does see those edges — a
  future union of the two is the fix, not a heuristic here.
* **The map is only as fresh as its build.** It is keyed to the commit it was measured
  on; code added since is absent, which forces FULL. That degrades selectivity, never
  safety.
* **No documentation exemption.** A changed ``.md``/``.txt`` forces FULL here, and that
  alone is most of the measured full-run rate. Tests in this repo *do* read Markdown and
  config files, so an exemption has to be evidence-gated per path (as
  ``scripts/testlanes/impacted.py`` does with an explicit allowlist), not assumed.
"""

from __future__ import annotations

import glob
import os
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.tia.coverage_map import CoverageMap

FULL = "full"
SUBSET = "subset"

#: Tests that run no matter what the diff says. A coverage map can only know that a test
#: executed a line; it cannot know that a test exists to prove a *policy* holds across
#: the tree (a counterfeit stays red, a gate stays closed, a path stays contained). Those
#: tests are cheap relative to being wrong about them.
ALWAYS_RUN_PATTERNS: tuple[str, ...] = (
    "tests/doctrine/**/test_*.py",
    "tests/counterfeits/**/test_*.py",
    "tests/gates/**/test_*.py",
    "tests/gates_scripts/**/test_*.py",
    "tests/acceptance/**/test_*.py",
    "tests/certification/**/test_*.py",
    "tests/**/test_*security*.py",
    "tests/**/test_*secret*.py",
    "tests/**/test_path_containment*.py",
)

#: Files whose change alters how the entire run is configured. No per-file edge can bound
#: their blast radius, so no map lookup is attempted for them.
GLOBAL_CONFIG_FILES: frozenset[str] = frozenset(
    {
        "conftest.py",
        "pyproject.toml",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "setup.py",
        ".coveragerc",
        "mypy.ini",
        "ruff.toml",
        ".ruff.toml",
        "Makefile",
        "sitecustomize.py",
    }
)

#: Dependency pins: a resolver change can alter behaviour anywhere.
LOCKFILE_NAMES: frozenset[str] = frozenset(
    {"uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "package-lock.json", "yarn.lock"}
)

#: Path prefixes that force FULL regardless of basename.
FORCE_FULL_PREFIXES: tuple[str, ...] = (
    "migrations/",
    "migrations-staging/",
    "omniagentos/db/migrations/",
    "omniagentos/db/migration_repairs/",
    ".github/",
    ".circleci/",
    ".gitlab-ci/",
    "ci/",
    "requirements/",
)

#: Files that register or configure pytest plugins for the whole session.
PLUGIN_REGISTRY_NAMES: frozenset[str] = frozenset({"pytest_plugin.py", "plugins.py"})

#: pytest's default `python_files`.
TEST_FILE_PREFIX = "test_"
TEST_FILE_SUFFIX = "_test.py"


class CriticalPatternError(RuntimeError):
    """An always-run pattern matched nothing, i.e. the guarantee it encodes is dead."""


@dataclass(frozen=True)
class Selection:
    """What the selector would run, and why.

    ``tests is None`` iff ``mode == "full"``. A subset is never empty — an empty subset
    and a full run are the two answers this type refuses to confuse, because the bug
    class this whole package exists to avoid is exactly that confusion.
    """

    mode: str
    tests: frozenset[str] | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mode not in (FULL, SUBSET):
            raise ValueError(f"unknown selection mode {self.mode!r}")
        if not self.reasons:
            raise ValueError("a selection must carry at least one reason")
        if self.mode == FULL and self.tests is not None:
            raise ValueError("a FULL selection must not carry a test list")
        if self.mode == SUBSET and not self.tests:
            raise ValueError("a SUBSET selection must be non-empty; use FULL instead")

    @property
    def is_full(self) -> bool:
        return self.mode == FULL

    @classmethod
    def full_run(cls, *reasons: str) -> Selection:
        return cls(mode=FULL, tests=None, reasons=tuple(reasons))

    @classmethod
    def subset_run(cls, tests: Iterable[str], reasons: Sequence[str]) -> Selection:
        return cls(mode=SUBSET, tests=frozenset(tests), reasons=tuple(reasons))

    def pytest_paths(self, test_root: str = "tests") -> tuple[str, ...]:
        """Paths to hand to pytest. FULL yields the suite root, never an empty argv."""
        if self.is_full or self.tests is None:
            return (test_root,)
        return tuple(sorted(self.tests))

    def covers(self, test_file: str) -> bool:
        """Would this selection have run ``test_file``?"""
        return self.is_full or (self.tests is not None and test_file in self.tests)

    def fraction_of(self, universe: Collection[str] | int) -> float | None:
        """Fraction of the suite selected, or ``None`` over an empty universe.

        A rate over an empty set is undefined. It is not 1.0 and it is not 0.0; either
        would let an empty run report a headline number.
        """
        total = universe if isinstance(universe, int) else len(universe)
        if total <= 0:
            return None
        if self.is_full or self.tests is None:
            return 1.0
        return len(self.tests) / total


def is_test_file(path: str) -> bool:
    """True for a path pytest would collect tests from."""
    norm = path.replace("\\", "/")
    if not norm.startswith("tests/"):
        return False
    name = os.path.basename(norm)
    if not name.endswith(".py"):
        return False
    return name.startswith(TEST_FILE_PREFIX) or name.endswith(TEST_FILE_SUFFIX)


def force_full_reason(path: str) -> str | None:
    """Why ``path`` forces a FULL run, or ``None`` if the map may be consulted."""
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    name = os.path.basename(norm)
    if name in GLOBAL_CONFIG_FILES:
        return f"{norm}: repo-wide run configuration"
    if name in LOCKFILE_NAMES or name.endswith(".lock"):
        return f"{norm}: dependency pin"
    if name.startswith("requirements") and name.endswith(".txt"):
        return f"{norm}: dependency pin"
    if name in PLUGIN_REGISTRY_NAMES:
        return f"{norm}: pytest plugin registry"
    for prefix in FORCE_FULL_PREFIXES:
        if norm.startswith(prefix):
            return f"{norm}: {prefix.rstrip('/')} (whole-run blast radius)"
    if "/migrations/" in norm:
        return f"{norm}: schema migration"
    if norm in (".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml"):
        return f"{norm}: CI configuration"
    return None


def critical_pattern_matches(repo_root: str | Path, pattern: str) -> frozenset[str]:
    """Test files matched by one always-run pattern, as repo-relative paths."""
    hits = glob.glob(pattern, root_dir=str(repo_root), recursive=True)
    return frozenset(h.replace("\\", "/") for h in hits if is_test_file(h.replace("\\", "/")))


def validate_critical_patterns(
    repo_root: str | Path,
    patterns: Iterable[str] = ALWAYS_RUN_PATTERNS,
) -> Mapping[str, frozenset[str]]:
    """Return ``pattern -> matches``; raise if any pattern matches nothing.

    A pattern that matches nothing is a typo or a moved directory, and it disables an
    always-run guarantee silently. Making it loud is the only way that stays true as the
    tree moves.
    """
    resolved = {pattern: critical_pattern_matches(repo_root, pattern) for pattern in patterns}
    dead = sorted(pattern for pattern, hits in resolved.items() if not hits)
    if dead:
        raise CriticalPatternError(
            f"always-run patterns match no test file under {repo_root}: {dead}"
        )
    return resolved


def _get_critical_tests(
    repo_root: str | Path,
    patterns: Iterable[str] = ALWAYS_RUN_PATTERNS,
) -> frozenset[str]:
    """The always-run set, read off the filesystem.

    This function is the one the two rejected attempts shipped as ``return set()``. If it
    ever does that again, :func:`select_tests` returns FULL for every input (see the
    guard there) and the safety suite fails on the specific always-run assertions rather
    than passing over dead config.
    """
    out: set[str] = set()
    for pattern in patterns:
        out |= critical_pattern_matches(repo_root, pattern)
    return frozenset(out)


def select_tests(
    changed_files: Iterable[str],
    coverage_map: CoverageMap | None,
    repo_root: str | Path,
    critical_tests: Iterable[str] | None = None,
) -> Selection:
    """Decide what to run for ``changed_files``. Defaults to FULL on any doubt.

    ``critical_tests`` exists for tests and audits that need to pin the always-run set;
    leaving it ``None`` (the production path, and the path the shadow harness uses) makes
    this call :func:`_get_critical_tests` itself.
    """
    root = Path(repo_root)
    critical = frozenset(critical_tests) if critical_tests is not None else _get_critical_tests(root)
    if not critical:
        return Selection.full_run(
            "always-run critical set resolved to nothing — the guarantee is not working"
        )

    if coverage_map is None or coverage_map.is_empty:
        return Selection.full_run("no coverage map available")

    changed = []
    for raw in changed_files:
        norm = (raw or "").replace("\\", "/").strip()
        while norm.startswith("./"):
            norm = norm[2:]
        if norm:
            changed.append(norm)
    if not changed:
        return Selection.full_run(
            "empty change set — a selector reached with no changed files means change "
            "detection failed, not that the suite is unnecessary"
        )

    selected: set[str] = set(critical)
    reasons: list[str] = [f"always-run critical set ({len(critical)} files)"]
    for path in sorted(set(changed)):
        forced = force_full_reason(path)
        if forced is not None:
            return Selection.full_run(forced)
        if is_test_file(path):
            selected.add(path)
            reasons.append(f"{path}: changed test file")
            continue
        mapped = coverage_map.tests_for(path)
        if mapped is None:
            return Selection.full_run(
                f"{path}: absent from the coverage map (no evidence either way)"
            )
        selected |= mapped
        reasons.append(f"{path}: {len(mapped)} covering test files")

    # A selected test file that no longer exists (deleted in this change) cannot fail;
    # keeping it would hand pytest an argument that errors the whole run.
    existing = {test for test in selected if (root / test).is_file()}
    if not existing:
        return Selection.full_run("every selected test file is missing from the tree")
    return Selection.subset_run(existing, reasons)
