"""Deterministic per-trace metrics.

Every metric here is a mechanical count over the normalized event stream — no
model calls, no heuristic scoring. These are the independent variables the
hypothesis stage correlates against ground-truth outcomes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from omniagentos.tracelab.events import EventKind, Trace

#: Tool-result payloads at or above this size are counted as context bombs —
#: single results that measurably crowd out working context downstream.
GIANT_RESULT_CHARS = 20_000

#: Tool names whose invocation counts as a verification step. Lowercase
#: substring match against the tool name and command excerpt.
VERIFICATION_MARKERS = (
    "pytest",
    "unittest",
    "npm test",
    "cargo test",
    "go test",
    "make test",
    "ruff",
    "eslint",
    "mypy",
    "tsc",
    "lint",
    "compile",
    "build",
)


@dataclass(slots=True)
class TraceMetrics:
    """Mechanical signals for one trace."""

    trace_id: str
    dataset: str
    n_events: int = 0
    n_tool_calls: int = 0
    n_tool_errors: int = 0
    error_episodes: int = 0
    error_streak_max: int = 0
    repeat_call_max: int = 0
    giant_results: int = 0
    largest_result_chars: int = 0
    total_result_chars: int = 0
    verification_calls: int = 0
    errors_recovered: int = 0
    assistant_chars: int = 0
    outcome: str = "unknown"

    @property
    def tool_error_rate(self) -> float | None:
        if not self.n_tool_calls:
            return None
        return self.n_tool_errors / self.n_tool_calls

    @property
    def recovery_rate(self) -> float | None:
        """Recovered error *episodes* over total episodes (an episode is a
        maximal run of consecutive error results) — episodes recover, not
        individual errors, so this can genuinely reach 1.0."""
        if not self.error_episodes:
            return None
        return self.errors_recovered / self.error_episodes

    def to_row(self) -> dict[str, object]:
        row = asdict(self)
        tool_error_rate = self.tool_error_rate
        recovery_rate = self.recovery_rate
        row["tool_error_rate"] = (
            round(tool_error_rate, 4) if tool_error_rate is not None else None
        )
        row["recovery_rate"] = round(recovery_rate, 4) if recovery_rate is not None else None
        return row


def _call_signature(tool_name: str, excerpt: str) -> str:
    """Stable identity for spotting agents re-issuing the same call.

    Signs the full (already 400-char-capped) excerpt — truncating further made
    distinct calls sharing a long common prefix count as identical repeats."""
    return f"{tool_name}\x00{excerpt}"


#: Tools whose *arguments* are commands — only these get excerpt-marker
#: matching. A Read of "build/index.js" is not a verification step.
_EXEC_TOOLS = frozenset(
    {"bash", "powershell", "terminal", "execute_bash", "shell", "run_command", ""}
)


def _is_verification(tool_name: str, excerpt: str) -> bool:
    name = tool_name.lower()
    if any(marker in name for marker in VERIFICATION_MARKERS):
        return True
    if name not in _EXEC_TOOLS:
        return False
    return any(marker in excerpt.lower() for marker in VERIFICATION_MARKERS)


def compute_metrics(trace: Trace) -> TraceMetrics:
    """Single pass over the event stream computing all mechanical signals."""
    m = TraceMetrics(trace_id=trace.trace_id, dataset=trace.dataset, outcome=trace.outcome.value)
    m.n_events = len(trace.events)

    repeat_streak = 1
    prev_signature = ""
    # Streak/recovery state is tracked PER TOOL: harnesses issue parallel tool
    # calls whose results interleave (CALL A, CALL B, RESULT A, RESULT B), so
    # a trivial successful parallel call must not reset another tool's error
    # streak or count as its recovery. Sequential single-tool formats
    # (everything under one "bash") degenerate to the global behavior.
    error_streak_by_tool: dict[str, int] = {}
    pending_error_by_tool: dict[str, bool] = {}

    for event in trace.events:
        if event.kind is EventKind.ASSISTANT:
            m.assistant_chars += event.content_chars
        elif event.kind is EventKind.TOOL_CALL:
            m.n_tool_calls += 1
            signature = _call_signature(event.tool_name, event.excerpt)
            if signature == prev_signature:
                repeat_streak += 1
            else:
                repeat_streak = 1
            prev_signature = signature
            m.repeat_call_max = max(m.repeat_call_max, repeat_streak)
            if _is_verification(event.tool_name, event.excerpt):
                m.verification_calls += 1
        elif event.kind is EventKind.TOOL_RESULT:
            m.total_result_chars += event.content_chars
            m.largest_result_chars = max(m.largest_result_chars, event.content_chars)
            if event.content_chars >= GIANT_RESULT_CHARS:
                m.giant_results += 1
            tool = event.tool_name
            if event.is_error is True:
                m.n_tool_errors += 1
                streak = error_streak_by_tool.get(tool, 0)
                if streak == 0:
                    m.error_episodes += 1
                error_streak_by_tool[tool] = streak + 1
                pending_error_by_tool[tool] = True
                m.error_streak_max = max(m.error_streak_max, streak + 1)
            elif event.is_error is False:
                if pending_error_by_tool.get(tool):
                    m.errors_recovered += 1
                    pending_error_by_tool[tool] = False
                error_streak_by_tool[tool] = 0

    return m
