"""Extract definition + reference tags from source files — the repo-map's front end.

Python uses the stdlib ``ast`` (exact — no third-party parser); TS/JS/TSX use a light
regex pass. Both languages emit the SAME ``Definition`` + reference-counter shape, so
the language-agnostic ranker (see :mod:`omniagentos.repomap.ranking`) never cares which
extractor produced them — adding a language is adding one function here.

Extraction is the expensive half of a repo map (~1.3s for this repo; ranking is ~0.1s),
so it is cached — see ``content_hash`` and ``TagPayload`` at the bottom of this module
for the cacheable, path-independent unit, and ``TagCache`` in
:mod:`omniagentos.repomap.service` for the two-tier store built on them.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".next",
        "dist",
        "build",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "var",
        ".tmp",
        "coverage",
        ".turbo",
        ".cache",
        "vendor",
        "target",
        ".idea",
        ".vscode",
    }
)
_PY_EXT: frozenset[str] = frozenset({".py"})
_JSTS_EXT: frozenset[str] = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_SOURCE_EXT: frozenset[str] = _PY_EXT | _JSTS_EXT

# Identity of the parse itself, folded into content_hash(). BUMP on any change to
# extract_python, extract_js_ts, _JS_DEF_PATTERNS, lang_for, or the TagPayload shape.
# A bump changes every key, so stale rows are never served and simply age out.
EXTRACTOR_VERSION = "1"

_MAX_FILE_BYTES = 1_500_000  # skip generated megafiles (bundles, lockfiles-as-js)


@dataclass(frozen=True)
class Definition:
    """One named symbol defined in a file, with a rendered one-line signature."""

    rel_path: str
    name: str  # methods are qualified: "ClassName.method"
    kind: str  # class | function | method | interface | type | const
    line: int
    signature: str


@dataclass
class FileTags:
    rel_path: str
    definitions: list[Definition] = field(default_factory=list)
    references: Counter[str] = field(default_factory=Counter)


def iter_source_files(repo_dir: str) -> list[str]:
    """Every indexable source file under ``repo_dir`` (skips vendored/build/hidden dirs)."""
    out: list[str] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if os.path.splitext(name)[1] in _SOURCE_EXT:
                out.append(os.path.join(root, name))
    return out


def _truncate(text: str, limit: int = 150) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


# --- Python (stdlib ast — exact) --------------------------------------------


def _py_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:  # noqa: BLE001 -- never let signature rendering break extraction
        args = "…"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return _truncate(f"{prefix} {node.name}({args}):")


def _py_class_signature(node: ast.ClassDef) -> str:
    try:
        bases = ", ".join(ast.unparse(base) for base in node.bases)
    except Exception:  # noqa: BLE001
        bases = ""
    return _truncate(f"class {node.name}({bases}):" if bases else f"class {node.name}:")


def extract_python(rel_path: str, source: str) -> FileTags:
    tags = FileTags(rel_path)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return tags  # unparseable file contributes nothing, never raises

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            tags.definitions.append(
                Definition(rel_path, node.name, "class", node.lineno, _py_class_signature(node))
            )
            self._class_stack.append(node.name)
            self.generic_visit(node)
            self._class_stack.pop()

        def _func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            if self._class_stack:
                qualified = ".".join([*self._class_stack, node.name])
                tags.definitions.append(
                    Definition(rel_path, qualified, "method", node.lineno, _py_func_signature(node))
                )
            else:
                tags.definitions.append(
                    Definition(
                        rel_path, node.name, "function", node.lineno, _py_func_signature(node)
                    )
                )
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._func(node)

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load):
                tags.references[node.id] += 1

        def visit_Attribute(self, node: ast.Attribute) -> None:
            tags.references[node.attr] += 1
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for alias in node.names:
                tags.references[alias.name] += 1
            self.generic_visit(node)

    _Visitor().visit(tree)
    return tags


# --- TS / JS (light regex) --------------------------------------------------

import re  # noqa: E402 -- kept next to the JS extractor it belongs to

_JS_DEF_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
        "function",
    ),
    (
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"),
        "class",
    ),
    (re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)"), "interface"),
    (re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*[=<]"), "type"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[:=]"), "const"),
)
_JS_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_JS_STOPWORDS: frozenset[str] = frozenset(
    {
        "const",
        "let",
        "var",
        "function",
        "class",
        "return",
        "if",
        "else",
        "for",
        "while",
        "import",
        "export",
        "from",
        "default",
        "async",
        "await",
        "new",
        "this",
        "typeof",
        "instanceof",
        "void",
        "null",
        "undefined",
        "true",
        "false",
        "string",
        "number",
        "boolean",
        "any",
        "unknown",
        "never",
        "type",
        "interface",
        "extends",
        "implements",
        "public",
        "private",
        "protected",
        "readonly",
        "static",
        "get",
        "set",
        "of",
        "in",
        "as",
        "is",
        "keyof",
        "enum",
        "namespace",
        "declare",
        "module",
        "require",
        "super",
        "try",
        "catch",
        "finally",
        "throw",
        "switch",
        "case",
        "break",
        "continue",
        "do",
        "delete",
        "yield",
        "then",
        "console",
        "window",
        "document",
        "Math",
        "JSON",
        "Object",
        "Array",
        "String",
        "Number",
        "Boolean",
        "Promise",
        "Map",
        "Set",
        "React",
        "props",
        "state",
        "value",
        "key",
        "name",
        "id",
    }
)


def extract_js_ts(rel_path: str, source: str) -> FileTags:
    tags = FileTags(rel_path)
    for lineno, line in enumerate(source.splitlines(), start=1):
        for pattern, kind in _JS_DEF_PATTERNS:
            match = pattern.match(line)
            if match:
                tags.definitions.append(
                    Definition(rel_path, match.group(1), kind, lineno, _truncate(line.strip()))
                )
                break
    for token in _JS_IDENT.findall(source):
        if token not in _JS_STOPWORDS and len(token) > 1:
            tags.references[token] += 1
    return tags


def extract_file(rel_path: str, source: str) -> FileTags:
    ext = os.path.splitext(rel_path)[1]
    if ext in _PY_EXT:
        return extract_python(rel_path, source)
    if ext in _JSTS_EXT:
        return extract_js_ts(rel_path, source)
    return FileTags(rel_path)


def read_source_bytes(abs_path: str) -> bytes | None:
    """Raw bytes of a source file, or ``None`` when unreadable or oversized (skipped).

    Bytes rather than text because the bytes are what gets hashed: decoding first would
    make the cache key depend on this process's decode behaviour instead of the file."""
    try:
        if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
            return None
        with open(abs_path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def extract_bytes(rel_path: str, data: bytes) -> FileTags:
    """Extract tags from raw source bytes (lenient decode; never raises)."""
    return extract_file(rel_path, data.decode("utf-8", errors="ignore"))


def read_and_extract(abs_path: str, rel_path: str) -> FileTags:
    """Read a file (best-effort) and extract its tags. Oversized/binary files skip."""
    data = read_source_bytes(abs_path)
    return FileTags(rel_path) if data is None else extract_bytes(rel_path, data)


# --- Content addressing: the cacheable unit ---------------------------------

_LANG_BY_EXT: dict[str, str] = {
    **{ext: "python" for ext in _PY_EXT},
    **{ext: "jsts" for ext in _JSTS_EXT},
}


def lang_for(rel_path: str) -> str:
    """Which extractor owns this path: ``python`` | ``jsts`` | ``other`` (no tags)."""
    return _LANG_BY_EXT.get(os.path.splitext(rel_path)[1], "other")


def content_hash(rel_path: str, data: bytes) -> str:
    """Cache key for a file's tags: sha256 hex over ``<lang>\\0<raw bytes>``.

    Content-addressed, deliberately NOT mtime-addressed. The same bytes always yield
    the same key, so a branch switch, a fresh worktree, a revert, a rebase or a bare
    ``touch`` — all of which change mtimes without changing content — reuse the parse,
    and two identical files parse once. Only the *language* of the path is folded in
    (not the path itself), because the same bytes parse differently as ``.py`` and
    ``.ts``; everything else about the path is irrelevant to the result.

    ``EXTRACTOR_VERSION`` is folded in because the cache key must identify *the parse*,
    not just the bytes. Without it, a persistent cache serves parses produced by an OLD
    extractor forever: add an ``enum`` pattern to ``_JS_DEF_PATTERNS`` and every
    already-cached ``.ts`` file keeps returning its pre-enum symbol set on a warm cache
    while a cold one returns the new symbols. BUMP IT on any change to the extractors
    (``extract_python``, ``extract_js_ts``, ``_JS_DEF_PATTERNS``, ``lang_for``, the
    ``TagPayload`` shape) — that invalidates every stale row for free, because a bumped
    version simply produces different keys and the old rows age out.

    Same sha256-hex convention as ``contracts.digest`` and
    ``sessions.policy_map.action_hash``; ``hashlib`` is called directly here to keep
    this package importable with nothing but the stdlib."""
    return hashlib.sha256(
        EXTRACTOR_VERSION.encode() + b"\0" + lang_for(rel_path).encode() + b"\0" + data
    ).hexdigest()


@dataclass(frozen=True)
class TagPayload:
    """A file's tags with the PATH REMOVED — the unit a content-addressed cache stores.

    ``FileTags`` is path-bound, but the parse result is not: the same bytes produce the
    same symbols wherever they sit. Stripping the path is what lets one cached row serve
    a file that moved, was copied, or lives at a different path in another worktree.
    ``bind()`` re-attaches a path, returning fresh mutable containers so callers can
    never corrupt a shared cache entry."""

    definitions: tuple[tuple[str, str, int, str], ...]  # (name, kind, line, signature)
    references: tuple[tuple[str, int], ...]

    @classmethod
    def of(cls, tags: FileTags) -> TagPayload:
        return cls(
            definitions=tuple((d.name, d.kind, d.line, d.signature) for d in tags.definitions),
            references=tuple(tags.references.items()),
        )

    def bind(self, rel_path: str) -> FileTags:
        return FileTags(
            rel_path,
            [Definition(rel_path, *fields) for fields in self.definitions],
            Counter(dict(self.references)),
        )

    def to_json(self) -> str:
        return json.dumps(
            {"d": [list(d) for d in self.definitions], "r": dict(self.references)},
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> TagPayload | None:
        """Decode a stored payload, or ``None`` if it is corrupt or an unknown shape.

        A cache row is never trusted: a bad row is simply a miss, and the file is
        re-parsed."""
        try:
            blob = json.loads(raw)
            return cls(
                definitions=tuple(
                    (str(name), str(kind), int(line), str(signature))
                    for name, kind, line, signature in blob["d"]
                ),
                references=tuple((str(k), int(v)) for k, v in blob["r"].items()),
            )
        except Exception:  # noqa: BLE001 -- a cache is never allowed to break a build
            return None
