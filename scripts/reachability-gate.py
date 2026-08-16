#!/usr/bin/env python3
"""Refuse a branch that adds a public function no production path calls.

WHY THIS IS MECHANICAL AND NOT A REVIEWER'S JOB
-----------------------------------------------
"Built, tested, never wired" is this repo's signature defect. It has now been found
TWELVE times, and the last three cost a full aggregate-review cycle each:

  - sw-notifications  build_approval_lookup / build_board_lookup / serialize_notification
                      added with ~700 lines of tests; the real route still called its own
                      private fail-open duplicates. Zero production callers.
  - sw-audit          run_trace_audit_over_events reachable only through an action nothing
                      emits, with a source comment asserting "this is the real production
                      call path".
  - sw-notifications' boundary test monkeypatched the buggy factory, so it passed unchanged
                      with the entire fix deleted.

Every one was invisible to the per-lane reviewer and obvious to a caller search. That makes
it a MEASUREMENT, not a judgement — and a measurement belongs in a gate, run in seconds,
rather than in a 25-minute review that a model has to remember to perform.

WHAT IT DOES NOT DO
It does not decide whether code is good. It answers one question: does any production path,
including a call elsewhere in the defining module, use this new public symbol? A `no` is a
refusal the author must either fix or explicitly justify.

HOW A NAME IS RESOLVED TO A SYMBOL
----------------------------------
Everything here is read from the AST of the candidate's own sources. `git grep` only
narrows the set of files worth parsing; it never decides anything. A name counts as
referring to the new symbol when the import graph says so, followed the way Python follows
it: relative imports with their level, aliases, package re-export chains
(`from .catalog import stats as catalog_stats`, re-exported by a package `__init__` — 345
public functions in this tree are exported that way, and 43 of them have no other
resolvable caller), and `importlib.import_module("pkg.mod")`, which names its module with a
string literal and so is every bit as static. Anywhere the graph does not reach, the gate
refuses to guess — an attribute chain nothing imported (`self.storage.read()` is not
`storage.py:read`), a module whose name merely shares a prefix (`search_helpers` is not
inside `search`), a mention in a docstring, or anything inside `if TYPE_CHECKING:`.

Holding a reference IS wiring: `HANDLERS = {"ping": handle_ping}` counts, because a
table-driven dispatcher will call it and demanding a literal `handle_ping(...)` call site
would refuse every dispatch table in the repo. Importing a name and never mentioning it
again does not count — that is the re-export defect verbatim.

KNOWN LIMITS, STATED RATHER THAN HIDDEN:
  - Reachability here means "production code holds or calls this", not "this executes". A
    call inside a helper that itself is never called still counts as a caller. Closing that
    needs whole-program reachability from real entry points.
  - `from importlib import import_module as im; im("pkg.mod")` is not recognised — only the
    attribute form is. That is conservative (it can refuse, never accept wrongly), and the
    tree contains no occurrences of it.
  - Conversely, ANY object with an `.import_module("literal")` method resolves like
    importlib, so a look-alike over-accepts the module. It cannot over-accept a symbol: the
    attribute must still be reached on that module and resolve to the target.

WHEN THE CALLER IS THE FRAMEWORK
--------------------------------
Strict call-site matching, alone, would make this gate wrong about an entire class of code.
A FastAPI route handler has no named caller anywhere in the repo — it is invoked by the
framework, reached through `@router.post(...)` plus `app.include_router(router)`. Under the
old word-match those handlers slipped through by accident; under AST matching they can only
be refused, so every new route would need a hand-written exemption line forever. Two entries
in devtasks/REACHABILITY-EXEMPT.txt existed for that reason and no other; they are the
receipt that this was a real cost, not an anticipated one. Strictness and framework
recognition therefore land together, or the gate trades one wrong answer for another.

Registration is verified, not assumed, and mountedness is a CLOSURE computed from the
application outward: a router is mounted because an ASGI application mounts it, or because
an already-mounted router mounts it. "Somebody passed it to include_router" is not enough —
an aggregator that nothing serves is the original defect with an extra hop in it, and the
hop is free to add. What makes an object an application is what it was CONSTRUCTED from,
never what it was named; a router called `app` is still a router. A commented-out mount, a
mount that exists only in a test harness, and a mount inside `if TYPE_CHECKING:` all wire
nothing.

THE ESCAPE HATCH IS DELIBERATE AND VISIBLE
A symbol genuinely intended for operator or external use is declared in
`devtasks/REACHABILITY-EXEMPT.txt`, one `module:symbol` per line with a reason. That turns
"never wired" from an accident into a recorded decision someone can disagree with.

    reachability-gate.py <branch> [base]     # default base: main
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections.abc import Iterator
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXEMPT_FILE = REPO / "devtasks" / "REACHABILITY-EXEMPT.txt"

# Decorator attributes that hand a function to a web framework's dispatcher. Anything not
# on this list (``@lru_cache``, ``@dataclass``, a project decorator) is NOT a registration:
# a decorator is not evidence that something calls the function.
ROUTE_DECORATORS = frozenset(
    {
        "get",
        "post",
        "put",
        "delete",
        "patch",
        "head",
        "options",
        "trace",
        "websocket",
        "api_route",
        "route",
    }
)
# Constructors whose result IS the served application. Mountedness is seeded here and
# nowhere else — and it is the CONSTRUCTOR that decides, never the variable's name.
APP_FACTORIES = frozenset({"FastAPI", "Starlette"})

SEARCH_ROOTS = ["omniagentos", "scripts"]


def _run(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=False)
    return p.returncode, p.stdout


def _changed_py(base: str, branch: str) -> list[str]:
    # NUL-delimited walk: `--name-only` alone is whitespace-tokenised by the
    # caller, so a path containing a space (bare) or a non-ASCII byte
    # (C-quoted under the default core.quotePath=true, e.g. "caf\303\251.py")
    # is silently discarded rather than mis-parsed. `-z` returns each path
    # intact and unquoted, terminated by NUL, so both carriers survive.
    rc, out = _run(["git", "diff", "--name-only", "-z", f"{base}...{branch}"])
    if rc != 0:
        # A probe that cannot run is not a pass. Refuse loudly.
        print(f"REFUSED: cannot diff {base}...{branch}", file=sys.stderr)
        raise SystemExit(2)
    return [
        f
        for f in out.split("\0")
        if f
        and f.endswith(".py")
        and f.startswith("omniagentos/")
        and "/tests/" not in f
        and not Path(f).name.startswith("test_")
    ]


def _added_public_defs(base: str, branch: str, path: str) -> list[tuple[str, int]]:
    """Public top-level functions the branch ADDED to `path`.

    Parses the branch's version of the file and keeps only names absent from the base —
    a rename or a moved body is not a new capability, and flagging it would train people
    to ignore this gate.
    """
    rc_new, new_src = _run(["git", "show", f"{branch}:{path}"])
    if rc_new != 0:
        return []
    rc_old, old_src = _run(["git", "show", f"{base}:{path}"])
    old_names: set[str] = set()
    if rc_old == 0:
        try:
            old_names = {
                n.name
                for n in ast.parse(old_src).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        except SyntaxError:
            return []
    try:
        tree = ast.parse(new_src)
    except SyntaxError:
        return []
    return [
        (n.name, n.lineno)
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not n.name.startswith("_")
        and n.name not in old_names
    ]


def _exemptions() -> set[str]:
    if not EXEMPT_FILE.exists():
        return set()
    out = set()
    for line in EXEMPT_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            # The documented format is "path:symbol  reason", entry and reason
            # separated by a run of 2+ spaces (see devtasks/REACHABILITY-EXEMPT.txt).
            # `line.split()[0]` used to take the first whitespace token, silently
            # truncating a space-bearing path entry (e.g. "omniagentos/my notes.py"
            # -> "omniagentos/my"). Split on the documented 2+-space separator
            # instead, so a single space inside the entry itself is preserved and
            # only an actual reason column is dropped.
            entry = re.split(r"\s{2,}", line, maxsplit=1)[0]
            out.add(entry)
    return out


# ======================================================================================
# AST primitives
# ======================================================================================


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _live_children(node: ast.AST) -> list[ast.AST]:
    """Child nodes that can actually run.

    An ``if TYPE_CHECKING:`` body is dead at runtime, so an import, a call or a mount
    inside one wires nothing. Its ``else`` branch is live.
    """
    if isinstance(node, ast.If) and _is_type_checking(node.test):
        return list(node.orelse)
    return list(ast.iter_child_nodes(node))


def _walk_live(node: ast.AST) -> Iterator[ast.AST]:
    """``ast.walk`` minus the code that never executes."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(_live_children(current))


def _module_scope(tree: ast.Module) -> Iterator[ast.stmt]:
    """Statements that run at import time, descending through if/try/with."""
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        stmt = stack.pop()
        yield stmt
        if isinstance(stmt, ast.If):
            if not _is_type_checking(stmt.test):
                stack.extend(stmt.body)
            stack.extend(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            stack.extend(stmt.body)
            stack.extend(stmt.orelse)
            stack.extend(stmt.finalbody)
            for handler in stmt.handlers:
                stack.extend(handler.body)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            stack.extend(stmt.body)


def _dotted_parts(node: ast.AST) -> list[str] | None:
    """Flatten ``a.b.c`` into ``['a', 'b', 'c']``; None if it is not a plain dotted name."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.insert(0, current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.insert(0, current.id)
        return parts
    return None


def _assigned_names(stmt: ast.stmt) -> tuple[list[str], ast.expr | None]:
    if isinstance(stmt, ast.Assign):
        return [t.id for t in stmt.targets if isinstance(t, ast.Name)], stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id], stmt.value
    return [], None


def _is_test_path(path: str) -> bool:
    return Path(path).name.startswith("test_") or "/tests/" in path


# ======================================================================================
# The import graph
# ======================================================================================


def _module_dotted(path: str) -> str:
    """``omniagentos/api/routes/x.py`` -> ``omniagentos.api.routes.x``."""
    p = Path(path)
    parts = p.parent.parts if p.name == "__init__.py" else p.with_suffix("").parts
    return ".".join(parts)


def _package_dotted(path: str) -> str:
    """The package a file lives in — the anchor for its relative imports."""
    return ".".join(Path(path).parent.parts)


def _absolute_module(path: str, node: ast.ImportFrom) -> str:
    """Resolve ``from . import x`` / ``from ..y import z`` to an absolute dotted module.

    Ignoring ``node.level`` is not a small inaccuracy: it silently resolves every relative
    import to the empty module, so a whole file's imports read as unrelated.
    """
    module = node.module or ""
    level = node.level or 0
    if not level:
        return module
    package = [p for p in _package_dotted(path).split(".") if p]
    if level > 1:
        package = package[: len(package) - (level - 1)]
    parts = package + (module.split(".") if module else [])
    return ".".join(parts)


@cache
def _module_tree(module: str, branch: str) -> tuple[str, ast.Module] | None:
    """(path, parsed source) for a dotted module on `branch`, module or package."""
    stem = module.replace(".", "/")
    for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
        rc, source = _run(["git", "show", f"{branch}:{candidate}"])
        if rc == 0:
            try:
                return candidate, ast.parse(source)
            except SyntaxError:
                return None
    return None


def _defines(tree: ast.Module, name: str) -> bool:
    """Whether the module BINDS `name` itself, rather than importing it from elsewhere.

    ``from x import name`` is deliberately excluded: that is the edge a re-export chain is
    followed along, and treating it as a definition would stop the walk at the first hop.
    """
    for stmt in _module_scope(tree):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if stmt.name == name:
                return True
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return True
        else:
            targets, _ = _assigned_names(stmt)
            if name in targets:
                return True
    return False


@cache
def _export_terminals(module: str, name: str, branch: str) -> frozenset[str]:
    """Where ``module.name`` actually comes from, as ``<module>:<name>`` identities.

    Follows package re-export chains to the module that defines the symbol, so a caller
    importing ``from omniagentos.filesearch import catalog_stats`` resolves to
    ``omniagentos.filesearch.catalog:stats`` and is recognised as calling it. A module the
    gate cannot read (a third-party package) terminates as written.
    """
    terminals: set[str] = set()
    seen: set[tuple[str, str]] = set()
    pending = [(module, name)]
    while pending:
        current_module, current_name = pending.pop()
        if (current_module, current_name) in seen:
            continue
        seen.add((current_module, current_name))
        found = _module_tree(current_module, branch)
        if found is None:
            terminals.add(f"{current_module}:{current_name}")
            continue
        path, tree = found
        if _defines(tree, current_name):
            terminals.add(f"{current_module}:{current_name}")
            continue
        followed = False
        for stmt in _module_scope(tree):
            if not isinstance(stmt, ast.ImportFrom):
                continue
            for alias in stmt.names:
                if (alias.asname or alias.name) != current_name:
                    continue
                origin = _absolute_module(path, stmt)
                if origin:
                    pending.append((origin, alias.name))
                    followed = True
        if not followed:
            terminals.add(f"{current_module}:{current_name}")
    return frozenset(terminals)


def _canonical(identity: str, branch: str) -> str:
    """An identity reduced to the module that defines it, when that is unambiguous."""
    module, _, name = identity.partition(":")
    terminals = _export_terminals(module, name, branch)
    return next(iter(terminals)) if len(terminals) == 1 else identity


def _dynamic_import_target(value: ast.expr | None) -> str | None:
    """The module named by ``importlib.import_module("pkg.mod")``, if that is what this is.

    Twenty-two production sites import this way to break cycles, and the module is named by
    a string literal — so the binding is exactly as static as an import statement, and
    ignoring it refuses functions whose only caller reaches them lazily.
    """
    if not isinstance(value, ast.Call) or len(value.args) != 1:
        return None
    parts = _dotted_parts(value.func)
    if not parts or parts[-1] != "import_module":
        return None
    argument = value.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return None


def _import_bindings(path: str, tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Local name -> (module, imported_name), for every runtime import in the file.

    ``imported_name`` is empty when the binding is a module object (``import pkg.mod``).
    """
    bindings: dict[str, tuple[str, str]] = {}
    for node in _walk_live(tree):
        if isinstance(node, ast.stmt):
            targets, value = _assigned_names(node)
            module = _dynamic_import_target(value)
            if module:
                for target in targets:
                    bindings[target] = (module, "")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = (alias.name, "")
                else:
                    root = alias.name.split(".")[0]
                    bindings[root] = (root, "")
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(path, node)
            for alias in node.names:
                if alias.name == "*":
                    continue
                bindings[alias.asname or alias.name] = (module, alias.name)
    return bindings


def _star_import_modules(path: str, tree: ast.Module) -> list[str]:
    return [
        _absolute_module(path, node)
        for node in _walk_live(tree)
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
    ]


# ======================================================================================
# Does this file use the symbol?
# ======================================================================================


def _names_bound_to(
    path: str,
    tree: ast.Module,
    bindings: dict[str, tuple[str, str]],
    symbol: str,
    target: str,
    branch: str,
) -> set[str]:
    """Local names in this file that refer to `target` (``<module>:<symbol>``)."""
    direct: set[str] = set()
    for local, (module, name) in bindings.items():
        if name and target in _export_terminals(module, name, branch):
            direct.add(local)
    for module in _star_import_modules(path, tree):
        if target in _export_terminals(module, symbol, branch):
            direct.add(symbol)
    return direct


def _module_alias_target(
    prefix: list[str],
    bindings: dict[str, tuple[str, str]],
) -> str | None:
    """The dotted module a qualified prefix denotes, if an import grounds it.

    Without this grounding the gate accepted any ``a.b.<stem>.<symbol>()`` chain, so
    ``self.storage.read()`` vouched for ``storage.py:read``. The tree is full of injected
    collaborators shaped exactly like that.
    """
    if not prefix:
        return None
    binding = bindings.get(prefix[0])
    if binding is None:
        return None
    module, name = binding
    base = f"{module}.{name}" if name else module
    return ".".join([base, *prefix[1:]])


def _file_uses_symbol(
    path: str,
    tree: ast.Module,
    bindings: dict[str, tuple[str, str]],
    symbol: str,
    defining: str,
    target: str,
    branch: str,
) -> bool:
    """Whether production code in `path` holds or calls the new symbol.

    A reference in a value position counts, not only a call: a function put in a dispatch
    table is wired. A bare import with no further mention does not count — that is the
    re-export defect, and it is how three unwired functions read as wired.
    """
    direct = _names_bound_to(path, tree, bindings, symbol, target, branch)
    skip: set[int] = set()
    if path == defining:
        # The symbol is defined here, so the bare name refers to it. Its own body does not
        # count: a function calling itself is not a production caller.
        direct.add(symbol)
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == symbol:
                skip.add(id(stmt))

    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if id(node) in skip:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in direct:
                return True
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            parts = _dotted_parts(node)
            if parts:
                module = _module_alias_target(parts[:-1], bindings)
                # The trailing name is resolved rather than compared, so a package attribute
                # reached under its re-exported alias (``filesearch.catalog_stats``) counts.
                if (
                    module
                    and module.split(".")[0] in SEARCH_ROOTS
                    and target in _export_terminals(module, parts[-1], branch)
                ):
                    return True
        stack.extend(_live_children(node))
    return False


def _production_callers(symbol: str, defining: str, branch: str) -> list[str]:
    """The first production file that holds or calls `symbol` — empty if there is none.

    Grepping the symbol's own name is not enough to FIND the callers. When a package
    re-exports it under another name (``from .catalog import stats as catalog_stats``) the
    call site contains only the alias, and `-w stats` never lists that file — the caller
    would be invisible and the symbol falsely refused. So discovery follows the aliases:
    any file that renames the symbol on import must itself mention the original name, which
    means each new alias is discovered from the previous round's hits, and the rounds
    terminate when no new name appears.

    Returns at most one file. The question is whether a production path exists, and the
    first proof answers it; parsing the remaining hits only costs time.
    """
    target = f"{_module_dotted(defining)}:{symbol}"
    names = {symbol}
    grepped: set[str] = set()
    parsed: set[str] = set()
    while names - grepped:
        batch = sorted(names - grepped)
        grepped |= set(batch)
        args = ["git", "grep", "-l", "-w"]
        for name in batch:
            args += ["-e", name]
        rc, out = _run([*args, branch, "--", *SEARCH_ROOTS])
        if rc not in (0, 1):
            return ["<grep failed — treat as unknown>"]
        for line in out.splitlines():
            f = line.split(":", 1)[-1]
            if not f.endswith(".py") or _is_test_path(f) or f in parsed:
                continue
            parsed.add(f)
            src_rc, source = _run(["git", "show", f"{branch}:{f}"])
            if src_rc != 0:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            bindings = _import_bindings(f, tree)
            if _file_uses_symbol(f, tree, bindings, symbol, defining, target, branch):
                return [f]
            for local, (module, name) in bindings.items():
                if (
                    name
                    and local not in names
                    and target in _export_terminals(module, name, branch)
                ):
                    names.add(local)
    return []


# ======================================================================================
# Framework registration
# ======================================================================================


def _calls_in_scope(
    node: ast.AST, bound: frozenset[str] = frozenset()
) -> Iterator[tuple[ast.Call, frozenset[str]]]:
    """Every live Call in `node`, paired with the names bound locally where it appears.

    Plain ``ast.walk`` loses scope, and scope is what tells a mounted router apart from a
    parameter that merely shares its name.
    """
    for child in _live_children(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            spec = child.args
            local = {a.arg for a in (*spec.posonlyargs, *spec.args, *spec.kwonlyargs)}
            if spec.vararg:
                local.add(spec.vararg.arg)
            if spec.kwarg:
                local.add(spec.kwarg.arg)
            for sub in _walk_live(child):
                names, _ = _assigned_names(sub) if isinstance(sub, ast.stmt) else ([], None)
                local |= set(names)
            yield from _calls_in_scope(child, bound | frozenset(local))
            continue
        if isinstance(child, ast.Call):
            yield child, bound
        yield from _calls_in_scope(child, bound)


def _application_locals(tree: ast.Module) -> set[str]:
    """Names in this file assigned the result of an application constructor.

    By value, never by name: ``app = APIRouter()`` is a router, and a gate that trusts the
    name lets any module declare itself served.
    """
    names: set[str] = set()
    for node in _walk_live(tree):
        if not isinstance(node, ast.stmt):
            continue
        targets, value = _assigned_names(node)
        if not targets or not isinstance(value, ast.Call):
            continue
        parts = _dotted_parts(value.func)
        if parts and parts[-1] in APP_FACTORIES:
            names |= set(targets)
    return names


def _identity_is_application(identity: str, branch: str) -> bool:
    """Whether an identity names an object built by an application constructor."""
    module, _, var = identity.partition(":")
    found = _module_tree(module, branch)
    if found is None:
        return False
    _, tree = found
    for stmt in _module_scope(tree):
        targets, value = _assigned_names(stmt)
        if var not in targets or not isinstance(value, ast.Call):
            continue
        parts = _dotted_parts(value.func)
        if parts and parts[-1] in APP_FACTORIES:
            return True
    return False


@cache
def _mounted_routers(branch: str) -> frozenset[str]:
    """Routers reachable from a served application, as canonical identities.

    Built as a fixed point rather than a flat list of include_router targets. Mountedness
    starts at objects constructed from an application factory and spreads only along
    mounts whose OWNER is already mounted; an aggregator nothing serves therefore spreads
    nothing. ``git grep`` narrows the files; the AST decides.
    """
    # NUL-delimited, same reason as _changed_py: `-l` alone C-quotes a non-ASCII
    # path under the default core.quotePath=true (e.g. "caf\303\251.py"), and the
    # quoted literal then fails every downstream `git show {branch}:{f}` lookup.
    # `-z` returns each filename intact and unquoted, terminated by NUL.
    rc, out = _run(
        ["git", "grep", "-l", "-z", "-w", "include_router", branch, "--", *SEARCH_ROOTS]
    )
    if rc not in (0, 1):
        return frozenset()

    # `git grep <rev> -- <paths>` prefixes each match with `<rev>:`. Git ref names
    # cannot contain a colon, so stripping the known branch prefix (rather than
    # splitting on the first colon in the line, which the old code did and which
    # would also work here) is unambiguous even if a path itself later grows one.
    rev_prefix = f"{branch}:"
    edges: list[tuple[str | None, bool, str]] = []
    for raw in out.split("\0"):
        if not raw:
            continue
        f = raw[len(rev_prefix) :] if raw.startswith(rev_prefix) else raw
        if not f.endswith(".py") or _is_test_path(f):
            continue
        src_rc, source = _run(["git", "show", f"{branch}:{f}"])
        if src_rc != 0:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        bindings = _import_bindings(f, tree)
        app_locals = _application_locals(tree)
        for node, local_names in _calls_in_scope(tree):
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "include_router":
                continue
            mounted_expr = next(
                (kw.value for kw in node.keywords if kw.arg == "router"),
                node.args[0] if node.args else None,
            )
            if mounted_expr is None:
                continue
            raw = _resolve(mounted_expr, bindings, f, tree, local_names, branch)
            if raw is None:
                continue
            owner_id: str | None = None
            owner_is_app = False
            owner_parts = _dotted_parts(func.value)
            if owner_parts and len(owner_parts) == 1 and owner_parts[0] in app_locals:
                owner_is_app = True
            else:
                owner_id = _resolve(func.value, bindings, f, tree, local_names, branch)
                if owner_id is not None:
                    owner_is_app = _identity_is_application(owner_id, branch)
            edges.append((owner_id, owner_is_app, raw))

    mounted = {target for _, is_app, target in edges if is_app}
    changed = True
    while changed:
        changed = False
        for owner, _, target in edges:
            if target not in mounted and owner is not None and owner in mounted:
                mounted.add(target)
                changed = True
    return frozenset(mounted)


def _resolve(
    expr: ast.AST,
    bindings: dict[str, tuple[str, str]],
    path: str,
    tree: ast.Module,
    local_names: frozenset[str],
    branch: str,
) -> str | None:
    """Canonical ``<defining module>:<variable>`` for a router or app expression.

    Unresolvable expressions (a call result, a subscript, an object built at runtime, a
    name handed in as a function argument) return None and therefore wire nothing — fail
    closed, as everywhere else in this gate.
    """
    parts = _dotted_parts(expr)
    if not parts:
        return None
    if parts[0] in local_names:
        # A parameter or local shadows whatever module-level name it matches, and we cannot
        # say which object it holds. `def _mount(app, router): app.include_router(router)`
        # in a route module must not launder that module's own handlers into "mounted".
        return None
    var = parts[-1]
    prefix = parts[:-1]
    if not prefix:
        binding = bindings.get(var)
        if binding is None:
            if any(var in _assigned_names(stmt)[0] for stmt in _module_scope(tree)):
                return _canonical(f"{_module_dotted(path)}:{var}", branch)
            return None
        module, name = binding
        if not name:
            # `import pkg.mod as router` — a module, not a router object.
            return None
        return _canonical(f"{module}:{name}", branch)
    module = _module_alias_target(prefix, bindings)
    return _canonical(f"{module}:{var}", branch) if module else None


def _route_decorator_targets(tree: ast.Module, symbol: str) -> list[str]:
    """Objects `symbol` is registered on, e.g. ``router`` for ``@router.post("/x")``."""
    targets: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != symbol:
            continue
        for decorator in node.decorator_list:
            attribute = decorator.func if isinstance(decorator, ast.Call) else decorator
            if not isinstance(attribute, ast.Attribute):
                continue
            if attribute.attr not in ROUTE_DECORATORS:
                continue
            parts = _dotted_parts(attribute.value)
            if parts and len(parts) == 1:
                targets.append(parts[0])
    return targets


def _framework_registration(symbol: str, defining: str, branch: str) -> str | None:
    """Where the framework picks `symbol` up, or None if nothing registers it.

    Two facts must both hold, and both are read from the AST: the function carries a route
    decorator, AND the object it is registered on is genuinely served — an application, or
    a router inside the mount closure of one. A decorator alone is a claim, not a wiring;
    honouring it unchecked would be a new false-GREEN.
    """
    found = _module_tree(_module_dotted(defining), branch)
    if found is None:
        return None
    _, tree = found
    targets = _route_decorator_targets(tree, symbol)
    if not targets:
        return None
    module = _module_dotted(defining)
    mounted = _mounted_routers(branch)
    for var in targets:
        identity = _canonical(f"{module}:{var}", branch)
        if _identity_is_application(identity, branch):
            return f"@{var}.* on the application object"
        if identity in mounted:
            return f"@{var}.* on a router mounted into a served application"
    return None


def main() -> int:
    branch = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    base = sys.argv[2] if len(sys.argv) > 2 else "main"
    exempt = _exemptions()

    unreached: list[tuple[str, str, int]] = []
    registered: list[tuple[str, str, str]] = []
    checked = 0
    for path in _changed_py(base, branch):
        for sym, line in _added_public_defs(base, branch, path):
            checked += 1
            if f"{path}:{sym}" in exempt or sym in exempt:
                continue
            if _production_callers(sym, path, branch):
                continue
            where = _framework_registration(sym, path, branch)
            if where:
                registered.append((path, sym, where))
                continue
            unreached.append((path, sym, line))

    print(f"reachability: {checked} new public symbol(s) on {branch} vs {base}")
    for path, sym, where in registered:
        print(f"  framework-registered: {path}  {sym}()  [{where}]")
    if not unreached:
        if registered:
            print(
                "  every new public symbol is called by production code or registered "
                "with the framework"
            )
        else:
            print("  every new public symbol has a production caller")
        return 0

    print(f"\nREFUSED — {len(unreached)} new public symbol(s) with NO production caller:\n")
    for path, sym, line in unreached:
        print(f"  {path}:{line}  {sym}()")
    print(
        "\nEach is 'built, tested, never wired' — this repo's signature defect, found twelve\n"
        "times. A capability that exists and is never invoked is not a feature.\n\n"
        "If it is a route handler, no served application reaches its router: add the missing\n"
        "`include_router(...)` line — including on whatever aggregator the router hangs off —\n"
        "and the framework becomes its caller, with no exemption needed or wanted.\n\n"
        "Otherwise fix by wiring a real production caller, or, if the symbol is genuinely\n"
        "meant for operator or external use, add `<path>:<symbol>  reason` to\n"
        "devtasks/REACHABILITY-EXEMPT.txt so the decision is recorded rather than implied."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
