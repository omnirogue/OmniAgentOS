"""Thin gate seams mapped to OmniAgentOS modules.

G0 intake lives at intake/orchestration, G1 plan at orchestrator, G2 dispatch
at routing/allocation, G3 tool use at toolplane, G4 budget at budget service,
G5 local verification at execution, G6 independent review at grants/review,
and G8 release at grants for consequential sends.

This package only returns a stable decision envelope; owning modules supply
policy evidence and enforcement.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omniagentos.gates.types import GateDecision, GateId

DEFAULT_POLICY_VERSION = "grok-gates/1"

_CONSEQUENTIAL_KINDS = frozenset(
    {
        "send",
        "release",
        "publish",
        "external_send",
        "consequential",
    }
)

# Caller-supplied flags that claim independent verification or override the
# G5 self-attestation refusal. The gate cannot validate them — a caller
# cannot vouch for its own independence — so they are stripped from the
# decision envelope and never consulted (M-38).
_CALLER_VERDICT_FLAGS = frozenset(
    {
        "independent_pass",
        "independent_verified",
        "independently_verified",
        "independent_verify_pass",
        "external_pass",
        "verify_override",
        "verification_override",
        "self_attestation_override",
    }
)


class GateService:
    """Default-pass gate envelope with a real G8 grant check for releases."""

    def __init__(self, policy_version: str = DEFAULT_POLICY_VERSION) -> None:
        self.policy_version = policy_version

    def _decision(
        self,
        gate_id: GateId,
        next_state: str,
        evidence: Mapping[str, Any] | None,
        *,
        decision: str = "allow",
    ) -> GateDecision:
        return GateDecision(
            gate_id,
            decision,
            dict(evidence or {}),
            next_state,
            self.policy_version,
        )

    def g0_intake(self, evidence: Mapping[str, Any] | None = None) -> GateDecision:
        return self._decision(GateId.G0, "intake_accepted", evidence)

    def g1_plan(self, evidence: Mapping[str, Any] | None = None) -> GateDecision:
        return self._decision(GateId.G1, "plan_accepted", evidence)

    def g2_dispatch(self, evidence: Mapping[str, Any] | None = None) -> GateDecision:
        """Dispatch gate: refuse when capacity/preconditions fail."""
        payload = dict(evidence or {})
        if payload.get("capacity_ok") is False:
            payload["reason"] = "capacity_exhausted"
            return self._decision(GateId.G2, "dispatch_blocked", payload, decision="deny")
        if payload.get("precondition_ok") is False:
            payload["reason"] = "precondition_failed"
            return self._decision(GateId.G2, "dispatch_blocked", payload, decision="deny")
        return self._decision(GateId.G2, "dispatched", payload)

    def g3_tool(self, evidence: Mapping[str, Any] | None = None) -> GateDecision:
        """Tool gate: refuse tools outside the declared allowlist."""
        payload = dict(evidence or {})
        tool = str(payload.get("tool") or payload.get("tool_name") or "").strip()
        allowed = payload.get("tools_allowed")
        if tool and isinstance(allowed, (list, tuple, set, frozenset)):
            if tool not in set(allowed) and "*" not in set(allowed):
                payload["reason"] = "tool_not_allowed"
                return self._decision(GateId.G3, "tool_blocked", payload, decision="deny")
        if payload.get("tool_allowed") is False:
            payload["reason"] = "tool_not_allowed"
            return self._decision(GateId.G3, "tool_blocked", payload, decision="deny")
        return self._decision(GateId.G3, "tool_authorized", payload)

    def g4_budget(self, evidence: Mapping[str, Any] | None = None) -> GateDecision:
        return self._decision(GateId.G4, "budget_ok", evidence)

    def g5_local_verify(self, evidence: Mapping[str, Any] | None = None) -> GateDecision:
        """Local verify gate: refuse when mechanical verification failed or is self-attested."""
        payload = dict(evidence or {})
        # M-38: caller-supplied verdict flags ("independent_pass" and
        # similarly named overrides) are never consulted and are stripped
        # from the envelope. Independence may only derive from the
        # mechanical verify evidence evaluated inside this gate; record the
        # attempt so the refusal degrades loudly instead of silently.
        ignored_flags = sorted(f for f in _CALLER_VERDICT_FLAGS if f in payload)
        for flag in ignored_flags:
            del payload[flag]
        if ignored_flags:
            payload["ignored_caller_verdict_flags"] = ignored_flags
        self_attested = bool(
            payload.get("self_attested")
            or payload.get("self_attested_pass")
            or payload.get("caller_self_attested")
        )
        if self_attested:
            payload["verify_ok"] = False
            payload["mechanical_pass"] = False
            payload["reason"] = "self_attested_verification_rejected"
            return self._decision(GateId.G5, "verify_blocked", payload, decision="deny")
        if payload.get("verify_ok") is False or payload.get("mechanical_pass") is False:
            payload["reason"] = payload.get("reason") or "local_verify_failed"
            return self._decision(GateId.G5, "verify_blocked", payload, decision="deny")
        return self._decision(GateId.G5, "locally_verified", payload)

    def g6_independent_review(self, evidence: Mapping[str, Any] | None = None) -> GateDecision:
        return self._decision(GateId.G6, "independent_reviewed", evidence)

    def g8_release(self, evidence: Mapping[str, Any] | None = None) -> GateDecision:
        """Release gate: consequential sends require a live grant (looked up)."""
        payload = dict(evidence or {})
        consequential = _is_consequential(payload)
        grant_id = str(payload.get("grant_id") or "").strip()
        payload["consequential"] = consequential
        if consequential and not grant_id:
            payload["reason"] = "missing_grant_id"
            return self._decision(
                GateId.G8,
                "release_blocked",
                payload,
                decision="deny",
            )
        if grant_id:
            payload["grant_id"] = grant_id
            store = payload.get("grant_store")
            # Fail closed: consequential releases must look up the grant. A bare
            # grant_id string is not proof of a live grant (Opus c4ddea9 P0).
            if store is None:
                if consequential:
                    payload["reason"] = "grant_lookup_unavailable"
                    return self._decision(GateId.G8, "release_blocked", payload, decision="deny")
                # Non-consequential with an orphan grant_id: ignore the id.
                return self._decision(GateId.G8, "released", payload)
            try:
                from omniagentos.grants.store import is_grant_live

                row = store.get_grant(grant_id)
                if row is None:
                    payload["reason"] = "grant_not_found"
                    return self._decision(GateId.G8, "release_blocked", payload, decision="deny")
                live, reason = is_grant_live(
                    row, capability=str(payload.get("capability") or "") or None
                )
                if not live:
                    payload["reason"] = reason or "grant_not_live"
                    return self._decision(GateId.G8, "release_blocked", payload, decision="deny")
                payload["grant_live"] = True
            except Exception as exc:  # noqa: BLE001
                payload["reason"] = f"grant_lookup_failed:{exc}"
                return self._decision(GateId.G8, "release_blocked", payload, decision="deny")
        return self._decision(GateId.G8, "released", payload)


def _is_consequential(evidence: Mapping[str, Any]) -> bool:
    if evidence.get("consequential") is True:
        return True
    if evidence.get("consequential") is False:
        return False
    kind = str(evidence.get("kind") or evidence.get("action") or "").strip().lower()
    if kind in _CONSEQUENTIAL_KINDS:
        return True
    action_class = str(evidence.get("action_class") or "").strip().lower()
    if action_class in _CONSEQUENTIAL_KINDS or action_class in {"r2", "r3", "external"}:
        return True
    # Explicit send/release flags
    if evidence.get("send") is True or evidence.get("release") is True:
        return True
    return False
