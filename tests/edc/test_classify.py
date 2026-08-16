"""Deterministic classifier tests — the §14 P1 Noise/Urgent/Needs-the operator matrix.

Pure (no DB): each fixture is a :class:`SourceEvent` classified against a fixed
clock. Proves the harm-before-suppress ordering (CODEX F04), the concrete
recommended-action templates (invariant 1), and that scary WORDING never reaches
URGENT.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omniagentos.edc.classify import classify
from omniagentos.steward.alerts.rules import edc_suppress

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)


class _AmbiguousMaybeClient:
    """Offline default for fixtures whose deterministic rules stay ambiguous."""

    def complete_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "consequence": "none",
            "consequence_evidence": "",
            "deadline_at": None,
            "deadline_evidence": "",
            "likelihood": 0.2,
            "category": "informational",
            "confidence": 0.2,
            "reason": "ambiguous offline fixture",
            "recommended_action": "",
        }


def _event(
    subject: str, body: str, sender: str, *, sender_verified: bool = True, **meta: Any
) -> dict[str, Any]:
    # Fixtures default to an AUTHENTICATED sender: they represent REAL provider
    # notices (Stripe/GitHub/Heroku/…), which in production are DKIM/SPF-verified.
    # The fake-urgency tests below pass ``sender_verified=False`` explicitly.
    return {
        "source": "email",
        "source_ref": "1",
        "source_account": "gmail_ownera",
        "owner_employee_id": "emp_owner",
        "company_slug": "",
        "occurred_at": "2026-08-13T09:00:00Z",
        "title": subject,
        "body": body,
        "counterparty": sender,
        "sender_verified": sender_verified,
        "metadata": meta,
    }


def _classify(
    subject: str,
    body: str,
    sender: str,
    *,
    sender_verified: bool = True,
    credible_domains: list[str] | None = None,
    **meta: Any,
) -> dict[str, Any]:
    return classify(
        _event(subject, body, sender, sender_verified=sender_verified, **meta),
        now=NOW,
        llm_client=_AmbiguousMaybeClient(),
        credible_domains=credible_domains,
    )


@pytest.mark.parametrize("error", [TimeoutError("timeout"), RuntimeError("outage")])
def test_llm_outage_or_timeout_fails_closed_to_maybe(error: Exception) -> None:
    class BrokenClient:
        def complete_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise error

    verdict = classify(
        _event("Thoughts?", "Could you look at this when you have time?", "person@example.com"),
        now=NOW,
        llm_client=BrokenClient(),
    )
    assert verdict["classification"] == "maybe"
    assert verdict["classification"] not in {"urgent", "ignore"}
    assert verdict["classifier"] == "llm_unavailable"


def test_llm_ungrounded_inputs_cannot_manufacture_urgent() -> None:
    class HallucinatingClient:
        def complete_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "consequence": "financial",
                "consequence_evidence": "your account will be terminated",
                "deadline_at": "2026-08-13T13:00:00Z",
                "deadline_evidence": "within one hour",
                "likelihood": 0.99,
                "category": "billing",
                "confidence": 0.99,
                "reason": "model estimate",
                "recommended_action": "Review the request",
            }

    verdict = classify(
        _event("Thoughts?", "Could you look at this when you have time?", "person@example.com"),
        now=NOW,
        llm_client=HallucinatingClient(),
    )
    assert verdict["classification"] == "needs_owner"
    assert verdict["classification"] != "urgent"
    assert verdict["deadline_at"] is None
    assert verdict["consequence"] == "none"


# --- Noise: suppressed rows, no surfacing -----------------------------------


def test_newsletter_is_ignored() -> None:
    verdict = _classify(
        "Weekly Newsletter — 5 growth tips",
        "Here are this week's tips. Unsubscribe here.",
        "news@marketing.example.com",
    )
    assert verdict["classification"] == "ignore"
    assert verdict["status"] == "suppressed"
    assert verdict["surfaced"] == 0


def test_routine_receipt_is_ignored() -> None:
    verdict = _classify(
        "Your receipt from Acme Store",
        "Thanks for your purchase. Order confirmation #55.",
        "receipts@acme.com",
    )
    assert verdict["classification"] == "ignore"


def test_cold_sales_is_ignored() -> None:
    verdict = _classify(
        "Quick question",
        "Book a demo to grow your revenue with our platform.",
        "sdr@vendor.io",
    )
    assert verdict["classification"] == "ignore"


def test_urgent_wording_marketing_is_ignored() -> None:
    """'URGENT' + 'tonight' are WORDING/deadline; with no consequence it's noise."""
    verdict = _classify(
        "URGENT: 50% off ends tonight!",
        "Limited time offer. Shop now and save big.",
        "deals@shop.example.com",
    )
    assert verdict["classification"] == "ignore"


def test_fake_invoice_spam_is_ignored() -> None:
    verdict = _classify(
        "Invoice #4821 — please view",
        "Your invoice is attached. Unsubscribe to stop these.",
        "billing@no-reply.invoice-alerts.co",
    )
    assert verdict["classification"] == "ignore"


# --- Urgent: interrupt + concrete resolution --------------------------------


def test_payment_failure_suspension_tonight_is_urgent_with_resolution() -> None:
    verdict = _classify(
        "Your payment failed",
        "We could not process your payment. Your account will be suspended "
        "tonight unless you update your payment method.",
        "billing@stripe.com",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "financial"
    line = verdict["recommended"]["human_line"]
    assert "payment method" in line.lower()
    assert "Stripe" in line
    assert "reply" not in line.lower()  # the resolution, not "reply to X"


def test_security_incident_is_urgent_without_explicit_deadline() -> None:
    verdict = _classify(
        "Security alert: new sign-in",
        "We detected a suspicious sign-in to your account from an unrecognized device.",
        "security@github.com",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "security"
    assert "security" in verdict["recommended"]["human_line"].lower()


def test_shutdown_in_24h_is_urgent() -> None:
    verdict = _classify(
        "Service shutdown notice",
        "Your dyno will be shut down within 24 hours due to a platform migration.",
        "ops@heroku.com",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "service_loss"
    assert verdict["deadline_at"] is not None


def test_legal_deadline_tomorrow_is_urgent() -> None:
    verdict = _classify(
        "Response required",
        "Our client's legal deadline is tomorrow; we need your response by tomorrow.",
        "legal@counsel.com",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "legal"


# --- Needs-the operator: morning digest, no interrupt --------------------------------


def test_payment_update_in_7_days_is_needs_owner() -> None:
    verdict = _classify(
        "Update your payment method",
        "Please update your payment method within 7 days to avoid a lapse.",
        "billing@aws.com",
    )
    assert verdict["classification"] == "needs_owner"
    assert verdict["surfaced"] == 0
    assert verdict["recommended"]["human_line"]  # invariant 1: concrete action present


def test_approval_request_is_needs_owner() -> None:
    verdict = _classify(
        "Approval needed on the new landing copy",
        "Approval needed before we ship the new page.",
        "bob@initech.example",
    )
    assert verdict["classification"] == "needs_owner"


def test_vendor_decision_is_needs_owner() -> None:
    verdict = _classify(
        "Which vendor should we pick?",
        "Need your decision on the hosting vendor before Friday's kickoff.",
        "alice@initech.example",
    )
    assert verdict["classification"] == "needs_owner"


def test_contract_issue_is_needs_owner() -> None:
    verdict = _classify(
        "Contract issue with the SaaS agreement",
        "There is a contract dispute in section 4 we should discuss.",
        "vendor@saas.com",
    )
    assert verdict["classification"] == "needs_owner"


# --- Calibration 2026-08-13: false-positive URGENTs / NEEDS_OWNERs -------------
# Restored 2026-08-14 (deleted by an earlier commit, covered guards restored by #403).
# These protect the OTP / product-nudge / meeting-prep / genuine-breach paths.
# Assertions REVALIDATED against the current classifier: it now runs a
# sender-credibility gate (URGENT needs a verified/allowlisted sender PLUS
# grounded active harm), and fixtures default to sender_verified=True to model
# real DKIM-signed provider mail.
#
# Inbox-regression fixtures: a meeting TIME is a deadline but not material harm, so
# meeting-prep must never reach URGENT; OTP codes and product nudges are
# automated no-action mail and must not reach NEEDS_OWNER.


def test_meeting_prep_is_not_urgent() -> None:
    """A meeting time is a deadline, NOT a material-harm consequence."""
    for subject, body, sender in (
        (
            "Meeting Prep: Initech <> Stripe",
            "Your meeting is tomorrow at 2pm. Agenda: review the integration "
            "plan and confirm your decision on pricing.",
            "notifications@calendar-tool.com",
        ),
        (
            "Meeting Prep: VWO <> Initech : Biweekly Sync",
            "Reminder: your biweekly sync is tomorrow. Review the agenda; "
            "waiting for you to add topics.",
            "hello@read.ai",
        ),
    ):
        verdict = _classify(subject, body, sender)
        assert verdict["classification"] != "urgent"
        assert verdict["classification"] in ("needs_owner", "maybe", "ignore")
        # Root cause fixed: scheduling wording no longer borrows a consequence.
        assert verdict["consequence"] == "none"


def test_meeting_prep_from_person_is_maybe_not_urgent() -> None:
    """Even from a human sender (no suppress), a meeting prep is at most MAYBE."""
    verdict = _classify(
        "Meeting Prep: Initech <> Stripe",
        "Agenda for our meeting tomorrow: review the plan and confirm your decision on pricing.",
        "alex@partner.com",
    )
    assert verdict["classification"] == "maybe"
    assert verdict["consequence"] == "none"


def test_verification_code_is_not_needs_owner() -> None:
    """An OTP's 'verify your identity' line is a login code, not a security incident."""
    verdict = _classify(
        "BytePlus Verification Code",
        "Your verification code is 483920. Use it to verify your identity. "
        "This code expires in 10 minutes.",
        "no-reply@verify.byteplus.com",
    )
    assert verdict["classification"] in ("ignore", "maybe")
    assert verdict["classification"] != "needs_owner"
    assert verdict["consequence"] == "none"


def test_product_nudge_is_ignored() -> None:
    """An automated engagement nudge needs no owner decision → IGNORE."""
    verdict = _classify(
        "Don't leave your team hanging",
        "Your team is waiting for you. Complete your profile and invite your "
        "teammates to get started.",
        "team@product.example.com",
    )
    assert verdict["classification"] == "ignore"
    assert verdict["consequence"] == "none"


def test_real_security_breach_with_code_still_urgent() -> None:
    """F04/calibration boundary: a hard-breach signal survives OTP wording.

    Revalidated against the current sender-credibility gate: URGENT requires a
    CREDIBLE sender in addition to grounded active harm. The default fixture
    sender is ``sender_verified=True`` (DKIM/SPF-aligned provider mail), so a
    genuine breach alert from ``security@github.com`` stays URGENT. The body
    carries grounded active harm ("we detected a suspicious sign-in from an
    unrecognized device") — this is the true-positive the gate must not cap.
    """
    verdict = _classify(
        "Security alert: suspicious sign-in",
        "We detected a suspicious sign-in from an unrecognized device. Your "
        "verification code is 111. Verify your identity now.",
        "security@github.com",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "security"


# --- CODEX F04: harm detected BEFORE suppression can hide it -----------------


def test_harm_from_noreply_sender_beats_suppression() -> None:
    """A real payment failure from a noreply sender a suppress rule WOULD hide."""
    subject = "Payment failed"
    body = "Your payment has failed and your account will be suspended. Unsubscribe."
    sender = "no-reply@aws.com"
    # The suppress net WOULD hide this (noreply sender + unsubscribe)...
    message = {"sender": sender, "subject": subject, "body_text": body}
    assert edc_suppress(message)  # non-empty => suppression was available
    # ...but harm is detected first, so it surfaces as URGENT anyway.
    verdict = _classify(subject, body, sender)
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "financial"


# --- Invariant 1: every surfaced verdict carries a concrete action -----------


def test_surfaced_verdicts_always_have_a_recommended_action() -> None:
    for verdict in (
        _classify("Payment failed", "will be suspended tonight", "billing@stripe.com"),
        _classify("Approval needed", "approval needed to proceed", "a@b.com"),
    ):
        assert verdict["classification"] in ("urgent", "needs_owner")
        assert verdict["recommended"]["human_line"]


# --- F05 calibration r2: representative false-positive shapes ---------------
#
# The first calibration pass used synthetic fixtures and missed these — a
# `edc_consequence` bucket firing on a TOPIC KEYWORD rather than harm directed
# at the recipient. Bodies below are fully-invented messages tuned to
# reproduce the same trigger phrase families that commonly cause this
# false-positive class: meeting-bot recording-consent boilerplate, marketing
# pitches that happen to mention "security", newsletters about a security
# topic, and repeated billing-provider payment-failure notices.


def test_meeting_bot_recording_consent_is_not_legal() -> None:
    """Fireflies.ai 'Meeting Prep' bot: recording-consent + contract-renewal
    agenda boilerplate must not trip the `legal` consequence bucket."""
    verdict = _classify(
        "Meeting Prep: VWO <> Initech : Biweekly Sync",
        "This meeting will be recorded. By joining, you consent to being "
        "recorded for legal and compliance purposes. Agenda: review contract "
        "renewal terms and next steps.",
        "Meeting Bot <bot@fireflies.ai>",
    )
    assert verdict["classification"] != "urgent"
    assert verdict["consequence"] == "none"


def test_meeting_bot_prep_about_a_billing_provider_is_not_legal() -> None:
    """Same meeting-bot template, this time the meeting is ABOUT Stripe — a
    meeting about a payment provider is not itself a payment failure."""
    verdict = _classify(
        "Meeting Prep: Initech <> Stripe",
        "This meeting will be recorded. By joining, you consent to being "
        "recorded for legal and compliance purposes. Agenda: discuss the "
        "Stripe integration and contract renewal terms.",
        "Meeting Bot <bot@fireflies.ai>",
    )
    assert verdict["classification"] != "urgent"
    assert verdict["consequence"] == "none"


def test_github_marketing_pitch_is_not_security_incident() -> None:
    """GitHub product-marketing email pitching 'security features' /
    'stay in control' is a pitch, not a security incident."""
    verdict = _classify(
        "Improve code quality, boost Copilot adoption, and stay in control",
        "The latest launches from GitHub. See the newest security features "
        "and how to get started. Unsubscribe here.",
        "GitHub <no-reply@github.com>",
    )
    assert verdict["classification"] != "urgent"
    assert verdict["consequence"] == "none"


def test_newsletter_about_security_topic_is_not_a_breach() -> None:
    """A newsletter ABOUT a security topic (watermarks) is informational, not
    a breach affecting the recipient's own account."""
    verdict = _classify(
        "The Invisible Watermarks",
        "This week's issue: how invisible watermarks help detect data "
        "breach and compromised AI-generated content. Unsubscribe anytime.",
        "Security Newsletter <newsletter@bulknews.example>",
    )
    assert verdict["classification"] != "urgent"
    assert verdict["consequence"] == "none"


def test_invisible_watermarks_newsletter_from_bulk_subdomain_is_not_urgent() -> None:
    """A typical newsletter about a security topic is not account harm."""
    verdict = _classify(
        "🦞 The Invisible Watermarks",
        "How invisible watermarks help with security and identify AI-generated "
        "content. This week's newsletter discusses data-protection techniques."
        " Unsubscribe anytime.",
        "newsletter@bulk.bulknews.example",
    )
    assert verdict["classification"] != "urgent"
    assert verdict["consequence"] == "none"


def test_real_security_alert_from_noreply_sender_still_urgent() -> None:
    """Grounding must not overcorrect: a genuine breach alert from a no-reply
    sender still surfaces — 'we detected unusual activity' names an action
    against the recipient's OWN account, unlike a topic mention."""
    verdict = _classify(
        "Security alert for your account",
        "We detected unusual activity on your account. If this wasn't you, "
        "secure your account now.",
        "no-reply@accounts.google.com",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "security"


def test_freshdesk_payment_failed_account_suspended_stays_urgent() -> None:
    """Freshdesk billing fixture: payment-failure + account-suspension notice
    from a billing sender about the recipient's own account — must stay URGENT."""
    verdict = _classify(
        "URGENT - Freshdesk Account Suspended (Payment Failed)",
        "Hi the operator, your Freshdesk account has been suspended because we "
        "could not process your payment after several attempts. Reactivate "
        "within 14 days.",
        "Freshdesk Billing <billing@freshworks.com>",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "financial"


def test_freshdesk_final_notice_payment_failed_stays_urgent() -> None:
    verdict = _classify(
        "Final Notice - Freshdesk Payment Failed (Account Suspension)",
        "Hi the operator, unfortunately your service will soon be suspended "
        "because we could not process your payment after several attempts.",
        "Freshdesk Billing <billing@freshworks.com>",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "financial"


def test_freshdesk_payment_failed_third_attempt_stays_urgent() -> None:
    verdict = _classify(
        "Action Required: Freshdesk Payment Failed (3rd Attempt)",
        "Hi the operator, your most recent payment was declined for the third "
        "time. Update your payment method as soon as possible.",
        "Freshdesk Billing <billing@freshworks.com>",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "financial"


def test_freshdesk_payment_failed_second_attempt_stays_urgent() -> None:
    verdict = _classify(
        "Action Required: Freshdesk Payment Failed (2nd Attempt)",
        "Hi the operator, we were unsuccessful in our second attempt to process "
        "your payment. Update your payment method to keep your subscription "
        "active.",
        "Freshdesk Billing <billing@freshworks.com>",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "financial"


def test_mailgun_failed_credit_card_transaction_stays_urgent() -> None:
    """Mailgun billing notice — card-decline, must stay URGENT."""
    verdict = _classify(
        "NOTICE: Failed Credit Card Transaction, 2nd Attempt",
        "Hi the operator, there's still a problem with your credit card — your "
        "payment was declined. Until you've updated your billing info, we "
        "are unable to process your balance currently due on your account.",
        "invoices@mailgun.net",
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "financial"


# --- OPUS-MAJOR: sender-credibility gate on URGENT --------------------------
#
# The SAME payment-failure wording is URGENT from a verified/allowlisted sender
# and only NEEDS_OWNER from an unverified, non-allowlisted stranger — scary wording
# from a stranger is exactly the fake-urgency the operator must not be pinged about.

_FAKE_URGENCY_BODY = (
    "Your payment has failed and your account will be suspended tonight "
    "unless you update your payment method immediately."
)
_FAKE_URGENCY_SUBJECT = "URGENT: your payment failed — act now"


def test_allowlisted_provider_payment_failure_stays_urgent_even_unverified() -> None:
    """A payment-failure from an allowlisted provider domain is URGENT even
    when the individual message did not carry a verifiable DKIM/SPF pass."""
    verdict = _classify(
        _FAKE_URGENCY_SUBJECT,
        _FAKE_URGENCY_BODY,
        "billing@freshworks.com",
        sender_verified=False,
        credible_domains=["freshworks.com", "mailgun.net"],
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "financial"
    assert verdict["recommended"]["human_line"]


def test_dkim_verified_sender_payment_failure_is_urgent() -> None:
    """A DKIM/SPF-verified sender (even off the allowlist) reaches URGENT."""
    verdict = _classify(
        _FAKE_URGENCY_SUBJECT,
        _FAKE_URGENCY_BODY,
        "billing@some-legit-saas.com",
        sender_verified=True,
        credible_domains=["freshworks.com"],
    )
    assert verdict["classification"] == "urgent"
    assert verdict["consequence"] == "financial"


def test_same_wording_from_unverified_stranger_caps_at_needs_owner() -> None:
    """IDENTICAL wording from an UNVERIFIED, non-allowlisted sender must NOT
    interrupt: it caps at NEEDS_OWNER (the fake-urgency guard)."""
    verdict = _classify(
        _FAKE_URGENCY_SUBJECT,
        _FAKE_URGENCY_BODY,
        "billing@sketchy-collections-xyz.io",
        sender_verified=False,
        credible_domains=["freshworks.com", "mailgun.net"],
    )
    assert verdict["classification"] == "needs_owner"
    assert verdict["classification"] != "urgent"
    # Harm was still detected (not suppressed) and a concrete action is attached —
    # it rides the digest, it just does not ping the operator.
    assert verdict["consequence"] == "financial"
    assert verdict["recommended"]["human_line"]


def test_credibility_gate_never_downgrades_a_genuine_provider() -> None:
    """The gate only ever CAPS urgent→needs_owner; a verified provider is untouched."""
    verified = _classify(
        _FAKE_URGENCY_SUBJECT,
        _FAKE_URGENCY_BODY,
        "billing@freshworks.com",
        sender_verified=True,
    )
    assert verified["classification"] == "urgent"
