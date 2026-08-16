"""Changed-file -> impacted-test analysis for the `test-dev` / `test-pr` lanes.

Why this exists (see docs/testing/TESTING_SPEED_PLAN.md Phase 4 and TESTING.md)
---------------------------------------------------------------------------
Warm full-tree collection alone is ~3.2-4.3s reported / ~4.0-4.3s wall (11,530 tests). A
single directory such as tests/scope (301 tests) collects in ~0.13-0.36s reported / ~0.5-0.65s
wall. `wall = COLLECT + max(cumulative/eff_par, longest_single_test)`, so a lane that wants a
sub-5s dev loop cannot pay the full-tree collection floor even once -- it must pass explicit
PATHS/node-ids to pytest, never a bare `-m` (which still collects everything and then
deselects: measured, `-m acceptance_smoke` alone spends ~2.5s of its own run just collecting
tests/acceptance before deselecting 619 of 637 items).

What this is
------------
A **reverse-dependency analysis**, not a directory-name heuristic. The first version of this
module mapped `omniagentos/<pkg>/**` to `tests/<pkg>/` and nothing else; review correctly
rejected that as unable to see cross-package regressions (`omniagentos/db/store.py` mapped
only to `tests/db`, while 131 test files outside `tests/db` import `SqliteStore`). A
directory mirror is a naming convention, not an impact analysis.

Three independent edge sources are unioned, so a test is selected if ANY of them can explain
why the change might reach it:

1. **Transitive import closure.** Every `.py` file under `omniagentos/`, `scripts/`, `tests/`
   and `tools/` is parsed with `ast` and its imports resolved to repo files (relative imports
   resolved against the file's own package; `from pkg import name` also considered as
   `pkg.name` in case `name` is a submodule; every package `__init__.py` on the path counts,
   because importing `a.b.c` executes those too). Edges are inverted and walked
   breadth-first, so a change to `omniagentos/db/store.py` reaches every test that imports it
   *transitively*, through any number of intermediate modules.
2. **Dynamic/textual module references.** Quoted dotted names (`importlib.import_module(
   "omniagentos.foo.bar")`, subprocess `-m` targets, patch targets) feed the same closure.
   This is the blind spot `docs/testing/TESTING_SPEED_PLAN.md` Phase 5 named when it rejected
   a pure AST graph on this repo.
3. **Data-file references.** Many tests read non-Python files (`Makefile`, `pyproject.toml`,
   `TESTING.md`, fixture JSON). Every file's mentioned path-like tokens are indexed, so a
   changed `Makefile` selects the tests that actually read it. Basename-only matches are
   accepted only for repo-unique basenames, so a change to some `store.py` does not select
   every test that merely mentions the string "store.py".

The directory mirror is *kept* as a fourth, additive source (it catches CLI/subprocess tests
that exercise a package without importing it), but it is no longer the whole analysis.

Honesty rules this module holds to
----------------------------------
* A changed file that no edge source can explain is **unresolved**, and unresolved input sets
  `full_required` -- the lane must say so loudly and defer to `test-full`. Silence is the one
  answer an impact analysis is never allowed to give.
* Documentation/asset files (`.md`, `.txt`, images) that no test references are recorded as
  `no_test_edge`, not as coverage. They do not force a full run, because a file no test reads
  cannot change a test outcome; that exemption is an explicit allowlist, not a default.
* Fractions over an empty denominator are reported as `None`, never as 1.0 or 0.0.
* `tests/doctrine` is partitioned into a **serial** bucket. Its revert/counterfeit harness
  mutates shared fixture files in place and is not xdist-safe (`pytest -n 8 tests/doctrine`
  produced 7 cross-worker mutation races and left `tests/doctrine/_fixtures/subject.py`
  dirty). Selecting it and then handing it to `-n 8` is worse than not selecting it at all.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO / "tests"

# Roots that are importable as packages from the repo root (`omniagentos`, `scripts` and
# `tests` all have __init__.py; `tools` is scanned so its scripts participate in the graph
# even though it is not a package).
SOURCE_ROOTS: tuple[str, ...] = ("omniagentos", "scripts", "tests", "tools")

# Test directories that MUST NOT be handed to pytest-xdist. tests/doctrine's revert harness
# mutates real files in place (see module docstring / TESTING.md "revert-test harness is not
# crash-safe"); running it under `-n` races workers against one shared fixture.
SERIAL_TEST_PREFIXES: tuple[str, ...] = ("tests/doctrine",)

# Quarantined suites the fast lanes deliberately do not run, matching `make test-fast`'s
# --ignore list. They are reported explicitly as `deferred` (never silently dropped) and are
# covered by `make test-full` / `make test-nightly`.
DEFERRED_TEST_PREFIXES: tuple[str, ...] = (
    "tests/simharness",
    "tests/counterfeits",
    "tests/longhaul",
)

# Suffixes that cannot change program behaviour on their own. A file with one of these
# suffixes that no test references is `no_test_edge` rather than `unresolved`.
DOC_SUFFIXES: frozenset[str] = frozenset(
    {".md", ".txt", ".rst", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf"}
)

# Changing any of these changes the whole run's configuration; no per-file edge can bound it.
GLOBAL_FILES: frozenset[str] = frozenset(
    {"pyproject.toml", "uv.lock", "tests/conftest.py", "conftest.py", "Makefile"}
)

# If the closure selects at least this fraction of the suite, running it as an explicit
# node-id list is not cheaper than running the tree -- escalate instead of pretending.
FULL_ESCALATION_FRACTION = 0.60

_INDEX_PATH = REPO / "var" / "testlanes" / "impact-index.json"

# Quoted dotted names that start at one of our roots: importlib.import_module("omniagentos.x"),
# monkeypatch/mock patch targets, `python -m scripts.foo` arguments.
_DOTTED_RE = re.compile(r"""['"]((?:omniagentos|scripts|tools|tests)(?:\.[A-Za-z_]\w*)+)['"]""")
# Same, but for the inside of an already-extracted string literal (no surrounding quotes).
_DOTTED_IN_LITERAL_RE = re.compile(r"\b((?:omniagentos|scripts|tools|tests)(?:\.[A-Za-z_]\w*)+)")
# Path-like tokens: anything that looks like a repo file reference plus the bare `Makefile`.
_PATHTOKEN_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|md|toml|cfg|ini|json|ya?ml|txt|sh|sql|lock|env)\b"
    r"|\bMakefile\b"
)


# --------------------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------------------
def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)
    return proc.stdout


def _merge_base(ref: str) -> str | None:
    proc = subprocess.run(
        ["git", "merge-base", "HEAD", ref], cwd=REPO, capture_output=True, text=True, check=False
    )
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def resolve_base(explicit: str | None = None) -> str:
    """Pick the ref to diff against. Prefer a merge-base with main so the impacted set
    covers everything this branch has changed, not just uncommitted edits."""
    if explicit:
        return explicit
    for ref in ("main", "origin/main"):
        mb = _merge_base(ref)
        if mb:
            return mb
    # No main reachable (e.g. shallow clone / detached history) -- last resort.
    return "HEAD~1"


def changed_files(base: str) -> list[str]:
    """Union of: committed since `base`, uncommitted (staged + unstaged), and untracked new
    files. A dev loop must see edits before they're committed, or it isn't a dev loop."""
    out: set[str] = set()
    for line in _git("diff", "--name-only", f"{base}...HEAD").splitlines():
        if line.strip():
            out.add(line.strip())
    for line in _git("diff", "--name-only", "HEAD").splitlines():
        if line.strip():
            out.add(line.strip())
    for line in _git("status", "--porcelain", "--untracked-files=all").splitlines():
        if line[:2] == "??":
            out.add(line[3:].strip())
    return sorted(out)


# --------------------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------------------
def _iter_py_files() -> list[str]:
    files: list[str] = []
    for root in SOURCE_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts or ".venv" in p.parts:
                continue
            files.append(p.relative_to(REPO).as_posix())
    return sorted(files)


def module_name(rel: str) -> str:
    """Dotted module name for a repo-relative .py path. `a/b/__init__.py` -> `a.b`."""
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_of(rel: str) -> str:
    """Dotted package that *contains* the file (what a relative import resolves against)."""
    mod = module_name(rel)
    if Path(rel).name == "__init__.py":
        return mod
    return mod.rpartition(".")[0]


def _resolve_relative(pkg: str, level: int, module: str | None) -> str:
    """PEP 328 relative import -> absolute dotted name."""
    parts = pkg.split(".") if pkg else []
    # level 1 == current package, level 2 == parent, ...
    drop = level - 1
    base_parts = parts[: len(parts) - drop] if drop <= len(parts) else []
    base = ".".join(base_parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


def _string_literals(tree: ast.AST) -> list[str]:
    """Every str constant in the module EXCEPT module/class/function docstrings.

    Comments never reach the AST, and docstrings are excluded deliberately: prose that
    happens to name a file is not a dependency on it. Without this, one sentence in
    `omniagentos/api/eventbus.py`'s module docstring ("see the Makefile") made a `Makefile`
    edit select hundreds of test files through eventbus's importers -- measured, not
    hypothetical.
    """
    doc_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                doc_nodes.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in doc_nodes
    ]


def _extract(rel: str, text: str) -> tuple[list[str], list[str]]:
    """(imported dotted names, path-like tokens) for one file."""
    names: set[str] = set()
    pkg = _package_of(rel)
    try:
        tree: ast.Module | None = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is None:
        # Unparseable: fall back to scanning the raw text. Over-selection is the safe
        # direction; silently returning "no edges" is not.
        names.update(_DOTTED_RE.findall(text))
        return sorted(names), sorted(set(_PATHTOKEN_RE.findall(text)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = (
                _resolve_relative(pkg, node.level, node.module)
                if node.level
                else (node.module or "")
            )
            if not base:
                continue
            names.add(base)
            for alias in node.names:
                if alias.name != "*":
                    names.add(f"{base}.{alias.name}")

    tokens: set[str] = set()
    for literal in _string_literals(tree):
        names.update(_DOTTED_IN_LITERAL_RE.findall(literal))
        tokens.update(_PATHTOKEN_RE.findall(literal))
    return sorted(names), sorted(tokens)


def index_version() -> str:
    """Cache key for the EXTRACTION LOGIC itself, derived from its own source.

    A hand-maintained version integer is a footgun: this module's extractor was changed
    (docstrings excluded, package-prefix edges added) without bumping the literal, so stale
    caches kept answering with the old edge set and two measurements of the same repo
    disagreed by 88 test files. Hashing the source means the cache cannot outlive a change
    to the code that produced it.
    """
    parts = [
        inspect.getsource(_extract),
        inspect.getsource(_string_literals),
        inspect.getsource(_resolve_relative),
        inspect.getsource(module_name),
        _DOTTED_RE.pattern,
        _DOTTED_IN_LITERAL_RE.pattern,
        _PATHTOKEN_RE.pattern,
        ",".join(SOURCE_ROOTS),
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:16]


def _load_cache() -> dict:
    try:
        raw = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if raw.get("version") != index_version():
        return {}
    entries = raw.get("entries")
    return entries if isinstance(entries, dict) else {}


def _save_cache(entries: dict) -> None:
    try:
        _INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _INDEX_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"version": index_version(), "entries": entries}), encoding="utf-8"
        )
        os.replace(tmp, _INDEX_PATH)
    except OSError:
        # A cache we cannot write is a performance problem, never a correctness one.
        pass


def build_index(use_cache: bool = True) -> dict:
    """Parse every source file once (mtime+size cached) and return the raw index.

    The cache key is (size, mtime_ns): git checkouts and editors both bump mtime_ns, so a
    stale hit would require a same-size edit inside one nanosecond. `--no-cache` exists for
    anyone who does not want to rely on that.
    """
    files = _iter_py_files()
    cache = _load_cache() if use_cache else {}
    entries: dict[str, dict] = {}
    parsed = 0
    for rel in files:
        path = REPO / rel
        try:
            st = path.stat()
        except OSError:
            continue
        hit = cache.get(rel)
        if hit and hit.get("size") == st.st_size and hit.get("mtime_ns") == st.st_mtime_ns:
            entries[rel] = hit
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names, tokens = _extract(rel, text)
        entries[rel] = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "imports": names,
            "tokens": tokens,
        }
        parsed += 1
    if use_cache and parsed:
        _save_cache(entries)
    return {"entries": entries, "parsed": parsed, "cached": len(entries) - parsed}


class ImpactGraph:
    """Reverse-dependency graph over repo files."""

    def __init__(self, index: dict) -> None:
        self.entries: dict[str, dict] = index["entries"]
        self.parsed = index.get("parsed", 0)
        self.cached = index.get("cached", 0)
        self.module_to_file: dict[str, str] = {}
        for rel in self.entries:
            self.module_to_file.setdefault(module_name(rel), rel)
        # file -> files that depend on it
        self.rdeps: dict[str, set[str]] = {}
        for rel, entry in self.entries.items():
            for name in entry["imports"]:
                for target in self._resolve_module(name):
                    if target != rel:
                        self.rdeps.setdefault(target, set()).add(rel)
        # basename -> how many tracked files share it (uniqueness gate for token matching)
        self._basename_counts: dict[str, int] = {}
        # token index: full token AND basename -> files mentioning it
        self._token_index: dict[str, set[str]] = {}
        for rel, entry in self.entries.items():
            for tok in entry["tokens"]:
                self._token_index.setdefault(tok, set()).add(rel)
                self._token_index.setdefault(Path(tok).name, set()).add(rel)

    def _resolve_module(self, name: str) -> set[str]:
        """Every dotted prefix of `name` that is a real file in the index.

        `import a.b.c` executes `a/__init__.py` and `a/b/__init__.py` too, so a change to a
        package's `__init__.py` really does reach every importer of its submodules. Resolving
        only the longest prefix left `scripts/testlanes/__init__.py` with zero dependents even
        though `run_lane.py` imports `scripts.testlanes.impacted`.
        """
        out: set[str] = set()
        probe = name
        while probe:
            hit = self.module_to_file.get(probe)
            if hit:
                out.add(hit)
            probe = probe.rpartition(".")[0]
        return out

    def basename_is_unique(self, rel: str) -> bool:
        """True if no other *tracked* file in the repo shares this basename.

        Counted over `git ls-files` rather than a filesystem walk: var/ holds hundreds of
        thousands of generated artifacts and walking it would cost more than the whole lane.
        """
        if not self._basename_counts:
            counts: dict[str, int] = {}
            for line in _git("ls-files").splitlines():
                line = line.strip()
                if line:
                    name = line.rpartition("/")[2]
                    counts[name] = counts.get(name, 0) + 1
            self._basename_counts = counts
        return self._basename_counts.get(Path(rel).name, 0) <= 1

    def referencing_files(self, rel: str) -> set[str]:
        """Files that textually mention `rel` (exact repo-relative path, or a repo-unique
        basename). Over-selection here is safe; under-selection is not, so the only thing
        held back is the ambiguous common-basename case."""
        out: set[str] = set(self._token_index.get(rel, set()))
        name = Path(rel).name
        if self.basename_is_unique(rel):
            out |= self._token_index.get(name, set())
        return out

    def dependents_of(self, seeds: set[str]) -> set[str]:
        """Breadth-first closure over rdeps. Seeds included."""
        seen = set(seeds)
        queue = deque(seeds)
        while queue:
            cur = queue.popleft()
            for dependent in self.rdeps.get(cur, ()):
                if dependent not in seen:
                    seen.add(dependent)
                    queue.append(dependent)
        return seen


# --------------------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------------------
def _is_test_file(rel: str) -> bool:
    return rel.startswith("tests/") and Path(rel).name.startswith("test_") and rel.endswith(".py")


_ALL_TESTS_CACHE: list[str] | None = None


def _all_test_files() -> list[str]:
    global _ALL_TESTS_CACHE
    if _ALL_TESTS_CACHE is None:
        _ALL_TESTS_CACHE = sorted(
            p.relative_to(REPO).as_posix()
            for p in TESTS_DIR.rglob("test_*.py")
            if "__pycache__" not in p.parts
        )
    return _ALL_TESTS_CACHE


def partition(node_ids: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split selected tests into (parallel-safe, serial-only, deferred-to-full).

    This is the fix for the blocker that this module used to select `tests/doctrine` and
    then let the lane runner hand it to `pytest -n 8`.
    """
    parallel: list[str] = []
    serial: list[str] = []
    deferred: list[str] = []
    for node in sorted(set(node_ids)):
        if node.startswith(DEFERRED_TEST_PREFIXES):
            deferred.append(node)
        elif node.startswith(SERIAL_TEST_PREFIXES):
            serial.append(node)
        else:
            parallel.append(node)
    return parallel, serial, deferred


def _seed_files(changed: list[str], graph: ImpactGraph) -> tuple[dict[str, set[str]], list[str]]:
    """changed file -> the test files it can reach; plus the list we could not explain."""
    per_file: dict[str, set[str]] = {}
    unresolved: list[str] = []

    for rel in changed:
        hits: set[str] = set()
        path = REPO / rel

        # 1/2. import + dynamic-reference closure (only for files that are in the graph)
        if rel in graph.entries:
            hits |= {f for f in graph.dependents_of({rel}) if _is_test_file(f)}

        # a changed test file always selects itself
        if _is_test_file(rel) and path.is_file():
            hits.add(rel)

        # a changed conftest selects everything below it
        if Path(rel).name == "conftest.py" and rel.startswith("tests/"):
            parent = str(Path(rel).parent)
            hits |= {t for t in _all_test_files() if t.startswith(parent + "/")}

        # 3. data-file references (Makefile, pyproject.toml, fixtures, docs a test reads)
        referencing = graph.referencing_files(rel)
        if referencing:
            hits |= {f for f in graph.dependents_of(referencing) if _is_test_file(f)}

        # 4. directory mirror, kept as an additive source: it catches CLI/subprocess tests
        #    that exercise a package without importing it.
        parts = Path(rel).parts
        if len(parts) > 1 and parts[0] in ("omniagentos", "tests"):
            mirror = TESTS_DIR / parts[1]
            if mirror.is_dir():
                hits |= {t for t in _all_test_files() if t.startswith(f"tests/{parts[1]}/")}

        if hits:
            per_file[rel] = hits
        else:
            per_file[rel] = set()
            if rel in GLOBAL_FILES or Path(rel).name in GLOBAL_FILES:
                unresolved.append(rel)
            elif Path(rel).suffix in DOC_SUFFIXES:
                pass  # recorded separately as no_test_edge
            else:
                unresolved.append(rel)
    return per_file, unresolved


def impacted(
    base: str | None = None,
    use_cache: bool = True,
    changed_override: list[str] | None = None,
) -> dict:
    """Analyse the working tree, or (with `changed_override`) answer the what-if question
    "which tests would this lane run if exactly these files had changed?".

    The override exists so lane benchmarks are reproducible: a lane's cost is a function of
    the change set, so a single "p50" with no stated change set is not a measurement of
    anything. See TESTING.md's per-scenario table.
    """
    if changed_override is not None:
        resolved_base = "(--changed override)"
        changed = sorted(set(changed_override))
    else:
        resolved_base = resolve_base(base)
        changed = changed_files(resolved_base)
    graph = ImpactGraph(build_index(use_cache=use_cache))

    per_file, unresolved = _seed_files(changed, graph)
    selected: set[str] = set()
    for hits in per_file.values():
        selected |= hits

    no_test_edge = sorted(
        rel for rel, hits in per_file.items() if not hits and rel not in unresolved
    )

    parallel, serial, deferred = partition(sorted(selected))

    total_tests = len(_all_test_files())
    # Rate over an empty denominator is undefined, never 1.0/0.0 (standing operator doctrine).
    fraction = round(len(selected) / total_tests, 4) if total_tests else None

    # Two DIFFERENT reasons a lane may not be able to do its normal job, which lanes are
    # allowed to treat differently:
    #   unresolved_input  -- the analysis has a hole; the subset provably does not cover the
    #                        diff. A pre-commit loop may continue (loudly); a pre-review lane
    #                        must not.
    #   closure_too_broad -- the analysis is fine and says most of the suite is impacted.
    #                        Handing pytest 700 explicit paths is then strictly worse than
    #                        letting it collect the tree, so EVERY lane escalates.
    unresolved_reason: str | None = None
    if unresolved:
        unresolved_reason = (
            f"{len(unresolved)} changed file(s) have no explainable test edge: "
            + ", ".join(unresolved[:8])
            + ("..." if len(unresolved) > 8 else "")
        )
    broad_reason: str | None = None
    if fraction is not None and fraction >= FULL_ESCALATION_FRACTION:
        broad_reason = (
            f"impact closure selects {len(selected)}/{total_tests} test files "
            f"({fraction:.0%}) -- past {FULL_ESCALATION_FRACTION:.0%} an explicit node-id "
            "list is not cheaper than running the tree"
        )
    reasons = [r for r in (unresolved_reason, broad_reason) if r]

    return {
        "base": resolved_base,
        "changed": changed,
        "impacted": parallel,
        "serial": serial,
        "deferred": deferred,
        "unresolved": unresolved,
        "no_test_edge": no_test_edge,
        "full_required": bool(reasons),
        "full_reasons": reasons,
        "unresolved_input": unresolved_reason is not None,
        "closure_too_broad": broad_reason is not None,
        "selected_test_files": len(selected),
        "total_test_files": total_tests,
        "selected_fraction": fraction,
        "index": {"parsed": graph.parsed, "cached": graph.cached},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--base", default=None, help="ref/sha to diff against (default: merge-base with main)"
    )
    ap.add_argument("--format", choices=["paths", "json"], default="paths")
    ap.add_argument(
        "--no-cache", action="store_true", help="re-parse every file instead of using var/ cache"
    )
    ap.add_argument(
        "--changed",
        action="append",
        default=None,
        help="what-if: analyse this exact changed-file set instead of the working tree "
        "(repeatable; used by the reproducible lane benchmarks in TESTING.md)",
    )
    args = ap.parse_args(argv)

    result = impacted(args.base, use_cache=not args.no_cache, changed_override=args.changed)

    if args.format == "json":
        print(json.dumps(result, indent=2))
        return 0

    for node_id in result["impacted"]:
        print(node_id)
    for node_id in result["serial"]:
        print(f"{node_id}  # SERIAL ONLY")
    if result["deferred"]:
        print(
            f"# {len(result['deferred'])} selected test file(s) are in quarantined suites the "
            "fast lanes do not run (tests/simharness, tests/counterfeits, tests/longhaul) -- "
            "covered by make test-full / make test-nightly:",
            file=sys.stderr,
        )
        for node_id in result["deferred"]:
            print(f"#   {node_id}", file=sys.stderr)
    if result["no_test_edge"]:
        print(
            f"# {len(result['no_test_edge'])} changed doc/asset file(s) are read by no test "
            "(no impact edge, not a gap): " + ", ".join(result["no_test_edge"]),
            file=sys.stderr,
        )
    if result["full_required"]:
        print("# IMPACT ANALYSIS INCOMPLETE -- run make test-full before merging:", file=sys.stderr)
        for reason in result["full_reasons"]:
            print(f"#   {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
