"""The counterfeit gate: mutations that make a guard LOOK present while removing it.

A green suite is non-evidence. It was written by the same process that wrote the
code, at the same moment, with the same misunderstanding — so the thing it is
most likely to prove is that the author's model of the system is internally
consistent. A mutation corpus is the only signal in this package that cannot be
forged that way, because it is not a claim about the code: it is a claim about
what the *tests* do when the code stops being safe.

Two files carry it. :mod:`tests.counterfeits.harness` is the mechanism —
materialise, patch, run, judge — and ``corpus.toml`` is the evidence, one entry
per safety or learning property, each naming the exact anchor it removes and the
exact tests that must go red when it does.

Nothing is exported from this package. It is a package rather than a directory
so that the harness and the manifest travel with the suite that uses them and so
``corpus.toml`` sits next to the code that reads it.
"""

from __future__ import annotations

__all__: list[str] = []
