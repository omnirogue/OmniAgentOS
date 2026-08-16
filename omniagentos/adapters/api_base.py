"""API-tier adapters: OpenAI-compatible ``/chat/completions`` over HTTP.

These are the planner chain's LAST RESORT rungs. Every rung above them is a
subscription CLI; when the whole CLI tier is unavailable (the failure mode this
module exists for — a swarm dispatch silently degrading to a flat solo plan
because one provider blipped) the chain still has somewhere to go.

Two hard rules govern this file:

* **Policy first.** ``omniagentos/routing/api_policy.py`` is consulted before
  ANY request is built. claude/anthropic and gpt-\\*/codex lineage models can
  never be routed here, and the check runs inside ``run()`` as well as in the
  chain builder so a direct adapter call cannot bypass it. A violation RAISES
  (:class:`~omniagentos.routing.api_policy.ApiRoutePolicyError`); it is never
  downgraded to an error result, because a silent downgrade is how a deny-list
  rots.
* **Never raise for an operational problem.** A missing key, a refused
  connection, a 500, a malformed body — all become ``AgentResult(status=ERROR)``
  so the chain advances. ``health()`` never touches the network: it answers
  "is this rung even configured?", which is what a chain builder needs.

The LiteLLM rung points at the local proxy (``http://localhost:4000/v1`` by
default — the same loopback proxy configs/modelintel.yaml's ``router_gemini``
uses) and serves the gemini models. OpenRouter lives in ``openrouter.py``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from omniagentos.adapters.common import (
    PROVIDER_COST_MISSING,
    append_cost_observation,
    build_cost_observation,
    normalize_provider_cost,
    structured_prompt,
    validate_structured_json,
)
from omniagentos.adapters.spend_guard import SpendGuardRefusal, classify_ledger_failure
from omniagentos.budget import check as budget_check
from omniagentos.budget.policy import blocks
from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    CostQuality,
    HealthStatus,
    Receipt,
    ResultStatus,
    estimate_tokens,
)
from omniagentos.routing.api_policy import (
    ApiRoutePolicyError,
    assert_api_route_allowed,
    litellm_api_base,
)

LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120.0
ERROR_TAIL_LENGTH = 500

#: Hard cap on how many models one ``run()`` may try. The caller's wall-clock
#: budget is already shared across candidates (see :meth:`run`), so this is the
#: second half of the same promise: a long ``openrouter_models`` list cannot
#: turn one planner rung into a dozen sequential HTTP calls.
MAX_API_CANDIDATES = 4

#: Below this there is no point starting another attempt — a request that
#: cannot even connect inside a second is not going to produce a plan, and
#: handing ``requests`` a ~0 timeout just burns a rung.
MIN_ATTEMPT_SECONDS = 1.0

# Model ids that mean "use this rung's configured default list" rather than a
# specific model (the planner may hand the rung a placeholder).
_SENTINEL_MODELS = frozenset({"", "auto", "default", "openrouter", "litellm"})

# Provider usage fields that may carry exact billed cost (OpenRouter uses ``cost``).
_COST_FIELD_NAMES = ("cost", "total_cost", "total_cost_usd", "cost_usd")

# M4 (2026-08-06 review): HTTP statuses that PROVE the request never reached
# model execution, so a reservation for one of these is provably $0, never
# an estimate. Deliberately narrow: 429 (rate limit) and 5xx can occur AFTER
# partial provider-side work in some deployments, so they stay indeterminate
# and conservative. Extending this set is a deliberate per-status decision,
# not a blanket "errors are free" policy.
_PROVABLY_NOT_BILLED_HTTP_STATUS = frozenset({401, 403, 404})


def _is_provably_not_billed_transport_error(exc: Exception) -> bool:
    """True only for a transport failure that PROVES no bytes reached the provider.

    NARROWED 2026-08-06 review (this was a real bug): the original version
    accepted ANY ``requests.exceptions.ConnectionError``. ``requests`` wraps
    post-send failures -- a peer RESET after the request was accepted, a
    connection aborted mid-response (``ProtocolError``/``RemoteDisconnected``/
    ``ConnectionResetError``) -- in that SAME plain ``ConnectionError`` class,
    so those shapes were wrongly released even though the provider may
    already have run inference. This is the mirror image of the
    favourable-unknown bug M4 exists to fix, in the one function whose own
    docstring forbids it.

    Only an explicit allow-list of PROVEN pre-send shapes returns True:
    ``ConnectTimeout``/``SSLError``/``ProxyError`` (all verified subclasses
    of ``ConnectionError`` that only ever occur before any bytes are sent),
    and a ``ConnectionError`` whose wrapped urllib3 reason is
    ``NewConnectionError`` (refused/unreachable TCP) or
    ``NameResolutionError`` (unresolved DNS name). Everything else --
    including a bare ``ConnectionError`` with no recognizable pre-send
    reason -- stays on ``settle_unknown``: indeterminate, conservative.
    """
    import requests
    from urllib3.exceptions import NameResolutionError, NewConnectionError

    if isinstance(
        exc,
        (requests.exceptions.ConnectTimeout, requests.exceptions.SSLError, requests.exceptions.ProxyError),
    ):
        return True
    if not isinstance(exc, requests.exceptions.ConnectionError):
        return False
    # requests wraps the real urllib3 failure as the ConnectionError's first
    # positional arg (typically a MaxRetryError); its ``.reason`` is the
    # actual low-level exception. A bare ConnectionError with no such
    # wrapped reason is NOT assumed pre-send.
    wrapped = exc.args[0] if exc.args else None
    reason = getattr(wrapped, "reason", None)
    return isinstance(reason, (NewConnectionError, NameResolutionError))


def parse_provider_cost(raw: Any) -> tuple[float | None, str | None, int | None]:
    """Parse a provider cost value into (float_or_none, decimal_text, nanos).

    Exact zero is a real observation ``(0.0, "0", 0)`` (or equivalent decimal
    text). Missing / non-finite / negative / unparseable values are
    ``(None, None, None)`` — unknown — never numeric zero-by-coercion.
    """
    if raw is PROVIDER_COST_MISSING:
        normalized = normalize_provider_cost()
    else:
        normalized = normalize_provider_cost(raw)
    if normalized.quality is CostQuality.UNKNOWN:
        return None, None, None
    return normalized.cost_usd, normalized.cost_usd_decimal, normalized.cost_usd_nanos


def cost_from_usage_dict(
    usage: dict[str, Any] | None,
) -> tuple[float | None, str | None, int | None]:
    """Extract exact cost from a provider ``usage`` object when present."""
    if not isinstance(usage, dict):
        return None, None, None
    for key in _COST_FIELD_NAMES:
        if key in usage:
            return parse_provider_cost(usage[key])
    return None, None, None


def _extract_json_object(text: str) -> str:
    """The outermost ``{...}`` span of ``text`` (unchanged when there is none).

    API models wrap structured answers in prose more often than the CLIs do;
    the shared validator only tolerates code fences, so trim to the object
    before handing it over."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return text
    return text[start : end + 1]


class OpenAiCompatibleAdapter:
    """AgentAdapter over any OpenAI-compatible ``/chat/completions`` endpoint.

    Satisfies the ``AgentAdapter`` protocol (name/version/run/cancel/health)
    without inheriting ``CliAdapter`` — there is no subprocess, no sandbox and
    no session to resume here, so sharing that base would be a lie.
    """

    name = "api"
    version = "1.0"
    #: ``omniagentos.routing.api_policy`` path constant for this transport.
    api_path = ""

    # --- configuration seams (subclasses override) --------------------------

    def api_base(self) -> str:
        raise NotImplementedError

    def api_key(self) -> str | None:
        """The bearer token, or None when this rung is unconfigured."""
        return None

    def extra_headers(self) -> dict[str, str]:
        return {}

    def default_models(self) -> tuple[str, ...]:
        """Candidates used when the caller names no specific model."""
        return ()

    def spend_guard_provider(self, model: str) -> str | None:
        """Billing-provider cap key for paid calls, or ``None`` for this transport.

        Direct paid adapters override this.  Keeping the invocation here, in
        the shared HTTP attempt seam, prevents a provider subclass from
        accidentally putting request construction ahead of the guard.
        """

        return None

    def spend_guard(self) -> Any:
        from omniagentos.adapters.spend_guard import default_spend_guard

        return default_spend_guard()

    def configured_models(self, input: AgentInput) -> list[str]:
        """Every model id this call could name, in order, UNCAPPED.

        A concrete requested model comes FIRST and the rung's other configured
        models follow it. This is the policy scope: the deny-list is applied to
        all of it, including entries the candidate cap will trim, so a denied id
        anywhere in ``api_fallback.openrouter_models`` fails the call loudly
        instead of lurking one config edit away from being reachable.
        """
        requested = str(input.model or "").strip().lower()
        configured = [model for model in self.default_models() if model]
        if requested and requested not in _SENTINEL_MODELS:
            return [requested, *[model for model in configured if model != requested]]
        return configured

    def candidate_models(self, input: AgentInput) -> list[str]:
        """Models this call may actually try, in order, capped.

        This is the last resort in the chain, so exhausting a short cheap list
        here is the intended behavior — but only a SHORT one: the list is
        truncated to :data:`MAX_API_CANDIDATES` because the wall-clock budget
        belongs to the caller, not to however many ids are in the config.

        When ``metadata["strict_model"]`` is truthy, only the explicitly
        requested model is attempted — identity cannot be silently swapped.
        """
        if self._strict_model(input):
            requested = str(input.model or "").strip()
            if requested and requested.lower() not in _SENTINEL_MODELS:
                return [requested]
            configured = self.configured_models(input)
            return configured[:1]
        return self.configured_models(input)[:MAX_API_CANDIDATES]

    @staticmethod
    def _strict_model(input: AgentInput) -> bool:
        meta = input.metadata if isinstance(input.metadata, dict) else {}
        return bool(meta.get("strict_model"))

    # --- protocol -----------------------------------------------------------

    #: True when an absent key makes the rung unusable (OpenRouter). The local
    #: LiteLLM proxy holds the real provider keys itself, so it is False there.
    requires_key = True

    def health(self) -> HealthStatus:
        """Configuration-only health (NO network call).

        Unhealthy = "this rung cannot possibly work" (no key, no base URL), which
        is what makes the chain builder skip it instead of burning a rung.
        """
        try:
            base = self.api_base()
            key = self.api_key()
        except Exception as exc:  # noqa: BLE001 - health never raises
            return HealthStatus(healthy=False, detail=str(exc), capabilities={"live_runs": False})
        if not base:
            return HealthStatus(
                healthy=False,
                detail=f"{self.name}: no api_base configured",
                capabilities={"live_runs": False},
            )
        if self.requires_key and not key:
            return HealthStatus(
                healthy=False,
                detail=f"{self.name}: no api key in the environment",
                capabilities={"live_runs": False},
            )
        return HealthStatus(healthy=True, detail=base, capabilities={"live_runs": True})

    def cancel(self, session_ref: str) -> bool:
        """No server-side session to cancel on a single-shot HTTP call."""
        return False

    def run(self, input: AgentInput) -> AgentResult:
        """One chat-completions turn; operational failures become ERROR results.

        The caller's ``budget.wall_ms_max`` is a budget for the WHOLE call, not
        a per-model allowance: one monotonic deadline is taken up front and each
        attempt only ever gets the time still left on it, so N candidates can
        never cost N x the budget (a planner rung that was supposed to take 30s
        must not quietly take 90).

        Raises ONLY :class:`ApiRoutePolicyError` — a denied lineage is a policy
        breach, not a provider outage, and must not be swallowed into a quiet
        fallback.
        """
        candidates = self.candidate_models(input)
        if not candidates:
            return self._error(f"{self.name}: no api model candidates configured")
        # Fail closed BEFORE any request is built: every CONFIGURED id is
        # checked — including the ones the candidate cap trimmed — so a denied
        # id anywhere in the list raises rather than being skipped.
        for model in self.configured_models(input):
            assert_api_route_allowed(model, path=self.api_path)

        # API calls bypass provider-side session caps, so enforce an explicit
        # cost cap locally before constructing or sending a request.  There is
        # no usage from this call yet: the runner accounts for provider-reported
        # usage after it returns.  Advisory mode deliberately leaves this as a
        # non-blocking observation, consistent with the other budget sites.
        if blocks() and input.budget.cost_usd_max is not None:
            try:
                budget_decision = budget_check(input.budget, 0, 0, 0.0)
                allowed = budget_decision.allowed
                reason = budget_decision.reason
            except Exception as exc:  # noqa: BLE001 - budget evaluation fails closed
                return self._error(f"{self.name}: budget cap check failed: {exc}")
            if not allowed:
                return self._error(
                    f"{self.name}: budget cap prevents API request: "
                    f"{reason or 'budget check rejected the request'}"
                )

        key = self.api_key()
        if self.requires_key and not key:
            return self._error(f"{self.name}: no api key configured; skipping api rung")

        prompt = input.prompt
        if input.output_schema is not None:
            prompt = structured_prompt(prompt, input.output_schema)
        budget = DEFAULT_TIMEOUT_SECONDS
        if input.budget.wall_ms_max is not None:
            budget = max(MIN_ATTEMPT_SECONDS, input.budget.wall_ms_max / 1000)
        deadline = time.monotonic() + budget

        last_error = "no attempt was made"
        last_result: AgentResult | None = None
        for attempt_index, model in enumerate(candidates):
            remaining = deadline - time.monotonic()
            if remaining < MIN_ATTEMPT_SECONDS:
                last_error = (
                    f"{self.name}: {budget:.1f}s wall-clock budget exhausted before "
                    f"{model} could be tried (last error: {last_error})"
                )
                LOG.warning("%s: budget exhausted, %s not attempted", self.name, model)
                break
            result = self._attempt(
                input,
                model,
                prompt,
                key,
                remaining,
                attempt_index=attempt_index,
            )
            if result.status == ResultStatus.OK:
                return result
            last_error = result.error or last_error
            last_result = result
            LOG.warning("%s: model %s failed (%s)", self.name, model, last_error)
        # Keep the authoritative terminal message (including budget-exhausted),
        # but never drop billed usage/receipts captured on a failed attempt.
        if last_result is not None:
            return self._error(
                last_error,
                wall_ms=int(last_result.usage.wall_ms) if last_result.usage else 1,
                usage=last_result.usage,
                receipts=list(last_result.receipts or ()),
            )
        return self._error(last_error)

    # --- internals ----------------------------------------------------------

    def _build_request_payload(self, model: str, prompt: str) -> dict[str, Any]:
        """Build the request payload for a chat completion.

        Subclasses may override to add provider-specific fields.
        """
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }

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
        import requests

        spend_ticket = None
        billing_provider = self.spend_guard_provider(model)
        if billing_provider is not None:
            # Terminal SpendGuardRefusal deliberately escapes ``run``. A cap is
            # policy, not an outage, and must never advance a model/provider
            # fallback chain.
            # The structured-output schema is part of the actual prompt and
            # therefore part of the conservative input-token ceiling.
            guard_input = (
                input if prompt == input.prompt else input.model_copy(update={"prompt": prompt})
            )
            spend_ticket = self.spend_guard().preflight(
                guard_input,
                provider=billing_provider,
                model=model,
                transport="http",
                adapter_key=self.name,
                attempt_index=attempt_index,
            )

        headers = {"Content-Type": "application/json", **self.extra_headers()}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = self._build_request_payload(model, prompt)
        if spend_ticket is not None and input.budget.tokens_max is not None:
            # M6 (2026-08-06 review): ``spend_ticket.output_tokens_ceiling``
            # is the guard's own conservative worst-case for COST reservation
            # purposes -- it falls back to a config default (e.g. 8192) when
            # the caller set no ceiling at all. That default is NOT a
            # request the caller made. ``BudgetSpec.tokens_max`` documents
            # "None means unlimited"; forwarding the reservation default as
            # a hard ``max_tokens`` silently truncated real generations
            # (Opus measured finish_reason='length' at 3.1% of the model's
            # context while the adapter reported status ok). Only forward
            # an output ceiling the caller actually asked for; the
            # reservation itself still uses the conservative default for
            # cost containment regardless of what is sent on the wire.
            payload["max_tokens"] = spend_ticket.output_tokens_ceiling
        started = _monotonic_ms()
        body: dict[str, Any] | None = None
        response = None
        try:
            response = requests.post(
                f"{self.api_base()}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            try:
                parsed = response.json()
                body = parsed if isinstance(parsed, dict) else None
            except Exception:  # noqa: BLE001 - non-JSON error bodies are fine
                body = None
            # Tolerate test doubles that omit status_code (historical mocks).
            status_code = int(getattr(response, "status_code", 200) or 200)
            if status_code >= 400:
                wall_ms = max(1, _monotonic_ms() - started)
                # Billed failures must retain served provider/model/cost when the
                # provider still returned a usage payload (e.g. 402 with cost).
                # M4 (2026-08-06 review): 401/403/404 PROVE the request never
                # reached model execution -- credential rejection, forbidden
                # route, or unknown resource all happen before any billable
                # work. This is a closed, deliberately narrow list of status
                # codes that are provably $0 by construction, not a general
                # "errors are free" assumption; anything else (429, 5xx,
                # unlisted 4xx) stays indeterminate/conservative.
                usage, receipts = self._usage_and_receipts(
                    input,
                    body,
                    text="",
                    wall_ms=wall_ms,
                    requested_model=model,
                    spend_ticket=spend_ticket,
                    provider_outcome=f"http_{status_code}",
                    not_billed=status_code in _PROVABLY_NOT_BILLED_HTTP_STATUS,
                )
                detail = f"{model}: HTTP {status_code}"
                if isinstance(body, dict) and body.get("error"):
                    detail = f"{detail}: {body.get('error')}"
                # 2026-08-06 review: a caller-side terminal-park decision
                # (quota/suspension/auth) must read the actual status code,
                # not regex-parse this formatted string two layers away --
                # a body's own error detail could otherwise contain a
                # misleading "429"/"403" substring. This receipt is the
                # single source of truth for that status.
                receipts = [
                    *receipts,
                    Receipt(
                        key="http_status_code",
                        action="provider_call",
                        target=str(status_code),
                    ),
                ]
                return self._error(detail, wall_ms=wall_ms, usage=usage, receipts=receipts)
            if body is None:
                raise ValueError("non-object chat-completions body")
        except SpendGuardRefusal:
            raise
        except Exception as exc:  # noqa: BLE001 - transport problems are results
            wall_ms = max(1, _monotonic_ms() - started)
            if body is not None:
                usage, receipts = self._usage_and_receipts(
                    input,
                    body,
                    text="",
                    wall_ms=wall_ms,
                    requested_model=model,
                    spend_ticket=spend_ticket,
                    provider_outcome=f"transport_error:{type(exc).__name__}",
                )
                return self._error(
                    f"{model}: transport_error:{type(exc).__name__}: {exc}",
                    wall_ms=wall_ms,
                    usage=usage,
                    receipts=receipts,
                )
            if spend_ticket is not None:
                try:
                    if _is_provably_not_billed_transport_error(exc):
                        # M4 (2026-08-06 review): connection-refused and DNS
                        # failure prove the request never reached the
                        # provider at all -- release, do not retain.
                        self.spend_guard().settle_released(
                            spend_ticket,
                            provider_outcome=f"transport_error:{type(exc).__name__}",
                            request_state="not_sent",
                        )
                    else:
                        self.spend_guard().settle_unknown(
                            spend_ticket,
                            provider_outcome=f"transport_error:{type(exc).__name__}",
                        )
                except Exception as settle_exc:
                    raise SpendGuardRefusal(
                        classify_ledger_failure(settle_exc),
                        f"provider-call estimate could not be settled: {settle_exc}",
                    ) from settle_exc
            return self._error(
                f"{model}: transport_error:{type(exc).__name__}: {exc}",
                wall_ms=wall_ms,
            )

        wall_ms = max(1, _monotonic_ms() - started)
        try:
            text = str(body["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
            usage, receipts = self._usage_and_receipts(
                input,
                body,
                text="",
                wall_ms=wall_ms,
                requested_model=model,
                spend_ticket=spend_ticket,
                provider_outcome="malformed_response",
            )
            return self._error(
                f"{model}: malformed chat-completions body ({exc})",
                wall_ms=wall_ms,
                usage=usage,
                receipts=receipts,
            )
        if not text.strip():
            usage, receipts = self._usage_and_receipts(
                input,
                body,
                text="",
                wall_ms=wall_ms,
                requested_model=model,
                spend_ticket=spend_ticket,
                provider_outcome="empty_completion",
            )
            return self._error(
                f"{model}: empty completion",
                wall_ms=wall_ms,
                usage=usage,
                receipts=receipts,
            )

        usage, receipts = self._usage_and_receipts(
            input,
            body,
            text,
            wall_ms,
            requested_model=model,
            spend_ticket=spend_ticket,
            provider_outcome="completed",
        )
        # M6 (2026-08-06 review): a response that hit its own max_tokens
        # ceiling is not a clean success -- the caller's actual generation
        # was cut off mid-output. Opus measured this reading back
        # status=ok/error=None with the shipped 8192-token default while
        # the real content was truncated. Surface it as an error instead of
        # letting a truncated answer read as OK -- but ONLY when the
        # caller did NOT explicitly request that ceiling. If the caller set
        # ``budget.tokens_max`` themselves, finish_reason=length means the
        # ceiling worked exactly as asked; raising an error there would let
        # run_with_fallback escalate a deliberately-bounded short
        # generation to another PAID provider, which is a real cost bug in
        # the opposite direction (2026-08-06 review, optional-but-fixed).
        finish_reason = None
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish_reason = choices[0].get("finish_reason")
        if finish_reason == "length" and input.budget.tokens_max is None:
            return self._error(
                f"{model}: response truncated at the output-token ceiling "
                "(finish_reason=length)",
                wall_ms=wall_ms,
                usage=usage,
                receipts=receipts,
            )
        served_model = body.get("model")
        if (
            self._strict_model(input)
            and isinstance(served_model, str)
            and served_model.strip()
            and served_model.strip() != model
        ):
            return self._error(
                f"{model}: strict model mismatch; provider served {served_model.strip()}",
                wall_ms=wall_ms,
                usage=usage,
                receipts=receipts,
            )
        if input.output_schema is None:
            return AgentResult(
                status=ResultStatus.OK, output_text=text, usage=usage, receipts=receipts
            )

        output_json, validation_error = validate_structured_json(
            _extract_json_object(text), input.output_schema
        )
        if output_json is None:
            return AgentResult(
                status=ResultStatus.ERROR,
                output_text=text,
                usage=usage,
                receipts=receipts,
                error=f"{model}: {validation_error}",
            )
        return AgentResult(
            status=ResultStatus.OK,
            output_text=text,
            output_json=output_json,
            usage=usage,
            receipts=receipts,
        )

    def _usage(
        self, input: AgentInput, body: dict[str, Any], text: str, wall_ms: int
    ) -> AgentUsage:
        usage, _ = self._usage_and_receipts(
            input, body, text, wall_ms, requested_model=str(input.model or "")
        )
        return usage

    def _usage_and_receipts(
        self,
        input: AgentInput,
        body: dict[str, Any] | None,
        text: str,
        wall_ms: int,
        *,
        requested_model: str,
        spend_ticket: Any = None,
        provider_outcome: str = "completed",
        not_billed: bool = False,
    ) -> tuple[AgentUsage, list[Receipt]]:
        reported = body.get("usage") if isinstance(body, dict) else None
        raw_cost: Any = PROVIDER_COST_MISSING
        if isinstance(reported, dict):
            for key in _COST_FIELD_NAMES:
                if key in reported:
                    raw_cost = reported[key]
                    break
        normalized = (
            normalize_provider_cost()
            if raw_cost is PROVIDER_COST_MISSING
            else normalize_provider_cost(raw_cost)
        )
        cost_usd = normalized.cost_usd
        cost_decimal = normalized.cost_usd_decimal
        cost_nanos = normalized.cost_usd_nanos

        served_model = requested_model
        provider_request_id: str | None = None
        if isinstance(body, dict):
            body_model = body.get("model")
            if isinstance(body_model, str) and body_model.strip():
                served_model = body_model.strip()
            req_id = body.get("id")
            if isinstance(req_id, str) and req_id.strip():
                provider_request_id = req_id.strip()

        input_tokens: int | None
        output_tokens: int | None
        if (
            isinstance(reported, dict)
            and isinstance(reported.get("prompt_tokens"), int)
            and isinstance(reported.get("completion_tokens"), int)
        ):
            input_tokens = reported["prompt_tokens"]
            output_tokens = reported["completion_tokens"]
            has_exact = normalized.quality is CostQuality.EXACT
            usage = AgentUsage(
                wall_ms=wall_ms,
                turns=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                estimated=not has_exact,
                source="cli-report" if has_exact else "mixed",
            )
        else:
            input_tokens = estimate_tokens(input.prompt)
            output_tokens = estimate_tokens(text) if text else None
            usage = AgentUsage(
                wall_ms=wall_ms,
                turns=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                estimated=True,
                source="estimator" if cost_usd is None else "mixed",
            )

        observation = build_cost_observation(
            input=input,
            normalized=normalized,
            provider=self.name,
            requested_model=requested_model,
            effective_model=served_model,
            transport="http",
            adapter_key=self.name,
            billing_provider=self.name,
            provider_request_id=provider_request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_source="provider-report",
        )
        append_cost_observation(input, observation)

        if spend_ticket is not None:
            if normalized.quality is CostQuality.EXACT:
                settled = False
                last_error: Exception | None = None
                for retry_index in range(2):
                    try:
                        self.spend_guard().settle_exact(
                            spend_ticket,
                            normalized,
                            provider_outcome=provider_outcome,
                            provider_request_id=provider_request_id,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                        settled = True
                        break
                    except Exception as exc:  # noqa: BLE001 - bounded reconciliation retry
                        last_error = exc
                        LOG.warning(
                            "%s: exact spend settlement attempt %d/2 failed: %s",
                            self.name,
                            retry_index + 1,
                            exc,
                        )
                if not settled:
                    # The provider already answered and may have billed us. Keep
                    # the answer, retain the bounded reservation, and make the
                    # reconciliation gap explicit instead of raising it away.
                    try:
                        self.spend_guard().settle_unknown(
                            spend_ticket,
                            provider_outcome=f"{provider_outcome}:settle_unknown",
                            request_state="sent",
                        )
                    except Exception as unknown_exc:  # noqa: BLE001 - answer still wins
                        LOG.error(
                            "%s: exact settlement failed twice (%s) and settle_unknown "
                            "also failed (%s); returning billed provider answer with the "
                            "reservation still outstanding",
                            self.name,
                            last_error,
                            unknown_exc,
                        )
            elif not_billed:
                # M4 (2026-08-06 review): this outcome PROVES the provider
                # never billed (e.g. a 401/403/404 with no usage payload).
                # Retaining the upper bound here is not conservatism -- it
                # burns real cap headroom for $0 of real spend, and N such
                # failures (a rotated credential, a dead endpoint) exhausts
                # the daily cap and self-inflicts a full-day outage.
                try:
                    self.spend_guard().settle_released(
                        spend_ticket,
                        provider_outcome=provider_outcome,
                        request_state="sent",
                    )
                except Exception as exc:  # noqa: BLE001 - never discard a billed answer
                    LOG.error(
                        "%s: settle_released failed after a provably-not-billed "
                        "response (%s); returning the answer with its bounded "
                        "reservation intact rather than losing the release",
                        self.name,
                        exc,
                    )
            else:
                try:
                    self.spend_guard().settle_unknown(
                        spend_ticket,
                        provider_outcome=provider_outcome,
                        request_state="sent",
                    )
                except Exception as exc:  # noqa: BLE001 - never discard a billed answer
                    LOG.error(
                        "%s: settle_unknown failed after provider response (%s); "
                        "returning the answer with its bounded reservation intact",
                        self.name,
                        exc,
                    )

        obs_payload = {
            "provider": observation.provider,
            "billing_provider": observation.billing_provider,
            "requested_model": observation.requested_model,
            "effective_model": observation.effective_model,
            "served_model": observation.effective_model,
            "cost_usd_decimal": cost_decimal,
            "cost_usd_nanos": cost_nanos,
            "cost_quality": observation.cost_quality.value
            if hasattr(observation.cost_quality, "value")
            else str(observation.cost_quality),
            "cost_source": observation.cost_source,
        }
        receipts = [
            Receipt(
                key="cost_observation",
                action="provider_call",
                target=(
                    f"{self.name}:{served_model}:"
                    f"{cost_decimal if cost_decimal is not None else 'unknown'}"
                ),
            ),
            Receipt(
                key="served_route",
                action="provider_call",
                target=f"{self.name}:{served_model}",
            ),
            Receipt(
                key="cost_observation_json",
                action="provider_call",
                target=json.dumps(obs_payload, separators=(",", ":"), sort_keys=True),
            ),
        ]
        return usage, receipts

    def _error(
        self,
        message: str,
        *,
        wall_ms: int = 1,
        usage: AgentUsage | None = None,
        receipts: list[Receipt] | None = None,
    ) -> AgentResult:
        return AgentResult(
            status=ResultStatus.ERROR,
            usage=usage
            if usage is not None
            else AgentUsage(wall_ms=max(1, wall_ms), estimated=True, source="estimator"),
            receipts=list(receipts or ()),
            error=message[-ERROR_TAIL_LENGTH:],
        )


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


class LiteLLMAdapter(OpenAiCompatibleAdapter):
    """The local LiteLLM proxy rung (gemini-3.6-flash / gemini-3.5-flash-lite).

    The proxy holds the real provider credentials, so no key is required from
    this process; ``LITELLM_API_KEY``/``GEMINI_API_KEY`` are forwarded when set
    and a placeholder is sent otherwise (the proxy's own auth mode decides
    whether it matters — the same idiom modelintel's dual-mode router uses).
    """

    name = "litellm"
    api_path = "litellm"
    requires_key = False

    #: Sent when nothing else resolves — the loopback proxy is keyless in this
    #: deployment and rejects a MISSING header, not a placeholder one.
    PLACEHOLDER_KEY = "sk-local-litellm-proxy-secure"

    def api_base(self) -> str:
        return litellm_api_base()

    def api_key(self) -> str | None:
        for name in ("LITELLM_API_KEY", "OMNIAGENTOS_LITELLM_API_KEY"):
            value = os.environ.get(name, "").strip()
            if value:
                return value
        try:
            from omniagentos.modelintel.config import gemini_api_key

            key = gemini_api_key()
            if key:
                return key
        except Exception:  # noqa: BLE001 - credential resolution never breaks a run
            LOG.debug("gemini_api_key() unavailable for the litellm rung", exc_info=True)
        return self.PLACEHOLDER_KEY

    def default_models(self) -> tuple[str, ...]:
        # The rung always names its model explicitly (the chain has two litellm
        # rungs on different models), so there is no meaningful default list.
        return ()


__all__ = [
    "ApiRoutePolicyError",
    "LiteLLMAdapter",
    "OpenAiCompatibleAdapter",
    "cost_from_usage_dict",
    "parse_provider_cost",
]
