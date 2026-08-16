"""OpenRouter adapter — the planner chain's low-cost API last resort.

OpenAI-compatible ``/chat/completions`` against ``https://openrouter.ai/api/v1``
with the key from ``OPENROUTER_API_KEY``. This is rung 8 of 8: it only ever runs
when every subscription CLI and the local proxy have already failed.

Two behaviors are load-bearing:

* **No key => unhealthy, never an exception.** ``health().healthy`` is False when
  ``OPENROUTER_API_KEY`` is absent, which is how the chain builder drops the rung
  on a machine that has no OpenRouter account. Calling ``run()`` anyway returns
  an ERROR result, not a raise.
* **The deny-list still applies.** ``omniagentos/routing/api_policy.py`` gates
  every candidate: claude/anthropic and gpt-\\*/codex lineage models must NEVER
  reach OpenRouter, no matter what someone puts in
  ``configs/swarm.yaml api_fallback.openrouter_models``. A denied id makes the
  call raise ``ApiRoutePolicyError`` (fail closed) rather than being skipped.

The candidate list itself is config, not code: ``api_fallback.openrouter_models``
(see :func:`omniagentos.routing.api_policy.openrouter_models`).
"""

from __future__ import annotations

from typing import Any

from omniagentos.adapters.api_base import OpenAiCompatibleAdapter
from omniagentos.adapters.spend_guard import SpendGuardRefusal
from omniagentos.connectors import broker, load_registry
from omniagentos.contracts import AgentInput, AgentResult
from omniagentos.routing.api_policy import API_PATH_OPENROUTER, openrouter_models

#: Public OpenRouter endpoint. Overridable for a proxy/mirror deployment.
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

#: Attribution headers OpenRouter uses for its dashboards; harmless when unset.
_REFERER = "https://github.com/omniagentos"
_TITLE = "OmniAgentOS"


class OpenRouterAdapter(OpenAiCompatibleAdapter):
    """API-tier fallback over OpenRouter (key: ``OPENROUTER_API_KEY``)."""

    name = "openrouter"
    api_path = API_PATH_OPENROUTER
    requires_key = True

    @staticmethod
    def _credential(env_name: str) -> str | None:
        try:
            return broker.resolve_one_for(
                load_registry().capability("openrouter.generate"), env_name
            )
        except broker.BrokerDenied:
            return None

    def api_base(self) -> str:
        override = (self._credential("OPENROUTER_API_BASE") or "").strip()
        return (override or OPENROUTER_API_BASE).rstrip("/")

    def api_key(self) -> str | None:
        key = (self._credential("OPENROUTER_API_KEY") or "").strip()
        return key or None

    def extra_headers(self) -> dict[str, str]:
        return {"HTTP-Referer": _REFERER, "X-Title": _TITLE}

    def default_models(self) -> tuple[str, ...]:
        return openrouter_models()

    #: Billing-provider cap key for :mod:`omniagentos.adapters.spend_guard`.
    #: Returning a non-``None`` value routes EVERY OpenRouter call through the
    #: spend-cap preflight (and exact-cost settle) — the $50/UTC-day cap in
    #: ``configs/spend-caps.yaml`` (provider ``openrouter``), added for the
    #: SI-loop revival (operator-approved). The guard refuses any
    #: model with no pricing row under that provider, so every id the last-resort
    #: rung can try MUST be priced there; the base class returned ``None`` (no
    #: cap), which is exactly why OpenRouter spend was previously unbounded.
    #:
    #: SCOPE — cap-safe IN PRACTICE, not by a proven invariant. SI uses a
    #: ~$0.00003/call model (qwen3.7-flash), so daily spend is fractions of a cent
    #: and the $50 cap is never approached. The admission cap (``prior + reserved
    #: <= cap``, or the ``store.py`` emergency override) and true-cost recording
    #: are BEST-EFFORT: the reservation prices input at ``estimate_tokens`` (an
    #: estimate, not a ceiling), so a token-dense prompt can under-reserve input
    #: and settle over-cap even when the provider honours ``max_tokens`` — i.e.
    #: ``settled <= reserved`` is NOT guaranteed here. A conservative input-token
    #: CEILING (so ``settled <= reserved`` by construction), the shared DAL
    #: boundary, credential-scoped enforcement, override-audit ordering, and
    #: settle-retry understatement are estate-wide spend-safety concerns
    #: consolidated in loopqueue proposal 9f7448b1 — NOT this lane (a per-adapter
    #: copy of that enforcement was fail-open and was removed).
    _SPEND_GUARD_PROVIDER = "openrouter"

    def spend_guard_provider(self, model: str) -> str | None:
        return self._SPEND_GUARD_PROVIDER

    def _model_output_ceiling(self, model: str) -> int | None:
        """The priced ``max_output_tokens`` for *model*, or ``None`` if unpriced.

        Read from the same spend-caps config the guard reserves against, so the
        reservation ceiling and the outbound ``max_tokens`` agree.

        NEW-2 (2026-08-12 review): this deliberately does NOT swallow config-read
        errors. Returning ``None`` on a config failure would make
        :meth:`_cap_bounded_input` drop ``max_tokens`` and send an UNCAPPED
        request — a fail-open that is wrong regardless of the cap magnitude. A
        config-read failure raises :class:`SpendGuardRefusal` (via
        ``SpendGuard._load_config``) and :meth:`_cap_bounded_input` fails closed.
        ``None`` is returned ONLY for a config that reads cleanly but has no
        pricing row for *model* — that model is refused by the guard's own
        preflight (``unknown_model``), so no uncapped request escapes.
        """
        config = self.spend_guard()._load_config()
        provider = config.providers.get(self._SPEND_GUARD_PROVIDER)
        if provider is None:
            return None
        pricing = provider.models.get(model)
        return pricing.max_output_tokens if pricing is not None else None

    def _cap_bounded_input(self, input: AgentInput, model: str) -> AgentInput:
        """Clamp an unbounded/oversized output budget to the model's priced max.

        F1 (2026-08-12 Class-A review): the guard reserved
        ``default_output_tokens`` (8192) when the caller set no ``tokens_max``,
        and — because the request then carried no ``max_tokens`` — a paid model
        could bill MORE than was reserved. Clamping ``tokens_max`` to the model's
        ``max_output_tokens`` closes it on both halves: the guard reserves the
        model's full output ceiling (so a near-cap call is REFUSED at admission,
        not settled-then-over), and the base adapter then forwards
        ``max_tokens = that ceiling`` on the wire (its M6 gate only forwards when
        ``tokens_max`` is set), so a recognized-model call physically cannot
        return more than was reserved. A caller with a tighter ceiling keeps it.

        NEW-2 (2026-08-12 review): if the ceiling cannot be READ (spend-caps
        config error), FAIL CLOSED — refuse rather than send an uncapped request.
        A config-read failure must never silently drop the token-cap mechanism.
        """
        try:
            ceiling = self._model_output_ceiling(model)
        except SpendGuardRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 - any read failure fails closed
            raise SpendGuardRefusal(
                "spend_cap_config_unreadable",
                f"cannot read the output-token ceiling for {model!r}; refusing rather "
                f"than sending an uncapped OpenRouter request ({type(exc).__name__}: {exc})",
            ) from exc
        current = input.budget.tokens_max
        if ceiling is None or (current is not None and current <= ceiling):
            return input
        bounded_budget = input.budget.model_copy(update={"tokens_max": ceiling})
        return input.model_copy(update={"budget": bounded_budget})

    def _attempt(
        self,
        input: AgentInput,
        model: str,
        prompt: str,
        key: str | None,
        timeout: float,
        *,
        attempt_index: int = 0,
    ) -> AgentResult:
        return super()._attempt(
            self._cap_bounded_input(input, model),
            model,
            prompt,
            key,
            timeout,
            attempt_index=attempt_index,
        )

    def _build_request_payload(
        self, model: str, prompt: str
    ) -> dict[str, Any]:
        """Build the OpenRouter-specific request payload.

        Includes usage: {include: true} so the response contains exact billed cost.
        """
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "usage": {"include": True},
        }


__all__ = ["OPENROUTER_API_BASE", "OpenRouterAdapter"]
