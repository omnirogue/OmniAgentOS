"""Adapter for the xAI Grok subscription CLI."""

from __future__ import annotations

import json

from omniagentos.adapters.common import (
    CliAdapter,
    ParsedResponse,
    estimated_usage,
    extract_trailing_json,
    requested_reasoning_effort,
    sandbox_level,
)
from omniagentos.contracts import AgentInput, AgentUsage


class GrokAdapter(CliAdapter):
    name = "grok"
    cli = "grok"
    supports_resume = True

    def _command(self, input: AgentInput, prompt: str, session_ref: str | None) -> list[str]:
        # Drive Access for projects (W4): as of this CLI version `grok --help` has
        # no multi-directory allowlist flag (only the single --cwd below) -- unlike
        # claude/codex/gemini/kimi's --add-dir/--include-directories, there is no
        # way to hand grok a granted extra dir (input.metadata["extra_dirs"], see
        # runner/core.py::_project_extra_dirs). Intentionally not wired here; a
        # scoped grok run is confined to --cwd until the CLI adds one.
        command = [
            "grok",
            "-p",
            prompt,
            "--output-format",
            "json",
            # grok's built-in profiles are off/workspace/devbox/read-only/strict
            # (~/.grok/docs/user-guide/18-sandbox.md). "workspace-write" is codex
            # vocabulary and made every launch refuse to start (live: "Custom
            # sandbox profile 'workspace-write' not found ... refusing to start",
            # 6 failed sessions 2026-07-23).
            "--sandbox",
            "workspace" if sandbox_level(input) == "workspace_write" else "read-only",
            "--cwd",
            self._working_dir(input),
        ]
        # Reasoning effort (048): `grok --help` documents `--reasoning-effort
        # <EFFORT>` (alias --effort). Only declared when the caller requested
        # one (swarm threads the router-decided effort through
        # metadata["reasoning_effort"]); otherwise the CLI default stands —
        # byte-for-byte unchanged argv for every existing caller.
        effort = requested_reasoning_effort(input)
        if effort is not None:
            command.extend(["--reasoning-effort", effort])
        if session_ref is None:
            command.extend(["--model", input.model or "grok-4.5"])
        else:
            command.extend(["--resume", session_ref])
        return command

    def _parse(self, stdout: str) -> ParsedResponse:
        # Strict parse first; under provider-exec's merged stderr/stdout pipe a
        # clean rc=0 stream may carry warning noise before the envelope, so fall
        # back to the last balanced envelope-shaped JSON object (see
        # extract_trailing_json — success envelope keys required, prose JSON
        # rejected; a success envelope beats a later stray error blob, and an
        # error-only stream surfaces the last "error"-keyed object so its real
        # message raises below instead of "no JSON envelope").
        try:
            decoded: object = json.loads(stdout)
        except json.JSONDecodeError:
            decoded = extract_trailing_json(stdout, envelope_keys=("text", "sessionId"))
            if decoded is None:
                raise ValueError("Grok output contained no JSON envelope") from None
        if not isinstance(decoded, dict):
            raise ValueError("Grok JSON envelope is not an object")
        error = decoded.get("error")
        if error is not None and not isinstance(decoded.get("text"), str):
            message = error.get("message") if isinstance(error, dict) else error
            raise ValueError(str(message or "Grok CLI returned an error envelope"))
        text = decoded.get("text")
        if not isinstance(text, str):
            raise ValueError("Grok JSON envelope did not contain a text string")
        # A known-bad stopReason (e.g. "cancelled" from hitting --max-turns)
        # means the envelope's .text is a narration stub and the substantive
        # reasoning — including a verdict line — is stranded in .thought
        # (see pipeline/ROUTING.md:66-69). Fail loudly rather than let a
        # truncated run read as a completed one. A MISSING stopReason is
        # tolerated on purpose (older/other envelope shapes) so the guard
        # only fires on a known-bad value and a shape change cannot silently
        # disable it. Raising reuses the existing infra-failover signal
        # (tests/swarm/test_reviewer_infra_failover.py:146-149 treats a
        # _parse ValueError as an infra failure, not a bad verdict).
        stop_reason = decoded.get("stopReason")
        if isinstance(stop_reason, str) and stop_reason != "end_turn":
            num_turns = decoded.get("num_turns")
            raise ValueError(
                f"Grok run did not complete: stopReason={stop_reason!r}"
                f"{f' at num_turns={num_turns}' if num_turns is not None else ''}; "
                f".text is a {len(text)}-char stub and the reasoning may be "
                "stranded in .thought (see pipeline/ROUTING.md:66-69)"
            )
        session_ref = decoded.get("sessionId")
        return ParsedResponse(
            text=text,
            session_ref=session_ref if isinstance(session_ref, str) else None,
            payload=decoded,
        )

    def _usage(self, input: AgentInput, parsed: ParsedResponse, wall_ms: int) -> AgentUsage:
        return estimated_usage(input, parsed.text, wall_ms)
