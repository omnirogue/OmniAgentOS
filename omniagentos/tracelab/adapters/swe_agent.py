"""Adapter for nebius/SWE-agent-trajectories parquet shards.

Row schema: instance_id, model_name, target (bool ground truth), trajectory
(list of {role, text, system_prompt, ...}), exit_status, generated_patch,
eval_logs. We stream rows and skip the two giant payload columns.

`ai` steps carry thought + a single fenced command block (the last triple-
backtick block). `user` steps are environment observations with a state
footer. Observations have no explicit error flag, so a small set of exact ACI
error markers (documented below) flags error results — this is scoped to tool
observations only, never prose.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from omniagentos.tracelab.adapters.base import TraceAdapter, iter_parquet_rows, parquet_columns
from omniagentos.tracelab.events import EventKind, Outcome, Trace, TraceEvent

_COLUMNS = ["instance_id", "model_name", "target", "trajectory", "exit_status"]
_FENCED = re.compile(r"```\n?(.*?)```", re.DOTALL)
_STATE_FOOTER = re.compile(r"\(Open file: [^)]*\)\s*\(Current directory: [^)]*\)\s*bash-\$\s*$")

#: Exact ACI/environment error markers observed in the corpus (see profile).
#: Deliberately NO bare "Error:"/"error:" substrings — file-view observations
#: render source code, and code that merely contains those strings would be
#: false-flagged (~14% of flags in a measured sample).
_OBSERVATION_ERROR_MARKERS = (
    "EXECUTION TIMED OUT",
    "Your proposed edit has introduced new syntax error",
    "Traceback (most recent call last)",
    "No such file or directory",
    "command not found",
    "not found\n",
    "E999",
)


def sniff(path: Path) -> bool:
    if path.suffix != ".parquet":
        return False
    columns = parquet_columns(path)
    return {"instance_id", "target", "trajectory", "eval_logs"} <= columns


def _observation_is_error(text: str) -> bool:
    head = text[:2000]
    return any(marker in head for marker in _OBSERVATION_ERROR_MARKERS)


def _target_outcome(value: object) -> tuple[Outcome, str]:
    """Map the ``target`` column to (outcome, outcome_field).

    Only an explicit bool is a label. Null/missing/unparseable is UNKNOWN —
    never forced into FAILURE via a truthiness check.
    """
    if not isinstance(value, bool):
        return Outcome.UNKNOWN, ""
    return (Outcome.SUCCESS if value else Outcome.FAILURE), "target"


def iter_traces(path: Path) -> Iterator[Trace]:
    for index, row in enumerate(iter_parquet_rows(path, _COLUMNS)):
        instance_id = str(row.get("instance_id") or f"row{index}")
        outcome, outcome_field = _target_outcome(row.get("target"))
        trace = Trace(
            trace_id=f"{path.stem}:{index}:{instance_id}",
            dataset="swe-agent",
            source_path=str(path),
            outcome=outcome,
            outcome_field=outcome_field,
            meta={
                "instance_id": instance_id,
                "model_name": str(row.get("model_name") or ""),
                "exit_status": str(row.get("exit_status") or ""),
            },
        )
        seq = 0
        for step in row.get("trajectory") or []:
            role = step.get("role")
            if role == "system":
                text = str(step.get("system_prompt") or "")
                trace.events.append(
                    TraceEvent(
                        seq=seq, kind=EventKind.SYSTEM, content_chars=len(text), excerpt=text
                    )
                )
                seq += 1
            elif role == "ai":
                text = str(step.get("text") or "")
                blocks = _FENCED.findall(text)
                command = blocks[-1].strip() if blocks else ""
                trace.events.append(
                    TraceEvent(
                        seq=seq,
                        kind=EventKind.ASSISTANT,
                        content_chars=len(text),
                        excerpt=text,
                        model=str(row.get("model_name") or ""),
                    )
                )
                seq += 1
                if command:
                    trace.events.append(
                        TraceEvent(
                            seq=seq,
                            kind=EventKind.TOOL_CALL,
                            tool_name="bash",
                            excerpt=command,
                            content_chars=len(command),
                        )
                    )
                    seq += 1
            elif role == "user":
                text = _STATE_FOOTER.sub("", str(step.get("text") or ""))
                trace.events.append(
                    TraceEvent(
                        seq=seq,
                        kind=EventKind.TOOL_RESULT,
                        tool_name="bash",
                        content_chars=len(text),
                        excerpt=text,
                        is_error=_observation_is_error(text),
                    )
                )
                seq += 1
        if trace.events:
            yield trace


ADAPTER = TraceAdapter(name="swe-agent", sniff=sniff, iter_traces=iter_traces)
