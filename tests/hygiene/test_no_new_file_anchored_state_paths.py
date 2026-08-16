"""D3 ratchet — no NEW ``__file__``-anchored runtime-state paths in ``omniagentos/``.

Re-deriving ``var`` / ``secrets`` / ``ledger`` / ``vault`` locations from
``__file__`` at each call site is how a simulation process ended up reading and
writing the operator checkout (O-17). The ~100 legacy sites are not converted in
one big bang; instead they are FROZEN in ``.allowlist_file_anchored_paths`` and
this test blocks the regrowth:

* a hit that is not allowlisted fails with ``file:line:symbol — use
  omniagentos.runtime_paths``;
* an allowlist entry that no longer exists fails too, so the list can only
  ratchet DOWN — converting a site is not done until its entry is deleted.

Detection is deliberately shape-agnostic. It walks EVERY expression node, so a
state path hidden in a ``with``/``for`` header, a decorator, a function default
argument or a comprehension is caught exactly like a plain assignment; and it
follows the indirect form (``_ROOT = Path(__file__)...`` then ``_ROOT / "var"``)
transitively. Entries are keyed by ``file:symbol``, never by line number: a
blocking test that reddens because an unrelated edit shifted a line teaches
people to distrust it.

``omniagentos/runtime_paths.py`` is scanned like every other module — its own
package anchor is an ordinary allowlist entry. Exempting the resolver would make
the one file most likely to accrue new state paths the only file the ratchet
cannot see.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _ROOT / "omniagentos"
_ALLOWLIST = Path(__file__).with_name(".allowlist_file_anchored_paths")
_STATE_SEGMENTS = frozenset({"var", "secrets", "ledger", "vault"})

#: Expression shapes that BUILD a path: ``a / "var"``, ``os.path.join(...)`` /
#: ``x.joinpath(...)`` / ``str(...)``, and f-strings.
_PATH_EXPRESSIONS = (ast.BinOp, ast.Call, ast.JoinedStr)


def _assignment_targets(node: ast.AST) -> Iterable[ast.expr]:
    if isinstance(node, ast.Assign):
        yield from node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        yield node.target


def _references(node: ast.AST, names: frozenset[str]) -> bool:
    """Does ``node`` read ``__file__`` or any name bound to a file anchor?"""
    return any(
        isinstance(item, ast.Name) and (item.id == "__file__" or item.id in names)
        for item in ast.walk(node)
    )


def _anchor_names(tree: ast.Module) -> frozenset[str]:
    """Names bound (transitively) to a ``__file__``-derived expression.

    Both bindings count, because both are used in this tree: assignment
    (``_ROOT = Path(__file__)...``) and *definition* (``def _repo_root():
    return ...__file__...``), so a later ``os.path.join(_repo_root(), "var")``
    is recognised as file-anchored too.
    """
    names: frozenset[str] = frozenset()
    while True:
        found = set(names)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                value = node.value
                if value is None or not _references(value, frozenset(found)):
                    continue
                for target in _assignment_targets(node):
                    if isinstance(target, ast.Name):
                        found.add(target.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                returns = [item for item in ast.walk(node) if isinstance(item, ast.Return)]
                if any(
                    item.value is not None and _references(item.value, frozenset(found))
                    for item in returns
                ):
                    found.add(node.name)
        if found == set(names):
            return names
        names = frozenset(found)


def _has_state_literal(node: ast.AST) -> bool:
    for item in ast.walk(node):
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            parts = item.value.replace("\\", "/").split("/")
            if any(part in _STATE_SEGMENTS for part in parts):
                return True
    return False


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _symbol(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """A line-stable name for the site: its variable, else its function/class."""
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            for target in _assignment_targets(current):
                if isinstance(target, ast.Name):
                    return target.id
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return current.name
        current = parents.get(current)
    return "<module>"


def _anchor_sites(path: Path, tree: ast.Module) -> dict[str, int]:
    """``{"<relpath>:<symbol>": first line}`` for every file-anchored state path."""
    names = _anchor_names(tree)
    parents = _parent_map(tree)
    relative = path.relative_to(_ROOT)
    sites: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, _PATH_EXPRESSIONS):
            continue
        if not _references(node, names) or not _has_state_literal(node):
            continue
        key = f"{relative}:{_symbol(node, parents)}"
        lineno = getattr(node, "lineno", 0)
        if key not in sites or lineno < sites[key]:
            sites[key] = lineno
    return sites


def observed_sites() -> dict[str, int]:
    """Every file-anchored state-path site under ``omniagentos/``, sorted."""
    sites: dict[str, int] = {}
    for path in sorted(_SCAN_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for key, lineno in _anchor_sites(path, tree).items():
            sites[key] = min(lineno, sites.get(key, lineno))
    return dict(sorted(sites.items()))


def allowlisted_sites() -> set[str]:
    return {
        line.strip()
        for line in _ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def ratchet_messages(observed: dict[str, int], allowlisted: set[str]) -> list[str]:
    """Both directions: a new hit blocks, and a stale allowlist entry blocks."""
    messages = [
        f"{site.rsplit(':', 1)[0]}:{observed[site]}:{site.rsplit(':', 1)[1]} "
        "— use omniagentos.runtime_paths"
        for site in sorted(set(observed) - allowlisted)
    ]
    messages += [
        f"ratchet down: {site} no longer exists — delete it from "
        f"{_ALLOWLIST.relative_to(_ROOT)} (the allowlist never grows back)"
        for site in sorted(allowlisted - set(observed))
    ]
    return messages


def test_file_anchored_state_paths_are_ratchet_allowlisted() -> None:
    assert not ratchet_messages(observed_sites(), allowlisted_sites()), "\n".join(
        ratchet_messages(observed_sites(), allowlisted_sites())
    )


def test_the_ratchet_blocks_in_both_directions() -> None:
    added = ratchet_messages({"omniagentos/new.py:_ROOT": 12}, set())
    assert added == ["omniagentos/new.py:12:_ROOT — use omniagentos.runtime_paths"]

    removed = ratchet_messages({}, {"omniagentos/old.py:_ROOT"})
    assert len(removed) == 1
    assert removed[0].startswith("ratchet down: omniagentos/old.py:_ROOT no longer exists")

    assert ratchet_messages({"omniagentos/x.py:_R": 3}, {"omniagentos/x.py:_R"}) == []


def test_allowlist_is_human_readable_and_sorted() -> None:
    """The freeze list is reviewed by humans, so it stays sorted and unique."""
    entries = [
        line.strip()
        for line in _ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert entries == sorted(entries), "allowlist entries must be sorted"
    assert len(entries) == len(set(entries)), "allowlist entries must be unique"
    for entry in entries:
        relative, _, symbol = entry.partition(":")
        assert relative.startswith("omniagentos/"), entry
        assert symbol, entry


def test_ratchet_detects_a_newly_introduced_state_path() -> None:
    """The detector must catch the shapes people actually write.

    A ratchet nobody can evade is the whole point: each of these SNEAKY forms
    slipped past a statement-type-based walk.
    """
    module = ast.parse(
        "from pathlib import Path\n"
        "import os\n"
        "_R = Path(__file__).resolve().parents[2]\n"
        "_DIRECT = Path(__file__).resolve().parents[2] / 'var' / 'x'\n"
        "_INDIRECT = _R / 'secrets' / 'y'\n"
        "_JOIN = os.path.join(_R, 'ledger')\n"
        "def _in_with():\n"
        "    with open(_R / 'var' / 'w') as fh:\n"
        "        return fh\n"
        "def _in_for():\n"
        "    for _ in [_R / 'var' / 'f']:\n"
        "        pass\n"
        "def _in_default(p=_R / 'var' / 'd'):\n"
        "    return p\n"
        "def _in_fstring():\n"
        "    return f'{_R}/vault/v'\n"
        "def _root_fn():\n"
        "    return Path(__file__).parent\n"
        "def _via_helper():\n"
        "    return os.path.join(_root_fn(), 'var', 'h')\n"
    )
    sites = _anchor_sites(_SCAN_ROOT / "sample.py", module)
    symbols = {key.split(":", 1)[1] for key in sites}
    assert symbols == {
        "_DIRECT",
        "_INDIRECT",
        "_JOIN",
        "_in_with",
        "_in_for",
        "_in_default",
        "_in_fstring",
        "_via_helper",
    }


def test_ratchet_ignores_non_state_and_non_anchored_paths() -> None:
    """No false positives for repo-relative non-state paths or env-derived roots."""
    module = ast.parse(
        "import os\n"
        "from pathlib import Path\n"
        "_R = Path(__file__).resolve().parents[2]\n"
        "_CONFIG = _R / 'configs' / 'policy.yaml'\n"
        "_ENV = Path(os.environ['OMNIAGENTOS_VAR_DIR']) / 'var' / 'secrets'\n"
    )
    assert _anchor_sites(_SCAN_ROOT / "sample.py", module) == {}
