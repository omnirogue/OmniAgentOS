"""Adapter for the Google Gemini subscription CLI.

`gemini -p ... -o json` prints one pretty-printed JSON object at exit:
`{"session_id": ..., "response": ..., "stats": {...}}` (or `{"error": {...}}`
on failure — see `JsonFormatter.format`/`formatError` in @google/gemini-cli-core).
`stats` (from `StreamJsonFormatter.convertToStreamStats`) reports real token
counts (`input_tokens`/`output_tokens`) but no cost, so usage is tokens-EXACT /
cost-ESTIMATED like codex, not fully estimated like grok.

`--approval-mode default` is used deliberately (never `auto_edit`/`yolo`) so the
CLI never auto-approves destructive tool calls in headless runs; the base
CliAdapter timeout (budget.wall_ms_max, default 300s) is the safety net if a
run stalls waiting on an approval headless mode cannot grant.

`session_id` is a UUID accepted directly by `--resume` (`SessionSelector.
resolveSession` matches a full UUID before falling back to a 1-based index —
see utils/sessionUtils.js), so resume-by-id is safe to wire up.
"""

from __future__ import annotations

import json

from omniagentos.adapters.common import (
    CliAdapter,
    ParsedResponse,
    estimated_usage,
    extract_trailing_json,
)
from omniagentos.contracts import AgentInput, AgentUsage


class GeminiAdapter(CliAdapter):
    name = "gemini"
    cli = "gemini"
    supports_resume = True

    def _command(self, input: AgentInput, prompt: str, session_ref: str | None) -> list[str]:
        # Reasoning effort (048): as of this CLI version `gemini --help` has no
        # effort/thinking knob, so a caller-requested metadata["reasoning_effort"]
        # is deliberately NOT threaded into argv (the swarm attempt row still
        # records the decided effort; the CLI just runs at its own default).
        command = [
            "gemini",
            "-p",
            prompt,
            "-o",
            "json",
            # auto_edit (not `default`): headless sessions have no approver, so
            # `default` silently kills every edit tool and workers can only
            # answer in text (drill-proven). auto_edit approves EDIT tools only;
            # shell stays gated, and the per-spawn Seatbelt profile + the
            # non-claude risk-class pin remain the enforcement layers.
            "--approval-mode",
            "auto_edit",
        ]
        if session_ref is None:
            # gemini-3-pro was shut down 2026-03-09; Google's migration note points
            # at 3.1 Pro, which is still the newest Pro (no 3.5/3.6 Pro exists).
            command.extend(["-m", input.model or "gemini-3.1-pro-preview"])
        else:
            command.extend(["--resume", session_ref])
        # Drive Access for projects (W4): gemini's --include-directories adds
        # directories to the workspace (repeatable); this adapter has no
        # sandbox-level gate of its own (see the module docstring on the
        # deliberately-conservative --approval-mode), so every granted extra
        # dir is always declared. See runner/core.py::_project_extra_dirs.
        for extra_dir in input.metadata.get("extra_dirs") or []:
            command.extend(["--include-directories", str(extra_dir)])
        return command

    def _parse(self, stdout: str) -> ParsedResponse:
        # Strict parse first; under provider-exec's merged stderr/stdout pipe a
        # clean rc=0 stream carries stderr noise (gemini emits plenty — observed
        # live: credential/log lines, sandboxed EPERM error-report writes) before
        # the envelope, so fall back to the last balanced envelope-shaped JSON
        # object (see extract_trailing_json — success envelope keys required,
        # prose JSON rejected). A success envelope beats any stray error blob
        # dumped after it; only when NO success-shaped object exists does the
        # last "error"-keyed object surface so its real message still raises
        # below instead of "no JSON envelope".
        try:
            decoded: object = json.loads(stdout)
        except json.JSONDecodeError:
            decoded = extract_trailing_json(stdout, envelope_keys=("response", "session_id"))
            if decoded is None:
                raise ValueError("Gemini output contained no JSON envelope") from None
        if not isinstance(decoded, dict):
            raise ValueError("Gemini JSON envelope is not an object")
        error = decoded.get("error")
        if isinstance(error, dict):
            raise ValueError(str(error.get("message") or "Gemini CLI returned an error envelope"))
        response = decoded.get("response")
        if not isinstance(response, str):
            raise ValueError("Gemini JSON envelope did not contain a response string")
        session_ref = decoded.get("session_id")
        return ParsedResponse(
            text=response,
            session_ref=session_ref if isinstance(session_ref, str) else None,
            payload=decoded,
        )

    def _usage(self, input: AgentInput, parsed: ParsedResponse, wall_ms: int) -> AgentUsage:
        stats = parsed.payload.get("stats")
        if (
            isinstance(stats, dict)
            and isinstance(stats.get("input_tokens"), int)
            and isinstance(stats.get("output_tokens"), int)
        ):
            return AgentUsage(
                wall_ms=wall_ms,
                turns=1,
                input_tokens=stats["input_tokens"],
                output_tokens=stats["output_tokens"],
                cost_usd=None,
                estimated=True,
                source="mixed",
            )
        return estimated_usage(input, parsed.text, wall_ms)
