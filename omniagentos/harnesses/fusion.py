"""FusionHarness — delegates to the fusion multi-agent SWARM machinery.

Implements AgentAdapter by spawning a Claude CLI subprocess that invokes
the user's fusion/ultrabuild skill hierarchy (/fusion, /ultrabuild, /superfast).
The Claude session fans out to N agents internally, returning results as-is.

This is Option A from the design brief: reuse the existing ultracode machinery
verbatim via subprocess, so no reimplementation of swarm coordination is needed.

Environment variables:
  OMNIAGENTOS_FUSION_ENTRYPOINT  -- which skill to invoke (default: /fusion)
                                     typically one of /fusion, /ultrabuild, /superfast
  OMNIAGENTOS_CLAUDE_CLI         -- explicit path to claude CLI (default: auto-resolved)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from omniagentos.adapters.common import _scrubbed_env
from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    HealthStatus,
    ResultStatus,
    estimate_tokens,
)


def _resolve_claude_cli(*, home: Path | None = None) -> str:
    """Resolve the Claude executable once, matching adapters/claude.py pattern."""
    override = os.environ.get("OMNIAGENTOS_CLAUDE_CLI", "").strip()
    if override:
        return override

    preferred = (home or Path.home()) / ".local" / "bin" / "claude"
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return str(preferred)

    return "claude"


def _resolve_fusion_entrypoint() -> str:
    """Resolve which skill/tier to invoke (default: /fusion auto-mode).

    Examples: /fusion, /ultrabuild, /superfast
    """
    override = os.environ.get("OMNIAGENTOS_FUSION_ENTRYPOINT", "").strip()
    return override if override else "/fusion"


class FusionHarness:
    """AgentAdapter that spawns Claude with a fusion-skill invocation.

    run() spawns: claude -p "/<entrypoint> <prompt>" --output-format json
    The Claude session fans out to N agents internally via the skill machinery.
    Results are parsed from the JSON response envelope (matching claude adapter).
    """

    name: str = "fusion"
    version: str = "1.0"

    def __init__(self) -> None:
        self._active: dict[str, subprocess.Popen[str]] = {}
        self._health: HealthStatus | None = None
        self.cli = _resolve_claude_cli()

    def run(self, input: AgentInput) -> AgentResult:
        """Execute a fusion run by spawning claude with skill invocation."""
        started = time.perf_counter()
        session_ref: str | None = None

        try:
            entrypoint = _resolve_fusion_entrypoint()
            working_dir = input.working_dir or os.getcwd()

            # Build the command: claude -p "/<skill> <prompt>" --output-format json
            skill_prompt = f"{entrypoint} {input.prompt}"
            command = [
                self.cli,
                "-p",
                skill_prompt,
                "--output-format",
                "json",
            ]

            # Optionally add model if specified
            if input.model:
                command.extend(["--model", input.model])

            # Run in subprocess with scrubbed environment
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=working_dir,
                start_new_session=True,
                env=_scrubbed_env(),
            )
            session_ref = str(proc.pid)
            self._active[session_ref] = proc

            try:
                # Communicate empty stdin (prompt is in command, not stdin)
                stdout, stderr = proc.communicate(timeout=300)  # 5min timeout
                wall_ms = max(1, int((time.perf_counter() - started) * 1000))

                if proc.returncode != 0:
                    return AgentResult(
                        status=ResultStatus.ERROR,
                        usage=AgentUsage(
                            wall_ms=wall_ms,
                            estimated=True,
                            source="estimator",
                        ),
                        error=stderr.strip()[-500:]
                        if stderr
                        else f"process exited with code {proc.returncode}",
                    )

                # Parse JSON response envelope (matching claude adapter format)
                try:
                    decoded = json.loads(stdout)
                except json.JSONDecodeError as exc:
                    return AgentResult(
                        status=ResultStatus.ERROR,
                        usage=AgentUsage(
                            wall_ms=wall_ms,
                            estimated=True,
                            source="estimator",
                        ),
                        error=f"invalid JSON response: {exc}",
                    )

                if not isinstance(decoded, dict):
                    return AgentResult(
                        status=ResultStatus.ERROR,
                        usage=AgentUsage(
                            wall_ms=wall_ms,
                            estimated=True,
                            source="estimator",
                        ),
                        error="response is not a JSON object",
                    )

                # Check for error envelope (claude adapter pattern)
                if decoded.get("is_error") or decoded.get("subtype") == "error":
                    return AgentResult(
                        status=ResultStatus.ERROR,
                        usage=AgentUsage(
                            wall_ms=wall_ms,
                            estimated=True,
                            source="estimator",
                        ),
                        error=str(decoded.get("result") or "error in response"),
                    )

                # Extract result text
                result_text = decoded.get("result")
                if not isinstance(result_text, str):
                    return AgentResult(
                        status=ResultStatus.ERROR,
                        usage=AgentUsage(
                            wall_ms=wall_ms,
                            estimated=True,
                            source="estimator",
                        ),
                        error="response did not contain a result string",
                    )

                # Extract session ID and usage
                session_id = decoded.get("session_id")
                usage = self._usage_from_response(decoded, input.prompt, result_text, wall_ms)

                return AgentResult(
                    status=ResultStatus.OK,
                    output_text=result_text,
                    session_ref=session_id if isinstance(session_id, str) else None,
                    usage=usage,
                )

            except subprocess.TimeoutExpired:
                wall_ms = max(1, int((time.perf_counter() - started) * 1000))
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                return AgentResult(
                    status=ResultStatus.TIMEOUT,
                    usage=AgentUsage(
                        wall_ms=wall_ms,
                        estimated=True,
                        source="estimator",
                    ),
                    error="fusion subprocess timed out",
                )

        except Exception as exc:  # noqa: BLE001
            wall_ms = max(1, int((time.perf_counter() - started) * 1000))
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=AgentUsage(
                    wall_ms=wall_ms,
                    estimated=True,
                    source="estimator",
                ),
                error=f"{type(exc).__name__}: {str(exc)[-500:]}",
            )
        finally:
            if session_ref is not None:
                self._active.pop(session_ref, None)

    def cancel(self, session_ref: str) -> bool:
        """Terminate a running fusion subprocess if still active."""
        proc = self._active.get(session_ref)
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.kill()
            return True
        except (ProcessLookupError, OSError):
            return False

    def health(self) -> HealthStatus:
        """Check if claude CLI is available."""
        if self._health is not None:
            return self._health

        try:
            result = subprocess.run(
                [self.cli, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=_scrubbed_env(),
            )
            detail = (result.stdout or result.stderr).strip()[-500:]
            self._health = HealthStatus(
                healthy=result.returncode == 0,
                detail=detail if detail else self.cli,
                capabilities={"live_runs": result.returncode == 0},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._health = HealthStatus(
                healthy=False,
                detail=f"claude CLI unavailable: {exc}",
                capabilities={"live_runs": False},
            )
        return self._health

    # -- internals --------------------------------------------------------

    def _usage_from_response(
        self,
        decoded: dict[str, Any],
        prompt: str,
        output: str,
        wall_ms: int,
    ) -> AgentUsage:
        """Extract usage from response envelope, fall back to estimator."""
        usage_info = decoded.get("usage")
        cost = decoded.get("total_cost_usd")
        turns = decoded.get("num_turns")

        # Match claude adapter's logic for cli-report vs estimator
        if (
            isinstance(usage_info, dict)
            and isinstance(usage_info.get("input_tokens"), int)
            and isinstance(usage_info.get("output_tokens"), int)
            and isinstance(cost, (int, float))
            and isinstance(turns, int)
        ):
            return AgentUsage(
                wall_ms=wall_ms,
                turns=turns,
                input_tokens=usage_info["input_tokens"],
                output_tokens=usage_info["output_tokens"],
                cost_usd=float(cost),
                estimated=False,
                source="cli-report",
            )

        # Fallback to estimator
        return AgentUsage(
            wall_ms=wall_ms,
            turns=1,
            input_tokens=estimate_tokens(prompt),
            output_tokens=estimate_tokens(output),
            cost_usd=None,
            estimated=True,
            source="estimator",
        )
