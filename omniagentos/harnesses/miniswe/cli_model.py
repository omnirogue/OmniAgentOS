"""CLI-shim Model for mini-swe-agent 2.4.5.

Duck-typed Protocol implementation that shells to
``claude -p --output-format json`` per query. No API key is required —
authentication comes from the user's Claude CLI session.

Matches the text-based action format used by default.yaml
(`` ```mswea_bash_command `` blocks), same surface as
``LitellmTextbasedModel`` / ``DeterministicModel``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

from omniagentos.contracts import estimate_tokens

# Matches minisweagent LitellmTextbasedModelConfig.action_regex
_ACTION_REGEX = r"```mswea_bash_command\s*\n(.*?)\n```"

_DEFAULT_OBSERVATION_TEMPLATE = (
    "{% if output.exception_info %}<exception>{{output.exception_info}}</exception>\n{% endif %}"
    "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>"
)

_DEFAULT_FORMAT_ERROR_TEMPLATE = (
    "Please always provide EXACTLY ONE action in triple backticks, "
    "found {{actions|length}} actions.\n"
    "<error>{{error}}</error>"
)


class ClaudeCliModelConfig:
    """Minimal config object (attribute access like pydantic models)."""

    def __init__(
        self,
        *,
        model_name: str = "haiku",
        action_regex: str = _ACTION_REGEX,
        observation_template: str = _DEFAULT_OBSERVATION_TEMPLATE,
        format_error_template: str = _DEFAULT_FORMAT_ERROR_TEMPLATE,
        multimodal_regex: str = "",
        cost_tracking: str = "ignore_errors",
        claude_bin: str = "claude",
        timeout_s: int = 600,
    ) -> None:
        self.model_name = model_name
        self.action_regex = action_regex
        self.observation_template = observation_template
        self.format_error_template = format_error_template
        self.multimodal_regex = multimodal_regex
        self.cost_tracking = cost_tracking
        self.claude_bin = claude_bin
        self.timeout_s = timeout_s

    def model_dump(self, mode: str = "python") -> dict[str, Any]:  # noqa: ARG002
        return {
            "model_name": self.model_name,
            "action_regex": self.action_regex,
            "observation_template": self.observation_template,
            "format_error_template": self.format_error_template,
            "multimodal_regex": self.multimodal_regex,
            "cost_tracking": self.cost_tracking,
            "claude_bin": self.claude_bin,
            "timeout_s": self.timeout_s,
        }


class ClaudeCliModel:
    """mini-swe-agent Model that drives ``claude -p`` (no litellm / no API key)."""

    def __init__(self, **kwargs: Any) -> None:
        os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
        self.config = ClaudeCliModelConfig(**kwargs)
        self.n_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        # None until a provider report supplies a number. 0.0 is a claim of free.
        self.cost_usd: float | None = None
        self.estimated = True
        self.source = "estimator"

    # -- Protocol methods -------------------------------------------------

    def query(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:  # noqa: ARG002
        from minisweagent.exceptions import FormatError
        from minisweagent.models import GLOBAL_MODEL_STATS
        from minisweagent.models.utils.actions_text import parse_regex_actions

        prompt = _messages_to_prompt(messages)
        raw, payload = self._invoke_claude(prompt)
        content = _extract_result_text(payload, raw)
        cost, in_tok, out_tok, estimated, source = _usage_from_payload(payload, prompt, content)

        self.n_calls += 1
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        # Only accumulate known cost. Unknown stays None — never seed 0.0.
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + cost
        # Once any call is estimated, whole run is estimated (or mixed).
        if estimated:
            if self.source == "cli-report":
                self.source = "mixed"
            self.estimated = True
        else:
            if self.source == "estimator" and self.n_calls == 1:
                self.estimated = False
                self.source = "cli-report"
            elif self.estimated is False:
                pass  # stay exact
            else:
                self.source = "mixed"

        try:
            # minisweagent's global budget tracker expects a float; unknown cost
            # contributes nothing measurable (0.0 to the tracker only — our
            # AgentUsage still keeps cost_usd=None when nothing was reported).
            GLOBAL_MODEL_STATS.add(0.0 if cost is None else cost)
        except RuntimeError:
            # Global limit exceeded — re-raise for DefaultAgent to handle.
            raise

        try:
            actions = parse_regex_actions(
                content,
                action_regex=self.config.action_regex,
                format_error_template=self.config.format_error_template,
            )
        except FormatError as e:
            try:
                e.messages[0].setdefault("extra", {})["response"] = (
                    payload if payload is not None else raw
                )
            except Exception:  # noqa: BLE001
                pass
            raise

        return {
            "role": "assistant",
            "content": content,
            "extra": {
                "actions": actions,
                "cost": cost,
                "timestamp": time.time(),
                "response": payload if payload is not None else {"raw": raw},
            },
        }

    def format_message(self, **kwargs: Any) -> dict[str, Any]:
        # Empty multimodal_regex → return kwargs unchanged (matches litellm path).
        if not self.config.multimodal_regex:
            return dict(kwargs)
        from minisweagent.models.utils.openai_multimodal import expand_multimodal_content

        return expand_multimodal_content(kwargs, pattern=self.config.multimodal_regex)

    def format_observation_messages(
        self,
        message: dict[str, Any],  # noqa: ARG002
        outputs: list[dict[str, Any]],
        template_vars: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        from minisweagent.models.utils.actions_text import format_observation_messages

        return format_observation_messages(
            outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
            multimodal_regex=self.config.multimodal_regex,
        )

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG002
        return self.config.model_dump()

    def serialize(self) -> dict[str, Any]:
        return {
            "info": {
                "config": {
                    "model": self.config.model_dump(),
                    "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                },
            }
        }

    # -- internals --------------------------------------------------------

    def _invoke_claude(self, prompt: str) -> tuple[str, dict[str, Any] | None]:
        bin_path = self.config.claude_bin
        if not shutil.which(bin_path) and bin_path == "claude":
            raise RuntimeError(
                "claude CLI not found on PATH. Install Claude Code CLI or set claude_bin."
            )
        cmd = [
            bin_path,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self.config.model_name,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.config.timeout_s,
            check=False,
        )
        raw = (proc.stdout or "").strip()
        if proc.returncode != 0 and not raw:
            err = (proc.stderr or "").strip() or f"claude exited {proc.returncode}"
            raise RuntimeError(f"claude CLI failed: {err}")
        payload: dict[str, Any] | None = None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = None
        return raw, payload


def _messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten OpenAI-style messages into a single claude -p prompt."""
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "user"))
        content = msg.get("content")
        text = _content_to_text(content)
        if not text and role == "exit":
            continue
        parts.append(f"[{role}]\n{text}")
    return "\n\n".join(parts)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    chunks.append(str(item["text"]))
                elif item.get("type") == "text" and "text" in item:
                    chunks.append(str(item["text"]))
            else:
                chunks.append(str(item))
        return "\n".join(chunks)
    return str(content)


def _extract_result_text(payload: dict[str, Any] | None, raw: str) -> str:
    if payload is None:
        return raw
    if isinstance(payload.get("result"), str):
        return payload["result"]
    # Fallbacks for alternate shapes
    for key in ("content", "message", "text", "output"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return raw


def _usage_from_payload(
    payload: dict[str, Any] | None,
    prompt: str,
    content: str,
) -> tuple[float | None, int, int, bool, str]:
    """Return (cost, input_tokens, output_tokens, estimated, source).

    ``cost`` is None when the CLI did not report total_cost_usd. A missing
    measurement must not become 0.0 — that is a claim of free spend, and is
    how budget caps get under-counted. An explicit ``total_cost_usd: 0.0``
    from the CLI remains 0.0 (a genuine free report).
    """
    if payload is None:
        return (
            None,
            estimate_tokens(prompt),
            estimate_tokens(content),
            True,
            "estimator",
        )

    usage_raw = payload.get("usage")
    usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
    in_tok = usage.get("input_tokens")
    out_tok = usage.get("output_tokens")
    cost_raw = payload.get("total_cost_usd")

    has_tokens = isinstance(in_tok, (int, float)) and isinstance(out_tok, (int, float))
    has_cost = isinstance(cost_raw, (int, float))

    if has_tokens or has_cost:
        cost_val: float | None = float(cost_raw) if has_cost else None  # type: ignore[arg-type]
        return (
            cost_val,
            int(in_tok) if isinstance(in_tok, (int, float)) else estimate_tokens(prompt),
            int(out_tok) if isinstance(out_tok, (int, float)) else estimate_tokens(content),
            not (has_tokens and has_cost),
            "cli-report" if (has_tokens and has_cost) else "mixed",
        )

    return (
        None,
        estimate_tokens(prompt),
        estimate_tokens(content),
        True,
        "estimator",
    )


def claude_on_path() -> bool:
    return shutil.which("claude") is not None


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
