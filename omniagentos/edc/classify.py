"""Deterministic-first Executive Decision Center classifier (synthesis §4).

Turns a :class:`~omniagentos.edc.adapters.base.SourceEvent` into a triage
verdict — URGENT / NEEDS_OWNER / MAYBE / IGNORE — derived from *consequence +
deadline + likelihood*, NEVER from wording. P2 adds one side-effect-free
``ShortCallClient.complete_json`` estimate only when deterministic rules remain
ambiguous; the class is still derived locally and failures become MAYBE.

**CODEX F04 — harm-before-suppress (the single most important ordering).**
Material-harm detection (``edc_consequence`` + ``edc_deadline`` +
``edc_active_harm``) runs FIRST. A suppress rule / IGNORE heuristic can only
ever downgrade a message that has NO detected material harm — harm detection is
not suppressible. This is the operator's #1 requirement ("never miss a real urgent"): a
payment-failure or shutdown notice from a ``noreply@`` sender that a suppress
rule *would* hide is still surfaced, because harm was detected before suppress
was ever consulted.

**Invariant 1 — every surfaced decision carries a concrete recommended action.**
A URGENT/NEEDS_OWNER verdict must ship a real resolution ("update the payment
method on Stripe now", not "reply to Stripe"). If a template cannot be produced,
the decision is demoted to MAYBE rather than surfaced empty.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from omniagentos.edc.adapters.base import SourceEvent
from omniagentos.steward.alerts.rules import (
    edc_active_harm,
    edc_consequence,
    edc_deadline,
    edc_deadline_imminent,
    edc_suppress,
)
from omniagentos.steward.config import AlertsConfig
from omniagentos.steward.quoting import quote_untrusted

__all__ = ["classify", "recommend_action"]

# Concrete recommended-action templates by consequence bucket. Each is the REAL
# resolution the owner would take, keyed so a future rule/LLM path can override
# per (consequence, category). ``{provider}``/``{subject}``/``{who}`` are filled
# from the message. A bucket with no template can never surface (invariant 1).
_TEMPLATES: dict[str, tuple[str, str]] = {
    "financial": (
        "update_payment",
        "Update the payment method on {provider} now to clear the failed charge",
    ),
    "service_loss": (
        "resolve_service",
        "Resolve the {provider} notice now to keep the account active",
    ),
    "security": (
        "secure_account",
        "Review the {provider} security alert and secure the account now",
    ),
    "legal": (
        "review_legal",
        "Review the legal/contract notice from {who} and respond before the deadline",
    ),
    "account": (
        "review_decision",
        "Review and decide: {subject}",
    ),
}

_ACTIONS_BY_CLASS: dict[str, list[str]] = {
    "urgent": ["snooze", "dismiss", "note", "edit"],
    "needs_owner": ["snooze", "dismiss", "note", "edit"],
    "maybe": ["edit", "dismiss", "note"],
    "ignore": ["edit", "dismiss"],
}

_STATUS_BY_CLASS: dict[str, str] = {
    "urgent": "open",
    "needs_owner": "open",
    "maybe": "open",
    "ignore": "suppressed",
}


class _JsonClient(Protocol):
    def complete_json(
        self,
        messages: list[dict[str, str]],
        required_keys: list[str],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "default",
    ) -> dict[str, Any]: ...


def _sender_domain(sender: str) -> str:
    """The lowercased From domain of a sender header, or '' (OPUS-MAJOR gate)."""
    match = re.search(r"@([\w.\-]+)", sender or "")
    return (match.group(1) if match else "").strip().rstrip(".").lower()


def _domain_allowlisted(domain: str, allowlist: Iterable[str] | None) -> bool:
    """Whether a From domain is on the credible-provider allowlist (org-level)."""
    if not domain or not allowlist:
        return False
    for entry in allowlist:
        cand = str(entry).strip().rstrip(".").lower()
        if cand and (domain == cand or domain.endswith("." + cand)):
            return True
    return False


def _as_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _grounded(span: Any, text: str) -> bool:
    evidence = str(span or "").strip()
    return bool(evidence) and evidence.casefold() in text.casefold()


def _llm_ambiguity(
    event: SourceEvent,
    message: dict[str, Any],
    *,
    now: datetime,
    llm_client: _JsonClient | None,
) -> dict[str, Any]:
    """Estimate inputs, then derive the class locally; errors fail to MAYBE."""
    client: _JsonClient
    if llm_client is None:
        from omniagentos.llm.client import ShortCallClient

        client = ShortCallClient()
    else:
        client = llm_client
    source_text = f"{message.get('subject') or ''}\n{message.get('body_text') or ''}"
    prompt = quote_untrusted(
        source_text,
        source=f"edc:{event.get('source')}:{event.get('source_ref')}",
    )
    required = [
        "consequence",
        "consequence_evidence",
        "deadline_at",
        "deadline_evidence",
        "likelihood",
        "category",
        "confidence",
        "reason",
        "recommended_action",
    ]
    try:
        estimate = client.complete_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Estimate consequence, deadline, likelihood and owner action. "
                        "Quote exact evidence spans. Do not assign an urgency class."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            required_keys=required,
            purpose="edc_classify",
            temperature=0.0,
            max_tokens=500,
        )
    except Exception:  # fail closed: outage/timeout/malformed output is silent MAYBE
        return {
            "classification": "maybe",
            "consequence": "none",
            "deadline_at": None,
            "likelihood": 0.0,
            "confidence": 0.0,
            "reason": "LLM ambiguity classifier unavailable; fail-closed to MAYBE",
            "classifier": "llm_unavailable",
            "rule_matches": [],
            "recommended": {},
        }

    consequence = str(estimate.get("consequence") or "none").strip().lower()
    if consequence not in {*_TEMPLATES, "none"}:
        consequence = "none"
    consequence_grounded = _grounded(estimate.get("consequence_evidence"), source_text)
    deadline = _as_utc(estimate.get("deadline_at"))
    deadline_grounded = _grounded(estimate.get("deadline_evidence"), source_text)
    try:
        confidence = max(0.0, min(1.0, float(estimate.get("confidence") or 0.0)))
        likelihood = max(0.0, min(1.0, float(estimate.get("likelihood") or 0.0)))
    except (TypeError, ValueError):
        confidence = likelihood = 0.0
    action = str(estimate.get("recommended_action") or "").strip()
    recommended = (
        {
            "kind": "reply",
            "human_line": action[:500],
            "params": {"category": str(estimate.get("category") or "")[:100]},
        }
        if action
        else {}
    )

    imminent = deadline is not None and deadline <= now + timedelta(hours=24)
    grounded_urgent = (
        consequence != "none"
        and consequence_grounded
        and deadline is not None
        and deadline_grounded
        and imminent
        and likelihood >= 0.7
        and confidence >= 0.6
        and bool(recommended)
    )
    if confidence < 0.6 or not recommended:
        classification = "maybe"
    elif grounded_urgent:
        classification = "urgent"
    else:
        # F05: invented consequence/deadline estimates can request review but
        # can never manufacture an URGENT interrupt.
        classification = "needs_owner"
    return {
        "classification": classification,
        "consequence": consequence if consequence_grounded else "none",
        "deadline_at": deadline.isoformat() if deadline and deadline_grounded else None,
        "likelihood": likelihood,
        "confidence": confidence,
        "reason": str(estimate.get("reason") or "LLM ambiguity estimate")[:500],
        "classifier": "llm",
        "rule_matches": [
            marker
            for marker, present in (
                ("llm:consequence_grounded", consequence_grounded),
                ("llm:deadline_grounded", deadline_grounded),
            )
            if present
        ],
        "recommended": recommended,
    }


def _message_view(event: SourceEvent) -> dict[str, Any]:
    """A rules-primitive-shaped dict built from a SourceEvent."""
    metadata = event.get("metadata") or {}
    return {
        "sender": event.get("counterparty", ""),
        "subject": event.get("title", ""),
        "body_text": event.get("body", ""),
        "list_unsubscribe": metadata.get("list_unsubscribe", ""),
        "headers": metadata.get("headers", {}),
    }


def _provider_name(sender: str) -> str:
    """Human provider label from a sender address (``billing@stripe.com`` → Stripe)."""
    match = re.search(r"@([\w.-]+)", sender or "")
    domain = match.group(1) if match else (sender or "")
    labels = [part for part in domain.split(".") if part]
    if not labels:
        return "the provider"
    # Registrable-ish label: the second-level name (aws.com → aws; a.b.co → b).
    label = labels[-2] if len(labels) >= 2 else labels[0]
    return label.upper() if len(label) <= 3 else label.capitalize()


def _display_name(sender: str) -> str:
    """The friendly name out of ``Name <addr>``, else the address, else provider."""
    if not sender:
        return "the sender"
    match = re.match(r"\s*\"?([^\"<]+?)\"?\s*<", sender)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return sender.strip()


def recommend_action(consequence: str, message: dict[str, Any]) -> dict[str, Any]:
    """A concrete ``recommended_json`` for a consequence, or ``{}`` if none applies."""
    template = _TEMPLATES.get(consequence)
    if template is None:
        return {}
    kind, line = template
    provider = _provider_name(str(message.get("sender") or ""))
    subject = str(message.get("subject") or "").strip()[:120] or "the request"
    human_line = line.format(
        provider=provider,
        subject=subject,
        who=_display_name(str(message.get("sender") or "")),
    )
    return {
        "kind": kind,
        "human_line": human_line,
        "params": {"provider": provider, "consequence": consequence},
    }


def _rule_matches(matcher: dict[str, Any], message: dict[str, Any]) -> bool:
    """Whether one rule matcher (sender / sender_domain / subject_re) hits.

    The single, shared matcher predicate for every owner-rule kind (suppress,
    surface, classify_hint, snooze_default, auto_delegate) so they agree exactly
    on what "matches" means. A malformed ``subject_re`` fails closed (no match).
    """
    sender = str(message.get("sender") or "").casefold()
    text = f"{message.get('subject') or ''} {message.get('body_text') or ''}".casefold()
    exact = str(matcher.get("sender") or "").casefold()
    domain = str(matcher.get("sender_domain") or "").casefold()
    subject_re = matcher.get("subject_re")
    if exact and exact == sender:
        return True
    if domain and sender.endswith("@" + domain):
        return True
    if subject_re:
        try:
            return bool(re.search(str(subject_re), text, re.IGNORECASE))
        except re.error:
            return False
    return False


def _active_rules_of(
    owner_rules: list[dict[str, Any]] | None, kind: str, message: dict[str, Any]
) -> list[dict[str, Any]]:
    """The owner's ACTIVE rules of ``kind`` whose matcher hits ``message``."""
    if not owner_rules:
        return []
    return [
        rule
        for rule in owner_rules
        if rule.get("kind") == kind
        and rule.get("state") == "active"
        and _rule_matches(rule.get("matcher") or {}, message)
    ]


def _owner_suppress_matches(
    owner_rules: list[dict[str, Any]] | None, message: dict[str, Any]
) -> list[str]:
    """Active owner ``suppress`` rules that match — reachable only on no-harm items."""
    return [f"rule:{rule.get('id')}" for rule in _active_rules_of(owner_rules, "suppress", message)]


def _owner_surface_forced(
    owner_rules: list[dict[str, Any]] | None, message: dict[str, Any]
) -> str | None:
    """The id of an active ``surface``/``classify_hint`` rule pinning to visible.

    Both kinds only ever RAISE visibility (a would-be MAYBE/IGNORE → NEEDS_OWNER);
    neither can manufacture an URGENT interrupt from wording (invariant 2/3), so
    a ``classify_hint`` seeded from ``urgent_patterns`` surfaces for review but
    never pings. Returns the matching rule id (for the audit trail), or ``None``.
    """
    for kind in ("surface", "classify_hint"):
        matched = _active_rules_of(owner_rules, kind, message)
        if matched:
            return str(matched[0].get("id"))
    return None


def _owner_snooze_default(
    owner_rules: list[dict[str, Any]] | None, message: dict[str, Any]
) -> dict[str, Any] | None:
    """The first active ``snooze_default`` rule that matches, or ``None``.

    A pre-fill hint only: it surfaces a suggested default snooze on the item; it
    NEVER auto-snoozes (that would silently hide an item — the very thing §9
    forbids). ``action.hours`` is the suggested duration.
    """
    matched = _active_rules_of(owner_rules, "snooze_default", message)
    return matched[0] if matched else None


def _owner_auto_delegate(
    owner_rules: list[dict[str, Any]] | None, message: dict[str, Any]
) -> dict[str, Any] | None:
    """The first active ``delegate``/``auto_delegate`` rule that matches, or ``None``.

    An active ``delegate`` rule (an approved learned/NL suggestion) PRE-FILLS the
    delegation for one-tap approval and NEVER runs unattended. Its promoted peer
    ``auto_delegate`` additionally opts into unattended execution through the
    per-rule ``action.live`` gate the triage loop consults (F11). classify only
    ever records the pre-fill; it never fires the effect.
    """
    for kind in ("auto_delegate", "delegate"):
        matched = _active_rules_of(owner_rules, kind, message)
        if matched:
            return matched[0]
    return None


def classify(
    event: SourceEvent,
    *,
    owner_rules: list[dict[str, Any]] | None = None,
    cfg: AlertsConfig | None = None,
    now: datetime | None = None,
    llm_client: _JsonClient | None = None,
    credible_domains: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Classify one event deterministically, using the LLM only on ambiguity.

    Returns the classification-derived fields ready to merge into a Decision
    payload: ``classification``, ``consequence``, ``deadline_at``,
    ``likelihood``, ``confidence``, ``reason``, ``classifier``, ``rule_matches``,
    ``recommended``, ``available_actions``, ``status``, ``surfaced``.

    **OPUS-MAJOR sender-credibility gate.** A message may reach URGENT only if
    its sender is authenticated (``event['sender_verified']`` — DKIM/SPF aligned
    to From) OR its From domain is on ``credible_domains`` (the known billing/
    provider allowlist). An unverified, non-allowlisted sender caps at
    NEEDS_OWNER even when the wording/consequence looks urgent — scary wording
    from a stranger is the fake-urgency the operator must not be pinged about. The LLM
    path additionally requires the deadline to be re-derivable via ``edc_deadline``.
    """
    now = now or datetime.now(UTC)
    message = _message_view(event)
    sender_verified = bool(event.get("sender_verified"))
    from_domain = _sender_domain(str(message.get("sender") or ""))
    credible_sender = sender_verified or _domain_allowlisted(from_domain, credible_domains)

    # --- HARM DETECTION FIRST (CODEX F04). Not suppressible. ----------------
    consequence = edc_consequence(message)
    deadline_at = edc_deadline(message, now=now)
    active = edc_active_harm(message)
    imminent = edc_deadline_imminent(deadline_at, now=now)
    has_harm = consequence != "none"

    rule_matches: list[str] = []
    recommended: dict[str, Any] = {}
    classifier = "deterministic"

    if has_harm:
        recommended = recommend_action(consequence, message)
        rule_matches = [f"consequence:{consequence}"]
        if deadline_at:
            rule_matches.append(f"deadline:{deadline_at}")
        if active:
            rule_matches.append("active_harm")
        if imminent or active:
            classification = "urgent"
            reason = f"material harm ({consequence}) with an imminent/active deadline"
        else:
            classification = "needs_owner"
            reason = f"material harm ({consequence}); owner judgment needed, no imminent deadline"
        confidence = 0.9
        likelihood = 0.9
        # Invariant 1: a surfaced decision MUST carry a concrete resolution.
        if not recommended.get("human_line"):
            classification = "maybe"
            recommended = {}
            reason = "consequence detected but no concrete recommended action; demoted to MAYBE"
            confidence = 0.4
            likelihood = 0.5
    else:
        # No material harm — NOW (and only now) suppression may act (F04).
        suppress_matches = edc_suppress(message, cfg)
        suppress_matches += _owner_suppress_matches(owner_rules, message)
        if suppress_matches:
            classification = "ignore"
            rule_matches = suppress_matches
            reason = "no material harm; suppressed as noise (" + ",".join(suppress_matches) + ")"
            confidence = 0.7
            likelihood = 0.2
        else:
            llm = _llm_ambiguity(event, message, now=now, llm_client=llm_client)
            classification = str(llm["classification"])
            consequence = str(llm["consequence"])
            deadline_at = llm["deadline_at"]
            likelihood = float(llm["likelihood"])
            confidence = float(llm["confidence"])
            reason = str(llm["reason"])
            rule_matches = list(llm["rule_matches"])
            recommended = dict(llm["recommended"])
            classifier = str(llm["classifier"])
            # LLM path: an URGENT verdict requires the deadline to be
            # independently re-derivable from the message text (not merely the
            # model's assertion) — a hallucinated deadline cannot manufacture an
            # interrupt (review F05, hardened).
            if classification == "urgent" and edc_deadline(message, now=now) is None:
                classification = "needs_owner"
                reason = "LLM urgency lacks a re-derivable deadline; capped at NEEDS_OWNER"

    # OPUS-MAJOR: URGENT requires a credible sender. An unverified, non-
    # allowlisted sender caps at NEEDS_OWNER even when the consequence/wording
    # reads urgent — this is the fake-urgency (spoofed "your payment failed")
    # guard. Real provider notices are DKIM/SPF-verified or allowlisted and stay
    # urgent. Never RAISES a class; it only ever caps urgent → needs_owner.
    if classification == "urgent" and not credible_sender:
        classification = "needs_owner"
        reason = (
            "material harm from an unverified, non-allowlisted sender; capped at "
            "NEEDS_OWNER (fake-urgency guard)"
        )

    # Owner surface / classify_hint rule pins a would-be MAYBE/IGNORE to visible
    # (raises visibility only, never lowers it — safe regardless of F04 ordering).
    surface_rule = _owner_surface_forced(owner_rules, message)
    if classification in ("maybe", "ignore") and surface_rule:
        classification = "needs_owner"
        recommended = recommend_action("account", message)
        reason = "owner surface/hint rule pinned this message to visible"
        confidence = max(confidence, 0.6)
        rule_matches = [*rule_matches, f"rule:{surface_rule}"]

    # snooze_default: a PRE-FILL hint only (never an auto-snooze). Attaches a
    # suggested default snooze duration for the owner to accept with one tap.
    snooze_rule = _owner_snooze_default(owner_rules, message)
    if snooze_rule is not None:
        hours = (snooze_rule.get("action") or {}).get("hours")
        recommended = dict(recommended)
        params = dict(recommended.get("params") or {})
        params["snooze_default_hours"] = hours
        params["snooze_rule_id"] = snooze_rule.get("id")
        recommended["params"] = params
        rule_matches = [*rule_matches, f"rule:{snooze_rule.get('id')}"]

    # auto_delegate: PRE-FILL the delegation on a match (never urgent — an urgent
    # harm item keeps its own concrete resolution so the owner sees it at once).
    # Whether it actually fires unattended is the per-rule ``action.live`` gate the
    # triage loop consults (F11); classify only records the pre-fill.
    if classification != "urgent":
        ad_rule = _owner_auto_delegate(owner_rules, message)
        if ad_rule is not None:
            action = ad_rule.get("action") or {}
            assignee = str(action.get("assignee") or "")
            if assignee:
                if classification in ("maybe", "ignore"):
                    classification = "needs_owner"
                # Unattended execution is opt-in and PER RULE: only a promoted
                # ``auto_delegate`` kind whose own ``action.live`` is set can fire
                # unattended. A plain approved ``delegate`` rule always pre-fills.
                live = ad_rule.get("kind") == "auto_delegate" and bool(action.get("live"))
                delegate_params = {
                    "auto_delegate": True,
                    "assignee": assignee,
                    "rule_id": ad_rule.get("id"),
                    "live": live,
                    "prefill": True,
                }
                if has_harm:
                    # F04 / harm-before-suppress: a NEEDS_OWNER item with DETECTED
                    # material harm already carries a concrete harm action ("update
                    # the payment method on {provider} now"). NEVER clobber it with
                    # the delegate pre-fill prompt — the harm signal must survive
                    # into the digest. Attach the delegate as SUPPLEMENTAL params so
                    # the one-tap affordance still exists without erasing the harm
                    # ``recommended.kind``/``human_line``/``reason``.
                    recommended = dict(recommended)
                    recommended["params"] = {
                        **(recommended.get("params") or {}),
                        **delegate_params,
                    }
                else:
                    recommended = {
                        "kind": "delegate",
                        "human_line": (
                            f"rule {ad_rule.get('id')} would assign this to {assignee} — approve?"
                        ),
                        "params": {
                            **(recommended.get("params") or {}),
                            **delegate_params,
                        },
                    }
                    reason = f"rule {ad_rule.get('id')} pre-filled a delegation"
                rule_matches = [*rule_matches, f"rule:{ad_rule.get('id')}"]

    status = _STATUS_BY_CLASS[classification]
    return {
        "classification": classification,
        "consequence": consequence,
        "deadline_at": deadline_at,
        "likelihood": likelihood,
        "confidence": confidence,
        "reason": reason,
        "classifier": classifier,
        "rule_matches": rule_matches,
        "recommended": recommended,
        "available_actions": list(_ACTIONS_BY_CLASS[classification]),
        "status": status,
        "surfaced": 0,
    }
