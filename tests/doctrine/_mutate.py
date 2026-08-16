"""Apply / restore file mutations for doctrine helpers.

Mutations are temporary and always restored in a ``finally`` block by the
caller. Prefer string replacements for readability; callables for structural
edits. Never uses ``git stash`` (banned across worktrees).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

Mutation = str | Callable[[str], str] | Callable[[Path], None]


@dataclass(frozen=True, slots=True)
class TextReplace:
    """Replace exactly one occurrence of ``old`` with ``new`` in a file."""

    old: str
    new: str

    def apply(self, source: str, *, path: Path) -> str:
        count = source.count(self.old)
        if count == 0:
            raise ValueError(f"mutation target not found in {path}: {self.old!r}")
        if count > 1:
            raise ValueError(
                f"mutation target is ambiguous in {path} "
                f"({count} occurrences of {self.old!r}); make the needle unique"
            )
        return source.replace(self.old, self.new, 1)


def _apply_mutation(path: Path, mutation: Mutation | TextReplace) -> None:
    if isinstance(mutation, TextReplace):
        original = path.read_text(encoding="utf-8")
        path.write_text(mutation.apply(original, path=path), encoding="utf-8")
        return

    if isinstance(mutation, str):
        # Whole-file rewrite.
        path.write_text(mutation, encoding="utf-8")
        return

    # Callable: either (source: str) -> str or (path: Path) -> None
    try:
        import inspect

        sig = inspect.signature(mutation)
        params = list(sig.parameters.values())
    except (TypeError, ValueError):
        params = []

    if params and params[0].annotation is Path:
        mutation(path)  # type: ignore[operator]
        return

    # Default: treat as text transformer if one positional arg, else path mutator.
    if len(params) == 1:
        ann = params[0].annotation
        name = params[0].name
        if ann is str or ann == "str" or name in {"source", "text", "content", "src"}:
            original = path.read_text(encoding="utf-8")
            updated = mutation(original)  # type: ignore[operator]
            if not isinstance(updated, str):
                raise TypeError(f"text mutation for {path} must return str, got {type(updated)!r}")
            path.write_text(updated, encoding="utf-8")
            return

    # Path-based mutator (may rewrite the file in place).
    mutation(path)  # type: ignore[operator]


@contextmanager
def mutated_file(path: Path, mutation: Mutation | TextReplace) -> Iterator[Path]:
    """Apply ``mutation`` to ``path``, yield, then restore the original bytes.

    Restores even if the caller raises. Uses the original file bytes so encoding
    and trailing newlines are preserved exactly.
    """
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"mutation target does not exist: {target}")

    original = target.read_bytes()
    try:
        _apply_mutation(target, mutation)
        yield target
    finally:
        target.write_bytes(original)
