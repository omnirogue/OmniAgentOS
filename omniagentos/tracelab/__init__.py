"""Tracelab — empirical mining of agent traces into testable improvement hypotheses.

Tracelab replaces the ad-hoc AI-Traces scripts with a repeatable, evidence-first
pipeline:

1. **Adapters** parse heterogeneous trace formats (Claude Code session JSONL,
   SWE-agent / OpenHands trajectories, generic chat records, our own ledger and
   session telemetry) into one unified :class:`~omniagentos.tracelab.events.Trace`
   event stream.
2. **Metrics** compute deterministic per-trace signals (tool-error streaks,
   repeated-command loops, context growth, verification presence, recovery).
3. **Mining** aggregates metrics across a corpus and — where ground-truth
   outcome labels exist — contrasts successful vs failed traces.
4. **Hypotheses** turn statistically grounded contrasts into `proposed` rows
   shaped for the `configtest_hypotheses` improvement lane (migration 083),
   each carrying machine-checkable evidence pointers back to real traces.

Every claim the pipeline emits cites trace IDs and event offsets; nothing is
generated from canned text. Run via ``python -m omniagentos.tracelab --help``.
"""

from omniagentos.tracelab.events import Trace, TraceEvent

__all__ = ["Trace", "TraceEvent"]
