"""Guards for traps this repo has actually hit.

1. **pytest | tail** — the pipe's exit code replaces pytest's. Two agents and a
   coordinator certified green from a truncated tail of a red run.
2. **Assertion-count drop** — a "fix" that deletes asserts while keeping the
   test function, so the suite stays green by observing less.
3. **New suppressions** — ``# type: ignore`` / ``# noqa`` added to silence the
   signal the change was supposed to satisfy.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from tests.doctrine.errors import DoctrineError

# pytest (or python -m pytest / uv run pytest) piped into tail/head — the
# consumer's exit code becomes the pipeline's under ``set -e`` without pipefail.
_PYTEST_PIPED_TO_TAIL = re.compile(
    r"""
    (?:^|[\s;|&(])                  # command boundary
    (?:
        (?:uv\s+run\s+)?pytest\b
        | python(?:3)?\s+-m\s+pytest\b
        | \bpy\.test\b
    )
    [^|\n]*                         # args (no pipe yet)
    \|\s*                           # pipe
    (?:tail|head)\b                 # the trap
    """,
    re.IGNORECASE | re.VERBOSE,
)

_SUPPRESSION_RE = re.compile(
    r"""
    \#\s*
    (?:
        type:\s*ignore(?:\[[^\]]*\])?
        | noqa(?:\s*:\s*[A-Z0-9, ]+)?
        | pragma:\s*no\s*cover
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class AssertionCount:
    path: Path | None
    total: int
    by_function: dict[str, int]


@dataclass(frozen=True, slots=True)
class SuppressionCount:
    path: Path | None
    total: int
    type_ignore: int
    noqa: int
    no_cover: int
    lines: tuple[str, ...]


def detect_pytest_piped_to_tail(command: str) -> list[str]:
    """Return matching snippets if ``command`` pipes pytest into tail/head."""
    hits: list[str] = []
    for line in command.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _PYTEST_PIPED_TO_TAIL.search(stripped):
            hits.append(stripped)
    # Also check the whole blob for single-line scripts without newlines.
    if not hits and _PYTEST_PIPED_TO_TAIL.search(command):
        hits.append(command.strip())
    return hits


def assert_no_pytest_piped_to_tail(command: str, *, context: str = "") -> None:
    """Fail loudly if a shell command pipes pytest into ``tail`` or ``head``.

    The pipe replaces pytest's exit code with the consumer's (almost always 0
    when there was any output). This fooled two agents and a coordinator.
    """
    hits = detect_pytest_piped_to_tail(command)
    if not hits:
        return
    where = f" ({context})" if context else ""
    joined = "\n  ".join(hits)
    raise DoctrineError(
        "TRAP: pytest is piped into tail/head — the pipe's exit code replaces "
        f"pytest's{where}.\n"
        f"  offending command(s):\n  {joined}\n"
        "Run pytest directly and read its exit code. Never: pytest … | tail …"
    )


def count_assertions(source: str, *, path: Path | str | None = None) -> AssertionCount:
    """Count ``assert`` statements in Python source (AST, not string search)."""
    tree = ast.parse(source)
    by_fn: dict[str, int] = {}
    total = 0

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._stack.append(node.name)
            by_fn.setdefault(node.name, 0)
            self.generic_visit(node)
            self._stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._stack.append(node.name)
            by_fn.setdefault(node.name, 0)
            self.generic_visit(node)
            self._stack.pop()

        def visit_Assert(self, node: ast.Assert) -> None:
            nonlocal total
            total += 1
            if self._stack:
                by_fn[self._stack[-1]] = by_fn.get(self._stack[-1], 0) + 1
            self.generic_visit(node)

    _Visitor().visit(tree)
    return AssertionCount(
        path=Path(path) if path is not None else None,
        total=total,
        by_function=by_fn,
    )


def assert_assertion_count_not_dropped(
    before: str,
    after: str,
    *,
    path: Path | str | None = None,
    allow_equal: bool = True,
) -> AssertionCount:
    """Fail if a modified test file has fewer ``assert`` statements than before.

    A common decoration move: keep the test name, delete the asserts that would
    go red. Counts must not drop; equal is allowed (refactor without loss).
    """
    pre = count_assertions(before, path=path)
    post = count_assertions(after, path=path)
    if post.total < pre.total:
        where = f" in {path}" if path else ""
        raise DoctrineError(
            f"TRAP: assertion count DROPPED{where}: {pre.total} → {post.total}.\n"
            f"  before by function: {pre.by_function}\n"
            f"  after  by function: {post.by_function}\n"
            "A weaker test is not a fix. Restore the asserts or justify a "
            "split/move with a net-zero or net-up count across the suite."
        )
    if not allow_equal and post.total == pre.total:
        raise DoctrineError(
            f"assertion count unchanged ({post.total}) but an increase was required"
        )
    return post


def count_suppressions(source: str, *, path: Path | str | None = None) -> SuppressionCount:
    """Count ``# type: ignore``, ``# noqa``, and ``# pragma: no cover`` markers."""
    type_ignore = 0
    noqa = 0
    no_cover = 0
    lines: list[str] = []
    for line in source.splitlines():
        if not _SUPPRESSION_RE.search(line):
            continue
        lines.append(line.rstrip())
        lower = line.lower()
        if "type:" in lower and "ignore" in lower:
            type_ignore += 1
        if "noqa" in lower:
            noqa += 1
        if "no cover" in lower:
            no_cover += 1
    return SuppressionCount(
        path=Path(path) if path is not None else None,
        total=len(lines),
        type_ignore=type_ignore,
        noqa=noqa,
        no_cover=no_cover,
        lines=tuple(lines),
    )


def assert_no_new_suppressions(
    before: str,
    after: str,
    *,
    path: Path | str | None = None,
) -> None:
    """Fail if the diff introduces new ``type: ignore`` / ``noqa`` / no-cover.

    Suppressions are sometimes legitimate; adding them *in the same change that
    claims to satisfy a gate* is the trap this guard names.
    """
    pre = count_suppressions(before, path=path)
    post = count_suppressions(after, path=path)
    if post.total <= pre.total:
        # Still flag when the set of lines grew even if totals match via swap.
        if set(post.lines) <= set(pre.lines):
            return

    new_lines = [ln for ln in post.lines if ln not in pre.lines]
    if not new_lines and post.total <= pre.total:
        return
    if not new_lines:
        new_lines = list(post.lines)

    where = f" in {path}" if path else ""
    raise DoctrineError(
        f"TRAP: new suppression marker(s){where} "
        f"(type:ignore/noqa/no-cover {pre.total} → {post.total}).\n"
        f"  new lines:\n    " + "\n    ".join(new_lines) + "\n"
        "Silencing the checker is not satisfying the claim. Remove the "
        "suppression or land it in a separate, named waiver."
    )


def scan_path_for_pytest_pipe(path: Path | str) -> list[tuple[int, str]]:
    """Scan a script/file for pytest|tail lines; return (lineno, line) hits."""
    text = Path(path).read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if detect_pytest_piped_to_tail(line):
            found.append((i, line.rstrip()))
    return found
