"""Agent Deck adapter (harness id ``agentdeck``).

Wraps `agent-deck` CLI (installed at `~/.local/bin/agent-deck`)
behind the `AgentAdapter` protocol (`omniagentos/contracts.py`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    HarnessProfile,
    HarnessType,
    HealthStatus,
    ResultStatus,
    estimate_tokens,
)
from omniagentos.harnesses.envhash import env_hash

_NO_BINARY_ERROR = "agent-deck: binary not found in PATH; live runs unavailable"


def _detect_binary() -> str | None:
    """Locate the agent-deck binary path on the system."""
    return shutil.which("agent-deck") or shutil.which("/Users/youruser/.local/bin/agent-deck")


def _get_version(binary_path: str) -> str:
    """Run agent-deck version to get the installed version string."""
    try:
        res = subprocess.run(
            [binary_path, "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            # "Agent Deck v1.10.10" -> extract v1.10.10 or return the text
            parts = res.stdout.strip().split()
            for p in parts:
                if p.startswith("v"):
                    return p[1:]
            return res.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "1.10.10"


def _elapsed_ms(started: float) -> int:
    return max(1, int((time.monotonic() - started) * 1000))


class AgentDeckAdapter:
    """AgentAdapter over agent-deck's session and TUI process manager.

    Registered as exactly
    `omniagentos.harnesses.agentdeck.adapter:AgentDeckAdapter`
    (contracts/interfaces.md Section "p04 -- omniagentos.adapters").
    """

    name = "agentdeck"

    def __init__(self) -> None:
        binary = _detect_binary()
        self.version = _get_version(binary) if binary else "0.0.0"

    def profile(self) -> HarnessProfile:
        """HarnessProfile for this adapter."""
        return HarnessProfile(
            harness=HarnessType.AGENTDECK,
            version=self.version,
            env_hash=env_hash(),
        )

    def health(self) -> HealthStatus:
        binary = _detect_binary()
        if not binary:
            return HealthStatus(
                healthy=False,
                detail=_NO_BINARY_ERROR,
                capabilities={"live_runs": False},
            )

        try:
            # Verify basic CLI status invocation doesn't fail
            subprocess.run(
                [binary, "status", "-q"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(
                healthy=False,
                detail=f"agent-deck: status check failed: {type(exc).__name__}: {exc}",
                capabilities={"live_runs": False},
            )

        return HealthStatus(
            healthy=True,
            detail=f"agent-deck installed & responsive (v{self.version}) at {binary}",
            capabilities={"live_runs": True},
        )

    def run(self, input: AgentInput) -> AgentResult:
        started = time.monotonic()
        binary = _detect_binary()
        if not binary:
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=AgentUsage(wall_ms=_elapsed_ms(started), estimated=True, source="estimator"),
                error=_NO_BINARY_ERROR,
            )

        run_id = input.run_id or f"run-{int(started)}"

        # Enforce Boundary Rule: Refuse swarm-owned sessions
        if run_id.startswith("swarm:") or (input.prompt and "[swarm:" in input.prompt):
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=AgentUsage(wall_ms=_elapsed_ms(started), estimated=True, source="estimator"),
                error="agentdeck adapter is restricted to interactive sessions and refuses swarm scheduler tasks",
            )

        # 1. Resolve agent tool mapping (-c) based on model
        model = (input.model or "").lower()
        tool_cmd = "claude"
        if "codex" in model or "gpt" in model or "openai" in model:
            tool_cmd = "codex"
        elif "gemini" in model:
            tool_cmd = "gemini"
        elif "opencode" in model:
            tool_cmd = "opencode"

        working_dir = input.working_dir or os.getcwd()

        # 2. Write prompt to a temp file to avoid shell quoting issues
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".md", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(input.prompt or "")
            prompt_file = tf.name

        try:
            # 3. Launch the session via agent-deck launch
            # --no-wait returns immediately after starting the tmux session
            launch_args = [
                binary,
                "launch",
                working_dir,
                "-t",
                run_id,
                "-c",
                tool_cmd,
                "--message-file",
                prompt_file,
                "--no-wait",
                "--idle-timeout",
                "30m",
            ]
            if input.model:
                launch_args.extend(["--model", input.model])

            subprocess.run(
                launch_args,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )

            # 4. Polling Loop: wait for the session status to change
            # Default timeout: 30 minutes
            poll_start = time.monotonic()
            poll_timeout = 1800
            tmux_session_name = None
            # Terminal status observed from agent-deck, or None if never listed /
            # never reached a terminal state. Must not default to success.
            terminal_status: str | None = None
            session_seen = False

            while (time.monotonic() - poll_start) < poll_timeout:
                time.sleep(2)

                # Query the sessions status
                res = subprocess.run(
                    [binary, "list", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode != 0:
                    continue

                try:
                    sessions = json.loads(res.stdout)
                except Exception:  # noqa: BLE001
                    continue

                # Locate our session by title
                sess = next((s for s in sessions if s.get("title") == run_id), None)
                if not sess:
                    # Missing/removed: not success. If we never saw it, the
                    # post-loop mapping reports "never appeared"; if it vanished
                    # mid-run without a terminal status, that is also an error
                    # (handled as non-completed after the loop).
                    break

                session_seen = True
                tmux_session_name = sess.get("tmux_session")
                status = str(sess.get("status") or "").lower()

                if status in {"completed", "failed", "stopped"}:
                    terminal_status = status
                    break

            # 5. Capture visual output via tmux capture-pane
            output_text = ""
            if tmux_session_name:
                try:
                    cap = subprocess.run(
                        ["tmux", "capture-pane", "-t", tmux_session_name, "-p"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if cap.returncode == 0:
                        output_text = cap.stdout
                except Exception:  # noqa: BLE001
                    pass

            # 6. Graceful cleanup of session processes
            try:
                subprocess.run([binary, "session", "stop", run_id], capture_output=True, timeout=10)
                subprocess.run([binary, "remove", run_id], capture_output=True, timeout=10)
            except Exception:  # noqa: BLE001
                pass

        except Exception as exc:  # noqa: BLE001
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=AgentUsage(wall_ms=_elapsed_ms(started), estimated=True, source="estimator"),
                error=f"agent-deck: run failed: {type(exc).__name__}: {exc}",
            )
        finally:
            # Ensure the temp prompt file is cleaned up
            try:
                os.unlink(prompt_file)
            except Exception:  # noqa: BLE001
                pass

        # Map the observed terminal status honestly. Capture text is evidence,
        # not a verdict — a failed session that left pane output is still failed.
        usage = AgentUsage(
            wall_ms=_elapsed_ms(started),
            input_tokens=estimate_tokens(input.prompt or ""),
            estimated=True,
            source="estimator",
        )
        if terminal_status == "completed":
            return AgentResult(
                status=ResultStatus.OK,
                output_text=output_text,
                usage=usage,
            )
        if terminal_status in {"failed", "stopped"}:
            return AgentResult(
                status=ResultStatus.ERROR,
                output_text=output_text,
                usage=usage,
                error=f"agent-deck session ended with status={terminal_status}",
            )
        if not session_seen:
            return AgentResult(
                status=ResultStatus.ERROR,
                output_text=output_text,
                usage=usage,
                error="agent-deck session never appeared in list (missing/not found)",
            )
        # Seen as running (or other non-terminal) until the poll window expired.
        return AgentResult(
            status=ResultStatus.TIMEOUT,
            output_text=output_text,
            usage=usage,
            error="agent-deck session poll timeout without a terminal status",
        )

    def cancel(self, session_ref: str) -> bool:
        binary = _detect_binary()
        if not binary or not session_ref:
            return False
        try:
            res = subprocess.run(
                [binary, "session", "stop", session_ref],
                capture_output=True,
                timeout=10,
            )
            return res.returncode == 0
        except Exception:  # noqa: BLE001
            return False
