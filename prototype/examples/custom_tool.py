"""The four extension points, as four edits to one file.

Everything you will ever add to ``selfloop`` is one of these, and none of them
requires touching the package: a **tool** (something that acts), a **port
adapter** (something the runtime talks to), a **signal source** (a new kind of
failure the loop can notice), and a **template** (a new shape of tick). There is
no plugin system, no entry-point scan and no directory that gets walked — every
one of them is a value you construct and hand over.

Run it as a script to see the tick, or drive it from the CLI, which reaches all
four through :func:`build_context` at the bottom::

    PYTHONPATH=examples python -m selfloop tick --tools custom_tool \\
        --instance notes-1 --template note_to_file --accept-inprocess-lease
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from selfloop.adapters.memory import MemoryClock, build_memory_context
from selfloop.context import LoopContext
from selfloop.contracts import (
    LearningSignal,
    LoopStatus,
    LoopTool,
    RecordKind,
    RiskTier,
    ToolRegistry,
    digest_key,
)
from selfloop.engine import END, CompiledGraph, Graph
from selfloop.gates import ArtifactGate
from selfloop.kit import add_effect, ensure_park, merge_data
from selfloop.runtime import run_once
from selfloop.templates import TEMPLATES, LoopTemplate, register_template

#: Where the tool below writes. A module constant with an environment override
#: rather than a temporary directory made at import: importing this file must
#: touch nothing, which is the same promise ``import selfloop`` makes, and it is
#: what lets the CLI import it to find :func:`build_context`.
OUT = Path(os.environ.get("SELFLOOP_CUSTOM_OUT", "selfloop-custom-note.txt"))

# --- 1. A NEW TOOL ---------------------------------------------------------
# A plain callable plus a tier and, when it matters, external evidence that it
# took effect. `verify` must look somewhere the tool does not control: the
# tool's own return value is its account of itself, and this package never
# treats an actor's narrative as a verdict.


def write_note(*, text: str, path: str) -> dict[str, Any]:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")
    return {"path": path, "ok": True}


def note_exists(result: Any, args: Mapping[str, Any]) -> bool:
    del result  # deliberately not read: the tool said ok, so ask the filesystem
    return Path(str(args["path"])).is_file()


NOTE_TOOL = LoopTool(
    name="write_note",
    tier=RiskTier.T1,  # T2+ would park this effect for a human on every tick
    call=write_note,
    verify=note_exists,
    description="write one note to disk",
)


# --- 2. A NEW PORT ADAPTER -------------------------------------------------
# Implement the Protocol in selfloop.ports; nothing subclasses anything. This
# Notifier returns True ONLY on confirmed delivery, which is the port's one
# semantic obligation: recording a page that never went out turns a transient
# outage into a permanently unpaged approval, because that recorded event is the
# dedupe key that stops the next tick paging again.


class FileNotifier:
    """A :class:`~selfloop.ports.Notifier` that appends pages to a file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def page(self, *, approval_id: str, summary: str, deep_link: str) -> bool:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{approval_id}\t{summary}\t{deep_link}\n")
        except OSError:
            return False  # not delivered, and saying so is the whole contract
        return True


# --- 3. A NEW SIGNAL SOURCE ------------------------------------------------
# The extension point of the learning loop: append one of these to
# ctx.signal_sources and the loop notices a new kind of failure. No port change,
# no schema change, no migration — the signals land in the kind-generic
# RecordStore.
#
# Three obligations, all honoured below: read the DURABLE RECORD after the fact
# (never a hot-path hook, which would observe a tick that has not been graded);
# never derive a signal from a NEUTRAL outcome (idle and parked ticks are
# working as designed); and never derive one from the actor's own prose.


class ReconciliationSignals:
    """Every effect a human had to go and reconcile is a lesson waiting.

    A reconciliation exists only because a machine could not establish what
    happened to an effect and a person went and looked. That is expensive,
    recurring and fixable — exactly what a lesson is for — and it is a fact in
    the ledger rather than anything a run said about itself.
    """

    name = "reconciliations"

    def extract(self, ctx: LoopContext, *, since_cursor: int) -> Iterator[LearningSignal]:
        del since_cursor  # reconciliations carry no event cursor; ids are content-stable
        for row in ctx.records.query(RecordKind.RECONCILIATION.value):
            yield LearningSignal(
                id=f"sig_recon_{digest_key(row['id'])[:16]}",
                scope=str(row.get("template") or "unscoped"),
                failure_tag="effect_state_unknown",
                text=f"{row.get('receipt_key')}: {row.get('note') or row.get('outcome')}",
                run_id="",  # extract() attributes it to the run whose pass mines it
                cursor=0,
            )


# --- 4. A NEW TEMPLATE -----------------------------------------------------
# A named graph builder. `add_effect` adds `<name>_gate` AND `<name>` together
# and returns the GATE's name, so there is no way to wire an ungated effect —
# not by forgetting, not by refactoring.

NOTE_TEMPLATE = "note_to_file"


def build_note_graph(ctx: LoopContext) -> CompiledGraph:
    graph = Graph()
    ensure_park(graph, ctx)
    gate = add_effect(
        graph,
        ctx,
        name="write",
        tool="write_note",
        args_fn=lambda s: {
            "text": str((s.get("params") or {}).get("text") or "hello"),
            "path": str(OUT),
        },
        # The business key is the IDENTITY of the act, never a random id: two
        # ticks that mean the same note are one effect, and the second is
        # short-circuited by the first one's receipt.
        key_fn=lambda s: digest_key(NOTE_TEMPLATE, (s.get("params") or {}).get("text")),
        on_result=lambda s, r: {**merge_data(s, note=r), "status": LoopStatus.COMPLETED.value},
    )
    graph.set_entry(gate)
    graph.add_edge("write", END)
    return graph.compile()


if NOTE_TEMPLATE not in TEMPLATES:  # importable twice without a duplicate-name refusal
    register_template(
        LoopTemplate(
            name=NOTE_TEMPLATE,
            family="observe_act",
            required_tools=("write_note",),
            build=build_note_graph,
            description="write one note, gated and receipted",
        )
    )


# --- Putting the four together --------------------------------------------


def build_context(*, instance_id: str, template: str, params: Mapping[str, Any]) -> LoopContext:
    """The one function ``selfloop.cli`` calls. See :mod:`selfloop.cli`.

    Everything the CLI refuses to decide for you is decided here: which tools
    this instance is granted, which store it writes to, which lease excludes a
    second process, and what independently verifies the work. Swapping the
    in-memory adapters for the durable ones is five keywords, not a rewrite.
    """
    tools = ToolRegistry()
    tools.register(NOTE_TOOL)
    clock = MemoryClock()
    ctx = build_memory_context(
        instance_id=instance_id,
        template=template,
        tools=tools,
        clock=clock,
        params=dict(params),
        # A GateRunner is a port too, and this one is real: it looks at the file
        # on disk and nothing else. The default in build_memory_context rules
        # nothing, which settles every tick uncorroborated — visible, honest, and
        # unable to learn, because the learning pass mines no neutral outcome.
        gate=ArtifactGate(clock=clock, artifacts=[OUT]),
        notifier=FileNotifier(OUT.with_name("pages.tsv")),
        scope_tiers={NOTE_TEMPLATE: RiskTier.T1},
        remedy_table={"effect_state_unknown": "include: an idempotency key in every send"},
    )
    ctx.signal_sources.append(ReconciliationSignals())
    return ctx


if __name__ == "__main__":
    ctx = build_context(
        instance_id="notes-1", template=NOTE_TEMPLATE, params={"text": "learned something"}
    )
    report = run_once(ctx, NOTE_TEMPLATE, params={"text": "learned something"})
    print(report.as_dict())
    print(f"{OUT}: {OUT.read_text() if OUT.exists() else '(nothing written)'}")


__all__ = [
    "NOTE_TEMPLATE",
    "NOTE_TOOL",
    "FileNotifier",
    "ReconciliationSignals",
    "build_context",
    "build_note_graph",
]
