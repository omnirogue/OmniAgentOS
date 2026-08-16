"""Spend guard and budget ledger for LiteLLM short calls."""

from __future__ import annotations

import datetime
import json
import os
from typing import Any

import yaml


class LLMError(Exception):
    """Base exception class for all LLM short-call errors."""


class LLMBudgetExceededError(LLMError):
    """Exception raised when daily short-call spend budget is exceeded."""


class LLMBudgetUnknownError(LLMBudgetExceededError):
    """Exception raised when daily spend budget cannot be calculated safely."""


class LLMTransportError(LLMError):
    """Exception raised for network, timeout, or server-side transport issues."""


class LLMClientError(LLMError):
    """Exception raised for non-retryable 4xx client errors (e.g. 401, 400)."""


class LLMInvalidResponseError(LLMError):
    """Exception raised when model response is unparseable or misses required keys."""


# Conservative default configurations when llm.yaml is missing or incomplete
DEFAULT_CONFIG: dict[str, Any] = {
    "daily_usd_cap": 10.0,
    "fallback_rate_per_million_input": 0.15,
    "fallback_rate_per_million_output": 0.60,
    "proxy_base_url": "http://localhost:4000/v1",
    "default_model": "gemini-3.6-flash",
    "rates": {
        "gemini-3.6-flash": {"input": 0.075, "output": 0.30},
        "gemini-2.5-flash": {"input": 0.075, "output": 0.30},
        "gpt-4o-mini": {"input": 0.150, "output": 0.600},
        "claude-3-5-haiku": {"input": 0.800, "output": 4.000},
    },
}


def _var_root() -> str:
    """Returns the var root directory, honoring OMNIAGENTOS_VAR_DIR / OMNIAGENTOS_VAR.

    Defaults to repo_root/var.
    """
    override = os.environ.get("OMNIAGENTOS_VAR_DIR") or os.environ.get("OMNIAGENTOS_VAR")
    if override:
        return override
    # Fallback to repo_root/var
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "var")


def _ledger_path() -> str:
    """Returns the absolute path to the short-calls ledger."""
    return os.path.join(_var_root(), "ledger", "llm_shortcalls.jsonl")


class BudgetGuard:
    """Tracks and enforces daily (UTC) USD spend limits for LiteLLM short calls."""

    def __init__(
        self,
        config_path: str | None = None,
        config_dict: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the budget guard with optional custom configuration.

        Args:
            config_path: Optional path to configs/llm.yaml.
            config_dict: Optional direct configuration dictionary.
        """
        self.config: dict[str, Any] = {}
        loaded: dict[str, Any] = {}

        if config_dict is not None:
            loaded = config_dict
        else:
            if config_path is None:
                # Resolve default config path relative to repository root
                repo_root = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
                config_path = os.path.join(repo_root, "configs", "llm.yaml")

            if os.path.exists(config_path):
                try:
                    with open(config_path, encoding="utf-8") as f:
                        loaded = yaml.safe_load(f) or {}
                except Exception:
                    # Fall back to default config if parsing fails
                    loaded = {}

        # Safely extract values with conservative defaults
        self.config["daily_usd_cap"] = loaded.get("daily_usd_cap", DEFAULT_CONFIG["daily_usd_cap"])
        self.config["fallback_rate_per_million_input"] = loaded.get(
            "fallback_rate_per_million_input",
            DEFAULT_CONFIG["fallback_rate_per_million_input"],
        )
        self.config["fallback_rate_per_million_output"] = loaded.get(
            "fallback_rate_per_million_output",
            DEFAULT_CONFIG["fallback_rate_per_million_output"],
        )
        self.config["proxy_base_url"] = loaded.get(
            "proxy_base_url", DEFAULT_CONFIG["proxy_base_url"]
        )
        self.config["default_model"] = loaded.get("default_model", DEFAULT_CONFIG["default_model"])
        self.config["rates"] = {
            **DEFAULT_CONFIG["rates"],
            **loaded.get("rates", {}),
        }

    def get_today_spend(self) -> float:
        """Calculate total spend (in USD) incurred during today's UTC day.

        Reads the JSON Lines ledger file and aggregates cost. Skip and continue on
        any malformed or unparseable lines to ensure resilience.

        Fail-closed: If the ledger file exists but cannot be opened or read (due to
        OSError, permissions, filesystem errors, or other read failures), this method
        raises LLMBudgetUnknownError to refuse subsequent calls for safety rather than
        returning an inaccurate budget spend.

        Returns:
            The total estimated USD spend today (UTC).

        Raises:
            LLMBudgetUnknownError: If the ledger file exists but cannot be read.
        """
        ledger_file = _ledger_path()
        if not os.path.exists(ledger_file):
            return 0.0

        today_str = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        total_spend = 0.0

        try:
            with open(ledger_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        timestamp = entry.get("timestamp", "")
                        # Check if the UTC timestamp starts with today's YYYY-MM-DD
                        if not (timestamp and str(timestamp).startswith(today_str)):
                            continue
                        if not isinstance(entry, dict):
                            continue
                        # Missing key on a partial line is skipped (malformed).
                        # Explicit null is UNKNOWN — fail closed, never free.
                        if "estimated_usd_cost" not in entry:
                            continue
                        raw_cost = entry["estimated_usd_cost"]
                        if raw_cost is None:
                            raise LLMBudgetUnknownError(
                                "Budget state is unknown: ledger entry has "
                                f"estimated_usd_cost=null at {ledger_file}. "
                                "The call was refused for safety (fail-closed)."
                            )
                        try:
                            total_spend += float(raw_cost)
                        except (TypeError, ValueError) as exc:
                            raise LLMBudgetUnknownError(
                                "Budget state is unknown: ledger entry has "
                                f"non-numeric estimated_usd_cost={raw_cost!r} at "
                                f"{ledger_file}. The call was refused for safety "
                                "(fail-closed)."
                            ) from exc
                    except LLMBudgetUnknownError:
                        raise
                    except Exception:
                        # Malformed ledger line: skip and continue as required
                        continue
        except OSError as e:
            raise LLMBudgetUnknownError(
                f"Budget state is unknown: failed to read the spend ledger at {ledger_file}. "
                f"The call was refused for safety (fail-closed). Underlying error: {e}"
            ) from e

        return total_spend

    def check_budget(self) -> None:
        """Check if today's (UTC) spend is at or over the daily USD cap.

        Fail-closed: If the spend ledger cannot be safely read, this raises
        LLMBudgetUnknownError (a subclass of LLMBudgetExceededError) to refuse
        subsequent calls, ensuring that we never allow unlimited spend when the
        budget state is unknown.

        Raises:
            LLMBudgetExceededError: If today's spend is at or over the cap.
            LLMBudgetUnknownError: If today's spend cannot be determined safely.
        """
        cap = self.config.get("daily_usd_cap", 10.0)
        spent = self.get_today_spend()
        if spent >= cap:
            raise LLMBudgetExceededError(
                f"Daily short-call LLM spend budget exceeded. Spent: ${spent:.6f}, Cap: ${cap:.6f}"
            )

    def record_spend(
        self,
        model: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        purpose: str = "default",
    ) -> float:
        """Record spend for a short-call LLM request.

        Estimates the USD cost based on model rates (or fallback rate if unknown),
        and appends a single JSON Line to the ledger. Parent directories are created
        on demand.

        Args:
            model: The model string used.
            prompt_tokens: Number of input prompt tokens, if available.
            completion_tokens: Number of completion tokens, if available.
            purpose: A tag indicating the caller or purpose of the request.

        Returns:
            The estimated USD cost of this specific request.
        """
        p_tokens = prompt_tokens if prompt_tokens is not None else 0
        c_tokens = completion_tokens if completion_tokens is not None else 0

        rates = self.config.get("rates", {})
        if model in rates:
            in_rate = rates[model].get("input", 0.0)
            out_rate = rates[model].get("output", 0.0)
        else:
            in_rate = self.config.get("fallback_rate_per_million_input", 0.15)
            out_rate = self.config.get("fallback_rate_per_million_output", 0.60)

        estimated_cost = (p_tokens * in_rate + c_tokens * out_rate) / 1_000_000.0

        entry = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_usd_cost": estimated_cost,
            "purpose": purpose,
        }

        ledger_file = _ledger_path()
        os.makedirs(os.path.dirname(ledger_file), exist_ok=True)

        # Atomic line append under POSIX (single write call)
        line = json.dumps(entry) + "\n"
        with open(ledger_file, "a", encoding="utf-8") as f:
            f.write(line)

        return estimated_cost
