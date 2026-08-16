"""Fireworks-default Kimi K3 API route with outage-only Moonshot fallback.

Spend-cap refusal is an exception and is never caught here. Only a Fireworks
network failure or HTTP 5xx advances to Moonshot, and the fallback adapter uses
the same shared :class:`OpenAiCompatibleAdapter` spend-guard seam.

Quota/suspension/auth refusals (2026-08-06 review, estate standing rule) are
TERMINAL, not outages -- but ONLY for a 403 (forbidden/suspended). A 429 was
in this set in an earlier pass and was REMOVED: 429 is the standard
RATE-LIMIT status for routine per-minute RPM/TPM throttling on both
Fireworks and Moonshot, not only quota exhaustion, and throttles are
self-healing. Parking on first strike for a routine throttle was STRICTLY
WORSE than the pre-fix behaviour it replaced (ERROR advancing the chain, the
caller still got an answer): terminally re-raising killed the entire chain,
including the free/local rungs, for a transient condition. 403 does not have
that self-healing property, so first-strike terminal-park is still correct
there: retrying the SAME provider would blind-retry-storm a dead credential,
and advancing to a DIFFERENT paid provider outside this module (the wider
planner fallback chain, e.g. OpenRouter) would defeat the whole point of a
per-provider spend cap by a different door than the identity split Blocker 1
fixed. ``ProviderAuthRefusal`` is raised instead of returned so it escapes
the same way ``SpendGuardRefusal`` does, and
``omniagentos.intake.fallback.run_with_fallback`` re-raises it without
advancing.
"""

from __future__ import annotations

import re
from typing import Any

from omniagentos.adapters.api_base import OpenAiCompatibleAdapter
from omniagentos.connectors import broker, load_registry
from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    Receipt,
    ResultStatus,
    new_id,
)
from omniagentos.routing.api_policy import API_PATH_DIRECT

FIREWORKS_API_BASE = "https://api.fireworks.ai/inference/v1"
MOONSHOT_API_BASE = "https://api.moonshot.ai/v1"
KIMI_K3_MODEL = "kimi-k3"

_HTTP_5XX = re.compile(r"\bHTTP 5\d\d\b", re.IGNORECASE)
# Terminal-park status: 403 (forbidden/suspended) only. NOT 429 (routine,
# self-healing rate-limit -- see the module docstring) and NOT 401 (handled
# by M4's release-the-reservation fix instead; an invalid/rotated key does
# not, on its own, prove the ACCOUNT is suspended the way 403 does).
_HTTP_AUTH_OR_QUOTA_STATUS = frozenset({403})


class ProviderAuthRefusal(Exception):
    """A quota/suspension/auth refusal. Terminal: park, alert once, never blind-retry.

    Deliberately NOT a subclass of ``SpendGuardRefusal`` -- this is not a
    spend-cap decision, it is a credential/account decision -- but callers
    that must fail closed the same way (never advance a fallback chain to
    another billable provider) should catch it alongside SpendGuardRefusal.
    """

    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(
            f"provider auth/quota refusal for {provider}: terminally parked; "
            f"no blind retry or provider failover ({detail})"
        )
        self.provider = provider
        self.detail = detail


class _DirectKimiAdapter(OpenAiCompatibleAdapter):
    api_path = API_PATH_DIRECT
    requires_key = True
    capability = ""
    credential_env = ""
    endpoint = ""

    @staticmethod
    def _credential(capability: str, env_name: str) -> str | None:
        try:
            return broker.resolve_one_for(load_registry().capability(capability), env_name)
        except broker.BrokerDenied:
            return None

    def api_base(self) -> str:
        return self.endpoint

    def api_key(self) -> str | None:
        value = self._credential(self.capability, self.credential_env)
        return value.strip() if value and value.strip() else None

    def default_models(self) -> tuple[str, ...]:
        return (KIMI_K3_MODEL,)

    def spend_guard_provider(self, model: str) -> str | None:
        return self.name

    def provider_health_gate(self) -> Any:
        from omniagentos.adapters.provider_health_gate import default_provider_health_gate

        return default_provider_health_gate()

    def provider_health(self) -> Any:
        from omniagentos.adapters.provider_health_gate import snapshot_health

        return snapshot_health(self.name)

    def run(self, input: AgentInput) -> AgentResult:
        scheduled_job_id = str(input.metadata.get("scheduled_job_id") or "").strip()
        if scheduled_job_id:
            decision = self.provider_health_gate().consult(
                job_id=scheduled_job_id,
                provider=self.name,
                health_check=self.provider_health,
            )
            if not decision.allowed:
                return AgentResult(
                    status=ResultStatus.CANCELLED,
                    usage=AgentUsage(wall_ms=1, estimated=True, source="provider-health-gate"),
                    error=(
                        f"provider health {decision.action}: {self.name}; "
                        f"failures={decision.consecutive_failures}; {decision.detail}"
                    ),
                    receipts=[
                        Receipt(
                            key=decision.receipt_id,
                            action=decision.action,
                            target=f"{scheduled_job_id}:{self.name}",
                        )
                    ],
                )
        return super().run(input)

    def _build_request_payload(self, model: str, prompt: str) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "usage": {"include": True},
        }


class MoonshotKimiK3Adapter(_DirectKimiAdapter):
    """Direct Moonshot fallback.

    ``name`` (and therefore ``spend_guard_provider``/``provider_health``) is
    ``"moonshot"``, not ``"kimi"``: this is the canonical billing identity
    that matches configs/spend-caps.yaml, the provider-sentinel paid-snapshot
    roster, and the live estate kimi-shim co-writer's ``billing_provider``
    string. See Blocker 1, 2026-08-06 review -- a mismatched identity here
    let the guard and the shim independently enforce two separate $100/day
    allowances against one real Moonshot cap.
    """

    name = "moonshot"
    capability = "moonshot.generate"
    credential_env = "MOONSHOT_API_KEY"
    endpoint = MOONSHOT_API_BASE


class FireworksKimiK3Adapter(_DirectKimiAdapter):
    """Default Kimi K3 paid route, falling back only for a Fireworks outage."""

    name = "fireworks"
    capability = "fireworks.generate"
    credential_env = "FIREWORKS_API_KEY"
    endpoint = FIREWORKS_API_BASE

    def fallback_adapter(self) -> MoonshotKimiK3Adapter:
        return MoonshotKimiK3Adapter()

    @staticmethod
    def _is_outage(result: AgentResult) -> bool:
        if result.status not in {ResultStatus.ERROR, ResultStatus.TIMEOUT}:
            return False
        detail = (result.error or "").lower()
        return bool(_HTTP_5XX.search(result.error or "")) or "transport_error:" in detail

    @staticmethod
    def _is_auth_or_quota_refusal(result: AgentResult) -> bool:
        if result.status not in {ResultStatus.ERROR, ResultStatus.TIMEOUT}:
            return False
        # 2026-08-06 review: read the actual status code from the
        # ``http_status_code`` receipt api_base.py attaches, rather than
        # regex-matching the formatted error string two layers away from
        # where the real status_code was in hand (a body's own error detail
        # could otherwise contain a misleading digit sequence).
        for receipt in result.receipts:
            if receipt.key != "http_status_code":
                continue
            try:
                status_code = int(receipt.target)
            except (TypeError, ValueError):
                continue
            return status_code in _HTTP_AUTH_OR_QUOTA_STATUS
        return False

    def run(self, input: AgentInput) -> AgentResult:
        primary = super().run(input)
        if self._is_auth_or_quota_refusal(primary):
            # Terminal at the source: retrying Fireworks would blind-retry a
            # dead credential, and letting this escape as a plain ERROR
            # result would let the WIDER planner fallback chain
            # (omniagentos.intake.fallback) advance past this entire rung to
            # an uncapped paid provider -- defeating the spend cap through a
            # different door than Blocker 1 did.
            raise ProviderAuthRefusal(self.name, primary.error or "auth/quota refusal")
        if not self._is_outage(primary):
            return primary

        metadata = dict(input.metadata)
        base_call_id = str(metadata.get("call_id") or new_id("call"))
        metadata["call_id"] = f"{base_call_id}:outage-fallback:kimi:{new_id('call')}"
        fallback_input = input.model_copy(update={"metadata": metadata})
        # SpendGuardRefusal deliberately escapes: a capped fallback is terminal,
        # not a reason to continue searching for another billable provider.
        fallback_result = self.fallback_adapter().run(fallback_input)
        if self._is_auth_or_quota_refusal(fallback_result):
            # The Fireworks-outage fallback also hit a terminal auth/quota
            # wall on Moonshot -- same reasoning as above, mirrored for the
            # fallback adapter's own credential.
            raise ProviderAuthRefusal(
                self.fallback_adapter().name,
                fallback_result.error or "auth/quota refusal",
            )
        return fallback_result


__all__ = [
    "FIREWORKS_API_BASE",
    "KIMI_K3_MODEL",
    "MOONSHOT_API_BASE",
    "FireworksKimiK3Adapter",
    "MoonshotKimiK3Adapter",
]
