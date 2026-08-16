"""EDC P4: rules in classify · NL rule parse · nightly learning · per-rule
promotion · flip the 501 stub. Every §14 "P4 Verify" bullet is proven here.

The load-bearing safety property is authority separation: learning proposes, the
owner approves, and NOTHING short of an explicit per-rule promotion can mint an
unattended automation kind. That is proven three ways — a source grep, a boundary
ValueError, and the end-to-end "pre-fill by default, fire only with the per-rule
live gate on" flow.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.edc import actions as edc_actions
from omniagentos.edc import learn as edc_learn
from omniagentos.edc import main as edc_main
from omniagentos.edc import rules as edc_rules
from omniagentos.edc.classify import classify
from omniagentos.edc.store import DecisionStore
from tests.edc.conftest import make_decision

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
_DOMAIN = "infra.example.com"


@pytest.fixture(autouse=True)
def _roster(employees: dict[str, str]) -> dict[str, str]:
    """Seed the roster for every P4 test (rules/decisions FK the employee rows)."""
    return employees


class _Notifier:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post_dm(self, slack_user_id: str, text: str, **_kwargs: Any) -> bool:
        self.posts.append(f"{slack_user_id}:{text}")
        return True


def _reverse() -> dict[str, str]:
    return {"emp_owner": "UOWNER", "emp_alice": "UALICE", "emp_bob": "UBOB"}


class _MaybeClient:
    """Offline stand-in for the ambiguity LLM: always a low-confidence MAYBE."""

    def complete_json(self, messages, required_keys, **_kwargs):  # type: ignore[no-untyped-def]
        return {key: "" for key in required_keys} | {"confidence": 0.0, "likelihood": 0.0}


def _classify(event: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
    return classify(event, owner_rules=rules, now=NOW, llm_client=_MaybeClient())


def _event(
    sender: str, subject: str = "access request", body: str = "please grant"
) -> dict[str, Any]:
    return {
        "source": "email",
        "source_ref": "m1",
        "source_account": "gmail_ownera",
        "owner_employee_id": "emp_owner",
        "company_slug": "",
        "occurred_at": "2026-08-13T09:00:00Z",
        "title": subject,
        "body": body,
        "counterparty": sender,
        "sender_verified": True,
        "metadata": {},
    }


def _seed_resolved(
    decisions: DecisionStore,
    *,
    n: int,
    resolution: str,
    domain: str = _DOMAIN,
    assignee: str | None = None,
    source: str = "email",
) -> None:
    for i in range(n):
        decisions.create_decision(
            make_decision(
                source=source,
                source_ref=f"{resolution}-{domain}-{i}",
                counterparty=f"ops{i}@{domain}",
                classification="needs_owner",
                status="done_verified" if resolution == "delegate" else "dismissed",
                resolution=resolution,
                decided_at="2026-08-13T09:00:00Z",
                assignee_employee_id=assignee,
            )
        )


# --- §14: 5 consistent delegations → ONE proposal Decision (not an action) ----


def test_five_consistent_delegations_propose_one_rule_not_an_action(
    store: SqliteStore, decisions: DecisionStore, employees: dict[str, str]
) -> None:
    _seed_resolved(decisions, n=5, resolution="delegate", assignee="emp_bob")

    stats = edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    assert stats["filed"] == 1

    proposed = decisions.list_rules(owner_employee_id="emp_owner", state="proposed")
    assert len(proposed) == 1
    rule = proposed[0]
    assert rule["kind"] == "delegate"  # NOT an automation kind
    assert rule["matcher"]["sender_domain"] == _DOMAIN
    assert rule["action"]["assignee"] == "emp_bob"

    # It is a Decision (approve/deny/edit), never an executed action.
    proposals = [
        d
        for d in decisions.list_decisions(owner_employee_id="emp_owner", status="open")
        if d["source"] == "rule_proposal"
    ]
    assert len(proposals) == 1
    from omniagentos.edc.store import available_actions_for

    assert available_actions_for(proposals[0]) == ["approve", "deny", "edit", "note"]

    # Idempotent: a second pass REFRESHES the standing proposal, never twins it.
    stats2 = edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    assert stats2["filed"] == 0 and stats2["refreshed"] == 1
    assert len(decisions.list_rules(owner_employee_id="emp_owner", state="proposed")) == 1


def test_contrary_decision_blocks_a_proposal(store: SqliteStore, decisions: DecisionStore) -> None:
    _seed_resolved(decisions, n=4, resolution="delegate", assignee="emp_bob")
    # One contrary handling of the SAME domain kills the cluster's consistency.
    decisions.create_decision(
        make_decision(
            source_ref="contrary-1",
            counterparty=f"ops@{_DOMAIN}",
            status="dismissed",
            resolution="dismiss",
            decided_at="2026-08-13T09:00:00Z",
        )
    )
    stats = edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    assert stats["filed"] == 0
    assert decisions.list_rules(owner_employee_id="emp_owner", state="proposed") == []


# --- §14: approve → next match PRE-FILLS; live gate on → auto-delegates --------


def test_approved_delegate_rule_prefills_next_match(
    store: SqliteStore, decisions: DecisionStore, employees: dict[str, str]
) -> None:
    _seed_resolved(decisions, n=3, resolution="delegate", assignee="emp_bob")
    edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    proposal = next(
        d
        for d in decisions.list_decisions(owner_employee_id="emp_owner", status="open")
        if d["source"] == "rule_proposal"
    )
    resolved = edc_rules.approve_rule_proposal(decisions, proposal, actor="emp_owner")
    assert resolved["status"] == "done_verified"

    active = decisions.list_rules(owner_employee_id="emp_owner", state="active", kind="delegate")
    assert len(active) == 1 and active[0]["approved_by"] == "emp_owner"

    verdict = _classify(_event(f"ops@{_DOMAIN}"), active)
    params = verdict["recommended"]["params"]
    assert params["auto_delegate"] is True
    assert params["assignee"] == "emp_bob"
    # Pre-fill ONLY — an approved delegate rule never fires unattended.
    assert params["live"] is False


def test_live_gate_auto_delegates_with_system_rule_actor(
    store: SqliteStore, decisions: DecisionStore, employees: dict[str, str]
) -> None:
    # A PROMOTED auto_delegate rule with its own live gate ON. Minted the ONLY
    # sanctioned way — a proposed delegate rule promoted per-rule (create_rule
    # refuses an automation kind outright; promote_rule is the sole mint path).
    proposed = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "delegate",
            "matcher": {"sender_domain": _DOMAIN},
            "action": {"assignee": "emp_bob"},
            "state": "proposed",
            "created_from": "learned",
        }
    )
    rule = decisions.promote_rule(
        proposed["id"],
        owner_employee_id="emp_owner",
        approved_by="emp_owner",
        kind="auto_delegate",
        live=True,
    )
    assert rule is not None
    verdict = _classify(
        _event(f"ops@{_DOMAIN}"),
        decisions.list_rules(owner_employee_id="emp_owner", state="active"),
    )
    assert verdict["recommended"]["params"]["live"] is True
    decision, _ = decisions.create_decision(
        make_decision(
            source_ref="auto-1",
            classification=verdict["classification"],
            recommended=verdict["recommended"],
        )
    )

    cfg = SimpleNamespace(edc=SimpleNamespace(auto_delegate_live=True))
    stats = {"auto_delegated": 0}
    edc_main._maybe_auto_delegate(decision, decisions, cfg, _Notifier(), _reverse(), stats)
    assert stats["auto_delegated"] == 1

    done = decisions.get_decision(decision["id"], owner_employee_id="emp_owner")
    assert done is not None
    assert done["assignee_employee_id"] == "emp_bob"
    actors = [
        e["actor"] for e in decisions.list_events(decision["id"], owner_employee_id="emp_owner")
    ]
    assert any(a.startswith(f"system:rule:{rule['id']}") for a in actors)


def test_master_gate_off_leaves_a_prefill_untouched(
    store: SqliteStore, decisions: DecisionStore, employees: dict[str, str]
) -> None:
    proposed = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "delegate",
            "matcher": {"sender_domain": _DOMAIN},
            "action": {"assignee": "emp_bob"},
            "state": "proposed",
            "created_from": "learned",
        }
    )
    decisions.promote_rule(
        proposed["id"],
        owner_employee_id="emp_owner",
        approved_by="emp_owner",
        kind="auto_delegate",
        live=True,
    )
    verdict = _classify(
        _event(f"ops@{_DOMAIN}"),
        decisions.list_rules(owner_employee_id="emp_owner", state="active"),
    )
    decision, _ = decisions.create_decision(
        make_decision(source_ref="auto-2", recommended=verdict["recommended"])
    )
    cfg = SimpleNamespace(edc=SimpleNamespace(auto_delegate_live=False))  # master OFF
    stats = {"auto_delegated": 0}
    edc_main._maybe_auto_delegate(decision, decisions, cfg, _Notifier(), _reverse(), stats)
    assert stats["auto_delegated"] == 0
    still = decisions.get_decision(decision["id"], owner_employee_id="emp_owner")
    assert still is not None and still["board_task_id"] is None


# --- A1: a failed auto-delegate re-surfaces for manual handling ---------------


def test_auto_delegate_failure_resurfaces_for_manual_handling(
    store: SqliteStore,
    decisions: DecisionStore,
    employees: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A1 crash-safety: ``resolve`` consumes authority (open→in_progress) BEFORE
    the external delegate I/O. If that I/O raises, the decision must NOT vanish
    into a stranded ``in_progress`` — it degrades to the owner's manual pre-fill
    (back to ``open``, delegate/approve still available)."""
    proposed = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "delegate",
            "matcher": {"sender_domain": _DOMAIN},
            "action": {"assignee": "emp_bob"},
            "state": "proposed",
            "created_from": "learned",
        }
    )
    rule = decisions.promote_rule(
        proposed["id"],
        owner_employee_id="emp_owner",
        approved_by="emp_owner",
        kind="auto_delegate",
        live=True,
    )
    assert rule is not None
    verdict = _classify(
        _event(f"ops@{_DOMAIN}"),
        decisions.list_rules(owner_employee_id="emp_owner", state="active"),
    )
    decision, _ = decisions.create_decision(
        make_decision(
            source_ref="auto-fail",
            classification=verdict["classification"],
            recommended=verdict["recommended"],
        )
    )

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise ConnectionError("slack/board timeout mid-delegate")

    monkeypatch.setattr("omniagentos.edc.actions.delegate", _boom)
    cfg = SimpleNamespace(edc=SimpleNamespace(auto_delegate_live=True))
    stats = {"auto_delegated": 0}
    edc_main._maybe_auto_delegate(decision, decisions, cfg, _Notifier(), _reverse(), stats)

    assert stats["auto_delegated"] == 0
    assert stats.get("auto_delegate_reverted") == 1
    back = decisions.get_decision(decision["id"], owner_employee_id="emp_owner")
    assert back is not None
    assert back["status"] == "open"  # NOT stranded in_progress, NOT terminal failed
    assert "delegate" in back["available_actions"]
    assert "reply" in back["available_actions"]

    # And the owner can still manually delegate/approve with the real executor.
    monkeypatch.undo()
    claimed = decisions.resolve(
        decision["id"],
        actor="emp_owner",
        resolution="delegate",
        params={"execution": {"assignee": "emp_bob"}},
    )
    result = edc_actions.delegate(
        decisions, claimed, actor="emp_owner", notifier=_Notifier(), reverse_map=_reverse()
    )
    assert result["status"] == "done_unverified"


# --- §14: rule edit/disable reverses it ---------------------------------------


def test_disable_rule_reverses_the_prefill(store: SqliteStore, decisions: DecisionStore) -> None:
    rule = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "delegate",
            "matcher": {"sender_domain": _DOMAIN},
            "action": {"assignee": "emp_bob"},
            "state": "active",
            "created_from": "nl",
        }
    )
    active = decisions.list_rules(owner_employee_id="emp_owner", state="active")
    assert _classify(_event(f"ops@{_DOMAIN}"), active)["recommended"]["params"].get("auto_delegate")

    decisions.update_rule(rule["id"], owner_employee_id="emp_owner", fields={"state": "disabled"})
    active2 = decisions.list_rules(owner_employee_id="emp_owner", state="active")
    verdict = _classify(_event(f"ops@{_DOMAIN}"), active2)
    assert not (verdict["recommended"].get("params") or {}).get("auto_delegate")


# --- §14: NL rules → structured proposals requiring approval -------------------


class _FakeParseClient:
    """Returns a canned parse for each of the three §14 NL phrasings."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def complete_json(self, messages, required_keys, **_kwargs):  # type: ignore[no-untyped-def]
        return {key: self.payload.get(key, "") for key in required_keys}


@pytest.mark.parametrize(
    ("payload", "expect_kind", "expect_match"),
    [
        (
            {"kind": "suppress", "sender_domain": "newsletters.example.com"},
            "suppress",
            ("sender_domain", "newsletters.example.com"),
        ),
        (
            {"kind": "suppress", "sender": "spam@bad.example.com"},
            "suppress",
            ("sender", "spam@bad.example.com"),
        ),
        (
            {"kind": "delegate", "sender_domain": "legal.example.com", "assignee": "emp_alice"},
            "delegate",
            ("sender_domain", "legal.example.com"),
        ),
    ],
)
def test_nl_rule_creation_lands_a_proposal(
    decisions: DecisionStore,
    payload: dict[str, Any],
    expect_kind: str,
    expect_match: tuple[str, str],
) -> None:
    result = edc_rules.propose_nl_rule(
        decisions,
        owner_employee_id="emp_owner",
        text="a plain-english instruction",
        llm_client=_FakeParseClient(payload),
    )
    assert result is not None
    rule, decision = result
    assert rule["state"] == "proposed"  # zero silent activation
    assert rule["kind"] == expect_kind
    assert rule["matcher"][expect_match[0]] == expect_match[1]
    assert decision["source"] == "rule_proposal"
    assert decision["source_ref"] == rule["id"]
    from omniagentos.edc.store import available_actions_for

    assert available_actions_for(decision) == ["approve", "deny", "edit", "note"]
    if expect_kind == "delegate":
        assert rule["action"]["assignee"] == "emp_alice"


def test_nl_automation_kind_is_clamped_to_prefill(decisions: DecisionStore) -> None:
    # A sentence that (somehow) asked for unattended delegation is clamped to the
    # pre-fill kind — it can never mint automation authority.
    result = edc_rules.propose_nl_rule(
        decisions,
        owner_employee_id="emp_owner",
        text="auto delegate everything",
        llm_client=_FakeParseClient(
            {"kind": "auto_delegate", "sender_domain": _DOMAIN, "assignee": "emp_bob"}
        ),
    )
    assert result is not None
    rule, _ = result
    assert rule["kind"] == "delegate"


# --- §14: learn.py provably CANNOT write automation kinds ---------------------


def test_learn_source_never_names_automation_kinds() -> None:
    src = (Path(edc_learn.__file__)).read_text(encoding="utf-8")
    # A grep-provable structural guarantee: the automation kind literals do not
    # appear anywhere in the learner (assembled here so this assertion is not a
    # self-match).
    forbidden = "auto_" + "delegate", "auto_" + "send"
    for literal in forbidden:
        assert literal not in src, f"learn.py must not name {literal!r}"


def test_learn_only_emits_prefill_kinds() -> None:
    # Every kind the learner can produce is a pre-fill kind, none an automation kind.
    for kind in edc_learn._RESOLUTION_TO_KIND.values():
        assert kind in {"suppress", "delegate", "snooze_default"}


def test_proposal_seam_refuses_automation_kinds(decisions: DecisionStore) -> None:
    for kind in ("auto_delegate", "auto_send"):
        with pytest.raises(ValueError, match="automation"):
            edc_rules.file_proposed_rule(
                decisions,
                owner_employee_id="emp_owner",
                kind=kind,
                matcher={"sender_domain": _DOMAIN},
                created_from="test",
            )


def test_promotion_is_the_only_path_to_an_automation_kind(
    decisions: DecisionStore,
) -> None:
    rule = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "delegate",
            "matcher": {"sender_domain": _DOMAIN},
            "action": {"assignee": "emp_bob"},
            "state": "proposed",
            "created_from": "learned",
        }
    )
    promoted = decisions.promote_rule(
        rule["id"],
        owner_employee_id="emp_owner",
        approved_by="emp_owner",
        kind="auto_delegate",
        live=True,
    )
    assert promoted is not None
    assert promoted["kind"] == "auto_delegate"
    assert promoted["state"] == "active"
    assert promoted["approved_by"] == "emp_owner"
    assert promoted["action"]["live"] is True
    # F11: the live gate lives PER RULE — a second rule promoted WITHOUT live
    # stays un-armed even though the first is live.
    other_proposed = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "delegate",
            "matcher": {"sender_domain": "other.example.com"},
            "action": {"assignee": "emp_alice"},
            "state": "proposed",
            "created_from": "learned",
        }
    )
    other = decisions.promote_rule(
        other_proposed["id"],
        owner_employee_id="emp_owner",
        approved_by="emp_owner",
        kind="auto_delegate",
    )
    assert other is not None
    assert (other["action"].get("live") or False) is False


# --- §14: rule-proposal decisions excluded from the learner (no inception) ----


def test_rule_proposal_decisions_never_feed_the_learner(
    store: SqliteStore, decisions: DecisionStore
) -> None:
    # A decided rule_proposal decision with a clusterable domain + resolution:
    # if it fed the learner it would (wrongly) propose a rule about rule proposals.
    for i in range(5):
        decisions.create_decision(
            {
                "owner_employee_id": "emp_owner",
                "source": "rule_proposal",
                "source_ref": f"dcr_{i}",
                "title": "Rule proposal",
                "classification": "needs_owner",
                "counterparty": f"bot{i}@{_DOMAIN}",
                "status": "dismissed",
                "resolution": "dismiss",
                "decided_at": "2026-08-13T09:00:00Z",
            }
        )
    assert (
        decisions.resolved_for_learning(
            owner_employee_id="emp_owner", since_iso="2026-08-01T00:00:00Z"
        )
        == []
    )
    stats = edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    assert stats == {"filed": 0, "refreshed": 0, "suppressed": 0}


def test_declined_proposal_suppresses_reproposal_30_days(
    store: SqliteStore, decisions: DecisionStore
) -> None:
    _seed_resolved(decisions, n=3, resolution="dismiss")
    edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    proposal = next(
        d
        for d in decisions.list_decisions(owner_employee_id="emp_owner", status="open")
        if d["source"] == "rule_proposal"
    )
    edc_rules.decline_rule_proposal(decisions, proposal, actor="emp_owner")
    assert decisions.list_rules(owner_employee_id="emp_owner", state="declined")

    # Within 30 days the same evidence must not re-propose.
    stats = edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    assert stats["suppressed"] == 1 and stats["filed"] == 0


def test_expired_decline_files_one_proposal_then_refreshes_not_twins(
    store: SqliteStore, decisions: DecisionStore
) -> None:
    """STORM: once a decline lapses past 30 days with a PERSISTING pattern, the
    learner files AT MOST ONE fresh proposal and REFRESHES it thereafter — the
    old code re-filed a new twin every tick (found the expired decline, which was
    neither suppressed nor proposed/active, and proposed again)."""
    _seed_resolved(decisions, n=3, resolution="dismiss")
    edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    proposal = next(
        d
        for d in decisions.list_decisions(owner_employee_id="emp_owner", status="open")
        if d["source"] == "rule_proposal"
    )
    edc_rules.decline_rule_proposal(decisions, proposal, actor="emp_owner")

    # The same pattern PERSISTS well past the 30-day suppression window.
    later = NOW + timedelta(days=45)
    for i in range(3):
        decisions.create_decision(
            make_decision(
                source_ref=f"persist-{i}",
                counterparty=f"late{i}@{_DOMAIN}",
                classification="needs_owner",
                status="dismissed",
                resolution="dismiss",
                decided_at=later.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )

    # Three nightly ticks after the window lapses.
    for _ in range(3):
        edc_learn.run_learning(store, ["emp_owner"], now=later)

    proposed_rules = decisions.list_rules(
        owner_employee_id="emp_owner", kind="suppress", state="proposed"
    )
    open_proposals = [
        d
        for d in decisions.list_decisions(owner_employee_id="emp_owner", status="open")
        if d["source"] == "rule_proposal"
    ]
    assert len(proposed_rules) == 1  # exactly ONE fresh proposed rule, no twins
    assert len(open_proposals) == 1  # exactly ONE open proposal Decision


def test_refresh_rerenders_the_linked_proposal_decision(
    store: SqliteStore, decisions: DecisionStore
) -> None:
    """When a standing delegate proposal's assignee drifts, the refresh re-renders
    the linked rule_proposal Decision so the owner approves what they see."""
    _seed_resolved(decisions, n=3, resolution="delegate", assignee="emp_bob")
    edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    proposal = next(
        d
        for d in decisions.list_decisions(owner_employee_id="emp_owner", status="open")
        if d["source"] == "rule_proposal"
    )
    assert "emp_bob" in proposal["recommended"]["human_line"]

    # The assignee for this domain drifts to emp_alice (repoint the same cluster),
    # so the standing proposal is REFRESHED in place — never twinned.
    for row in decisions.list_decisions(owner_employee_id="emp_owner"):
        if row.get("assignee_employee_id") == "emp_bob":
            decisions.update_decision(
                row["id"], owner_employee_id="emp_owner", fields={"assignee_employee_id": "emp_alice"}
            )
    stats = edc_learn.run_learning(store, ["emp_owner"], now=NOW)
    assert stats["refreshed"] == 1 and stats["filed"] == 0

    refreshed = decisions.get_decision(proposal["id"], owner_employee_id="emp_owner")
    assert refreshed is not None
    assert "emp_alice" in refreshed["recommended"]["human_line"]
    assert "emp_alice" in refreshed["title"]


# --- §14: a suppress rule does NOT hide a grounded-harm URGENT (F04) ----------


def test_suppress_rule_cannot_hide_a_grounded_harm_urgent(
    decisions: DecisionStore,
) -> None:
    suppress_rule = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "suppress",
            "matcher": {"sender_domain": "billing.example.com"},
            "action": {},
            "state": "active",
            "created_from": "nl",
        }
    )
    event = _event(
        "noreply@billing.example.com",
        subject="Your payment failed",
        body=(
            "We could not process your payment. Your account will be suspended "
            "tonight unless you update your payment method. Unsubscribe here."
        ),
    )
    verdict = classify(event, owner_rules=[suppress_rule], now=NOW)
    # Harm is detected BEFORE suppression is ever consulted — still URGENT.
    assert verdict["classification"] == "urgent"
    assert verdict["status"] == "open"


def test_harm_item_keeps_harm_recommended_over_delegate_prefill(
    decisions: DecisionStore,
) -> None:
    """MAJOR harm-overwrite: a NEEDS_OWNER material-harm item with a matching
    delegate rule keeps its concrete harm recommended action — the delegate
    pre-fill is attached only SUPPLEMENTALLY, never clobbering the harm signal."""
    rule = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "delegate",
            "matcher": {"sender_domain": "billing.example.com"},
            "action": {"assignee": "emp_bob"},
            "state": "active",
            "created_from": "nl",
        }
    )
    event = _event(
        "billing@billing.example.com",
        subject="Payment method expired",
        body=(
            "Your payment method on file has expired. Please update your billing "
            "details to keep your subscription."
        ),
    )
    verdict = classify(event, owner_rules=[rule], now=NOW)
    assert verdict["classification"] == "needs_owner"
    assert verdict["consequence"] != "none"  # material harm detected first (F04)
    # The harm action survives — NOT overwritten by the delegate pre-fill prompt.
    assert verdict["recommended"]["kind"] != "delegate"
    assert verdict["recommended"]["kind"] == "update_payment"
    # The delegate pre-fill is still present, but as supplemental params.
    params = verdict["recommended"].get("params") or {}
    assert params.get("auto_delegate") is True
    assert params.get("assignee") == "emp_bob"


def test_create_rule_refuses_an_automation_kind(decisions: DecisionStore) -> None:
    """Defense-in-depth (F11): the generic inserter never mints an automation
    kind — those come ONLY from the audited promote_rule transition."""
    for kind in ("auto_delegate", "auto_send"):
        with pytest.raises(ValueError, match="automation"):
            decisions.create_rule(
                {
                    "owner_employee_id": "emp_owner",
                    "kind": kind,
                    "matcher": {"sender_domain": _DOMAIN},
                    "action": {"assignee": "emp_bob"},
                    "state": "active",
                    "created_from": "bogus",
                }
            )


def test_suppress_rule_downgrades_a_no_harm_item(decisions: DecisionStore) -> None:
    suppress_rule = decisions.create_rule(
        {
            "owner_employee_id": "emp_owner",
            "kind": "suppress",
            "matcher": {"sender_domain": "newsletters.example.com"},
            "action": {},
            "state": "active",
            "created_from": "nl",
        }
    )
    verdict = classify(
        _event("hello@newsletters.example.com", subject="weekly digest", body="news"),
        owner_rules=[suppress_rule],
        now=NOW,
    )
    assert verdict["classification"] == "ignore"
    assert f"rule:{suppress_rule['id']}" in verdict["rule_matches"]


# --- bootstrap seeder ---------------------------------------------------------


def test_bootstrap_seeder_is_idempotent(decisions: DecisionStore) -> None:
    cfg = SimpleNamespace(
        alerts=SimpleNamespace(
            vip_senders=["vip@partner.example.com", "@keyaccount.example.com"],
            urgent_patterns=["urgent", "asap"],
        )
    )
    first = edc_rules.seed_bootstrap_rules(decisions, cfg)
    assert first == 4
    hints = decisions.list_rules(owner_employee_id="emp_owner", kind="classify_hint")
    assert len(hints) == 4
    assert all(r["state"] == "active" and r["created_from"] == "bootstrap" for r in hints)
    # Re-running seeds nothing new.
    assert edc_rules.seed_bootstrap_rules(decisions, cfg) == 0
    assert len(decisions.list_rules(owner_employee_id="emp_owner", kind="classify_hint")) == 4
