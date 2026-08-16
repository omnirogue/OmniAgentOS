"""Fixtures every ``selfloop`` suite shares. The one that matters is ``ctx``.

**Every storage-touching test in this package runs twice**: once against the
in-memory adapters and once against sqlite. That is not thoroughness for its own
sake. The two adapters have genuinely different semantics under the covers — one
compares Python objects, the other compares JSON byte strings; one holds a
``threading.Lock``, the other holds ``BEGIN IMMEDIATE`` — and a test that only
ever ran against dicts would let a compare-and-set that works on tuples but not
on the lists sqlite stores them as ship green. :func:`selfloop.adapters.memory.canonical_value`
exists precisely because that divergence was found, and the parametrised
``storage`` fixture below is what keeps finding it.

The seam that makes it one line for a test author: storage is five keyword
arguments on a :class:`~selfloop.context.LoopContext` and nothing else. Clock,
policy, model, gate, notifier and lease are unaffected by the choice of backend,
so ``make_ctx()`` swaps the five and leaves the rest alone.

Two fixtures rather than one, and the difference is worth knowing before you
write a test:

* ``ctx`` is a ready-built context. Use it for anything that does not care how
  the context was configured.
* ``make_ctx`` BUILDS one, and may be called more than once. Two contexts from
  the same ``make_ctx`` share the same durable storage while holding separate
  port objects — which is how a test simulates a process that died and a
  scheduler that started a fresh one. Against sqlite that really is a second
  connection to the same file.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

# The package refuses to mutate ``sys.path`` at import — that refusal is a stated
# property of the library and ``tests/test_packaging.py`` enforces it in a
# subprocess. Somebody still has to make the package importable, and that
# somebody is the harness, here, in a file that is never installed. Doing it in
# ``conftest.py`` is what lets a stranger run ``pytest`` from any directory
# without first arranging an editable install.
_PROTOTYPE_ROOT = Path(__file__).resolve().parent.parent
if str(_PROTOTYPE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROTOTYPE_ROOT))

from selfloop.adapters.memory import (  # noqa: E402 - after the path bootstrap above
    MemoryApprovalStore,
    MemoryCheckpointStore,
    MemoryClock,
    MemoryEventLog,
    MemoryReceiptStore,
    MemoryRecordStore,
    build_memory_context,
)
from selfloop.adapters.sqlite import SqliteBackend  # noqa: E402
from selfloop.context import LoopContext  # noqa: E402
from selfloop.contracts import LoopTool, RiskTier  # noqa: E402
from selfloop.lease import InProcessLease  # noqa: E402


class Storage:
    """One durable store, openable more than once. See :meth:`overrides`."""

    name = "abstract"

    def overrides(self) -> dict[str, Any]:
        """The five storage keywords for a :class:`LoopContext` over THIS store.

        Called once per simulated process. Two calls return two sets of port
        objects reading and writing the same underlying data, which is what a
        crash-and-restart test needs: the durable state must survive, the live
        objects must not.
        """
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class MemoryStorage(Storage):
    """Dicts and locks. Fast, and deliberately not durable.

    :meth:`overrides` hands back the SAME five store objects every time, which is
    the closest a dict gets to "the file was still there when the next process
    opened it". It is honest about its limit: nothing here survives the
    interpreter, so the kill drill belongs to the sqlite backend.
    """

    name = "memory"

    def __init__(self) -> None:
        self._ports: dict[str, Any] = {
            "receipts": MemoryReceiptStore(),
            "approvals": MemoryApprovalStore(),
            "records": MemoryRecordStore(),
            "events": MemoryEventLog(),
            "checkpoints": MemoryCheckpointStore(),
        }

    def overrides(self) -> dict[str, Any]:
        return dict(self._ports)

    def close(self) -> None:
        """Nothing to close; the dicts go with the fixture."""


class SqliteStorage(Storage):
    """One sqlite file, and a fresh connection per :meth:`overrides` call.

    A new connection is genuinely a new process's view of the file, so a test
    that writes through one set of ports and reads through another is exercising
    durability rather than a shared Python object. Every connection opened is
    tracked and closed by the fixture, because a leaked one keeps a WAL file open
    and makes the next test's temporary directory teardown noisy on some
    platforms.
    """

    name = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._open: list[SqliteBackend] = []

    def overrides(self) -> dict[str, Any]:
        backend = SqliteBackend(self.path)
        self._open.append(backend)
        return backend.as_context_overrides()

    def close(self) -> None:
        for backend in self._open:
            backend.close()
        self._open.clear()


@pytest.fixture(params=("memory", "sqlite"))
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    """The storage backend under test. **Every dependent test runs twice.**

    Parametrised rather than chosen, because the alternative — a suite that runs
    against dicts and a handful of sqlite tests bolted on — tests the two
    adapters unequally, and the one that gets less coverage is the one production
    runs on.
    """
    backend: Storage = (
        MemoryStorage() if request.param == "memory" else SqliteStorage(tmp_path / "loop.db")
    )
    try:
        yield backend
    finally:
        backend.close()


@pytest.fixture
def clock() -> MemoryClock:
    """A controllable clock, shared by every context a test builds.

    Shared on purpose: an expiry test advances time once and both the writer and
    the reader see it. It also means no test in this package sleeps.
    """
    return MemoryClock()


@pytest.fixture
def make_ctx(storage: Storage, clock: MemoryClock) -> Callable[..., LoopContext]:
    """Build a :class:`LoopContext` over the parametrised storage. Call it twice.

    Any keyword overrides one context field. The five storage ports come from the
    ``storage`` fixture, so a second call returns a context that shares the
    durable data and shares nothing else — a restarted process, in one line.
    """

    def build(**overrides: Any) -> LoopContext:
        settings: dict[str, Any] = {
            "instance_id": "t1",
            "template": "demo",
            "clock": clock,
            # In-process is correct here and nowhere else: the suite is one
            # interpreter, and a FlockLease would put a real file in a real
            # directory for no gain. Anything a scheduler starts needs
            # FlockLease or SqliteLease.
            "lease": InProcessLease(accept_single_process_only=True),
        }
        settings.update(storage.overrides())
        settings.update(overrides)
        return build_memory_context(**settings)

    return build


@pytest.fixture
def ctx(make_ctx: Callable[..., LoopContext]) -> LoopContext:
    """A ready-built context over the parametrised storage."""
    return make_ctx()


@pytest.fixture
def make_tool() -> Callable[..., LoopTool]:
    """Build a :class:`~selfloop.contracts.LoopTool` that is NOT sealed.

    Deliberately not registered through a :class:`~selfloop.contracts.ToolRegistry`.
    Registration runs a tool through the execution seam's sealer, after which the
    only way to call it is :func:`selfloop.tools.execute_effect` — which is
    exactly right for a seam test and wrong for a receipt test, where the whole
    point is to drive :func:`selfloop.receipts.guarded` directly with a callable
    a test controls. A suite that needs the seal imports ``selfloop.tools`` and
    registers its own.
    """

    def build(
        name: str = "do_thing",
        tier: RiskTier = RiskTier.T1,
        call: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> LoopTool:
        return LoopTool(name=name, tier=tier, call=call or (lambda **_: None), **kwargs)

    return build
