"""Adapter for the Anthropic Claude subscription CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path

from omniagentos.adapters.common import (
    CliAdapter,
    ParsedResponse,
    cli_reasoning_effort,
    estimated_usage,
    normalize_provider_cost,
    sandbox_level,
)
from omniagentos.contracts import AgentInput, AgentResult, AgentUsage, ResultStatus
from omniagentos.routing.account_pool import (
    DEFAULT_PROVIDER,
    Outcome,
    account_pool_enabled,
    classify_outcome,
    get_default_pool,
)


def _resolve_claude_cli(*, home: Path | None = None) -> str:
    """Resolve the Claude executable once without relying on daemon PATH order."""

    override = os.environ.get("OMNIAGENTOS_CLAUDE_CLI", "").strip()
    if override:
        return override

    preferred = (home or Path.home()) / ".local" / "bin" / "claude"
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return str(preferred)

    # Preserve the original PATH-based lookup on machines without a preferred install.
    return "claude"


def _is_error_envelope(decoded: dict) -> bool:
    """True when a Claude result envelope is a structured failure, not a success.

    Matches the gate used by ``sessions/supervisor`` and ``longhaul/limits``:
    explicit ``is_error is True``, any ``subtype`` that starts with ``error``,
    or a ``terminal_reason`` that contains ``error`` (live: ``api_error``).

    Why this is stricter than bare truthiness of ``is_error`` plus exact
    ``subtype == "error"``:

    * Live oauth-expiry / spend-limit envelopes can exit 0 with
      ``subtype: "success"`` and still be failures (``is_error`` and/or
      ``terminal_reason: "api_error"``). Treating those as OK records
      ``cost_usd=0.0`` with ``estimated=False`` and surfaces the error text
      as the model's answer — the denied-as-COMPLETED class.
    * ``is_error is True`` (not bare truthiness) keeps a missing flag as
      "unknown", then the structured subtype/reason signals still fail closed.
    * ``error_max_turns`` / ``error_during_execution`` style subtypes must not
      require an exact ``"error"`` match.
    """
    if decoded.get("is_error") is True:
        return True
    subtype = str(decoded.get("subtype") or "").lower()
    if subtype.startswith("error"):
        return True
    terminal_reason = str(decoded.get("terminal_reason") or "").lower()
    return "error" in terminal_reason


class ClaudeAdapter(CliAdapter):
    name = "claude"
    cli = "claude"
    supports_resume = True

    def __init__(self) -> None:
        super().__init__()
        # The registry caches adapter instances, so a long-lived daemon resolves once.
        self.cli = _resolve_claude_cli()

    def run(self, input: AgentInput) -> AgentResult:
        """Account-pool aware entry point.

        Opt-in via ``OMNIAGENTOS_ACCOUNT_POOL=1`` (see
        omniagentos/routing/account_pool.py). When off, or when no accounts
        are configured for the "claude" provider, this is exactly
        ``CliAdapter.run()`` -- zero behavior change (no regression).

        When on: picks an account, injects its `config_dir` as
        ``CLAUDE_CONFIG_DIR`` for the call, and classifies the result. A
        ``rate_limited`` outcome cools that ONE account and retries on the
        next one (same model, same input) -- round-robin, same as
        ``AccountPool.pick()``. Only once every configured account is cooling
        (``all_cooling()``) does this return the (rate-limited-flavored)
        result to the caller -- which lets ``omniagentos.intake.fallback.
        run_with_fallback`` advance to the next MODEL lineage using its
        existing, unmodified retry classification (its error-text patterns
        already match a real CLI rate-limit message). ``ok``/plain-``error``
        outcomes return immediately, same as the unpooled path.
        """
        if not account_pool_enabled():
            return super().run(input)
        pool = get_default_pool()
        if not pool.has_accounts(DEFAULT_PROVIDER):
            return super().run(input)

        last_result: AgentResult | None = None
        while True:
            account = pool.pick(DEFAULT_PROVIDER)
            if account is None:
                break  # every account is cooling/disabled/saturated right now
            result = self._run_once(
                input, env_overrides={"CLAUDE_CONFIG_DIR": account.resolved_config_dir()}
            )
            outcome = classify_outcome(result)
            pool.report(account.id, outcome)
            last_result = result
            if outcome is not Outcome.RATE_LIMITED:
                return result
            if pool.all_cooling(DEFAULT_PROVIDER):
                break

        if last_result is not None:
            # At least one account was tried; propagate its real (rate-limited)
            # result so the model-fallback layer's existing classifier sees an
            # authentic CLI error message and advances to the next model.
            return last_result
        # No account was EVER available (pool was already fully cooling before
        # this call started) -- synthesize a rate-limited-flavored result so
        # the same fallback classification still engages, with zero wasted CLI
        # spawns. "rate limited" is a substring match for
        # omniagentos.intake.fallback._is_limit_or_unavailable_error's "rate
        # limit" pattern.
        return AgentResult(
            status=ResultStatus.ERROR,
            usage=AgentUsage(wall_ms=1, estimated=True, source="estimator"),
            error=f"account pool exhausted: every {DEFAULT_PROVIDER} account is rate limited (cooling)",
        )

    def _command(self, input: AgentInput, prompt: str, session_ref: str | None) -> list[str]:
        command = [
            self.cli,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            input.model or "sonnet",
        ]
        # Reasoning effort (e.g. Fable routing: --model fable --effort high).
        # T4.5: this used to read metadata["effort"] ONLY, which meant swarm's
        # router-decided effort -- written to metadata["reasoning_effort"] by
        # swarm/provider_exec.py, including the complex-tier claude-opus-5
        # xhigh pin -- never reached a single Claude worker. The shared resolver
        # reads envelope.effort > metadata["reasoning_effort"] >
        # metadata["effort"] and maps the result into this CLI's own vocabulary
        # (see adapters/common.py: no "minimal" here, "max" IS accepted).
        # Still only emitted when a caller actually requested one, so every
        # harness path that requests nothing is byte-for-byte unchanged.
        effort = cli_reasoning_effort(input, self.name)
        if effort is not None:
            command.extend(["--effort", effort])
        if input.budget.max_turns is not None:
            command.extend(["--max-turns", str(input.budget.max_turns)])
        if session_ref is not None:
            command.extend(["--resume", session_ref])
        if sandbox_level(input) == "workspace_write":
            command.extend(
                ["--permission-mode", "acceptEdits", "--add-dir", self._working_dir(input)]
            )
            # Drive Access for projects (W4): every OTHER directory the run's
            # project has been granted (root_dirs/allowed_dirs, including a
            # granted Drive subfolder -- see omniagentos/runner/core.py
            # ::_project_extra_dirs and omniagentos/provision/drive.py
            # ::grant_drive_dir), so Claude can actually read/write there too.
            for extra_dir in input.metadata.get("extra_dirs") or []:
                command.extend(["--add-dir", str(extra_dir)])
        return command

    def _parse(self, stdout: str) -> ParsedResponse:
        decoded = json.loads(stdout)
        if not isinstance(decoded, dict):
            raise ValueError("Claude JSON envelope is not an object")
        if _is_error_envelope(decoded):
            raise ValueError(str(decoded.get("result") or "Claude CLI returned an error envelope"))
        result = decoded.get("result")
        if not isinstance(result, str):
            raise ValueError("Claude JSON envelope did not contain a result string")
        session_ref = decoded.get("session_id")
        return ParsedResponse(
            text=result,
            session_ref=session_ref if isinstance(session_ref, str) else None,
            payload=decoded,
        )

    def _usage(self, input: AgentInput, parsed: ParsedResponse, wall_ms: int) -> AgentUsage:
        usage = parsed.payload.get("usage")
        normalized_cost = normalize_provider_cost(parsed.payload.get("total_cost_usd"))
        turns = parsed.payload.get("num_turns")
        if (
            isinstance(usage, dict)
            and isinstance(usage.get("input_tokens"), int)
            and isinstance(usage.get("output_tokens"), int)
            and isinstance(turns, int)
        ):
            exact = normalized_cost.cost_usd is not None
            return AgentUsage(
                wall_ms=wall_ms,
                turns=turns,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cost_usd=normalized_cost.cost_usd,
                estimated=not exact,
                source="cli-report" if exact else "mixed",
            )
        return estimated_usage(input, parsed.text, wall_ms)
