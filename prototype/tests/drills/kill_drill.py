"""One real tick in a fresh interpreter, killed with ``os._exit(9)`` at a named instant.

Run as a child process::

    python tests/drills/kill_drill.py <workdir> <kill_point>

``<kill_point>`` is one of :data:`KILL_NONE`, :data:`KILL_INSIDE_TOOL` or
:data:`KILL_AFTER_RECEIPT`. The child exits 0 when it survived and **9** when it
was killed, and the exit status is itself part of the evidence: a drill that
exits 0 where a kill was requested did not crash, and any conclusion drawn from
that run is about a different experiment.

The two kill points, and why they are the two
---------------------------------------------

They bracket the interval in which a crash is genuinely ambiguous:

``inside_tool_after_write``
    The irreversible act has happened and **nothing has recorded that it
    happened**. The receipt row is ``claimed`` with no result — the state this
    package calls UNKNOWN. There is no honest way for a later process to decide
    whether the effect went out, so the correct behaviour is to refuse to
    re-send and escalate. Anything else double-bills.

``after_receipt_before_checkpoint``
    The effect happened AND its receipt is durable, but the executor never got
    to write the checkpoint naming the next node. The resumed tick therefore
    re-enters the effect node — that re-entry is not a bug, it is the price of
    durability — and the receipt is what makes the re-entry a replay instead of
    a second send.

Ground truth lives outside every store under test
-------------------------------------------------

The append-only ledger at ``<workdir>/effects.ledger`` is written with an
explicit ``flush()`` and ``os.fsync()`` before each kill point, and it is neither
sqlite nor the checkpoint. That separation is the whole point of the file: if the
evidence lived in the same store as the bookkeeping, a bug that lost both would
look like a loop that behaved perfectly, and a drill whose oracle can be
corrupted by the defect it is hunting is not an oracle.

What this drill deliberately does not exercise
----------------------------------------------

The ``act`` tool is registered at **T1**. The ledger append it performs is
irreversible in fact — that is what makes the drill mean anything — but a T2
declaration would route every tick through the approval park, and a failure
would then be ambiguous between the crash-safety spine and the approval spine.
The approval path has its own tests and its own counterfeit entries. A drill
must be a clean oracle for one thing.

One template, ``observe_decide_act_verify``. It is the reference for the
gate/receipt/park spine and it acts on exactly one subject per tick, which is
what makes "did this effect happen exactly once?" a question with an answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: This file, and the project root two levels above ``tests/drills``. Computed
#: from ``__file__`` rather than from a working directory so that the parent test
#: can spawn a child from anywhere.
DRILL_PATH = Path(__file__).resolve()
PROJECT_ROOT = DRILL_PATH.parents[2]

#: Survive the tick. The control case, and the one that proves three clean
#: processes still produce exactly one effect.
KILL_NONE = "none"
#: Die inside the tool, after the irreversible write and before the receipt is
#: completed. Leaves the receipt CLAIMED with no result.
KILL_INSIDE_TOOL = "inside_tool_after_write"
#: Die after ``ReceiptStore.complete`` returns and before the executor commits
#: the checkpoint that would name the next node.
KILL_AFTER_RECEIPT = "after_receipt_before_checkpoint"

KILL_POINTS: tuple[str, ...] = (KILL_NONE, KILL_INSIDE_TOOL, KILL_AFTER_RECEIPT)

#: The exit status a killed child reports. Distinct from every status the CLI
#: uses, so "the child was killed" and "the tick failed" are never confused.
KILL_STATUS = 9

#: Ledger event kinds. ``effect`` is the one the assertions count: one line per
#: time the world was actually touched.
EFFECT = "effect"
RECEIPT_COMPLETED = "receipt_completed"
TICK = "tick"

INSTANCE_ID = "kill-drill"
#: The single subject every tick handles, so every tick derives the same business
#: key and therefore the same receipt. A drill over a moving key would produce one
#: effect per tick by construction and prove nothing.
SUBJECT_ID = "drill-subject-1"

LEDGER_NAME = "effects.ledger"
DB_NAME = "loop.db"
LEASE_DIR = "leases"


def ledger_path(workdir: str | os.PathLike[str]) -> Path:
    """The append-only ground-truth file for a drill rooted at *workdir*."""
    return Path(workdir) / LEDGER_NAME


def append(path: Path, payload: Mapping[str, Any]) -> None:
    """Append one JSON line and make it durable BEFORE returning.

    ``flush()`` then ``os.fsync()``, every time. Without the fsync the line sits
    in the kernel's page cache and, more to the point, without the flush it sits
    in Python's own buffer — which ``os._exit`` discards. A drill whose evidence
    is lost by the very kill it is measuring would report "the effect never
    happened", which is the most dangerous wrong answer available: it looks
    exactly like the safe outcome.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_ledger(workdir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every ledger line, oldest first. Missing file reads as no history.

    A line that is not JSON is kept as ``{"event": "unparsable", "raw": ...}``
    rather than dropped. A torn write is evidence about the drill — it would mean
    the fsync above is not doing what this module claims — and silently skipping
    it would hide the one failure this file cannot afford.
    """
    path = ledger_path(workdir)
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            entries.append({"event": "unparsable", "raw": line})
            continue
        entries.append(parsed if isinstance(parsed, dict) else {"event": "unparsable", "raw": line})
    return entries


def effects(workdir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Ledger lines recording that the world was touched. The thing being counted."""
    return [entry for entry in read_ledger(workdir) if entry.get("event") == EFFECT]


def ticks(workdir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """One line per tick that RAN TO COMPLETION. A killed tick writes none."""
    return [entry for entry in read_ledger(workdir) if entry.get("event") == TICK]


class KillAfterCompletion:
    """A :class:`~selfloop.ports.ReceiptStore` that dies the instant a receipt lands.

    This wrapper is how :data:`KILL_AFTER_RECEIPT` hits an instant that no public
    hook exposes. ``selfloop.receipts`` calls ``complete()`` and returns; the
    executor then writes the checkpoint naming the next node. Killing inside
    ``complete()``'s caller — here, immediately after the inner store's write is
    durable — lands exactly between those two events and requires no change to
    the package under test. A drill that had to patch the code it is validating
    would be measuring the patch.

    Every other method delegates untouched, so the only difference between a
    drilled tick and a production tick is when it stops existing.
    """

    def __init__(self, inner: Any, ledger: Path) -> None:
        self._inner = inner
        self._ledger = ledger

    def claim(self, key: str, *, instance_id: str, node: str, at: str) -> bool:
        return bool(self._inner.claim(key, instance_id=instance_id, node=node, at=at))

    def get(self, key: str) -> Mapping[str, Any] | None:
        return self._inner.get(key)

    def complete(self, key: str, *, envelope_json: str, at: str) -> None:
        """Complete the receipt durably, record that it landed, then vanish."""
        self._inner.complete(key, envelope_json=envelope_json, at=at)
        append(self._ledger, {"event": RECEIPT_COMPLETED, "key": key, "pid": os.getpid()})
        os._exit(KILL_STATUS)

    def release(self, key: str) -> bool:
        return bool(self._inner.release(key))


def build_context(workdir: Path, kill_point: str) -> tuple[Any, Any]:
    """Wire one loop instance against durable storage. Returns ``(ctx, backend)``.

    Imports live inside the function because this module is also imported by the
    parent test process purely for its constants and its ledger readers, and that
    process has no business pulling ``sqlite3`` in to read a text file.

    The gate is the shipped :class:`~selfloop.gates.ArtifactGate` pointed at the
    ledger, which is the production default and cannot pass without something
    having been produced. It has no bearing on the assertions — those are about
    statuses and effect counts — but a drill wired differently from production is
    a drill about a system nobody runs.
    """
    from selfloop.adapters.memory import MemoryClock, build_memory_context
    from selfloop.adapters.sqlite import SqliteBackend
    from selfloop.contracts import LoopTool, RiskTier, ToolRegistry
    from selfloop.gates import ArtifactGate
    from selfloop.lease import FlockLease
    from selfloop.templates.observe_decide_act_verify import NAME

    ledger = ledger_path(workdir)
    backend = SqliteBackend(workdir / DB_NAME)
    overrides = backend.as_context_overrides()
    if kill_point == KILL_AFTER_RECEIPT:
        overrides["receipts"] = KillAfterCompletion(overrides["receipts"], ledger)

    def observe(*, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """One subject, every tick, forever. See :data:`SUBJECT_ID`."""
        del params
        return [{"id": SUBJECT_ID, "detail": "the one thing this drill sends"}]

    def decide(*, subject: Mapping[str, Any]) -> dict[str, Any]:
        return {"action": "send", "subject_id": subject.get("id", "")}

    def act(*, subject: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
        """THE irreversible write, and the first kill point one instruction later.

        The append is durable before the process can die, so a ledger with one
        ``effect`` line and no completed receipt is an honest record of the worst
        case this package has to survive: it happened, and nothing knows it did.
        """
        append(
            ledger,
            {
                "event": EFFECT,
                "key": str(subject.get("id") or ""),
                "action": str(decision.get("action") or ""),
                "pid": os.getpid(),
            },
        )
        if kill_point == KILL_INSIDE_TOOL:
            os._exit(KILL_STATUS)
        return {"ok": True, "sent": subject.get("id", "")}

    def verify(
        *, subject: Mapping[str, Any], decision: Mapping[str, Any], result: Any
    ) -> dict[str, Any]:
        """Independent of the act's own report: it counts lines in the ledger.

        ``result`` is accepted and not consulted. A verifier that reads the
        actor's account of itself corroborates nothing, and this one is cheap
        enough to do properly: it goes and looks at the world the effect wrote to.
        """
        del decision, result
        key = str(subject.get("id") or "")
        seen = [entry for entry in effects(workdir) if entry.get("key") == key]
        return {"verified": bool(seen), "sends": len(seen)}

    tools = ToolRegistry()
    tools.register(LoopTool(name="observe", tier=RiskTier.T0, call=observe))
    tools.register(LoopTool(name="decide", tier=RiskTier.T0, call=decide))
    tools.register(
        LoopTool(
            name="act",
            tier=RiskTier.T1,
            call=act,
            description="append one irreversible line to the drill's ground-truth ledger",
        )
    )
    tools.register(LoopTool(name="verify", tier=RiskTier.T0, call=verify))

    ctx = build_memory_context(
        instance_id=INSTANCE_ID,
        template=NAME,
        tools=tools,
        lease=FlockLease(workdir / LEASE_DIR),
        gate=ArtifactGate(clock=MemoryClock(), artifacts=[str(ledger)]),
        **overrides,
    )
    return ctx, backend


def run_tick(workdir: Path, kill_point: str) -> int:
    """Run exactly one tick. Returns the child's exit status when it survives."""
    from selfloop.runtime import run_once
    from selfloop.templates.observe_decide_act_verify import NAME

    ctx, backend = build_context(workdir, kill_point)
    try:
        report = run_once(ctx, NAME)
        append(
            ledger_path(workdir),
            {
                "event": TICK,
                "status": report.status.value,
                "resumed": report.resumed,
                "tick": report.tick,
                "detail": report.detail[:600],
                "pid": os.getpid(),
            },
        )
    finally:
        backend.close()
    return 0


def run_child(
    workdir: str | os.PathLike[str], kill_point: str, *, timeout_s: int = 180
) -> subprocess.CompletedProcess[str]:
    """Spawn one drill child and wait for it. **A fresh interpreter, every time.**

    Not ``multiprocessing``, and not a fork: a forked child inherits this
    process's open sqlite connection, its imported modules and its
    already-executed import side effects, so what it demonstrates about a cold
    start is nothing. The child gets the project root on ``PYTHONPATH`` and
    imports everything for itself, which is what a scheduler does.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(DRILL_PATH), str(workdir), kill_point],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=timeout_s,
        check=False,
    )


def main(argv: list[str]) -> int:
    """``<workdir> <kill_point>``. Validates its arguments before touching anything."""
    if len(argv) != 2:
        sys.stderr.write(f"usage: {DRILL_PATH.name} <workdir> <kill_point>\n")
        return 2
    workdir, kill_point = Path(argv[0]), argv[1]
    if kill_point not in KILL_POINTS:
        sys.stderr.write(f"unknown kill point {kill_point!r}; expected one of {KILL_POINTS}\n")
        return 2
    workdir.mkdir(parents=True, exist_ok=True)
    return run_tick(workdir, kill_point)


__all__ = [
    "DB_NAME",
    "DRILL_PATH",
    "EFFECT",
    "INSTANCE_ID",
    "KILL_AFTER_RECEIPT",
    "KILL_INSIDE_TOOL",
    "KILL_NONE",
    "KILL_POINTS",
    "KILL_STATUS",
    "LEDGER_NAME",
    "PROJECT_ROOT",
    "RECEIPT_COMPLETED",
    "SUBJECT_ID",
    "TICK",
    "KillAfterCompletion",
    "append",
    "build_context",
    "effects",
    "ledger_path",
    "main",
    "read_ledger",
    "run_child",
    "run_tick",
    "ticks",
]


# Last, so that importing this module for its constants and its ledger readers —
# which the parent test process does — binds every name above before anything can
# exit. As ``__main__`` the interpreter never reaches ``__all__``, which costs
# nothing: a script has no importers.
if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
