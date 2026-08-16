"""The kill drill: crash-safety demonstrated by actually crashing.

An in-process test cannot demonstrate what this package claims. Every mechanism
that makes a durable loop safe — the receipt written before the act, the
checkpoint written before the next node — exists to survive a process that stops
existing between two instructions, and a ``raise`` inside a ``try`` block does
not stop existing. It unwinds, runs ``finally`` blocks, flushes buffers, commits
whatever a context manager was holding, and hands control back. Those are exactly
the affordances a ``SIGKILL`` removes, so a test built on an exception is testing
the recovery path with the crash taken out of it.

:mod:`tests.drills.kill_drill` therefore runs a real tick in a real child
interpreter and ends it with ``os._exit(9)``: no unwinding, no ``atexit``, no
buffer flush, no checkpoint commit. Ground truth is an append-only file OUTSIDE
sqlite and outside the checkpoint, ``flush()``-ed and ``fsync()``-ed before the
kill point, so the evidence does not depend on the interpreter having had a
chance to shut down tidily.
"""

from __future__ import annotations

__all__: list[str] = []
