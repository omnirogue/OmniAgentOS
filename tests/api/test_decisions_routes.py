"""API scoping tests for /api/decisions (synthesis §10.2, P1).

Proves the owner is derived from the principal (never a param), a foreign id is
404, the ``system`` principal is 403, and the P1 decide actions work. Uses the
real app with ``get_decision_store`` overridden onto a temp store.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.api.routes import decisions as decisions_routes
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore
from omniagentos.edc.store import DecisionStore

_OWNER = {"X-Omni-Authenticated-Principal": "emp_owner"}
_BOB = {"X-Omni-Authenticated-Principal": "emp_bob"}


@pytest.fixture
def store(tmp_path: Path) -> DecisionStore:
    base = SqliteStore(str(tmp_path / "decisions.db"))
    goals = CompanyGoalsStore(base)
    goals.ensure_employee(employee_id="emp_owner", name="the operator", role="operator")
    goals.ensure_employee(employee_id="emp_bob", name="Bob", role="author")
    return DecisionStore(base)


@pytest.fixture
def client(store: DecisionStore) -> Iterator[TestClient]:
    app.dependency_overrides[decisions_routes.get_decision_store] = lambda: store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(decisions_routes.get_decision_store, None)


def _make(store: DecisionStore, *, owner: str, ref: str, **over: object) -> dict:
    payload: dict[str, object] = {
        "owner_employee_id": owner,
        "source": "email",
        "source_ref": ref,
        "title": "AWS payment method expired",
        "classification": "needs_owner",
        "recommended": {"kind": "update_payment", "human_line": "update the card"},
    }
    payload.update(over)
    row, _created = store.create_decision(payload)
    return row


def test_list_is_owner_scoped(client: TestClient, store: DecisionStore) -> None:
    _make(store, owner="emp_owner", ref="s1")
    _make(store, owner="emp_bob", ref="g1")
    resp = client.get("/api/decisions", headers=_OWNER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["owner_employee_id"] == "emp_owner"
    assert [d["source_ref"] for d in body["decisions"]] == ["s1"]


def test_system_principal_is_forbidden(client: TestClient, store: DecisionStore) -> None:
    _make(store, owner="emp_owner", ref="s1")
    # No principal header => resolves to the machine 'system' principal.
    resp = client.get("/api/decisions")
    assert resp.status_code == 403


def test_foreign_id_reads_as_404(client: TestClient, store: DecisionStore) -> None:
    other = _make(store, owner="emp_bob", ref="g1")
    resp = client.get(f"/api/decisions/{other['id']}", headers=_OWNER)
    assert resp.status_code == 404


def test_decide_dismiss(client: TestClient, store: DecisionStore) -> None:
    row = _make(store, owner="emp_owner", ref="s1")
    resp = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "dismiss", "note": "not relevant"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"
    assert resp.json()["decided_by"] == "emp_owner"


def test_decide_snooze_requires_until(client: TestClient, store: DecisionStore) -> None:
    row = _make(store, owner="emp_owner", ref="s1")
    bad = client.post(f"/api/decisions/{row['id']}/decide", headers=_OWNER, json={"action": "snooze"})
    assert bad.status_code == 400
    ok = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "snooze", "params": {"until": "2026-08-20T09:00:00Z"}},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "snoozed"
    assert ok.json()["snooze_until"] == "2026-08-20T09:00:00Z"


def test_decide_edit_reclassify_promotes_maybe(client: TestClient, store: DecisionStore) -> None:
    row = _make(store, owner="emp_owner", ref="s1", classification="maybe", recommended={})
    resp = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "edit", "params": {"reclassify": "needs_owner"}},
    )
    assert resp.status_code == 200
    assert resp.json()["classification"] == "needs_owner"


def test_decide_note_appends(client: TestClient, store: DecisionStore) -> None:
    row = _make(store, owner="emp_owner", ref="s1")
    resp = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "note", "note": "call the vendor"},
    )
    assert resp.status_code == 200
    assert "call the vendor" in resp.json()["notes"]


def test_cannot_decide_a_foreign_decision(client: TestClient, store: DecisionStore) -> None:
    other = _make(store, owner="emp_bob", ref="g1")
    resp = client.post(
        f"/api/decisions/{other['id']}/decide", headers=_OWNER, json={"action": "dismiss"}
    )
    assert resp.status_code == 404


def test_unsupported_action_is_rejected(client: TestClient, store: DecisionStore) -> None:
    row = _make(store, owner="emp_owner", ref="s1")
    resp = client.post(
        f"/api/decisions/{row['id']}/decide", headers=_OWNER, json={"action": "execute"}
    )
    assert resp.status_code == 400


def _seed_company_goal(store: DecisionStore, slug: str = "globex") -> None:
    from omniagentos.contracts import utc_now_iso

    base = store._store
    base._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (f"co_{slug}", slug, slug.title(), "active", utc_now_iso()),
    )
    CompanyGoalsStore(base).create_goal(
        goal_id=f"cgl_{slug}",
        org_company_id=f"co_{slug}",
        title="General engineering — keep the lights on",
        horizon="quarter",
    )


def test_decide_delegate_creates_card(
    client: TestClient, store: DecisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # P3 review F2: delegate is advertised on an open decision and must be
    # serviceable — previously it was refused 400.
    CompanyGoalsStore(store._store).ensure_employee(
        employee_id="emp_alice", name="Alice", role="reviewer"
    )
    monkeypatch.setattr("omniagentos.edc.main._build_notifier", lambda _dry: (None, {}))
    row = _make(store, owner="emp_owner", ref="p3-del")
    resp = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "delegate", "params": {"assignee": "emp_alice"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done_unverified"
    assert body["assignee_employee_id"] == "emp_alice"
    assert body["board_task_ref"] == f"EDC-{row['number']}"


def test_decide_delegate_requires_valid_assignee(
    client: TestClient, store: DecisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omniagentos.edc.main._build_notifier", lambda _dry: (None, {}))
    row = _make(store, owner="emp_owner", ref="p3-del-bad")
    # Missing assignee.
    miss = client.post(
        f"/api/decisions/{row['id']}/decide", headers=_OWNER, json={"action": "delegate"}
    )
    assert miss.status_code == 400
    # Self-delegation is refused BEFORE authority is consumed (row stays open).
    selfd = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "delegate", "params": {"assignee": "emp_owner"}},
    )
    assert selfd.status_code == 400
    # A non-roster assignee is refused.
    ghost = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "delegate", "params": {"assignee": "emp_ghost"}},
    )
    assert ghost.status_code == 400
    still = store.get_decision(row["id"], owner_employee_id="emp_owner")
    assert still is not None and still["status"] == "open"


def test_decide_defer_queue_creates_pool_card(client: TestClient, store: DecisionStore) -> None:
    _seed_company_goal(store, "globex")
    row = _make(store, owner="emp_owner", ref="p3-defq", company_slug="globex")
    resp = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "defer", "params": {"mode": "queue"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done_unverified"
    assert body["board_task_ref"] == f"EDC-{row['number']}"


def test_decide_defer_is_unavailable_for_non_operator(
    client: TestClient, store: DecisionStore
) -> None:
    # A non-the operator owner without repo-shaped work is not offered defer at all — the
    # read model never advertises the queue-add the matrix forbids → 400.
    row = _make(store, owner="emp_bob", ref="p3-defq-notowner")
    resp = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_BOB,
        json={"action": "defer", "params": {"mode": "queue"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "action_unavailable"


def test_p2_reply_edit_approve_send_and_second_surface_gets_409(
    client: TestClient,
    store: DecisionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _make(
        store,
        owner="emp_owner",
        ref="p2-reply",
        source_account="gmail_ownera",
        counterparty="Customer <customer@example.com>",
        context="Please confirm the renewal.",
    )

    class DraftClient:
        def complete_json(self, *_args: Any, **_kwargs: Any) -> dict[str, str]:
            return {"subject": "Re: renewal", "body": "Confirmed."}

    monkeypatch.setattr("omniagentos.llm.client.ShortCallClient", DraftClient)

    replied = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "reply", "params": {"intent": "confirm renewal"}},
    )
    assert replied.status_code == 200
    assert replied.json()["status"] == "draft_pending"
    old_sha = replied.json()["draft"]["sha256"]

    edited = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={
            "action": "edit",
            "params": {"subject": "Re: renewal approved", "body": "Approved. Thank you."},
        },
    )
    assert edited.status_code == 200
    new_sha = edited.json()["draft"]["sha256"]
    assert new_sha != old_sha
    assert edited.json()["draft"]["approved_sha256"] is None

    def fake_run_executor(
        decision_store: DecisionStore, claimed: dict[str, Any], *, actor: str
    ) -> dict[str, Any]:
        return decision_store.transition_effect(
            claimed["id"],
            owner_employee_id=actor,
            from_status="in_progress",
            to_status="done_unverified",
            event="send",
            execution={"provider_message_id": "gmail-1"},
        )

    monkeypatch.setattr(decisions_routes, "run_executor", fake_run_executor)
    approved = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "approve", "params": {"draft_sha256": new_sha}},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "done_unverified"
    assert approved.json()["decided_by"] == "emp_owner"

    second = client.post(
        f"/api/decisions/{row['id']}/decide",
        headers=_OWNER,
        json={"action": "approve", "params": {"draft_sha256": new_sha}},
    )
    assert second.status_code == 409


# --- P4: wired NL rule create + per-rule promote routes -----------------------


def _patch_parse(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    """Force the NL parser's LLM to return one canned structured payload."""

    class _ParseClient:
        def complete_json(
            self, messages: Any, required_keys: list[str], **_kwargs: Any
        ) -> dict[str, Any]:
            return {key: payload.get(key, "") for key in required_keys}

    monkeypatch.setattr("omniagentos.llm.client.ShortCallClient", _ParseClient)


def _proposed_rule(store: DecisionStore, *, owner: str, domain: str) -> dict:
    return store.create_rule(
        {
            "owner_employee_id": owner,
            "kind": "delegate",
            "matcher": {"sender_domain": domain},
            "action": {"assignee": "emp_bob"},
            "state": "proposed",
            "created_from": "learned",
        }
    )


def test_nl_create_lands_a_proposal_not_active(
    client: TestClient, store: DecisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_parse(monkeypatch, {"kind": "suppress", "sender_domain": "news.example.com"})
    resp = client.post(
        "/api/decisions/rules",
        headers=_OWNER,
        json={"nl_text": "stop showing me newsletters from news.example.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rule"]["kind"] == "suppress"
    assert body["rule"]["state"] == "proposed"  # NEVER active from NL
    assert body["decision"]["source"] == "rule_proposal"
    assert body["decision"]["status"] == "open"


def test_nl_create_clamps_automation_kind_to_prefill(
    client: TestClient, store: DecisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An instruction that tries to mint unattended authority is clamped to the
    # pre-fill delegate kind and still lands PROPOSED (never active).
    _patch_parse(
        monkeypatch,
        {"kind": "auto_delegate", "sender_domain": "legal.example.com", "assignee": "emp_bob"},
    )
    resp = client.post(
        "/api/decisions/rules",
        headers=_OWNER,
        json={"nl_text": "automatically assign legal.example.com to Bob"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rule"]["kind"] == "delegate"  # clamped, not auto_delegate
    assert body["rule"]["state"] == "proposed"


def test_nl_create_is_owner_scoped(
    client: TestClient, store: DecisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_parse(monkeypatch, {"kind": "suppress", "sender_domain": "news.example.com"})
    resp = client.post("/api/decisions/rules", json={"nl_text": "hush news.example.com"})
    assert resp.status_code == 403  # no principal → system principal → forbidden
    assert store.list_rules(owner_employee_id="emp_owner") == []


def test_nl_create_unparseable_is_422(
    client: TestClient, store: DecisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_parse(monkeypatch, {"kind": "", "sender_domain": ""})
    resp = client.post("/api/decisions/rules", headers=_OWNER, json={"nl_text": "hello there"})
    assert resp.status_code == 422


def test_promote_makes_a_proposed_rule_an_active_automation_kind(
    client: TestClient, store: DecisionStore
) -> None:
    rule = _proposed_rule(store, owner="emp_owner", domain="infra.example.com")
    resp = client.post(
        f"/api/decisions/rules/{rule['id']}/promote",
        headers=_OWNER,
        json={"kind": "auto_delegate", "live": True},
    )
    assert resp.status_code == 200, resp.text
    promoted = resp.json()["rule"]
    assert promoted["kind"] == "auto_delegate"
    assert promoted["state"] == "active"
    assert promoted["approved_by"] == "emp_owner"
    assert promoted["action"]["live"] is True


def test_promote_rejects_a_non_automation_kind(client: TestClient, store: DecisionStore) -> None:
    rule = _proposed_rule(store, owner="emp_owner", domain="infra.example.com")
    resp = client.post(
        f"/api/decisions/rules/{rule['id']}/promote",
        headers=_OWNER,
        json={"kind": "delegate"},
    )
    assert resp.status_code == 400


def test_promote_cross_owner_rule_is_404(client: TestClient, store: DecisionStore) -> None:
    rule = _proposed_rule(store, owner="emp_bob", domain="infra.example.com")
    resp = client.post(
        f"/api/decisions/rules/{rule['id']}/promote",
        headers=_OWNER,  # emp_owner cannot promote emp_bob's rule
        json={"kind": "auto_delegate"},
    )
    assert resp.status_code == 404
    still = store.get_rule(rule["id"], owner_employee_id="emp_bob")
    assert still is not None and still["state"] == "proposed"  # untouched


# --- P4 minors: rule_proposal note uses body.note; invalid action rejected ----


def test_rule_proposal_note_uses_body_note(
    client: TestClient, store: DecisionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_parse(monkeypatch, {"kind": "suppress", "sender_domain": "news.example.com"})
    created = client.post(
        "/api/decisions/rules", headers=_OWNER, json={"nl_text": "hush news.example.com"}
    ).json()
    decision_id = created["decision"]["id"]
    resp = client.post(
        f"/api/decisions/{decision_id}/decide",
        headers=_OWNER,
        json={"action": "note", "note": "think about this one"},
    )
    assert resp.status_code == 200, resp.text
    assert "think about this one" in resp.json()["notes"]


def test_rule_proposal_rejects_unsupported_action_explicitly(store: DecisionStore) -> None:
    from omniagentos.api.routes import decisions as dr
    from omniagentos.api.services import ApiError
    from omniagentos.edc.rules import file_proposed_rule

    _rule, decision = file_proposed_rule(
        store,
        owner_employee_id="emp_owner",
        kind="delegate",
        matcher={"sender_domain": "x.example.com"},
        action={"assignee": "emp_bob"},
        created_from="test",
    )
    with pytest.raises(ApiError) as exc:
        dr._decide_rule_proposal(store, decision, "emp_owner", "snooze", params={})
    assert exc.value.status_code == 400
    assert exc.value.code == "unsupported_action"
    assert "edit requires" not in exc.value.message  # not the misleading edit error


# --- session source: suggestions-only, enforced at the HTTP layer -------------


def test_session_decision_refuses_a_non_advertised_action(
    client: TestClient, store: DecisionStore
) -> None:
    """A session decision may be snoozed/dismissed/noted — never executed.

    The store-level matrix already restricts ``source='session'``; this pins the
    guarantee where a caller actually reaches it. ``reply`` is the dangerous one:
    it is a perfectly ordinary action on an email decision, and on a session
    decision it would try to draft a message to a ``session:<id>`` counterparty.
    """
    decision = _make(
        store,
        owner="emp_owner",
        ref="ses_x|needs_input|2026-08-15T12:00:00Z",
        source="session",
        title="Session Bob is waiting: needs approval to push",
        counterparty="session:ses_x",
        recommended={"kind": "review", "human_line": "Open the Sessions panel and approve/deny"},
        available_actions=["snooze", "dismiss", "note"],
    )
    for forbidden in ("reply", "delegate", "defer", "approve"):
        resp = client.post(
            f"/api/decisions/{decision['id']}/decide",
            headers=_OWNER,
            json={"action": forbidden},
        )
        assert resp.status_code == 400, f"{forbidden} was not refused: {resp.text}"
        body = resp.json()["error"] if "error" in resp.json() else resp.json()
        assert body.get("code") in {"unsupported_action", "action_unavailable"}, resp.text

    allowed = client.post(
        f"/api/decisions/{decision['id']}/decide",
        headers=_OWNER,
        json={"action": "dismiss"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "dismissed"
