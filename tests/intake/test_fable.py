"""The planner-chain gate in omniagentos/intake/fable.py.

REGRESSION (critic finding 4). ``_use_fallback_chain()`` used to be
``len(PLANNER_FALLBACKS) > 1`` against a value snapshotted at IMPORT time, so:

* a single-rung override (``OMNIAGENTOS_PLANNER_FALLBACKS=sol``) silently ran
  the hard-coded legacy Fable call instead of Sol — the operator asked for a
  different provider and got claude anyway; and
* the decision could not see an env change made after import at all.

The rule now: unset -> the DEFAULT chain; the exact value ``fable`` -> the
legacy single call; EVERYTHING else -> the chain resolver, rung for rung.

Entirely offline: the adapter registry is mocked, no CLI is ever launched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.intake import fable as fable_module


def _ok(payload: dict[str, Any]) -> AgentResult:
    return AgentResult(
        status=ResultStatus.OK,
        output_json=payload,
        usage=AgentUsage(wall_ms=10, turns=1, estimated=True),
    )


class TestUseFallbackChain:
    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            (None, True),  # unset -> the DEFAULT chain
            ("", True),  # blank is not a pin
            ("   ", True),
            ("fable", False),  # the ONE legacy value
            ("FABLE", False),  # ...case-insensitively
            (" fable ", False),
            ("sol", True),  # the finding: a single non-fable rung
            ("opus", True),
            ("gemini", True),
            ("fable:opus", True),
            ("fable:opus:sol", True),
            ("sol:fable", True),
        ],
    )
    def test_only_an_explicit_fable_keeps_the_legacy_path(
        self, monkeypatch: pytest.MonkeyPatch, env: str | None, expected: bool
    ) -> None:
        if env is None:
            monkeypatch.delenv("OMNIAGENTOS_PLANNER_FALLBACKS", raising=False)
        else:
            monkeypatch.setenv("OMNIAGENTOS_PLANNER_FALLBACKS", env)
        assert fable_module._use_fallback_chain() is expected

    def test_the_env_is_read_at_call_time_not_import_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The import-time snapshot is exactly how the bug hid."""
        monkeypatch.setenv("OMNIAGENTOS_PLANNER_FALLBACKS", "fable")
        assert fable_module._use_fallback_chain() is False
        monkeypatch.setenv("OMNIAGENTOS_PLANNER_FALLBACKS", "sol")
        assert fable_module._use_fallback_chain() is True


class TestRunFableJsonRouting:
    """What the gate actually decides: which model serves the plan."""

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_a_single_rung_sol_override_actually_runs_sol(
        self, mock_resolve: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE finding: `=sol` must not quietly run the legacy Fable call."""
        monkeypatch.setenv("OMNIAGENTOS_PLANNER_FALLBACKS", "sol")
        adapter = MagicMock()
        adapter.run.return_value = _ok({"from": "sol"})
        mock_resolve.return_value = adapter

        result = fable_module.run_fable_json("plan it", {}, effort="high")

        assert result == {"from": "sol"}
        assert adapter.run.call_count == 1
        agent_input = adapter.run.call_args[0][0]
        assert agent_input.model == "gpt-5.6-sol"  # the sol rung, not fable
        assert mock_resolve.call_args[0][0] == "cli-codex"

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_an_explicit_fable_pin_uses_the_legacy_single_call(
        self, mock_resolve: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_PLANNER_FALLBACKS", "fable")
        adapter = MagicMock()
        adapter.run.return_value = _ok({"from": "fable"})
        mock_resolve.return_value = adapter

        result = fable_module.run_fable_json("plan it", {}, effort="max")

        assert result == {"from": "fable"}
        assert adapter.run.call_count == 1
        agent_input = adapter.run.call_args[0][0]
        assert agent_input.model == fable_module.FABLE_MODEL
        assert agent_input.metadata.get("effort") == "max"

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_a_multi_rung_override_is_honored_rung_for_rung(
        self, mock_resolve: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_PLANNER_FALLBACKS", "sol:opus")
        adapter = MagicMock()
        adapter.run.side_effect = [
            AgentResult(
                status=ResultStatus.ERROR,
                error="Rate limit exceeded",
                usage=AgentUsage(wall_ms=10, turns=1, estimated=True),
            ),
            _ok({"from": "opus"}),
        ]
        mock_resolve.return_value = adapter

        assert fable_module.run_fable_json("plan it", {}) == {"from": "opus"}
        models = [call[0][0].model for call in adapter.run.call_args_list]
        assert models == ["gpt-5.6-sol", "opus"]

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_a_grok_only_override_never_touches_the_claude_adapter(
        self, mock_resolve: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_PLANNER_FALLBACKS", "grok")
        adapter = MagicMock()
        adapter.run.return_value = _ok({"from": "grok"})
        mock_resolve.return_value = adapter

        assert fable_module.run_fable_json("plan it", {}) == {"from": "grok"}
        assert mock_resolve.call_args[0][0] == "cli-grok"
        assert adapter.run.call_args[0][0].model == "grok-4.5"
