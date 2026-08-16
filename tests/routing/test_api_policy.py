"""The HARD deny-list for direct/paid API routing (omniagentos/routing/api_policy.py).

This is the invariant the whole api-tier fallback rests on, so it is tested as a
rule, not as an implementation detail:

  * claude/anthropic lineage NEVER leaves the Claude subscription CLI;
  * gpt-*/codex lineage NEVER leaves the Codex subscription CLI;
  * only grok, gemini and the explicitly configured openrouter_models may use an
    API path at all — everything else fails CLOSED;
  * the deny-list beats the allow-list: listing an anthropic id in
    api_fallback.openrouter_models makes the build RAISE, it does not enable it.

Entirely offline: no network, no CLI, no credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omniagentos.routing.api_policy import (
    ALLOWED_API_LINEAGES,
    API_PATH_DIRECT,
    API_PATH_LITELLM,
    API_PATH_OPENROUTER,
    DENIED_API_LINEAGES,
    LINEAGE_UNKNOWN,
    ApiRoutePolicyError,
    api_path_for_base,
    api_route_denial,
    assert_api_route_allowed,
    is_api_route_allowed,
    litellm_api_base,
    model_lineage,
    openrouter_models,
)

API_PATHS = (API_PATH_LITELLM, API_PATH_OPENROUTER)
ALL_API_PATHS = (API_PATH_LITELLM, API_PATH_OPENROUTER, API_PATH_DIRECT)


def _repo_root() -> Path:
    import omniagentos

    return Path(omniagentos.__file__).resolve().parent.parent


def _write_swarm_config(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "swarm.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class TestClaudeLineageIsNeverApiRoutable:
    """Rule 1. Subscription `claude` CLI or nothing."""

    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-5",
            "anthropic/claude-opus-5",
            "claude-sonnet-5",
            "claude-haiku-4.5",
            "claude-fable-5",
            # The planner's own bare rung names must resolve too, or the
            # deny-list would be bypassed by the chain that calls it.
            "fable",
            "opus",
            "haiku",
            # An id the model registry has never seen still trips the marker.
            "anthropic/claude-opus-9-experimental",
        ],
    )
    @pytest.mark.parametrize("path", API_PATHS)
    def test_denied_on_every_api_path(self, model: str, path: str) -> None:
        with pytest.raises(ApiRoutePolicyError) as excinfo:
            assert_api_route_allowed(model, path=path)
        assert "claude" in str(excinfo.value)
        assert is_api_route_allowed(model, path=path) is False


class TestGptLineageIsNeverApiRoutable:
    """Rule 2. Subscription `codex` CLI or nothing."""

    @pytest.mark.parametrize(
        "model",
        [
            "gpt-5.6-sol",
            "openai/gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-7-unreleased",
            "sol",
            "codex",
        ],
    )
    @pytest.mark.parametrize("path", API_PATHS)
    def test_denied_on_every_api_path(self, model: str, path: str) -> None:
        with pytest.raises(ApiRoutePolicyError):
            assert_api_route_allowed(model, path=path)


class TestAllowedApiCandidates:
    """Rule 3. grok + gemini by lineage, everything else by explicit config."""

    @pytest.mark.parametrize(
        "model",
        [
            "grok-4.5",
            "x-ai/grok-4.5",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "google/gemini-3.6-flash",
        ],
    )
    @pytest.mark.parametrize("path", API_PATHS)
    def test_grok_and_gemini_are_allowed(self, model: str, path: str) -> None:
        assert_api_route_allowed(model, path=path)  # does not raise
        assert is_api_route_allowed(model, path=path) is True

    @pytest.mark.parametrize(
        "model", ["deepseek/deepseek-v4-pro", "qwen/qwen3.7-max", "x-ai/grok-4.5"]
    )
    def test_shipped_openrouter_models_are_allowed(self, model: str) -> None:
        assert model in openrouter_models()
        assert_api_route_allowed(model, path=API_PATH_OPENROUTER)

    def test_a_registry_alias_of_a_listed_model_is_allowed(self) -> None:
        """`deepseek-v4-pro` is the same model as the listed `deepseek/deepseek-v4-pro`."""
        assert_api_route_allowed("deepseek-v4-pro", path=API_PATH_OPENROUTER)

    def test_configured_list_wins_over_the_shipped_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _write_swarm_config(
            tmp_path, {"api_fallback": {"openrouter_models": ["minimax/minimax-m3"]}}
        )
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(config))

        assert openrouter_models() == ("minimax/minimax-m3",)
        assert_api_route_allowed("minimax/minimax-m3", path=API_PATH_OPENROUTER)
        # ...and a model that is only in the SHIPPED default is now denied.
        with pytest.raises(ApiRoutePolicyError):
            assert_api_route_allowed("deepseek/deepseek-v4-pro", path=API_PATH_OPENROUTER)


class TestFailsClosed:
    """Rule 4 + the argument surface: anything undecidable is denied."""

    @pytest.mark.parametrize("model", ["mystery-model-9", "kimi-k3", "llama-5", ""])
    def test_unlisted_or_unknown_models_are_denied(self, model: str) -> None:
        with pytest.raises(ApiRoutePolicyError):
            assert_api_route_allowed(model, path=API_PATH_OPENROUTER)

    @pytest.mark.parametrize("path", ["", "http", "cli-claude", "bedrock"])
    def test_unknown_api_paths_are_denied(self, path: str) -> None:
        with pytest.raises(ApiRoutePolicyError):
            assert_api_route_allowed("grok-4.5", path=path)

    def test_deny_list_beats_the_configured_allow_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE invariant: config cannot buy a claude/gpt model an API route."""
        config = _write_swarm_config(
            tmp_path,
            {
                "api_fallback": {
                    "openrouter_models": [
                        "anthropic/claude-opus-5",
                        "openai/gpt-5.6-sol",
                        "x-ai/grok-4.5",
                    ]
                }
            },
        )
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(config))

        for denied in ("anthropic/claude-opus-5", "openai/gpt-5.6-sol"):
            assert denied in openrouter_models()  # it IS in the candidate list
            with pytest.raises(ApiRoutePolicyError):  # ...and still denied
                assert_api_route_allowed(denied, path=API_PATH_OPENROUTER)
        assert_api_route_allowed("x-ai/grok-4.5", path=API_PATH_OPENROUTER)


class TestLineageResolution:
    @pytest.mark.parametrize(
        ("model", "lineage"),
        [
            ("fable", "claude"),
            ("claude-opus-5", "claude"),
            ("anthropic/claude-haiku-4.5", "claude"),
            ("sol", "codex"),
            ("gpt-5.6-sol", "codex"),
            ("grok-4.5", "grok"),
            ("x-ai/grok-4.3", "grok"),
            ("gemini-3.6-flash", "gemini"),
            ("deepseek/deepseek-v4-pro", "deepseek"),
            ("qwen/qwen3.7-max", "qwen"),
            ("kimi-k3", "kimi"),
            ("totally-made-up", "unknown"),
        ],
    )
    def test_lineage(self, model: str, lineage: str) -> None:
        assert model_lineage(model) == lineage


class TestVendorPrefixCannotLaunderADeniedLineage:
    """REGRESSION (critic blocker 1). A namespace is not a lineage.

    `google/claude-opus-5` and `x-ai/openai/gpt-5.6-sol` were ALLOWED: the
    first-segment vendor lookup ran before the denied-lineage markers, so any
    denied model became routable by typing an approved vendor in front of it.
    The complete identifier is now scanned FIRST — every segment, every token,
    the punctuation-stripped whole, unicode-folded — and a denied signal
    anywhere wins over every other signal.
    """

    #: The adversarial battery. Each of these must be denied on EVERY api path.
    LAUNDERING_ATTEMPTS = [
        # The two ids from the finding, verbatim.
        "google/claude-opus-5",
        "x-ai/openai/gpt-5.6-sol",
        # Nested / repeated prefixes.
        "google/anthropic/claude-opus-5",
        "x-ai/google/anthropic/claude-opus-5",
        "google/openai/gpt-5.6-sol",
        "x-ai/grok-4.5/../anthropic/claude-opus-5",
        # An allowed vendor in front of a bare planner rung name.
        "google/fable",
        "google/opus",
        "x-ai/sol",
        "google/o3",
        # A listed low-cost vendor in front of a denied model.
        "deepseek/claude-3-opus",
        "qwen/gpt-5.6-sol",
        "minimax/claude-sonnet-5",
        # Mixed / upper case.
        "GOOGLE/Claude-Opus-5",
        "Google/CLAUDE-opus-5",
        "X-AI/OpenAI/GPT-5.6-SOL",
        # Whitespace, inside and out.
        "  google/claude-opus-5  ",
        "google/claude opus 5",
        "google/ claude-opus-5",
        # Punctuation / separator variants.
        "google/cl.aude-opus-5",
        "google/c_l_a_u_d_e-opus-5",
        "google/c l a u d e-opus-5",
        "google/claude..opus..5",
        # Unicode: fullwidth, Cyrillic homoglyphs, zero-width space, combining
        # accent, BOM. Folded to ASCII before the markers run; anything still
        # non-ASCII afterwards resolves to unknown, which is denied too.
        "google/ｃlaude-opus-5",
        "google/сlaude-opus-5",
        "google/сlаudе-opus-5",
        "google/cla​ude-opus-5",
        "google/cláude-opus-5",
        "﻿google/claude-opus-5",
        "google/ɡpt-5.6-sol",
        # The reverse direction: a denied vendor with an approved-looking model.
        "anthropic/gemini-3.6-flash",
        "openai/grok-4.5",
    ]

    @pytest.mark.parametrize("model", LAUNDERING_ATTEMPTS)
    @pytest.mark.parametrize("path", ALL_API_PATHS)
    def test_denied_on_every_api_path(self, model: str, path: str) -> None:
        with pytest.raises(ApiRoutePolicyError):
            assert_api_route_allowed(model, path=path)
        assert is_api_route_allowed(model, path=path) is False

    @pytest.mark.parametrize("model", LAUNDERING_ATTEMPTS)
    def test_the_lineage_itself_is_never_reported_as_allowed(self, model: str) -> None:
        """`model_lineage` must not hand an allowed answer to any other caller."""
        assert model_lineage(model) not in ALLOWED_API_LINEAGES

    @pytest.mark.parametrize(
        "model",
        [
            "google/claude-opus-5",
            "deepseek/claude-3-opus",
            "GOOGLE/Claude-Opus-5",
            "google/сlaude-opus-5",
        ],
    )
    def test_a_claude_prefix_attack_is_reported_as_claude(self, model: str) -> None:
        assert model_lineage(model) == "claude"

    @pytest.mark.parametrize(
        "model", ["x-ai/openai/gpt-5.6-sol", "qwen/gpt-5.6-sol", "x-ai/sol", "google/o3"]
    )
    def test_a_gpt_prefix_attack_is_reported_as_codex(self, model: str) -> None:
        assert model_lineage(model) == "codex"

    def test_a_planted_claude_id_in_the_config_still_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blocker 1 x blocker 2: the prefix trick, planted in openrouter_models."""
        planted = ["google/claude-opus-5", "x-ai/openai/gpt-5.6-sol"]
        config = _write_swarm_config(
            tmp_path, {"api_fallback": {"openrouter_models": [*planted, "x-ai/grok-4.5"]}}
        )
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(config))

        for model in planted:
            assert model in openrouter_models()  # it IS in the candidate list
            with pytest.raises(ApiRoutePolicyError):  # ...and still denied
                assert_api_route_allowed(model, path=API_PATH_OPENROUTER)
        assert_api_route_allowed("x-ai/grok-4.5", path=API_PATH_OPENROUTER)

    def test_the_chain_builder_refuses_a_planted_prefix_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: the planner chain build raises, it does not skip the rung."""
        from omniagentos.intake import fallback as fallback_module

        config = _write_swarm_config(
            tmp_path, {"api_fallback": {"openrouter_models": ["google/claude-opus-5"]}}
        )
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(config))
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

        with pytest.raises(ApiRoutePolicyError):
            fallback_module.default_chain_rungs()


class TestConfigListingIsNecessaryButNotSufficient:
    """REGRESSION (critic blocker 2). Being listed is not a lineage.

    `_listed_for_api` used to return allow on its own, so an id whose lineage
    could not be established (`o3`, `mystery-model-9`) became API-routable just
    by appearing in `api_fallback.openrouter_models`. A listed model must ALSO
    resolve authoritatively to a known, non-denied lineage; LINEAGE_UNKNOWN is
    never allowed on an api path.
    """

    UNKNOWN_LINEAGE_IDS = [
        "mystery-model-9",
        "llama-5",
        "acme/mystery-model-9",
        "madeupvendor/gemini-3.6-flash",  # a fake vendor cannot confer gemini
        "x-ai/qwen3.7-max",  # two conflicting non-denied signals
    ]

    @pytest.mark.parametrize("model", UNKNOWN_LINEAGE_IDS)
    def test_unknown_lineage_is_denied_when_unlisted(self, model: str) -> None:
        assert model_lineage(model) == LINEAGE_UNKNOWN
        with pytest.raises(ApiRoutePolicyError):
            assert_api_route_allowed(model, path=API_PATH_OPENROUTER)

    @pytest.mark.parametrize("model", UNKNOWN_LINEAGE_IDS)
    def test_unknown_lineage_is_denied_even_when_listed(
        self, model: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _write_swarm_config(tmp_path, {"api_fallback": {"openrouter_models": [model]}})
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(config))

        assert model in openrouter_models()  # listed...
        with pytest.raises(ApiRoutePolicyError) as excinfo:  # ...and still denied
            assert_api_route_allowed(model, path=API_PATH_OPENROUTER)
        assert "authoritative lineage" in str(excinfo.value)

    def test_o3_is_openai_lineage_not_merely_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The finding's example: an o-series id is codex, listed or not."""
        config = _write_swarm_config(
            tmp_path, {"api_fallback": {"openrouter_models": ["o3", "o4-mini"]}}
        )
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(config))

        for model in ("o3", "o4-mini"):
            assert model_lineage(model) == "codex"
            assert model_lineage(model) in DENIED_API_LINEAGES
            with pytest.raises(ApiRoutePolicyError):
                assert_api_route_allowed(model, path=API_PATH_OPENROUTER)

    @pytest.mark.parametrize("model", ["moonshotai/kimi-k3", "minimax/minimax-m3"])
    def test_a_known_non_denied_lineage_is_still_enabled_by_listing(
        self, model: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The allow-list still WORKS — it just cannot stand in for a lineage."""
        config = _write_swarm_config(tmp_path, {"api_fallback": {"openrouter_models": [model]}})
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(config))

        assert model_lineage(model) not in DENIED_API_LINEAGES
        assert model_lineage(model) != LINEAGE_UNKNOWN
        assert_api_route_allowed(model, path=API_PATH_OPENROUTER)  # does not raise
        # ...and the same model is denied the moment it leaves the list.
        monkeypatch.delenv("OMNIAGENTOS_SWARM_CONFIG")
        with pytest.raises(ApiRoutePolicyError):
            assert_api_route_allowed(model, path=API_PATH_OPENROUTER)


class TestLegitimateApiRoutingIsNotOverDenied:
    """The other half of the invariant: grok/gemini must still get through.

    A deny-list that denies everything is not a fix. These are the paths the
    planner chain and modelintel actually use.
    """

    @pytest.mark.parametrize(
        "model",
        [
            "grok-4.5",
            "grok-4.3",
            "grok-build-0.1",
            "x-ai/grok-4.5",
            "x-ai/grok-4.3",
            "gemini-3.6-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-pro",
            "google/gemini-3.6-flash",
            "google/gemini-3.1-pro-preview-customtools",
            # Case and whitespace variants of a legitimate id.
            "GROK-4.5",
            "  gemini-3.6-flash  ",
        ],
    )
    @pytest.mark.parametrize("path", ALL_API_PATHS)
    def test_grok_and_gemini_route_on_every_path(self, model: str, path: str) -> None:
        assert_api_route_allowed(model, path=path)  # does not raise
        assert is_api_route_allowed(model, path=path) is True

    @pytest.mark.parametrize(
        "model", ["deepseek/deepseek-v4-pro", "qwen/qwen3.7-max", "x-ai/grok-4.5"]
    )
    def test_the_shipped_openrouter_list_still_routes(self, model: str) -> None:
        assert_api_route_allowed(model, path=API_PATH_OPENROUTER)

    def test_the_planner_chains_api_rungs_still_build(self) -> None:
        from omniagentos.intake import fallback as fallback_module

        rungs = fallback_module._resolve_chain("gemini-flash-api:gemini-lite-api:openrouter")
        assert [rung.name for rung in rungs] == [
            "gemini-flash-api",
            "gemini-lite-api",
            "openrouter",
        ]

    @pytest.mark.parametrize(
        "model", ["upstage/solar-10.7b", "x-ai/grok-solar-1", "consolidator-7b"]
    )
    def test_the_sol_marker_does_not_swallow_words_containing_it(self, model: str) -> None:
        """`sol` is a whole-token marker: `solar`/`console` are not codex."""
        assert model_lineage(model) not in DENIED_API_LINEAGES

    def test_a_grok_model_whose_name_contains_solar_still_routes(self) -> None:
        assert_api_route_allowed("x-ai/grok-solar-1", path=API_PATH_OPENROUTER)


class TestDirectPathAndDenialHelper:
    """`api_path_for_base` / `api_route_denial` — the seam modelintel calls."""

    def test_the_loopback_proxy_is_the_litellm_path(self) -> None:
        assert api_path_for_base("http://localhost:4000/v1") == API_PATH_LITELLM
        assert api_path_for_base("http://127.0.0.1:4000/v1/") == API_PATH_LITELLM

    def test_a_vendor_endpoint_is_the_direct_path(self) -> None:
        assert api_path_for_base("https://api.x.ai/v1") == API_PATH_DIRECT

    def test_an_allowed_model_has_no_denial(self) -> None:
        assert api_route_denial("grok-4.5", api_base="https://api.x.ai/v1") is None
        assert api_route_denial("gemini-3.6-flash", api_base="http://localhost:4000/v1") is None

    @pytest.mark.parametrize(
        ("model", "api_base"),
        [
            ("claude-opus-5", "https://api.x.ai/v1"),
            ("google/claude-opus-5", "http://localhost:4000/v1"),
            ("gpt-5.6-sol", "https://api.x.ai/v1"),
            ("mystery-model-9", "http://localhost:4000/v1"),
        ],
    )
    def test_a_denied_model_returns_a_policy_deny_reason(self, model: str, api_base: str) -> None:
        denial = api_route_denial(model, api_base=api_base)
        assert denial is not None
        assert denial.startswith("POLICY DENY")


class TestModelIntelRouterIsGated:
    """REGRESSION (critic blocker 3). modelintel's HTTP calls had NO gate.

    `router._call_gemini` posted straight to the LiteLLM proxy, so pointing
    `router_gemini.model` at a claude/gpt id in configs/modelintel.yaml sent
    that lineage over an API path. Both router calls (and the research sweep)
    now clear api_policy before a request object exists; a denial degrades to
    the mechanical scorer instead of raising, because `route()` is contracted
    to always return a verdict.
    """

    @pytest.fixture
    def router_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        import json

        from omniagentos.modelintel import router as router_mod

        rankings = tmp_path / "rankings.json"
        rankings.write_text(
            json.dumps(
                {
                    "agents": [
                        {
                            "id": "fast-coder",
                            "provider": "codex",
                            "model": "fast",
                            "role": "coder",
                            "capabilityTier": "architectural",
                            "maxReasoning": "xhigh",
                            "available": True,
                            "warmLatencyMs": 1000,
                            "codingScore": 0.7,
                            "toolUseScore": 0.8,
                            "costScore": 0.9,
                            "fastLane": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        digest = tmp_path / "digest.json"
        digest.write_text(
            json.dumps(
                {
                    "agents": [{"id": "fast-coder", "model": "fast", "lineage": "codex"}],
                    "domains": {},
                    "topByDomain": {},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(router_mod, "FUSION_RANKINGS", rankings)
        monkeypatch.setattr(router_mod, "FUSION_DIGEST", digest)
        monkeypatch.setattr(router_mod, "xai_api_key", lambda: "xai-test")
        monkeypatch.setattr(router_mod, "gemini_api_key", lambda: "gemini-test")

        def _never(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("a denied router model must never reach the network")

        monkeypatch.setattr(router_mod.requests, "post", _never)
        return router_mod

    def _config(self, router_model: str, gemini_model: str) -> Any:
        from omniagentos.modelintel.config import ModelIntelConfig, RouterConfig

        return ModelIntelConfig(
            router=RouterConfig(model=router_model, api_base="https://api.x.ai/v1"),
            router_gemini=RouterConfig(model=gemini_model, api_base="http://localhost:4000/v1"),
        )

    @pytest.mark.parametrize(
        "denied", ["claude-opus-5", "google/claude-opus-5", "gpt-5.6-sol", "o3"]
    )
    def test_a_denied_router_gemini_model_never_posts(
        self, router_env: Any, monkeypatch: pytest.MonkeyPatch, denied: str
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ROUTER_LLM", "gemini-flash")
        verdict = router_env.route("task", mode="fusionbuild", cfg=self._config("grok-4.5", denied))
        assert verdict.router == "mechanical-fallback"
        assert "POLICY DENY" in (verdict.fallback_reason or "")

    @pytest.mark.parametrize("denied", ["claude-opus-5", "x-ai/openai/gpt-5.6-sol"])
    def test_a_denied_incumbent_router_model_never_posts(
        self, router_env: Any, monkeypatch: pytest.MonkeyPatch, denied: str
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ROUTER_LLM", "default")
        verdict = router_env.route(
            "task", mode="fusionbuild", cfg=self._config(denied, "gemini-3.6-flash")
        )
        assert verdict.router == "mechanical-fallback"
        assert "POLICY DENY" in (verdict.fallback_reason or "")

    def test_shadow_mode_gates_both_calls(
        self, router_env: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ROUTER_SHADOW", "1")
        monkeypatch.delenv("OMNIAGENTOS_ROUTER_LLM", raising=False)
        verdict = router_env.route(
            "task",
            mode="fusionbuild",
            cfg=self._config("claude-opus-5", "anthropic/claude-opus-5"),
        )
        assert verdict.router == "mechanical-fallback"
        assert "POLICY DENY" in (verdict.fallback_reason or "")

    def test_the_shipped_router_models_are_still_allowed(self) -> None:
        """The gate must not break the real configs/modelintel.yaml routers."""
        from omniagentos.modelintel.config import load_config

        cfg = load_config()
        assert api_route_denial(cfg.router.model, api_base=cfg.router.api_base) is None
        assert (
            api_route_denial(cfg.router_gemini.model, api_base=cfg.router_gemini.api_base) is None
        )
        assert api_route_denial(cfg.research.model, api_base=cfg.research.api_base) is None

    def test_the_research_sweep_refuses_a_denied_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        from omniagentos.modelintel import research
        from omniagentos.modelintel.config import ModelIntelConfig, ResearchConfig

        def _never(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("a denied research model must never reach the network")

        monkeypatch.setattr(requests, "post", _never)
        cfg = ModelIntelConfig(
            research=ResearchConfig(model="google/claude-opus-5", api_base="https://api.x.ai/v1")
        )
        result = research.sweep(cfg, "xai-test")
        assert result.ok is False
        assert "POLICY DENY" in (result.error or "")


class TestApiFallbackConfig:
    def test_litellm_api_base_defaults_to_the_configured_proxy(self) -> None:
        assert litellm_api_base() == "http://localhost:4000/v1"

    def test_litellm_api_base_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNIAGENTOS_LITELLM_API_BASE", "http://127.0.0.1:9999/v1/")
        assert litellm_api_base() == "http://127.0.0.1:9999/v1"

    def test_missing_config_falls_back_to_the_shipped_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(tmp_path / "absent.yaml"))
        assert openrouter_models()  # never empty
        assert litellm_api_base() == "http://localhost:4000/v1"


class TestShippedSwarmConfig:
    """Deliverable 1: the optimistic per-account scheduler ceilings."""

    def test_every_provider_allows_twenty_inflight_per_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.routing import limit_state

        path = _repo_root() / "configs" / "swarm.yaml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        providers = config["limits"]["providers"]
        assert set(providers) == {"claude", "codex", "grok", "gemini", "kimi", "qwen"}
        for provider, entry in providers.items():
            assert entry["max_inflight_per_account"] == 20, provider

        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(path))
        for provider in providers:
            assert limit_state.max_inflight_per_account(provider) == 20


class TestOpenRouterAdapterGate:
    """The adapter re-checks policy itself: no chain, no bypass."""

    @pytest.fixture(autouse=True)
    def _isolated_spend_ledger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F5 (2026-08-12 review): the OpenRouter adapter now routes every call
        through the spend guard, so these tests must NEVER reserve/settle against
        the production ledger. Pin a scratch ``OMNIAGENTOS_SPEND_DB`` and force a
        fresh process-local guard bound to it."""
        import omniagentos.adapters.spend_guard as spend_guard

        monkeypatch.setenv("OMNIAGENTOS_SPEND_DB", str(tmp_path / "spend.sqlite3"))
        monkeypatch.setattr(spend_guard, "_DEFAULT_GUARD", None)

    def _adapter(self) -> Any:
        from omniagentos.adapters.openrouter import OpenRouterAdapter

        return OpenRouterAdapter()

    def _input(self, model: str) -> Any:
        from omniagentos.contracts import AgentInput, new_id

        return AgentInput(
            run_id=new_id("run"), task_id=new_id("tsk"), prompt="plan it", model=model
        )

    def test_unhealthy_without_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        health = self._adapter().health()
        assert health.healthy is False
        assert "key" in (health.detail or "")

    def test_healthy_with_a_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        assert self._adapter().health().healthy is True

    def test_denied_model_raises_before_any_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import requests

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

        def _never(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("a denied model must never reach the network")

        monkeypatch.setattr(requests, "post", _never)
        with pytest.raises(ApiRoutePolicyError):
            self._adapter().run(self._input("claude-opus-5"))
        with pytest.raises(ApiRoutePolicyError):
            self._adapter().run(self._input("gpt-5.6-sol"))

    def test_absent_key_is_an_error_result_not_an_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        from omniagentos.contracts import ResultStatus

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        def _never(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("no key means no request")

        monkeypatch.setattr(requests, "post", _never)
        result = self._adapter().run(self._input(""))
        assert result.status == ResultStatus.ERROR
        assert "api key" in (result.error or "")

    def test_allowed_model_posts_and_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import requests

        from omniagentos.contracts import ResultStatus

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        captured: dict[str, Any] = {}

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {
                    "choices": [{"message": {"content": '{"decision": "swarm"}'}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
                }

        def _post(url: str, **kwargs: Any) -> Any:
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            captured["headers"] = kwargs.get("headers")
            return _Response()

        monkeypatch.setattr(requests, "post", _post)

        adapter = self._adapter()
        input_obj = self._input("x-ai/grok-4.5")
        input_obj.output_schema = {"required": ["decision"]}
        result = adapter.run(input_obj)

        assert result.status == ResultStatus.OK
        assert result.output_json == {"decision": "swarm"}
        assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
        assert captured["json"]["model"] == "x-ai/grok-4.5"
        assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
        assert result.usage.input_tokens == 11
        assert result.usage.output_tokens == 3

    def test_a_failed_model_advances_to_the_next_configured_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The last rung in the chain exhausts its own cheap list before giving up."""
        import requests

        from omniagentos.contracts import ResultStatus

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        tried: list[str] = []

        class _Response:
            def __init__(self, ok: bool) -> None:
                self.status_code = 200 if ok else 503

            def json(self) -> dict[str, Any]:
                return {"choices": [{"message": {"content": '{"ok": 1}'}}]}

        def _post(url: str, **kwargs: Any) -> Any:
            model = kwargs["json"]["model"]
            tried.append(model)
            return _Response(ok=len(tried) > 1)

        monkeypatch.setattr(requests, "post", _post)

        result = self._adapter().run(self._input(""))

        assert result.status == ResultStatus.OK
        assert tried == list(openrouter_models())[:2]


class TestConfigCodeMirrorParity:
    """The shipped default tuple and the YAML list must never drift.

    Regression for the 2026-07-26 integration finding: qwen/qwen3-coder-flash was
    added to configs/swarm.yaml but not to DEFAULT_OPENROUTER_MODELS, so it was
    denied on the degraded path (api_fallback missing/unloadable).

    Exact sequence equality (order + multiplicity) is required — set equality
    masks duplicate YAML entries (e.g. a repeated qwen3-coder-flash) that make
    the configured path retry a model the fallback path only tries once.
    """

    def test_default_tuple_matches_shipped_config(self) -> None:
        from pathlib import Path

        import yaml

        from omniagentos.routing.api_policy import DEFAULT_OPENROUTER_MODELS

        config_path = Path(__file__).resolve().parents[2] / "configs" / "swarm.yaml"
        shipped = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        listed = (shipped.get("api_fallback") or {}).get("openrouter_models") or []
        assert tuple(listed) == DEFAULT_OPENROUTER_MODELS, (
            "configs/swarm.yaml api_fallback.openrouter_models and "
            "api_policy.DEFAULT_OPENROUTER_MODELS must match exactly (order and "
            "multiplicity); the code tuple is the fallback when the config "
            "section is unloadable, so drift changes degraded-path candidate "
            f"behavior. yaml={list(listed)!r} default={list(DEFAULT_OPENROUTER_MODELS)!r}"
        )

    def test_every_allowlisted_model_is_actually_routable(self) -> None:
        """Third mirror edge: allow-listed ⇒ routable.

        2026-07-29: z-ai/glm-5.2 was allow-listed with no modelintel lineage and
        was dead-on-arrival at chain build; a missing lineage registration must
        be a CI failure, not a runtime one.
        """
        from pathlib import Path

        import yaml

        from omniagentos.routing.api_policy import DEFAULT_OPENROUTER_MODELS

        config_path = Path(__file__).resolve().parents[2] / "configs" / "swarm.yaml"
        shipped = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        listed = (shipped.get("api_fallback") or {}).get("openrouter_models") or []
        for model in sorted({*listed, *DEFAULT_OPENROUTER_MODELS}):
            assert_api_route_allowed(model, path=API_PATH_OPENROUTER)


class TestGlmAndKimiBothHalvesRegistration:
    """2026-07-29 force-multiplier registration: z-ai/glm-5.2 + moonshotai/kimi-k2.6.

    BOTH halves are required by the task contract: the allow-list entry in
    configs/swarm.yaml AND a lineage declaration in configs/modelintel.yaml.

    Critical binding note for Kimi: ``_VENDOR_LINEAGE`` already maps
    ``moonshotai`` → ``kimi``, so ``model_lineage("moonshotai/kimi-k2.6")`` and
    ``assert_api_route_allowed`` stay green even after the modelintel block is
    deleted. That is exactly why a previous lane was REJECTED — removing the
    claimed registry fix did not move the suite. These tests therefore bind
    ``_registry_lineage`` / own modelintel key presence, not only the policy
    gate. Deleting the ``kimi-k2.6`` (or ``glm-5.2``) registry block MUST fail
    ``test_openrouter_id_has_modelintel_registry_lineage``.
    """

    #: (OpenRouter id, expected lineage, modelintel key)
    NEW_MODELS = (
        ("z-ai/glm-5.2", "glm", "glm-5.2"),
        ("moonshotai/kimi-k2.6", "kimi", "kimi-k2.6"),
    )

    @pytest.mark.parametrize(("openrouter_id", "lineage", "key"), NEW_MODELS)
    def test_openrouter_id_is_on_both_allow_list_surfaces(
        self, openrouter_id: str, lineage: str, key: str
    ) -> None:
        from omniagentos.routing.api_policy import DEFAULT_OPENROUTER_MODELS

        assert openrouter_id in openrouter_models()
        assert openrouter_id in DEFAULT_OPENROUTER_MODELS

    @pytest.mark.parametrize(("openrouter_id", "lineage", "key"), NEW_MODELS)
    def test_openrouter_id_has_modelintel_registry_lineage(
        self, openrouter_id: str, lineage: str, key: str
    ) -> None:
        """FAILS-ON-REVERT for the modelintel half (including Kimi).

        Must use ``_registry_lineage``, not ``model_lineage``: the latter is
        still ``kimi`` for moonshotai/* via the vendor hardcode after the
        registry block is removed, which is the false-green the first-pass
        reviewer demonstrated.
        """
        from omniagentos.routing.api_policy import _model_specs, _registry_lineage

        assert _registry_lineage(openrouter_id) == lineage, (
            f"{openrouter_id!r} must resolve via configs/modelintel.yaml "
            f"(key {key!r}, lineage {lineage!r}); vendor hardcode is not a "
            "substitute for the registry half"
        )
        assert _registry_lineage(key) == lineage
        keys = {spec_key for spec_key, _lin, _aliases in _model_specs()}
        assert key in keys, f"modelintel must have its own key {key!r}"

    @pytest.mark.parametrize(("openrouter_id", "lineage", "key"), NEW_MODELS)
    def test_openrouter_id_routes_on_openrouter(
        self, openrouter_id: str, lineage: str, key: str
    ) -> None:
        assert model_lineage(openrouter_id) == lineage
        assert_api_route_allowed(openrouter_id, path=API_PATH_OPENROUTER)

    def test_glm_allow_list_without_registry_is_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counterfeit: allow-list half alone for z-ai/glm-5.2 → POLICY DENY.

        z-ai is not a known vendor namespace, so without the modelintel entry
        lineage is unknown and the gate refuses — correctly.
        """
        from omniagentos.routing import api_policy as pol

        real = pol._model_specs()
        stripped = tuple(
            (k, lin, aliases)
            for k, lin, aliases in real
            if k != "glm-5.2" and "z-ai/glm-5.2" not in aliases and "glm-5.2" not in aliases
        )
        monkeypatch.setattr(pol, "_model_specs", lambda: stripped)

        assert "z-ai/glm-5.2" in openrouter_models()  # allow-list half still present
        assert pol._registry_lineage("z-ai/glm-5.2") is None
        with pytest.raises(ApiRoutePolicyError, match="authoritative lineage"):
            assert_api_route_allowed("z-ai/glm-5.2", path=API_PATH_OPENROUTER)

    def test_kimi_k26_is_not_an_alias_of_kimi_k3(self) -> None:
        """Own entry: aliasing onto k3 would bleed allow-list membership via
        ``_listed_for_api`` alias-set intersection.
        """
        from omniagentos.routing.api_policy import _model_specs

        found_k26 = False
        for key, _lineage, aliases in _model_specs():
            if key == "kimi-k3":
                assert "moonshotai/kimi-k2.6" not in aliases
                assert "kimi-k2.6" not in aliases
            if key == "kimi-k2.6":
                found_k26 = True
                assert "moonshotai/kimi-k2.6" in aliases
        assert found_k26, "kimi-k2.6 must be its own modelintel key"


def test_direct_kimi_carveout_requires_spend_cap_pricing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    from omniagentos.routing import api_policy as policy

    assert_api_route_allowed("kimi-k3", path=API_PATH_DIRECT)
    with pytest.raises(ApiRoutePolicyError, match="no pricing row"):
        assert_api_route_allowed("kimi-k2.6", path=API_PATH_DIRECT)

    shipped = yaml.safe_load(policy.DEFAULT_SPEND_CAPS_PATH.read_text(encoding="utf-8"))
    fireworks_models = shipped["providers"]["fireworks"]["models"]
    fireworks_models["kimi-k2.6"] = dict(fireworks_models["kimi-k3"])
    priced = tmp_path / "spend-caps.yaml"
    priced.write_text(yaml.safe_dump(shipped), encoding="utf-8")
    monkeypatch.setenv(policy.SPEND_CAPS_CONFIG_ENV, str(priced))

    assert_api_route_allowed("kimi-k2.6", path=API_PATH_DIRECT)
