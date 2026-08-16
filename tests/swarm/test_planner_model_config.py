"""Swarm planner model is config/env-selectable (default Qwen, not hard-wired Fable).

Covers register item O-4 and the concurrent-sim rate-limit fix:
* module default + env override + strict parser (role_pack_mode pattern)
* SELECTED model reaches the call site (counterfeit-resistant)
* env=qwen does NOT invoke fable/claude at all
* REVERT-TEST: ``test_default_planner_is_not_hardwired_fable`` fails if the
  hard-wired Fable default is restored
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from omniagentos.swarm import planner as planner_mod
from omniagentos.swarm.planner import (
    DEFAULT_SWARM_PLANNER_EFFORT,
    DEFAULT_SWARM_PLANNER_MODEL,
    SWARM_PLANNER_EFFORT_ENV,
    SWARM_PLANNER_FORMATION_SENTINEL,
    SWARM_PLANNER_MODEL_ENV,
    SWARM_PLANNER_MODELS,
    make_swarm_planner_llm,
    parse_swarm_planner_effort,
    parse_swarm_planner_model,
    plan_swarm,
    swarm_planner_effort,
    swarm_planner_model,
)

# ---------------------------------------------------------------------------
# Strict parsers (role_pack_mode contract)
# ---------------------------------------------------------------------------


class TestParseSwarmPlannerModel:
    def test_default_is_a_known_non_anthropic_model(self) -> None:
        # Asserting a literal pinned this to whichever model was current the day
        # it was written; the default moved three times in one afternoon as
        # latency and plan-quality measurements came in. The INVARIANT is what
        # matters: the default must be selectable and must not be an Anthropic
        # rung (the hardwired fable default cost 296s per plan and burned two
        # Anthropic rungs before succeeding).
        assert DEFAULT_SWARM_PLANNER_MODEL in SWARM_PLANNER_MODELS
        assert not any(
            a in DEFAULT_SWARM_PLANNER_MODEL.lower()
            for a in ("fable", "opus", "claude", "sonnet", "haiku")
        )
        assert DEFAULT_SWARM_PLANNER_EFFORT == "low"

    def test_known_proxy_and_alias(self) -> None:
        assert parse_swarm_planner_model("qwen37-plus") == "qwen37-plus"
        # The "qwen" alias targets a specific model and is deliberately INDEPENDENT
        # of DEFAULT_SWARM_PLANNER_MODEL; coupling them was a wrong "fix".
        assert parse_swarm_planner_model("qwen") == "qwen37-plus"
        assert parse_swarm_planner_model("Qwen37-Plus") == "qwen37-plus"

    def test_known_fable_aliases(self) -> None:
        assert parse_swarm_planner_model("fable") == "fable"
        assert parse_swarm_planner_model("opus") == "opus"
        # "sol" removed from planner aliases (DEFECT #2 fix: ambiguity → impossible to hit).
        # Unrecognised "sol" falls back to DEFAULT_SWARM_PLANNER_MODEL.
        assert parse_swarm_planner_model("sol") == DEFAULT_SWARM_PLANNER_MODEL

    def test_formation_sentinel(self) -> None:
        assert parse_swarm_planner_model("formation") == SWARM_PLANNER_FORMATION_SENTINEL

    def test_unrecognised_falls_back(self) -> None:
        assert parse_swarm_planner_model("not-a-real-model") == DEFAULT_SWARM_PLANNER_MODEL
        assert parse_swarm_planner_model("") == DEFAULT_SWARM_PLANNER_MODEL
        assert parse_swarm_planner_model(None) == DEFAULT_SWARM_PLANNER_MODEL
        assert parse_swarm_planner_model(123) == DEFAULT_SWARM_PLANNER_MODEL

    def test_effort_parser(self) -> None:
        assert parse_swarm_planner_effort("low") == "low"
        assert parse_swarm_planner_effort("HIGH") == "high"
        assert parse_swarm_planner_effort("nope") == DEFAULT_SWARM_PLANNER_EFFORT
        assert parse_swarm_planner_effort(None) == DEFAULT_SWARM_PLANNER_EFFORT


class TestSwarmPlannerModelResolution:
    def test_env_wins_over_config_and_formation(self) -> None:
        assert (
            swarm_planner_model(
                {SWARM_PLANNER_MODEL_ENV: "qwen37-flash"},
                config={"model": "fable"},
                formation_planner="sol",
            )
            == "qwen37-flash"
        )

    def test_unrecognised_env_falls_through_to_config(self) -> None:
        assert (
            swarm_planner_model(
                {SWARM_PLANNER_MODEL_ENV: "typo-model"},
                config={"model": "qwen37-flash"},
            )
            == "qwen37-flash"
        )

    def test_formation_sentinel_uses_formation_planner(self) -> None:
        # "sol" no longer a known planner alias (DEFECT #2 fix); test with "fable" instead
        assert (
            swarm_planner_model(
                {SWARM_PLANNER_MODEL_ENV: "formation"},
                formation_planner="fable",
            )
            == "fable"
        )

    def test_auto_formation_does_not_pull_anthropic_aliases(self) -> None:
        # Without env/config, formation.planner=sol must NOT override the Qwen
        # default (concurrent-sim rate-limit safety).
        assert (
            swarm_planner_model(
                {},
                config={},
                formation_planner="sol",
            )
            == DEFAULT_SWARM_PLANNER_MODEL
        )

    def test_auto_formation_proxy_planner_applies(self) -> None:
        assert (
            swarm_planner_model(
                {},
                config={},
                formation_planner="qwen37-flash",
            )
            == "qwen37-flash"
        )

    def test_effort_env_wins(self) -> None:
        assert (
            swarm_planner_effort(
                {SWARM_PLANNER_EFFORT_ENV: "medium"},
                config={"effort": "high"},
            )
            == "medium"
        )

    def test_effort_default_is_low(self) -> None:
        assert swarm_planner_effort({}, config={}) == "low"


# ---------------------------------------------------------------------------
# COUNTERFEIT-resistant: selected model reaches the call; qwen never hits fable
# ---------------------------------------------------------------------------


class RecordingProxyClient:
    """Stand-in for ShortCallClient that records the model it was asked for."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = (
            response
            if response is not None
            else {
                "goal": "test",
                "tasks": [
                    {
                        "id": "t1",
                        "title": "Task one",
                        "owned_paths": ["src/a"],
                        "est_agent_minutes": 10,
                    },
                    {
                        "id": "t2",
                        "title": "Task two",
                        "owned_paths": ["src/b"],
                        "est_agent_minutes": 10,
                    },
                ],
            }
        )

    def complete_json(
        self,
        messages: list[dict[str, str]],
        required_keys: list[str],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "default",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "model": model,
                "purpose": purpose,
                "required_keys": required_keys,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.response is None:
            raise RuntimeError("proxy unavailable")
        return self.response


class TestCounterfeitSelectedModelReachesCall:
    def test_env_qwen_reaches_proxy_and_never_calls_fable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COUNTERFEIT guard: env is not merely READ — the selected model is
        the one the backend is asked for, and fable/claude are not invoked."""
        recorder = RecordingProxyClient()
        fable_calls: list[Any] = []

        def _boom_fable(*_a: Any, **_k: Any) -> dict[str, Any] | None:
            fable_calls.append((_a, _k))
            raise AssertionError("fable must not be invoked when env selects qwen")

        monkeypatch.setenv(SWARM_PLANNER_MODEL_ENV, "qwen37-plus")
        # Isolate from a config pin that might differ.
        monkeypatch.setattr(planner_mod, "_swarm_planner_config_section", lambda: {})

        llm = make_swarm_planner_llm(client=recorder, env=os_environ_with_qwen())
        # Also patch the module-level default path's fable entry so any leak fails.
        monkeypatch.setattr(planner_mod, "_fable_swarm_planner_llm", _boom_fable)

        result = llm("plan this multi-part goal", {"type": "object"}, "low")

        assert result is not None
        assert isinstance(result.get("tasks"), list)
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["model"] == "qwen37-plus"
        assert recorder.calls[0]["purpose"] == "swarm_planner"
        assert fable_calls == []
        assert llm.model == "qwen37-plus"

    def test_default_model_attribute_tracks_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SWARM_PLANNER_MODEL_ENV, raising=False)
        monkeypatch.setattr(planner_mod, "_swarm_planner_config_section", lambda: {})
        llm = make_swarm_planner_llm(env={}, config={})
        assert llm.model == DEFAULT_SWARM_PLANNER_MODEL

    def test_plan_swarm_default_path_uses_resolved_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """plan_swarm() without planner_llm= uses make_swarm_planner_llm, and
        the selected model reaches the proxy client."""
        recorder = RecordingProxyClient()
        fable_calls: list[Any] = []

        def _boom_fable(*_a: Any, **_k: Any) -> dict[str, Any] | None:
            fable_calls.append(1)
            raise AssertionError("fable must not run on default qwen path")

        monkeypatch.setenv(SWARM_PLANNER_MODEL_ENV, "qwen37-plus")
        monkeypatch.setattr(planner_mod, "_swarm_planner_config_section", lambda: {})
        monkeypatch.setattr(planner_mod, "_fable_swarm_planner_llm", _boom_fable)
        # Inject recording client into make_swarm_planner_llm via wrapper.
        real_make = planner_mod.make_swarm_planner_llm

        def _make_with_client(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("client", recorder)
            return real_make(*args, **kwargs)

        monkeypatch.setattr(planner_mod, "make_swarm_planner_llm", _make_with_client)

        def _clarify(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            return {
                "mode": "spec",
                "spec": {
                    "title": "Ship two modules",
                    "description": "Implement module A and module B independently.",
                    "acceptance_criteria": ["both modules land"],
                },
            }

        plan = plan_swarm(
            "Implement module A and module B independently.",
            str(tmp_path),
            clarify_llm=_clarify,
            recall_fn=lambda _g: "",
            playbook_path=tmp_path / "missing.json",
        )

        assert len(recorder.calls) >= 1
        assert recorder.calls[0]["model"] == "qwen37-plus"
        assert fable_calls == []
        assert any("swarm_planner: model=qwen37-plus" in a for a in plan.assumptions)
        # O-4: formation.planner stamped with the model that actually planned.
        if plan.formation is not None:
            assert plan.formation.planner == "qwen37-plus"


def os_environ_with_qwen() -> dict[str, str]:
    return {SWARM_PLANNER_MODEL_ENV: "qwen37-plus"}


# ---------------------------------------------------------------------------
# REVERT-TEST target: fails if default is hard-wired back to Fable
# ---------------------------------------------------------------------------


class TestDefaultIsNotHardwiredFable:
    def test_default_planner_is_not_hardwired_fable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REVERT-TEST: restore the hard-wired Fable default and this fails.

        Named so the verification procedure can break the fix, run this test,
        capture the verbatim failure, restore, and re-run.
        """
        monkeypatch.delenv(SWARM_PLANNER_MODEL_ENV, raising=False)
        monkeypatch.setattr(planner_mod, "_swarm_planner_config_section", lambda: {})

        assert DEFAULT_SWARM_PLANNER_MODEL in SWARM_PLANNER_MODELS
        assert swarm_planner_model(env={}, config={}) == DEFAULT_SWARM_PLANNER_MODEL
        assert swarm_planner_effort(env={}, config={}) == "low"

        llm = make_swarm_planner_llm(env={}, config={})
        assert llm.model == DEFAULT_SWARM_PLANNER_MODEL
        # The default callable must be a proxy-path model, not a Fable alias.
        assert planner_mod._is_proxy_planner_model(llm.model)
        assert llm.model not in planner_mod.SWARM_PLANNER_FABLE_ALIASES

    def test_default_swarm_planner_llm_uses_proxy_not_fable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """default_swarm_planner_llm itself must not call run_fable_json on the
        Qwen default path (the pre-fix hard-wire)."""
        recorder = RecordingProxyClient()
        fable_mock = MagicMock(side_effect=AssertionError("fable leak"))

        monkeypatch.delenv(SWARM_PLANNER_MODEL_ENV, raising=False)
        monkeypatch.setattr(planner_mod, "_swarm_planner_config_section", lambda: {})
        monkeypatch.setattr(planner_mod, "_fable_swarm_planner_llm", fable_mock)

        # Route proxy path through the recorder.
        def _proxy(prompt, schema, effort, *, model, client=None):
            return recorder.complete_json(
                [{"role": "user", "content": prompt}],
                [],
                model=model,
                purpose="swarm_planner",
            )

        monkeypatch.setattr(planner_mod, "_proxy_swarm_planner_llm", _proxy)

        out = planner_mod.default_swarm_planner_llm("decompose this", {"type": "object"}, "low")
        assert out is not None
        assert recorder.calls[0]["model"] == DEFAULT_SWARM_PLANNER_MODEL
        fable_mock.assert_not_called()
