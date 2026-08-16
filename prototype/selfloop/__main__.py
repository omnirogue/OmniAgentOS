"""``python -m selfloop`` — the module entry point. See :mod:`selfloop.cli`.

Deliberately four lines. Everything a scheduler drives lives in
:func:`selfloop.cli.main`, which RETURNS an exit status rather than exiting, so
that a test can call it and an embedder can wrap it. Turning that return value
into a process exit is this file's only job, and keeping it here means the
console-script entry point in ``pyproject.toml`` and ``python -m selfloop`` run
exactly the same code.

The ``__name__`` guard is not ceremony. ``python -m selfloop`` runs this file
with ``__name__ == "__main__"``, so the guard costs nothing there — while
without it, anything that merely IMPORTS ``selfloop.__main__`` (a test that
walks the package asserting no module does work at import, a documentation
tool, a bundler) would execute the whole command line and exit the interpreter.
"""

from __future__ import annotations

from selfloop.cli import main

if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())

__all__ = ["main"]
