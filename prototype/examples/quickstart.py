"""The five-minute promise, executable: ``python examples/quickstart.py``.

No API key, no config file, no database, no network. One tool you write, three
ticks, and a lesson at the end that did not exist at the start.

**These are the production defaults.** ``min_support`` is 2, the promotion
threshold is untouched, the gate is real. Nothing here is a demo knob, because a
quickstart that greens by lowering a bar teaches the bar.

The arithmetic, which the script prints as it goes: a lesson needs
``min_support`` DISTINCT RUNS of evidence before it may be promoted, and a
promoted lesson can only change a run that starts AFTER it — so the minimum is
``min_support + 1 = 3`` ticks. Ticks 1 and 2 each contribute one graded failure;
tick 3 is the first run the lesson is in front of.

What makes this honest rather than a puppet show: **the loop's own evaluator is
satisfied on every one of the three ticks.** It scores the draft 1.0 and the
tick reports COMPLETED. What disagrees is the gate — which never sees the
prompt, never sees the score, and only looks at the file on disk. Twice it rules
that the published notes are too short to be release notes; that contradiction
is the evidence, and the third tick's file is bigger because of a rule the loop
promoted from it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from selfloop import LoopTool, RiskTier, ToolRegistry, run_once
from selfloop.adapters.memory import MemoryClock, build_memory_context
from selfloop.gates import ArtifactGate
from selfloop.learn import recall
from selfloop.runtime import TAG_GATE_CONTRADICTED
from selfloop.templates.propose_evaluate_promote import NAME, default_tools

SCOPE = "release-notes"

#: The artifact this loop exists to produce, written into your working
#: directory. Named as a constant rather than derived from ``__file__``, and
#: created only when the tool runs: importing this module touches nothing,
#: exactly as importing ``selfloop`` does not. Override it with
#: ``SELFLOOP_QUICKSTART_NOTES`` if you would rather it went elsewhere.
NOTES = Path(os.environ.get("SELFLOOP_QUICKSTART_NOTES", "quickstart-release-notes.md"))


def publish(*, candidate: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    """THE ONE TOOL YOU WRITE. It puts something in the world; everything else reads.

    A plain function with no idea that selfloop exists — which is the point:
    this runtime never reimplements a connector and never handles a credential.
    """
    del evaluation
    NOTES.parent.mkdir(parents=True, exist_ok=True)
    NOTES.write_text(str(candidate.get("text") or ""), encoding="utf-8")
    return {"path": str(NOTES), "bytes": NOTES.stat().st_size}


def wrote_the_file(result: Any, args: dict[str, Any]) -> bool:
    """Independent evidence that ``publish`` did what it said. Not its return value."""
    del args
    return Path(str((result or {}).get("path") or "")).is_file()


def main() -> None:
    # A previous run of this script leaves a file that already satisfies the
    # gate, so tick 1 would pass, nothing adverse would be recorded, and there
    # would be no evidence to learn from. Starting from nothing is what a loop's
    # first tick actually looks like.
    NOTES.unlink(missing_ok=True)

    clock = MemoryClock()
    tools = ToolRegistry()
    for shipped in default_tools():  # plain-Python propose + evaluate, no model
        tools.register(shipped)
    tools.register(
        LoopTool(name="promote", tier=RiskTier.T1, call=publish, verify=wrote_the_file)
    )

    ctx = build_memory_context(
        instance_id="quickstart",
        template=NAME,
        tools=tools,
        clock=clock,
        # The independent verifier. It reads a file and nothing else — no prompt,
        # no score. 60 bytes is the publishing rule: notes shorter than that are
        # a stub, not release notes.
        gate=ArtifactGate(clock=clock, artifacts=[NOTES], min_bytes=60),
        params={
            "scope": SCOPE,
            "brief": "Draft this week's release notes.",
            "spec": {"must_include": ["breaking-changes"]},
        },
        # T1 = a reversible local rule, so a lesson for this scope promotes on
        # evidence. Leave a scope undeclared and its lessons park for a human.
        scope_tiers={SCOPE: RiskTier.T1},
        # What an operator wrote down about this failure. The loop supplies the
        # noticing, never the prose: a model may not author what gets injected.
        remedy_table={TAG_GATE_CONTRADICTED: "include: upgrade-steps, rollback-plan, checksums"},
    )

    print(f"min_support={ctx.min_support}, so the minimum is min_support + 1 = "
          f"{ctx.min_support + 1} ticks: {ctx.min_support} of evidence, then one that uses it.\n")

    for tick in range(1, ctx.min_support + 2):
        report = run_once(ctx, NAME)
        size = NOTES.stat().st_size if NOTES.exists() else 0
        news = report.detail.split(";")[0] or "the gate agreed"
        print(f"tick {tick}: {report.status.value:9} ({report.outcome}) — {size} bytes on disk")
        print(f"         {news}")
        clock.advance(3600)  # in production each tick is a separate process, later

    print("\nWhat the loop learned, from its own graded runs and nothing else:")
    promoted = recall(ctx, SCOPE)
    if not promoted:
        # Printed rather than swallowed. A quickstart that says nothing when the
        # cycle did not close is a quickstart that can be broken without anyone
        # noticing — which is the failure this whole package is about.
        print("  nothing was promoted; run `selfloop lessons` to see what is staged and why")
    for lesson in promoted:
        print(f"  {lesson.guidance}")
        print(f"  admitted on {lesson.support} distinct runs; used {lesson.used}x, "
              f"helped {lesson.helped}x")

    print(f"\n{NOTES}:\n{NOTES.read_text()}")


if __name__ == "__main__":
    main()
