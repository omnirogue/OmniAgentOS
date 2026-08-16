"""Adapter for the Alibaba Qwen Code CLI.

Qwen Code's headless ``--output-format json`` mode emits one JSON array at
exit.  The array contains system/assistant events followed by a terminal
``type: "result"`` event carrying the response text, session id, turn count,
and provider-reported token usage.

The CLI has an honest read-only mode: ``--approval-mode plan`` disables file
changes and command execution.  Workspace-write runs use ``auto-edit`` so
headless workers can edit files without granting unattended shell execution;
the shared outer Seatbelt profile remains the filesystem enforcement layer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from omniagentos.adapters.common import (
    CliAdapter,
    ParsedResponse,
    estimated_usage,
    sandbox_level,
)
from omniagentos.contracts import AgentInput, AgentUsage


def _resolve_qwen_cli(*, home: Path | None = None) -> str:
    """Resolve the installed Qwen executable without relying on daemon PATH."""

    override = os.environ.get("OMNIAGENTOS_QWEN_CLI", "").strip()
    if override:
        return override

    preferred = (home or Path.home()) / ".local" / "bin" / "qwen"
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return str(preferred)
    return "qwen"


def _decode_message_array(stdout: str) -> list[dict[str, Any]]:
    """Decode Qwen's JSON array, tolerating warnings on a merged output stream."""

    try:
        decoded: object = json.loads(stdout)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        decoded = None
        index = 0
        while True:
            start = stdout.find("[", index)
            if start == -1:
                break
            try:
                candidate, end = decoder.raw_decode(stdout, start)
            except json.JSONDecodeError:
                index = start + 1
                continue
            if (
                isinstance(candidate, list)
                and any(
                    isinstance(event, dict) and event.get("type") == "result"
                    for event in candidate
                )
            ):
                decoded = candidate
            index = max(end, start + 1)

    if not isinstance(decoded, list):
        raise ValueError("Qwen output contained no JSON message array")
    messages = [event for event in decoded if isinstance(event, dict)]
    if not messages:
        raise ValueError("Qwen JSON message array was empty")
    return messages


class QwenAdapter(CliAdapter):
    name = "qwen"
    cli = "qwen"
    supports_resume = True

    def __init__(self) -> None:
        super().__init__()
        self.cli = _resolve_qwen_cli()

    def _command(self, input: AgentInput, prompt: str, session_ref: str | None) -> list[str]:
        approval_mode = (
            "auto-edit" if sandbox_level(input) == "workspace_write" else "plan"
        )
        command = [
            self.cli,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--approval-mode",
            approval_mode,
        ]
        if session_ref is not None:
            command.extend(["--resume", session_ref])
        elif input.model:
            command.extend(["--model", input.model])
        if input.budget.max_turns is not None:
            command.extend(["--max-session-turns", str(input.budget.max_turns)])
        for extra_dir in input.metadata.get("extra_dirs") or []:
            command.extend(["--include-directories", str(extra_dir)])
        return command

    def _stdin_payload(self, prompt: str) -> None:
        # Qwen appends ``-p`` to stdin when both exist. The prompt is already in
        # argv, so sending it again would duplicate every adapter-direct request.
        del prompt
        return None

    def _parse(self, stdout: str) -> ParsedResponse:
        messages = _decode_message_array(stdout)
        result = next(
            (
                event
                for event in reversed(messages)
                if event.get("type") == "result"
            ),
            None,
        )
        if result is None:
            raise ValueError("Qwen JSON output did not contain a result event")
        if result.get("is_error") is True or result.get("subtype") not in (
            None,
            "success",
        ):
            error = result.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            raise ValueError(str(message or "Qwen CLI returned an error result"))
        text = result.get("result")
        if not isinstance(text, str):
            raise ValueError("Qwen result event did not contain a result string")
        session_ref = result.get("session_id")
        return ParsedResponse(
            text=text,
            session_ref=session_ref if isinstance(session_ref, str) else None,
            payload=result,
        )

    def _usage(self, input: AgentInput, parsed: ParsedResponse, wall_ms: int) -> AgentUsage:
        usage = parsed.payload.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if (
                isinstance(input_tokens, int)
                and not isinstance(input_tokens, bool)
                and isinstance(output_tokens, int)
                and not isinstance(output_tokens, bool)
            ):
                turns = parsed.payload.get("num_turns")
                return AgentUsage(
                    wall_ms=wall_ms,
                    turns=(
                        turns
                        if isinstance(turns, int)
                        and not isinstance(turns, bool)
                        and turns >= 0
                        else 1
                    ),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=None,
                    estimated=True,
                    source="mixed",
                )
        return estimated_usage(input, parsed.text, wall_ms)
