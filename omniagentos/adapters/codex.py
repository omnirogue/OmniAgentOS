"""Adapter for the OpenAI Codex CLI."""

from __future__ import annotations

import json
from typing import Any

from omniagentos.adapters.common import (
    CliAdapter,
    ParsedResponse,
    estimated_usage,
    requested_reasoning_effort,
    sandbox_level,
)
from omniagentos.contracts import AgentInput, AgentUsage


class CodexAdapter(CliAdapter):
    name = "codex"
    cli = "codex"
    supports_resume = True

    def _command(self, input: AgentInput, prompt: str, session_ref: str | None) -> list[str]:
        command = ["codex", "exec"]
        if session_ref is not None:
            command.extend(["resume", session_ref])
        # Reasoning effort: the caller-requested value (swarm's router-decided
        # effort rides metadata["reasoning_effort"], 048) wins; the historical
        # hardcoded "low" remains the default for every other caller.
        effort = requested_reasoning_effort(input) or "low"
        command.extend(
            [
                "--json",
                "-m",
                input.model or "gpt-5.6-luna",
                "-c",
                f'model_reasoning_effort="{effort}"',
                "--sandbox",
                "workspace-write" if sandbox_level(input) == "workspace_write" else "read-only",
                "--skip-git-repo-check",
                "-C",
                self._working_dir(input),
            ]
        )
        # Drive Access for projects (W4): codex's own --add-dir grants directories
        # "writable alongside the primary workspace" (codex exec --help), so this
        # is declared unconditionally alongside -C, same as the primary working
        # dir -- --sandbox is what actually governs read-only vs workspace-write
        # for all of them. See runner/core.py::_project_extra_dirs for the source.
        for extra_dir in input.metadata.get("extra_dirs") or []:
            command.extend(["--add-dir", str(extra_dir)])
        command.append("-")
        return command

    def _parse(self, stdout: str) -> ParsedResponse:
        thread_id: str | None = None
        text = ""
        completed_usage: dict[str, Any] = {}
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # CLI banner / plain-text log line interleaved with the JSONL
                # stream (observed live: codex prints a non-JSON first line and
                # the whole attempt crashed) -- noise, never fatal.
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                thread_id = event["thread_id"]
            elif event.get("type") == "item.completed":
                item = event.get("item")
                if (
                    isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and isinstance(item.get("text"), str)
                ):
                    text = item["text"]
            elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                completed_usage = event["usage"]
        return ParsedResponse(text=text, session_ref=thread_id, payload={"usage": completed_usage})

    def _usage(self, input: AgentInput, parsed: ParsedResponse, wall_ms: int) -> AgentUsage:
        usage = parsed.payload.get("usage")
        if (
            isinstance(usage, dict)
            and isinstance(usage.get("input_tokens"), int)
            and isinstance(usage.get("output_tokens"), int)
        ):
            return AgentUsage(
                wall_ms=wall_ms,
                turns=1,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cost_usd=None,
                estimated=True,
                source="mixed",
            )
        return estimated_usage(input, parsed.text, wall_ms)
