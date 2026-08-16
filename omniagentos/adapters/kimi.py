"""Adapter for the Moonshot AI Kimi subscription CLI.

The `kimi -p ... --output-format stream-json` writer emits newline-delimited
JSON: a `role: "assistant"` line per flushed text chunk (interleaved with tool
calls), and a trailing `role: "meta", type: "session.resume_hint"` line that
carries the session id kimi itself recommends for `kimi -r <id>` (see
`resolveNativeSession`/`writeResumeHint` in the CLI bundle). Kimi's headless
`-p` mode always forces permission mode to "auto" internally (`forceAuto()`)
regardless of any flag we pass, and `--plan` is rejected outright when combined
with `-p` ("Cannot combine --prompt with --plan."), so there is no CLI-level
read-only guard available in headless mode — unlike claude/codex/grok this
adapter cannot honor a read_only sandbox level via argv.

Transport (E2BIG guard 2026-07-31): kimi -p only accepts the prompt via argv,
which fails with E2BIG when prompts exceed macOS ARG_MAX (1,048,576 bytes).
The adapter guards before spawn: if encoded prompt + env + argv overhead
exceeds 700KB (conservative headroom under ARG_MAX), it raises a clear
"transport budget exceeded" error, never letting malformed large prompts
reach posix_spawn. The evidence capping in the reflection stage keeps
prompts under 512KB, and this guard catches edge cases.
"""

from __future__ import annotations

import json

from omniagentos.adapters.common import CliAdapter, ParsedResponse, estimated_usage
from omniagentos.contracts import AgentInput, AgentUsage

# Pre-spawn guard: reject prompts that would exceed macOS ARG_MAX (E2BIG guard).
# Conservative budget accounting for argv array overhead and environment size.
_PROMPT_BYTE_BUDGET = 700_000  # 700KB headroom under ARG_MAX 1,048,576


class KimiAdapter(CliAdapter):
    name = "kimi"
    cli = "kimi"
    supports_resume = True
    # Headless `kimi -p` forces permission mode to "auto" (forceAuto()) regardless
    # of flags, so it cannot honor a read-only sandbox level -- it is refused for
    # unattended workspace_write steps unless a human elevates it (AC-policy #B1).
    honors_read_only_sandbox = False

    def _command(self, input: AgentInput, prompt: str, session_ref: str | None) -> list[str]:
        # Pre-spawn guard: reject prompts that would cause E2BIG (2026-07-31).
        # Measure the encoded prompt bytes; if it exceeds our budget, raise a
        # distinct transport error instead of letting posix_spawn fail silently.
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > _PROMPT_BYTE_BUDGET:
            raise OSError(
                f"E2BIG: kimi prompt {prompt_bytes} bytes exceeds transport budget "
                f"{_PROMPT_BYTE_BUDGET} bytes (macOS ARG_MAX 1,048,576). "
                f"Evidence capping may have been insufficient."
            )

        # Reasoning effort (048): as of this CLI version `kimi --help` has no
        # effort/thinking knob, so a caller-requested metadata["reasoning_effort"]
        # is deliberately NOT threaded into argv (the swarm attempt row still
        # records the decided effort; the CLI just runs at its own default).
        command = ["kimi", "-p", prompt, "--output-format", "stream-json"]
        # Omit -m when unset rather than hardcode a model alias: kimi's -m takes a
        # LOCAL alias name resolved against the user's config.toml (set up by
        # `kimi login`/`kimi provider add`), not a universal provider model id like
        # claude's "sonnet" or grok's "grok-4.5". A hardcoded alias here could not
        # exist in another operator's config and would fail every invocation;
        # omitting -m lets kimi fall back to its own configured default_model.
        if input.model:
            command.extend(["-m", input.model])
        if session_ref is not None:
            command.extend(["-r", session_ref])
        # Drive Access for projects (W4): kimi's own --add-dir "adds an additional
        # workspace directory for this session" (repeatable). Headless -p mode has
        # no read-only guard of its own (see the module docstring), so every
        # granted extra dir is always declared. See runner/core.py::_project_extra_dirs.
        for extra_dir in input.metadata.get("extra_dirs") or []:
            command.extend(["--add-dir", str(extra_dir)])
        return command

    def _parse(self, stdout: str) -> ParsedResponse:
        session_ref: str | None = None
        text_parts: list[str] = []
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
            if event.get("role") == "assistant":
                content = event.get("content")
                if isinstance(content, str):
                    text_parts.append(content)
            elif event.get("role") == "meta" and event.get("type") == "session.resume_hint":
                candidate = event.get("session_id")
                if isinstance(candidate, str):
                    session_ref = candidate
        return ParsedResponse(text="".join(text_parts), session_ref=session_ref, payload={})

    def _usage(self, input: AgentInput, parsed: ParsedResponse, wall_ms: int) -> AgentUsage:
        return estimated_usage(input, parsed.text, wall_ms)
