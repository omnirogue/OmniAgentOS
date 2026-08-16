"""Owner + per-mailbox identity stamping at comms ingest (review F01 + §8)."""

from __future__ import annotations

from omniagentos.edc.accounts import accounts_map
from omniagentos.edc.ingest import stamp_message_owner
from omniagentos.steward.config import EdcAccountCfg, EdcConfig
from omniagentos.steward.store import StewardStore


def _accounts() -> dict[str, object]:
    return accounts_map(
        EdcConfig(
            accounts={
                "gmail_ownera": EdcAccountCfg(
                    owner_employee_id="emp_owner", source_account="gmail_ownera"
                ),
                "gmail_initech": EdcAccountCfg(
                    owner_employee_id="emp_owner", source_account="gmail_initech"
                ),
            }
        )
    )


def _normalized(external_id: str) -> dict[str, object]:
    # Mirrors normalize_imap output: source hard-coded to 'imap' for every mailbox.
    return {
        "source": "imap",
        "external_id": external_id,
        "sender": "billing@vendor.example",
        "subject": "Invoice",
        "body_text": "body",
        "recipients": [],
        "attachments": [],
        "raw": {},
    }


def test_stamp_sets_per_mailbox_source_and_owner() -> None:
    stamped, mapped = stamp_message_owner(_normalized("<m1>"), "gmail_ownera", _accounts())
    assert mapped is True
    assert stamped["source"] == "gmail_ownera"  # F01: no longer the shared 'imap'
    assert stamped["owner_employee_id"] == "emp_owner"


def test_unmapped_source_left_owner_null_but_identified() -> None:
    stamped, mapped = stamp_message_owner(_normalized("<m1>"), "gmail_stranger", _accounts())
    assert mapped is False
    # Still per-mailbox identity (no collision), but no owner is guessed.
    assert stamped["source"] == "gmail_stranger"
    assert "owner_employee_id" not in stamped


def test_two_owners_same_message_id_are_two_rows(
    steward: StewardStore, employees: dict[str, str]
) -> None:
    """F01: a shared Message-ID across two mailboxes must NOT collide/lose a row."""
    accounts = _accounts()

    a, created_a = steward.insert_comms_message(
        stamp_message_owner(_normalized("<shared@vendor>"), "gmail_ownera", accounts)[0]
    )
    b, created_b = steward.insert_comms_message(
        stamp_message_owner(_normalized("<shared@vendor>"), "gmail_initech", accounts)[0]
    )
    assert created_a and created_b, "the second owner's row must not be swallowed by dedupe"
    assert a["id"] != b["id"]
    assert a["source"] == "gmail_ownera"
    assert b["source"] == "gmail_initech"


def test_same_mailbox_same_message_id_still_dedupes(
    steward: StewardStore, employees: dict[str, str]
) -> None:
    # Per-mailbox re-poll of the same UID/Message-ID is still one row.
    accounts = _accounts()
    msg = stamp_message_owner(_normalized("<dup@vendor>"), "gmail_ownera", accounts)[0]
    _first, created_1 = steward.insert_comms_message(dict(msg))
    _second, created_2 = steward.insert_comms_message(dict(msg))
    assert created_1 is True
    assert created_2 is False


def test_list_comms_owner_filter(steward: StewardStore, employees: dict[str, str]) -> None:
    accounts = _accounts()
    steward.insert_comms_message(
        stamp_message_owner(_normalized("<own>"), "gmail_ownera", accounts)[0]
    )
    steward.insert_comms_message(_normalized("<noowner>"))  # source 'imap', owner NULL
    owned = steward.list_comms_messages(owner_employee_id="emp_owner")
    assert [m["external_id"] for m in owned] == ["<own>"]
