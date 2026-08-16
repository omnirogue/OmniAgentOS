"""OPUS-MAJOR: sender authentication derived from ingest headers.

``event_from_comms_row`` sets ``sender_verified`` on the SourceEvent from the
``Authentication-Results`` header the gmail/imap normalizers already preserve in
``raw['headers']``. A DKIM/SPF ``pass`` counts ONLY when it aligns to the From
domain; anything absent/unparseable/unaligned fails CLOSED to ``False``.
"""

from __future__ import annotations

from omniagentos.edc.accounts import SourceOwner
from omniagentos.edc.adapters.email import (
    event_from_comms_row,
    sender_verified_from_headers,
)

_ACCOUNTS = {"gmail_ownera": SourceOwner("emp_owner", "", "gmail_ownera")}


def test_dkim_pass_aligned_to_from_is_verified() -> None:
    headers = {
        "From": "Freshworks Team <billing@freshworks.com>",
        "Authentication-Results": (
            "mx.google.com; dkim=pass header.i=@freshworks.com header.s=s1 "
            "header.b=abc; spf=pass (google.com: domain of billing@freshworks.com) "
            "smtp.mailfrom=billing@freshworks.com; dmarc=pass"
        ),
    }
    assert sender_verified_from_headers(headers, "billing@freshworks.com") is True


def test_spf_pass_aligned_to_from_is_verified() -> None:
    headers = {
        "Authentication-Results": (
            "mx.google.com; dkim=none; spf=pass smtp.mailfrom=noreply@mailgun.net"
        ),
    }
    assert sender_verified_from_headers(headers, "noreply@mailgun.net") is True


def test_dkim_pass_for_a_different_domain_is_not_aligned() -> None:
    # Attacker gets a DKIM pass for their OWN domain but spoofs the From — unaligned.
    headers = {
        "Authentication-Results": (
            "mx.google.com; dkim=pass header.i=@attacker.test header.s=s1; "
            "spf=pass smtp.mailfrom=bounce@attacker.test"
        ),
    }
    assert sender_verified_from_headers(headers, "billing@stripe.com") is False


def test_no_auth_headers_fails_closed() -> None:
    assert sender_verified_from_headers({"From": "x@y.com"}, "x@y.com") is False
    assert sender_verified_from_headers(None, "x@y.com") is False
    assert sender_verified_from_headers({}, "") is False


def test_subdomain_alignment_is_accepted() -> None:
    headers = {
        "Authentication-Results": "mx; dkim=pass header.d=eu.freshworks.com",
    }
    assert sender_verified_from_headers(headers, "billing@freshworks.com") is True


def test_event_from_comms_row_carries_sender_verified() -> None:
    row = {
        "id": 42,
        "source": "gmail_ownera",
        "sender": "billing@freshworks.com",
        "subject": "Payment failed",
        "body_text": "Your payment failed.",
        "sent_at": "2026-08-13T09:00:00Z",
        "owner_employee_id": "emp_owner",
        "raw": {
            "headers": {
                "From": "billing@freshworks.com",
                "Authentication-Results": (
                    "mx.google.com; dkim=pass header.i=@freshworks.com; spf=pass "
                    "smtp.mailfrom=billing@freshworks.com"
                ),
            }
        },
    }
    event = event_from_comms_row(row, _ACCOUNTS)
    assert event is not None
    assert event["sender_verified"] is True

    row["raw"] = {"headers": {"From": "billing@freshworks.com"}}
    unverified = event_from_comms_row(row, _ACCOUNTS)
    assert unverified is not None
    assert unverified["sender_verified"] is False
