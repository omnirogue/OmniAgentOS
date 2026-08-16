"""MiniSweAdapter — mini-swe-agent 2.4.5 driven by a Claude CLI-shim Model.

B1 baseline ladder arm. No API key: the Model shells to
``claude -p --output-format json``. Lazy-imports minisweagent so adapter
discovery works even when the harness extra is not installed.
"""

from __future__ import annotations

import os
import time
from importlib import metadata
from typing import Any

from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    HealthStatus,
    ResultStatus,
    estimate_tokens,
)

_DIST_NAME = "mini-swe-agent"
_ADAPTER_NAME = "mini-swe"


def _resolve_version() -> str:
    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:
        try:
            import minisweagent  # type: ignore[import-untyped]

            return str(getattr(minisweagent, "__version__", "0.0.0"))
        except Exception:  # noqa: BLE001
            return "0.0.0"


class MiniSweAdapter:
    """AgentAdapter for mini-swe-agent with Claude CLI-shim Model (B1)."""

    name: str = _ADAPTER_NAME
    version: str = _resolve_version()

    def __init__(self, working_dir: str = ".") -> None:
        # Lazy imports so missing harness extra does not break module import.
        os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
        os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

        self.working_dir = working_dir
        self.version = _resolve_version()
        self._config: dict[str, Any] | None = None
        self._model: Any = None
        self._package_dir: Any = None

        try:
            from pathlib import Path

            import yaml
            from minisweagent import package_dir

            from omniagentos.harnesses.miniswe.cli_model import ClaudeCliModel

            self._package_dir = package_dir
            cfg_path = Path(package_dir) / "config" / "default.yaml"
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            self._config = loaded if isinstance(loaded, dict) else {}
            model_cfg = dict(self._config.get("model") or {})
            # Strip litellm-only keys that ClaudeCliModel does not accept.
            model_cfg.pop("model_kwargs", None)
            model_cfg.pop("model_name", None)
            model_name = os.environ.get("MSWEA_MODEL_NAME") or os.environ.get(
                "OMNIAGENTOS_CLAUDE_MODEL", "haiku"
            )
            self._model = ClaudeCliModel(model_name=model_name, **model_cfg)
        except Exception:
            # Defer failure to run()/health(); discovery must still work.
            self._config = None
            self._model = None

    def run(self, input: AgentInput) -> AgentResult:
        started = time.monotonic()
        try:
            result_extra, model = self._run_agent(input)
        except Exception as e:  # noqa: BLE001
            wall_ms = max(1, int((time.monotonic() - started) * 1000))
            return AgentResult(
                status=ResultStatus.ERROR,
                output_text="",
                session_ref=None,
                usage=AgentUsage(
                    wall_ms=wall_ms,
                    turns=0,
                    input_tokens=estimate_tokens(input.prompt),
                    output_tokens=0,
                    # Unknown spend is None, not 0.0 — a non-measurement must
                    # not look like a free run under a dollar cap.
                    cost_usd=None,
                    estimated=True,
                    source="estimator",
                ),
                error=f"{type(e).__name__}: {e}",
            )

        wall_ms = max(1, int((time.monotonic() - started) * 1000))
        exit_status = str(result_extra.get("exit_status", "") or "")
        submission = str(result_extra.get("submission", "") or "")

        ok_statuses = {"Submitted", "exit", "success", "completed"}
        status = ResultStatus.OK if exit_status in ok_statuses else ResultStatus.ERROR
        error = None if status is ResultStatus.OK else (exit_status or "agent failed")

        usage = self._usage_from_model(model, input.prompt, submission, wall_ms)
        return AgentResult(
            status=status,
            output_text=submission,
            output_json=None,
            session_ref=None,
            usage=usage,
            error=error,
        )

    def cancel(self, session_ref: str) -> bool:  # noqa: ARG002
        # CLI-shim has no async/parallel sessions.
        return False

    def health(self) -> HealthStatus:
        parts: list[str] = []
        mswe_ok = False
        try:
            ver = metadata.version(_DIST_NAME)
            mswe_ok = True
            parts.append(f"mini-swe-agent={ver}")
        except metadata.PackageNotFoundError:
            try:
                import minisweagent  # type: ignore[import-untyped]

                mswe_ok = True
                parts.append(f"mini-swe-agent={getattr(minisweagent, '__version__', '?')}")
            except Exception as e:  # noqa: BLE001
                parts.append(f"minisweagent unavailable: {e}")

        from omniagentos.harnesses.miniswe.cli_model import claude_on_path

        claude_ok = claude_on_path()
        parts.append("claude=found" if claude_ok else "claude=missing")

        return HealthStatus(
            healthy=mswe_ok and claude_ok,
            detail="; ".join(parts),
            capabilities={"live_runs": claude_ok},
        )

    # -- internals --------------------------------------------------------

    def _run_agent(self, input: AgentInput) -> tuple[dict[str, Any], Any]:
        os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
        os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

        from pathlib import Path

        import yaml
        from minisweagent import package_dir
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.environments.local import LocalEnvironment

        from omniagentos.harnesses.miniswe.cli_model import ClaudeCliModel

        if self._config is None:
            cfg_path = Path(package_dir) / "config" / "default.yaml"
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            self._config = loaded if isinstance(loaded, dict) else {}

        agent_cfg = dict(self._config.get("agent") or {})
        env_cfg = dict(self._config.get("environment") or {})
        model_cfg = dict(self._config.get("model") or {})
        model_cfg.pop("model_kwargs", None)
        model_cfg.pop("model_name", None)

        workdir = input.working_dir or self.working_dir or "."
        env_cfg.setdefault("cwd", workdir)

        model_name = (
            input.model
            or os.environ.get("MSWEA_MODEL_NAME")
            or os.environ.get("OMNIAGENTOS_CLAUDE_MODEL")
            or "haiku"
        )
        model = ClaudeCliModel(model_name=model_name, **model_cfg)
        self._model = model

        # Honour budget max_turns as step_limit when provided.
        if input.budget.max_turns is not None and input.budget.max_turns > 0:
            agent_cfg["step_limit"] = input.budget.max_turns
        if input.budget.cost_usd_max is not None and input.budget.cost_usd_max > 0:
            agent_cfg["cost_limit"] = input.budget.cost_usd_max
        if input.budget.wall_ms_max is not None and input.budget.wall_ms_max > 0:
            agent_cfg["wall_time_limit_seconds"] = max(1, input.budget.wall_ms_max // 1000)

        env = LocalEnvironment(**env_cfg)
        agent = DefaultAgent(model, env, **agent_cfg)
        result = agent.run(input.prompt)
        # agent.run returns last message's extra dict: {exit_status, submission, ...}
        if not isinstance(result, dict):
            result = {"exit_status": "error", "submission": str(result)}
        return result, model

    def _usage_from_model(
        self,
        model: Any,
        prompt: str,
        submission: str,
        wall_ms: int,
    ) -> AgentUsage:
        turns = int(getattr(model, "n_calls", 0) or 0)
        in_tok = getattr(model, "input_tokens", None)
        out_tok = getattr(model, "output_tokens", None)
        cost = getattr(model, "cost_usd", None)
        estimated = bool(getattr(model, "estimated", True))
        source = str(getattr(model, "source", "estimator") or "estimator")

        if in_tok is None:
            in_tok = estimate_tokens(prompt)
            estimated = True
            source = "estimator"
        if out_tok is None:
            out_tok = estimate_tokens(submission) if submission else 0
            estimated = True
            source = "estimator" if source == "estimator" else "mixed"

        # Estimator (or missing) cost is unknown — never coerce to 0.0.
        # A numeric cost from cli-report / mixed / imported is a real figure,
        # including an explicit free report of 0.0.
        if cost is not None and source in {"cli-report", "mixed", "imported"}:
            cost_usd: float | None = float(cost)
        elif cost is not None and not estimated:
            cost_usd = float(cost)
        else:
            cost_usd = None

        return AgentUsage(
            wall_ms=wall_ms,
            turns=turns or None,
            input_tokens=int(in_tok),
            output_tokens=int(out_tok),
            cost_usd=cost_usd,
            estimated=estimated,
            source=source,
        )
