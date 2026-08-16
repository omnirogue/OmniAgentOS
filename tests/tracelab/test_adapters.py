"""Adapter tests over small synthetic fixtures in each real format."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.tracelab.adapters import ADAPTERS
from omniagentos.tracelab.adapters.claude_code import ADAPTER as CLAUDE_CODE
from omniagentos.tracelab.adapters.conversations import ADAPTER as CONVERSATIONS
from omniagentos.tracelab.adapters.openhands import ADAPTER as OPENHANDS
from omniagentos.tracelab.adapters.own_ledger import ADAPTER as OWN_LEDGER
from omniagentos.tracelab.adapters.pi_session import ADAPTER as PI_SESSION
from omniagentos.tracelab.adapters.swe_agent import ADAPTER as SWE_AGENT
from omniagentos.tracelab.events import EventKind, Outcome, Trace
from omniagentos.tracelab.metrics import compute_metrics
from omniagentos.tracelab.scan import scan_corpus


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


# ---------------------------------------------------------------- claude-code


def _claude_session(tmp_path: Path, with_limit_leaf: bool) -> Path:
    records: list[dict[str, object]] = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "s1",
            "timestamp": "2026-07-27T00:00:00Z",
            "message": {"role": "user", "content": "build the thing"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "s1",
            "timestamp": "2026-07-27T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-fable-5",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [
                    {"type": "text", "text": "on it"},
                    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "parentUuid": "a1",
            "sessionId": "s1",
            "timestamp": "2026-07-27T00:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": "No such file or directory",
                        "is_error": True,
                    }
                ],
            },
        },
        {"type": "queue-operation", "operation": "enqueue", "sessionId": "s1", "content": "hi"},
    ]
    if with_limit_leaf:
        records.append(
            {
                "type": "assistant",
                "uuid": "a2",
                "parentUuid": "u2",
                "sessionId": "s1",
                "message": {
                    "role": "assistant",
                    "model": "<synthetic>",
                    "content": [
                        {"type": "text", "text": "You've hit your session limit · resets 2:40pm"}
                    ],
                },
            }
        )
    return _write_jsonl(tmp_path / "abc-session.jsonl", records)


def test_claude_code_adapter(tmp_path: Path) -> None:
    path = _claude_session(tmp_path, with_limit_leaf=True)
    assert CLAUDE_CODE.sniff(path)
    traces = list(CLAUDE_CODE.iter_traces(path))
    assert len(traces) == 1
    trace = traces[0]
    kinds = [e.kind for e in trace.events]
    assert EventKind.TOOL_CALL in kinds and EventKind.TOOL_RESULT in kinds
    tool_result = next(e for e in trace.events if e.kind is EventKind.TOOL_RESULT)
    assert tool_result.is_error and tool_result.tool_name == "Bash"
    # synthetic session-limit leaf → labeled mechanical failure
    assert trace.outcome is Outcome.FAILURE
    assert trace.outcome_field == "synthetic_session_limit"


def test_claude_code_no_limit_is_unlabeled(tmp_path: Path) -> None:
    path = _claude_session(tmp_path, with_limit_leaf=False)
    (trace,) = CLAUDE_CODE.iter_traces(path)
    assert trace.outcome is Outcome.UNKNOWN


def test_claude_code_session_without_events_survives_scan(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "metadata-only.jsonl",
        [
            {
                "type": "system",
                "subtype": "turn_duration",
                "sessionId": "s1",
                "durationMs": 12,
            }
        ],
    )

    (trace,) = scan_corpus([path])

    assert trace.trace_id == "metadata-only"
    assert trace.dataset == "claude-code"
    assert trace.events == []
    assert trace.outcome is Outcome.UNKNOWN


def test_claude_code_non_session_without_events_is_ignored(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "not-a-session.jsonl",
        [{"type": "summary", "summary": "metadata without a session identifier"}],
    )

    assert list(CLAUDE_CODE.iter_traces(path)) == []
def test_claude_code_preserves_omitted_tool_result_error_flag(tmp_path: Path) -> None:
    records: list[dict[str, object]] = [
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": None,
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "e1", "name": "Bash", "input": {}},
                    {"type": "tool_use", "id": "u1", "name": "Bash", "input": {}},
                    {"type": "tool_use", "id": "s1", "name": "Bash", "input": {}},
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": "a1",
            "sessionId": "s1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "e1",
                        "content": "failed",
                        "is_error": True,
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "u1",
                        "content": "no status recorded",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "s1",
                        "content": "ok",
                        "is_error": False,
                    },
                ],
            },
        },
    ]
    path = _write_jsonl(tmp_path / "missing-flag.jsonl", records)

    (trace,) = CLAUDE_CODE.iter_traces(path)
    results = [event for event in trace.events if event.kind is EventKind.TOOL_RESULT]

    assert [event.is_error for event in results] == [True, None, False]
    before_explicit_success = Trace(
        trace_id=trace.trace_id,
        dataset=trace.dataset,
        source_path=trace.source_path,
        events=trace.events[:-1],
    )
    assert compute_metrics(before_explicit_success).errors_recovered == 0
    assert compute_metrics(trace).errors_recovered == 1


# ----------------------------------------------------------------- pi-session


def test_pi_session_adapter(tmp_path: Path) -> None:
    records: list[dict[str, object]] = [
        {"type": "session", "version": 3, "id": "sess", "timestamp": 1, "cwd": "/x"},
        {"type": "model_change", "provider": "ollama", "modelId": "kimi-k2.5:cloud"},
        {
            "type": "message",
            "id": "m1",
            "message": {"role": "user", "content": [{"type": "text", "text": "fix the bug"}]},
        },
        {
            "type": "message",
            "id": "m2",
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "usage": {"input": 100, "output": 20},
                "content": [
                    {"type": "text", "text": "running"},
                    {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"cmd": "ls"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "m3",
            "message": {
                "role": "toolResult",
                "toolCallId": "c1",
                "toolName": "bash",
                "content": [{"type": "text", "text": "ok"}],
            },
        },
        {"type": "compaction", "summary": "we did things"},
        {
            "type": "message",
            "id": "m4",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "prompt too long",
                "content": [],
            },
        },
    ]
    path = _write_jsonl(tmp_path / "2026-03-05T00-00-00_sess.jsonl", records)
    assert PI_SESSION.sniff(path)
    assert not CLAUDE_CODE.sniff(path)
    (trace,) = PI_SESSION.iter_traces(path)
    kinds = [e.kind for e in trace.events]
    assert kinds.count(EventKind.TOOL_CALL) == 1
    assert any(
        e.kind is EventKind.META and e.excerpt.startswith("compaction:") for e in trace.events
    )
    provider_errors = [e for e in trace.events if e.is_error and e.kind is EventKind.TOOL_RESULT]
    assert len(provider_errors) == 1 and "prompt too long" in provider_errors[0].excerpt
    bash_result = next(
        e for e in trace.events if e.kind is EventKind.TOOL_RESULT and e.tool_name == "bash"
    )
    assert bash_result.is_error is None


# ------------------------------------------------------------------ own-runs


def test_own_ledger_adapter(tmp_path: Path) -> None:
    records: list[dict[str, object]] = [
        {
            "run_id": "swr_1",
            "task_id": "btk_1",
            "state": "completed",
            "harness": {"harness": "swarm"},
            "usage": {"wall_ms": 1000},
            "extra": {
                "goal": "do the task",
                "score": 88.0,
                "attempts": [
                    {
                        "seq": 1,
                        "provider": "grok",
                        "model": "grok-4.5",
                        "tier": "simple",
                        "effort": "low",
                        "end_reason": "review_denied",
                    },
                    {
                        "seq": 2,
                        "provider": "openai",
                        "model": "gpt-5.6-sol",
                        "tier": "standard",
                        "effort": "xhigh",
                        "end_reason": "completed",
                    },
                ],
            },
        },
        {"run_id": "run_mock", "state": "completed", "harness": {"harness": "mock"}, "extra": {}},
    ]
    path = _write_jsonl(tmp_path / "runs-202607.jsonl", records)
    assert OWN_LEDGER.sniff(path)
    traces = list(OWN_LEDGER.iter_traces(path))
    assert len(traces) == 1  # mock filtered
    trace = traces[0]
    assert trace.outcome is Outcome.SUCCESS
    errors = [e for e in trace.events if e.is_error]
    assert len(errors) == 1 and errors[0].excerpt == "review_denied"


def test_own_ledger_missing_end_reason_is_not_successful(tmp_path: Path) -> None:
    records: list[dict[str, object]] = [
        {
            "run_id": "swr_missing_end_reason",
            "state": "failed",
            "harness": {"harness": "swarm"},
            "extra": {
                "attempts": [
                    {
                        "seq": 1,
                        "provider": "grok",
                        "model": "grok-4.5",
                    }
                ]
            },
        }
    ]
    path = _write_jsonl(tmp_path / "runs-202607.jsonl", records)

    (trace,) = OWN_LEDGER.iter_traces(path)
    result = next(event for event in trace.events if event.kind is EventKind.TOOL_RESULT)

    assert result.excerpt == "unknown"
    assert result.is_error
    assert compute_metrics(trace).n_tool_errors == 1


# ------------------------------------------------------- parquet-based formats


def test_swe_agent_adapter(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    trajectory = [
        {
            "role": "system",
            "system_prompt": "SETTING: you are an autonomous programmer",
            "text": None,
            "mask": False,
            "cutoff_date": None,
        },
        {
            "role": "ai",
            "system_prompt": None,
            "mask": True,
            "cutoff_date": None,
            "text": "Let's list files\n\n```\nls src\n```",
        },
        {
            "role": "user",
            "system_prompt": None,
            "mask": False,
            "cutoff_date": None,
            "text": "Directory src not found\n(Open file: n/a)\n(Current directory: /repo)\nbash-$",
        },
        {
            "role": "ai",
            "system_prompt": None,
            "mask": True,
            "cutoff_date": None,
            "text": "Try again\n\n```\nls\n```",
        },
        {
            "role": "user",
            "system_prompt": None,
            "mask": False,
            "cutoff_date": None,
            "text": "setup.py\n(Open file: n/a)\n(Current directory: /repo)\nbash-$",
        },
    ]
    table = pa.table(
        {
            "instance_id": ["repo__pkg-1"],
            "model_name": ["swe-agent-llama-70b"],
            "target": [False],
            "trajectory": [trajectory],
            "exit_status": ["submitted"],
            "eval_logs": ["...Some tests failed."],
        }
    )
    path = tmp_path / "train-00000-of-00001.parquet"
    pq.write_table(table, path)
    assert SWE_AGENT.sniff(path)
    (trace,) = SWE_AGENT.iter_traces(path)
    assert trace.outcome is Outcome.FAILURE and trace.outcome_field == "target"
    calls = [e for e in trace.events if e.kind is EventKind.TOOL_CALL]
    assert [c.excerpt for c in calls] == ["ls src", "ls"]
    results = [e for e in trace.events if e.kind is EventKind.TOOL_RESULT]
    assert results[0].is_error and not results[1].is_error
    # state footer stripped
    assert "Current directory" not in results[1].excerpt


def test_openhands_adapter(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    trajectory = [
        {
            "role": "system",
            "content": "You are OpenHands agent",
            "name": None,
            "tool_call_id": None,
            "tool_calls": None,
        },
        {
            "role": "user",
            "content": "<issue_description>fix it</issue_description>",
            "name": None,
            "tool_call_id": None,
            "tool_calls": None,
        },
        {
            "role": "assistant",
            "content": "I'll look",
            "name": None,
            "tool_call_id": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "execute_bash", "arguments": '{"command": "ls"}'},
                }
            ],
        },
        {
            "role": "tool",
            "content": "ERROR: No such file",
            "name": "execute_bash",
            "tool_call_id": "c1",
            "tool_calls": None,
        },
    ]
    table = pa.table(
        {
            "trajectory_id": ["chatcmpl-x"],
            "instance_id": ["Repo__pkg-2"],
            "trajectory": [trajectory],
            "exit_status": ["submit"],
            "resolved": [1],
        }
    )
    path = tmp_path / "trajectories.parquet"
    pq.write_table(table, path)
    assert OPENHANDS.sniff(path)
    (trace,) = OPENHANDS.iter_traces(path)
    assert trace.outcome is Outcome.SUCCESS and trace.outcome_field == "resolved"
    call = next(e for e in trace.events if e.kind is EventKind.TOOL_CALL)
    assert call.tool_name == "execute_bash"
    result = next(e for e in trace.events if e.kind is EventKind.TOOL_RESULT)
    assert result.is_error


def test_conversations_adapter_terminus(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    convo = [
        {"role": "user", "content": "You are an AI... task: create a file"},
        {
            "role": "assistant",
            "content": '<think>hmm</think>{"analysis": "a", "plan": "p", '
            '"commands": [{"keystrokes": "touch x\\n", "duration": 0.1}], '
            '"task_complete": false}',
        },
        {"role": "user", "content": "New Terminal Output:\nroot@abc:/app# touch x"},
    ]
    table = pa.table(
        {
            "conversations": [convo],
            "agent": ["terminus-2"],
            "model": ["gpt-5-nano"],
            "result": [None],
            "original_source": ["nl2bash"],
            "id": ["row-1"],
            "tools": [None],
        }
    )
    path = tmp_path / "train-00001-of-00038.parquet"
    pq.write_table(table, path)
    assert CONVERSATIONS.sniff(path)
    (trace,) = CONVERSATIONS.iter_traces(path)
    assert trace.dataset == "agenttrove"
    call = next(e for e in trace.events if e.kind is EventKind.TOOL_CALL)
    assert call.excerpt == "touch x\n"
    # second user turn became an observation
    assert trace.events[-1].kind is EventKind.TOOL_RESULT


def test_sniff_priority_no_cross_matches(tmp_path: Path) -> None:
    """Each fixture must match exactly one adapter (the intended one)."""
    claude = _claude_session(tmp_path, with_limit_leaf=False)
    matches = [a.name for a in ADAPTERS if a.sniff(claude)]
    assert matches == ["claude-code"]


def test_conversations_result_outcome_mapping() -> None:
    from omniagentos.tracelab.adapters.conversations import _result_outcome

    assert _result_outcome("") is Outcome.UNKNOWN
    assert _result_outcome("1.0") is Outcome.SUCCESS
    assert _result_outcome("0.0") is Outcome.FAILURE
    assert _result_outcome("AgentTimeoutError") is Outcome.FAILURE


def test_openhands_null_resolved_is_unknown_not_labeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ground-truth label is UNKNOWN — never SUCCESS or FAILURE.

    Counterfeit: `Outcome.SUCCESS if row.get("resolved") else Outcome.FAILURE`
    maps null/missing resolved onto FAILURE and pollutes labeled failure stats.
    """
    path = tmp_path / "trajectories.parquet"
    path.touch()
    row = {
        "trajectory_id": "chatcmpl-null",
        "instance_id": "Repo__pkg",
        "trajectory": [{"role": "user", "content": "fix it", "tool_calls": None}],
        "exit_status": "submit",
        "resolved": None,
    }
    monkeypatch.setattr(
        "omniagentos.tracelab.adapters.openhands.iter_parquet_rows",
        lambda *_a, **_k: iter([row]),
    )

    (trace,) = OPENHANDS.iter_traces(path)
    assert trace.outcome is Outcome.UNKNOWN
    assert trace.outcome_field == ""


def test_openhands_resolved_zero_is_still_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit resolved=0 must remain FAILURE (control against always-UNKNOWN)."""
    path = tmp_path / "trajectories.parquet"
    path.touch()
    row = {
        "trajectory_id": "chatcmpl-0",
        "instance_id": "Repo__pkg",
        "trajectory": [{"role": "user", "content": "fix it", "tool_calls": None}],
        "exit_status": "submit",
        "resolved": 0,
    }
    monkeypatch.setattr(
        "omniagentos.tracelab.adapters.openhands.iter_parquet_rows",
        lambda *_a, **_k: iter([row]),
    )

    (trace,) = OPENHANDS.iter_traces(path)
    assert trace.outcome is Outcome.FAILURE
    assert trace.outcome_field == "resolved"


def test_swe_agent_null_target_is_unknown_not_labeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing target bool is UNKNOWN — never forced into the failure bucket.

    Counterfeit: `Outcome.SUCCESS if row.get("target") else Outcome.FAILURE`.
    """
    path = tmp_path / "train-00000.parquet"
    path.touch()
    row = {
        "instance_id": "repo__pkg-1",
        "model_name": "x",
        "target": None,
        "trajectory": [{"role": "system", "system_prompt": "hi", "text": None}],
        "exit_status": "submitted",
    }
    monkeypatch.setattr(
        "omniagentos.tracelab.adapters.swe_agent.iter_parquet_rows",
        lambda *_a, **_k: iter([row]),
    )

    (trace,) = SWE_AGENT.iter_traces(path)
    assert trace.outcome is Outcome.UNKNOWN
    assert trace.outcome_field == ""


def test_swe_agent_target_false_is_still_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit target=False must remain FAILURE (control against always-UNKNOWN)."""
    path = tmp_path / "train-00000.parquet"
    path.touch()
    row = {
        "instance_id": "repo__pkg-1",
        "model_name": "x",
        "target": False,
        "trajectory": [{"role": "system", "system_prompt": "hi", "text": None}],
        "exit_status": "submitted",
    }
    monkeypatch.setattr(
        "omniagentos.tracelab.adapters.swe_agent.iter_parquet_rows",
        lambda *_a, **_k: iter([row]),
    )

    (trace,) = SWE_AGENT.iter_traces(path)
    assert trace.outcome is Outcome.FAILURE
    assert trace.outcome_field == "target"
