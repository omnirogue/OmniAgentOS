"""The two shipped adapter sets, and how to choose between them.

``selfloop`` defines twelve ports and implements none of them in its core. This
package is where the batteries live, and there are exactly two sets:

* :mod:`selfloop.adapters.memory` — all twelve ports in dicts and a lock, plus
  :func:`~selfloop.adapters.memory.build_memory_context`, the one-call
  constructor behind the five-minute promise. Zero config, zero files, zero
  dependencies. **Not durable**: nothing survives the process, so it cannot back
  a real loop and cannot be the subject of the kill drill.
* :mod:`selfloop.adapters.sqlite` — the five storage ports on one stdlib
  ``sqlite3`` file. This is what a scheduled loop runs on.

They compose rather than compete. The storage backend is orthogonal to the
clock, the policy, the model, the gate and the notifier, so a durable context is
the in-memory one with five keywords replaced::

    from selfloop.adapters.memory import build_memory_context
    from selfloop.adapters.sqlite import SqliteBackend
    from selfloop.lease import FlockLease

    backend = SqliteBackend("var/loop.db")
    ctx = build_memory_context(
        instance_id="daily-digest",
        template="digest",
        lease=FlockLease("var/leases"),
        **backend.as_context_overrides(),
    )

The lease is not in here. Leases are not storage — they are mutual exclusion,
they have their own failure taxonomy, and choosing one is a decision about how
many processes and how many hosts you have. They live in :mod:`selfloop.lease`.

Deferred, and named here so it reads as a decision rather than an omission: a
JSONL adapter for the two streaming ports. ``sqlite`` covers durability and
``memory`` covers the demo, so a third backend was cut before it could be paid
for by trimming the counterfeit corpus.

The sqlite names below resolve lazily (PEP 562), so importing this package does
not pull in ``sqlite3`` for a caller who only wants the in-memory adapters.
"""

from __future__ import annotations

from typing import Any

from selfloop.adapters.memory import (
    MemoryApprovalStore,
    MemoryCheckpointStore,
    MemoryClock,
    MemoryEventLog,
    MemoryReceiptStore,
    MemoryRecordStore,
    NullModel,
    RecordingModel,
    RecordingNotifier,
    ScriptedGate,
    ScriptedSignalSource,
    StaticPolicy,
    build_memory_context,
    failing_receipt,
    passing_receipt,
)

#: Names whose module imports ``sqlite3``. Resolved on first attribute access so
#: the cost of ``import selfloop.adapters`` stays flat.
_LAZY: dict[str, str] = {
    "SqliteApprovalStore": "selfloop.adapters.sqlite",
    "SqliteBackend": "selfloop.adapters.sqlite",
    "SqliteCheckpointStore": "selfloop.adapters.sqlite",
    "SqliteEventLog": "selfloop.adapters.sqlite",
    "SqliteReceiptStore": "selfloop.adapters.sqlite",
    "SqliteRecordStore": "selfloop.adapters.sqlite",
}


def __getattr__(name: str) -> Any:
    """Resolve a sqlite adapter name on first access (PEP 562)."""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "MemoryApprovalStore",
    "MemoryCheckpointStore",
    "MemoryClock",
    "MemoryEventLog",
    "MemoryReceiptStore",
    "MemoryRecordStore",
    "NullModel",
    "RecordingModel",
    "RecordingNotifier",
    "ScriptedGate",
    "ScriptedSignalSource",
    "SqliteApprovalStore",
    "SqliteBackend",
    "SqliteCheckpointStore",
    "SqliteEventLog",
    "SqliteReceiptStore",
    "SqliteRecordStore",
    "StaticPolicy",
    "build_memory_context",
    "failing_receipt",
    "passing_receipt",
]
