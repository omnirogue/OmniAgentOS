"""EmailAdapter + triage pipeline tests (synthesis §3/§11, P1).

Drives the real DecisionStore/StewardStore composition over one SqliteStore (the
conftest ``store`` fixture) and asserts the §14 end-to-end read path: suppressed
rows with ZERO DMs, an URGENT DM carrying the concrete resolution, the O(new)
watermark, the D3 dedupe backstop, the F06 reclassify pass, and the Future
Sources proof (a ``source='agent'`` event through the same core).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omniagentos.edc.accounts import SourceOwner
from omniagentos.edc.adapters.base import SourceEvent
from omniagentos.edc.adapters.email import EmailAdapter
from omniagentos.edc.main import run_reclassify, run_triage
from omniagentos.steward.config import EdcAccountCfg, EdcConfig, StewardConfig
from omniagentos.steward.store import StewardStore

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
_ACCOUNTS = {"gmail_ownera": SourceOwner("emp_owner", "", "gmail_ownera")}
_CFG = StewardConfig(
    edc=EdcConfig(
        accounts={
            "gmail_ownera": EdcAccountCfg(
                owner_employee_id="emp_owner", source_account="gmail_ownera"
            )
        }
    )
)


class _RecordingNotifier:
    def __init__(self) -> None:
        self.dms: list[tuple[str, str]] = []

    def post_dm(
        self,
        slack_user_id: str,
        text: str,
        *,
        blocks: list | None = None,
        color: str | None = None,
    ) -> bool:
        self.dms.append((slack_user_id, text))
        return True


def _seed_message(
    steward: StewardStore,
    *,
    external_id: str,
    subject: str,
    body: str,
    sender: str,
    owner: str = "emp_owner",
    source: str = "gmail_ownera",
) -> int:
    row, _created = steward.insert_comms_message(
        {
            "source": source,
            "external_id": external_id,
            "sender": sender,
            "subject": subject,
            "body_text": body,
            "sent_at": "2026-08-13T09:00:00Z",
            "owner_employee_id": owner,
        }
    )
    return int(row["id"])


def _triage(
    store: Any, notifier: Any = None, reverse: dict[str, str] | None = None
) -> dict[str, int]:
    return run_triage(
        store,
        cfg=_CFG,
        now=NOW,
        notifier=notifier,
        slack_reverse=reverse,
        adapters=[EmailAdapter(accounts=_ACCOUNTS)],
    )


def test_noise_produces_suppressed_rows_and_zero_dms(store, decisions, steward, employees) -> None:
    _seed_message(
        steward,
        external_id="n1",
        subject="Weekly Newsletter",
        body="tips. Unsubscribe here.",
        sender="news@marketing.example.com",
    )
    _seed_message(
        steward,
        external_id="n2",
        subject="Your receipt from Acme",
        body="Thanks for your purchase.",
        sender="receipts@acme.com",
    )
    notifier = _RecordingNotifier()
    stats = _triage(store, notifier, {"emp_owner": "U1"})

    rows = decisions.list_decisions(owner_employee_id="emp_owner")
    assert len(rows) == 2  # every message becomes a row, including IGNORE
    assert {r["status"] for r in rows} == {"suppressed"}
    assert stats["ignored"] == 2
    assert notifier.dms == []  # ZERO DMs for noise


def test_urgent_sends_dm_with_concrete_resolution(store, decisions, steward, employees) -> None:
    _seed_message(
        steward,
        external_id="u1",
        subject="Your payment failed",
        body="We could not process your payment. Your account will be suspended "
        "tonight unless you update your payment method.",
        sender="billing@stripe.com",
    )
    notifier = _RecordingNotifier()
    stats = _triage(store, notifier, {"emp_owner": "U1"})

    assert stats["urgent"] == 1
    assert stats["dm_sent"] == 1
    assert len(notifier.dms) == 1
    slack_id, text = notifier.dms[0]
    assert slack_id == "U1"
    assert "payment method" in text.lower()

    row = decisions.list_decisions(owner_employee_id="emp_owner", classification="urgent")[0]
    assert row["surfaced"] == 1  # one ping per decision
    events = decisions.list_events(row["id"], owner_employee_id="emp_owner")
    assert any(e["event"] == "surface" for e in events)


def test_needs_owner_does_not_interrupt(store, decisions, steward, employees) -> None:
    _seed_message(
        steward,
        external_id="ns1",
        subject="Update your payment method",
        body="Please update your payment method within 7 days.",
        sender="billing@aws.com",
    )
    notifier = _RecordingNotifier()
    _triage(store, notifier, {"emp_owner": "U1"})
    assert notifier.dms == []  # NEEDS_OWNER rides the morning digest, no interrupt
    row = decisions.list_decisions(owner_employee_id="emp_owner", classification="needs_owner")[0]
    assert row["surfaced"] == 0


def test_watermark_makes_rescan_a_noop_and_is_o_new(store, decisions, steward, employees) -> None:
    first = _seed_message(
        steward, external_id="w1", subject="Weekly Newsletter", body="Unsubscribe", sender="a@b.co"
    )
    _triage(store)
    cursor = decisions.get_source_cursor("email", "emp_owner")
    assert cursor is not None
    assert int(cursor["last_message_id"]) == first

    # A second sweep with no new mail sees nothing beyond the watermark.
    stats = _triage(store)
    assert stats["seen"] == 0

    # A new message advances the watermark; the old one is never re-read.
    second = _seed_message(
        steward,
        external_id="w2",
        subject="Security alert",
        body="suspicious sign-in detected",
        sender="s@ok.com",
    )
    stats = _triage(store)
    assert stats["seen"] == 1
    assert int(decisions.get_source_cursor("email", "emp_owner")["last_message_id"]) == second


def test_dedupe_backstop_survives_a_reset_cursor(store, decisions, steward, employees) -> None:
    msg_id = _seed_message(
        steward, external_id="d1", subject="Weekly Newsletter", body="Unsubscribe", sender="a@b.co"
    )
    _triage(store)
    assert len(decisions.list_decisions(owner_employee_id="emp_owner")) == 1
    # Simulate a crash between the durable write and the cursor advance: rewind
    # the cursor. The UNIQUE(source, source_ref, owner) backstop makes the
    # re-scan a no-op — no duplicate row.
    decisions.advance_source_cursor("email", "emp_owner", last_message_id="0")
    stats = _triage(store)
    assert stats["duplicate"] == 1
    assert len(decisions.list_decisions(owner_employee_id="emp_owner")) == 1
    assert int(decisions.get_source_cursor("email", "emp_owner")["last_message_id"]) == msg_id


def test_cross_owner_is_isolated(store, decisions, steward, employees) -> None:
    accounts = {
        "gmail_ownera": SourceOwner("emp_owner", "", "gmail_ownera"),
        "gmail_bob": SourceOwner("emp_bob", "", "gmail_bob"),
    }
    _seed_message(
        steward,
        external_id="a1",
        subject="Security alert",
        body="suspicious sign-in",
        sender="x@y.com",
    )
    _seed_message(
        steward,
        external_id="b1",
        subject="Security alert",
        body="suspicious sign-in",
        sender="x@y.com",
        owner="emp_bob",
        source="gmail_bob",
    )
    run_triage(store, cfg=_CFG, now=NOW, adapters=[EmailAdapter(accounts=accounts)])
    assert len(decisions.list_decisions(owner_employee_id="emp_owner")) == 1
    assert len(decisions.list_decisions(owner_employee_id="emp_bob")) == 1
    # Each owner's watermark advances independently.
    assert decisions.get_source_cursor("email", "emp_owner") is not None
    assert decisions.get_source_cursor("email", "emp_bob") is not None


def test_reclassify_promotes_a_fail_closed_maybe(store, decisions, steward, employees) -> None:
    """CODEX F06: an llm_unavailable MAYBE over a harm-bearing message is re-run."""
    msg_id = _seed_message(
        steward,
        external_id="r1",
        subject="Your payment failed",
        body="Your account will be suspended tonight; update your payment method.",
        sender="billing@stripe.com",
    )
    # Simulate a P2 LLM outage that fail-closed this real urgent to MAYBE.
    decisions.create_decision(
        {
            "owner_employee_id": "emp_owner",
            "source": "email",
            "source_ref": str(msg_id),
            "title": "Your payment failed",
            "classification": "maybe",
            "classifier": "llm_unavailable",
            "recommended": {},
        }
    )
    notifier = _RecordingNotifier()
    stats = run_reclassify(
        store, cfg=_CFG, now=NOW, notifier=notifier, slack_reverse={"emp_owner": "U1"}
    )
    assert stats["reclassified"] == 1
    assert stats["promoted"] == 1
    row = decisions.list_decisions(owner_employee_id="emp_owner")[0]
    assert row["classification"] == "urgent"
    assert row["classifier"] == "deterministic"
    assert row["recommended"]["human_line"]
    assert len(notifier.dms) == 1


def test_future_source_agent_event_enters_pipeline_with_zero_core_change(
    store, decisions, employees
) -> None:
    """A SourceEvent(source='agent') runs the identical core — Future Sources."""

    class _AgentAdapter:
        name = "agent"

        def pending_events(self, _store: Any) -> list[SourceEvent]:
            return [
                SourceEvent(
                    source="agent",
                    source_ref="agent-run-7",
                    source_account="orchestrator",
                    owner_employee_id="emp_owner",
                    company_slug="",
                    occurred_at="2026-08-13T09:00:00Z",
                    title="Agent needs a decision: which vendor should we pick?",
                    body="Need your decision on the hosting vendor.",
                    counterparty="orchestrator",
                    sender_verified=False,
                    metadata={},
                )
            ]

    stats = run_triage(store, cfg=_CFG, now=NOW, adapters=[_AgentAdapter()])
    assert stats["created"] == 1
    rows = decisions.list_decisions(owner_employee_id="emp_owner")
    assert len(rows) == 1
    assert rows[0]["source"] == "agent"
    assert rows[0]["classification"] == "needs_owner"
    # No email watermark was touched for a non-email source.
    assert decisions.get_source_cursor("email", "emp_owner") is None
