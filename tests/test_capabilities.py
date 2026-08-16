"""Capability system: registry, broker gates, grant store, runner enforcement.

These tests are adversarial on purpose. The system's job is to stop an agent from
refunding a customer or dropping a DNS record, so the tests try to make it do
exactly that -- by granting the capability outright, by editing policy, by asking
for a secret through a read grant, and by widening a grant from inside a task.
"""

from __future__ import annotations

import pytest

from omniagentos.connectors import AUTO_CLASSES, ConnectorError, load_registry
from omniagentos.connectors.broker import (
    HARD_HUMAN_CLASSES,
    BrokerDenied,
    authorize,
    describe_grant,
    effective_grant,
    validate_request,
)
from omniagentos.connectors.store import CapabilityStore
from omniagentos.contracts import ActionClass
from omniagentos.policy import PolicyError, load_policy, validate_tools

# Capabilities that must NEVER be auto-executable. If a change makes any of these
# runnable without a human, the business is one agent loop away from a bad day.
MONEY_AND_DESTRUCTION = [
    "stripe_acmeuni.refund",
    "stripe_acmeuni.charge",
    "stripe_initech.refund",
    "paypal.refund",
    "paypal.payout",
    "nmi_fortpoint.refund",
    "nmi_glacier.refund",
    "chargeblast.refund",
    "vandelay.refund",
    "meta_acmeuni.launch",
    "meta_acmeuni.budget_change",
    "meta_initech.launch",
    "meta_globex.launch",
    "meta_globex.budget_change",
    "meta_agentpro.launch",
    "meta_agentpro.budget_change",
    "postgres.write",
    "cloudflare.dns_write",
    "aws.s3_delete",
    "clerk.user_delete",
    "paypal_claude.refund",
    "paypal_claude.payout",
    "runpod.pod_create",
    "runpod.pod_delete",
    "crm_internal.db_write",
    "zapier.trigger",
]


# --- registry ---------------------------------------------------------------


def test_registry_loads_and_is_namespaced() -> None:
    registry = load_registry()
    assert registry.connectors, "registry must not be empty"
    for cap_id, cap in registry.capabilities.items():
        assert cap_id.startswith(f"{cap.connector}."), f"{cap_id} is not namespaced"
        assert cap.group in registry.groups


def test_registry_contains_no_secret_values() -> None:
    """The registry names credentials; it must never contain one."""
    from pathlib import Path

    raw = (Path(__file__).resolve().parents[1] / "configs" / "connectors.yaml").read_text()
    for needle in ("sk_live", "sk_test", "sk-ant-", "Bearer ", "postgres://", "rk_live"):
        assert needle not in raw, f"connectors.yaml appears to contain a secret ({needle})"


@pytest.mark.parametrize("cap_id", MONEY_AND_DESTRUCTION)
def test_dangerous_capabilities_are_classified_consequential(cap_id: str) -> None:
    cap = load_registry().capability(cap_id)
    assert cap.action_class is ActionClass.CONSEQUENTIAL, (
        f"{cap_id} is classified {cap.action_class.value}; it moves money or destroys "
        "state and must be consequential"
    )
    assert not cap.is_auto


def test_auto_and_hard_human_classes_are_disjoint() -> None:
    assert not (AUTO_CLASSES & HARD_HUMAN_CLASSES)


# --- broker gate 1: consequential is refused even when granted ---------------


@pytest.mark.parametrize("cap_id", MONEY_AND_DESTRUCTION)
def test_consequential_denied_even_when_explicitly_granted(cap_id: str) -> None:
    """Holding the grant buys the right to ASK, never the right to act."""
    with pytest.raises(BrokerDenied) as exc:
        authorize(cap_id, granted=[cap_id])
    assert exc.value.reason == "requires_human_approval"


def test_consequential_denied_even_if_policy_yaml_is_subverted(monkeypatch) -> None:
    """Gate 2 is in code. Editing policy.yaml must not be enough to unlock a refund.

    Simulates an operator (or an agent with file_write) flipping
    consequential.requires_approval to false, then confirms the broker still says no.
    """
    cfg = load_policy()
    subverted = cfg.action_classes[ActionClass.CONSEQUENTIAL].model_copy(
        update={"requires_approval": False, "always_human": False}
    )
    monkeypatch.setitem(cfg.action_classes, ActionClass.CONSEQUENTIAL, subverted)

    with pytest.raises(BrokerDenied) as exc:
        authorize("stripe_acmeuni.refund", granted=["stripe_acmeuni.refund"])
    assert exc.value.reason == "requires_human_approval"


def test_ungranted_capability_is_denied() -> None:
    with pytest.raises(BrokerDenied) as exc:
        authorize("stripe_acmeuni.read", granted=["piedpiper_acmeuni.read"])
    assert exc.value.reason == "not_granted"


def test_unknown_capability_is_denied() -> None:
    with pytest.raises(BrokerDenied) as exc:
        authorize("stripe_acmeuni.drain_account", granted=["stripe_acmeuni.drain_account"])
    assert exc.value.reason == "unknown_capability"


def test_fanbasis_read_has_a_reviewed_get_only_probe_path() -> None:
    """FanBasis may only be probed via its reviewed GET collection paths."""
    cap = authorize("fanbasis.read", granted=["fanbasis.read"])
    assert cap.http is not None
    assert cap.http.methods == ["GET"]


def test_reviewed_read_capability_authorizes() -> None:
    """The counterpart: a capability WITH a reviewed path passes authorize()."""
    cap = authorize("piedpiper_acmeuni.read", granted=["piedpiper_acmeuni.read"])
    assert cap.callable_now
    assert cap.action_class is ActionClass.READ_ONLY


def test_support_and_marketing_read_connectors_load_as_callable() -> None:
    registry = load_registry()
    connector_ids = {
        "gmail",
        "freshdesk_acmeuni",
        "freshdesk_initech",
        "customerio_acmeuni",
        "customerio_initech",
        "clickup",
        "zendesk",
    }
    assert connector_ids <= registry.connectors.keys()

    cap_ids = [
        "gmail.search",
        "gmail.get_message",
        "gmail.get_thread",
        "freshdesk_acmeuni.read",
        "freshdesk_initech.read",
        "customerio_acmeuni.read",
        "customerio_initech.read",
        "clickup.read",
        "zendesk.read",
        "clerk.acmeuni_read",
        "clerk.initech_read",
        "bunny.read",
    ]
    for cap_id in cap_ids:
        cap = registry.capability(cap_id)
        assert cap.callable_now, f"{cap_id} must have a reviewed HTTP spec"
        assert cap.action_class is ActionClass.READ_ONLY


# --- broker gate: the read/write boundary is the allowlist, not the credential --


def test_read_capability_cannot_issue_a_write_request() -> None:
    """A read grant is enforced by method, because the credential behind it can write."""
    from omniagentos.connectors import Capability, HttpSpec

    read_cap = Capability(
        id="stripe_acmeuni.read",
        connector="stripe_acmeuni",
        group="payments",
        label="read",
        action_class=ActionClass.READ_ONLY,
        http=HttpSpec(
            base_url="https://api.stripe.com",
            methods=["GET"],
            path_prefixes=["/v1/charges", "/v1/customers"],
        ),
    )
    validate_request(read_cap, "GET", "/v1/charges")  # allowed

    with pytest.raises(BrokerDenied) as exc:
        validate_request(read_cap, "POST", "/v1/refunds")
    assert exc.value.reason == "method_not_allowed"

    with pytest.raises(BrokerDenied) as exc:
        validate_request(read_cap, "GET", "/v1/account")
    assert exc.value.reason == "path_not_allowed"


def test_auto_write_cannot_escape_its_subresource() -> None:
    """piedpiper_acmeuni.note_write runs UNATTENDED, so its reach must be pinned exactly.

    A path *prefix* of '/contacts/' would admit POST /contacts/{id} -- editing
    customer-visible fields -- which is a different capability at a higher action
    class. These are the escapes a prefix alone would let through.
    """
    note = load_registry().capability("piedpiper_acmeuni.note_write")

    validate_request(note, "POST", "/contacts/abc123/notes")  # the intended write

    for method, path in [
        ("POST", "/contacts/abc123"),  # edit the contact itself
        ("POST", "/contacts/abc123/tasks"),  # sibling subresource
        ("POST", "/contacts/abc123/notes/../../conversations"),  # traversal
        ("POST", "/conversations/abc123/messages"),  # different collection
    ]:
        with pytest.raises(BrokerDenied, match="path_not_allowed"):
            validate_request(note, method, path)

    with pytest.raises(BrokerDenied, match="method_not_allowed"):
        validate_request(note, "DELETE", "/contacts/abc123/notes")


def test_stripe_read_grant_cannot_reach_a_refund() -> None:
    """The credential behind stripe_acmeuni.read can refund. The allowlist is the boundary."""
    read = load_registry().capability("stripe_acmeuni.read")

    validate_request(read, "GET", "/v1/charges")

    with pytest.raises(BrokerDenied, match="method_not_allowed"):
        validate_request(read, "POST", "/v1/refunds")
    with pytest.raises(BrokerDenied, match="path_not_allowed"):
        validate_request(read, "GET", "/v1/refunds")


@pytest.mark.parametrize(
    ("method", "path", "reason"),
    [
        ("POST", "/gmail/v1/users/me/messages/msg_123/modify", "method_not_allowed"),
        ("GET", "/gmail/v1/users/me/messages/send", "path_not_allowed"),
        ("POST", "/upload/gmail/v1/users/me/messages/send", "method_not_allowed"),
    ],
)
def test_gmail_search_denies_write_and_non_search_routes(
    method: str, path: str, reason: str
) -> None:
    search = authorize("gmail.search", granted=["gmail.search"])

    with pytest.raises(BrokerDenied) as exc:
        validate_request(search, method, path)
    assert exc.value.reason == reason


@pytest.mark.parametrize(
    ("cap_id", "path"),
    [
        # A crafted traversal path satisfies the allowed prefix via startswith but
        # httpx would normalize it on the wire; the broker must refuse it first.
        ("stripe_acmeuni.read", "/v1/charges/../../admin"),
        ("freshdesk_acmeuni.read", "/api/v2/tickets/../agents/../../admin"),
        ("clickup.read", "/api/v2/task/../../team/../secrets"),
        ("google_drive_files.find", "/drive/v3/files/../../../etc/passwd"),
    ],
)
def test_dot_segment_traversal_is_refused_at_the_boundary(cap_id: str, path: str) -> None:
    cap = authorize(cap_id, granted=[cap_id])
    with pytest.raises(BrokerDenied) as exc:
        validate_request(cap, "GET", path)
    assert exc.value.reason == "path_not_allowed"


@pytest.mark.parametrize(
    ("cap_id", "read_path"),
    [
        ("zendesk.read", "/api/v2/tickets"),
        ("freshdesk_acmeuni.read", "/api/v2/tickets"),
        ("freshdesk_initech.read", "/api/v2/tickets"),
        ("clickup.read", "/api/v2/task/task_123"),
        ("customerio_acmeuni.read", "/v1/campaigns"),
        ("customerio_initech.read", "/v1/campaigns"),
    ],
)
def test_support_and_marketing_caps_allow_get_and_deny_post(cap_id: str, read_path: str) -> None:
    cap = authorize(cap_id, granted=[cap_id])
    validate_request(cap, "GET", read_path)

    with pytest.raises(BrokerDenied) as exc:
        validate_request(cap, "POST", read_path)
    assert exc.value.reason == "method_not_allowed"


def test_google_workspace_connectors_use_the_pinned_oauth2_scheme() -> None:
    """G1/G2 contract: broker mints a Bearer token from a refresh token; the

    registry only ever names env vars. All connectors share the same
    three-env oauth2 spec. The broker has no credential-injection API, so the
    refresh token / client secret never reach an agent subprocess.
    """
    registry = load_registry()
    expected_auth = (
        "oauth2:GOOGLE_OAUTH_REFRESH_TOKEN:GOOGLE_OAUTH_CLIENT_ID:GOOGLE_OAUTH_CLIENT_SECRET"
    )
    google_cap_ids = [
        "google_sheets.read",
        "google_sheets.write",
        "google_sheets.append",
        "google_docs.read",
        "google_docs.write",
        "google_drive_files.find",
        "google_drive_files.get",
        "gmail.search",
        "gmail.get_message",
        "gmail.get_thread",
    ]
    for cap_id in google_cap_ids:
        cap = registry.capability(cap_id)
        assert cap.group == "google"
        assert cap.callable_now
        assert cap.http is not None
        assert cap.http.auth == expected_auth

    connector_env = {
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
    }
    for connector_id in ("google_sheets", "google_docs", "google_drive_files", "gmail"):
        assert set(registry.connectors[connector_id].env) == connector_env


def test_every_auto_write_with_a_call_path_is_pinned_by_regex() -> None:
    """Structural guard: an unattended, non-GET capability MUST carry a path_regex.

    This is the rule that stops the next person from adding an auto-write with a
    loose prefix. It fails at test time rather than in production.
    """
    registry = load_registry()
    for cap in registry.capabilities.values():
        if not cap.callable_now or not cap.is_auto:
            continue
        assert cap.http is not None
        writes = {m.upper() for m in cap.http.methods} - {"GET", "HEAD"}
        if writes:
            assert cap.http.path_regex, (
                f"{cap.id} runs unattended and can {'/'.join(sorted(writes))}, but has no "
                "path_regex -- a bare prefix cannot pin the subresource"
            )


# --- credential containment --------------------------------------------------


def test_broker_has_no_credential_injection_surface() -> None:
    from omniagentos.connectors import broker

    assert not hasattr(broker, "INJECTABLE_GROUPS")
    assert not hasattr(broker, "injectable_env")


def test_describe_grant_leaks_names_not_values(monkeypatch) -> None:
    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "sk_live_pretend")
    summary = describe_grant(["stripe_acmeuni.read", "stripe_acmeuni.refund"])
    flat = repr(summary)
    assert "ACMEUNI_STRIPE_PRIMARY_SECRET_KEY" in flat  # the name is useful
    assert "sk_live_pretend" not in flat  # the value never is
    assert summary["always_human"] == ["stripe_acmeuni.refund"]
    assert "payments" in summary["touches_danger_group"]


# --- the grant chain: agent >= task >= step ----------------------------------


def test_task_cannot_widen_an_agent_grant() -> None:
    assert effective_grant(["piedpiper_acmeuni.read"], ["piedpiper_acmeuni.read", "stripe_acmeuni.refund"]) == [
        "piedpiper_acmeuni.read"
    ]


def test_task_may_narrow_an_agent_grant() -> None:
    assert effective_grant(["piedpiper_acmeuni.read", "piedpiper_acmeuni.note_write"], ["piedpiper_acmeuni.read"]) == [
        "piedpiper_acmeuni.read"
    ]


def test_absent_task_grant_inherits_the_agent_grant() -> None:
    assert effective_grant(["piedpiper_acmeuni.read"], None) == ["piedpiper_acmeuni.read"]


# --- policy validation -------------------------------------------------------


def test_policy_accepts_primitives_and_registry_capabilities() -> None:
    cfg = load_policy()
    validate_tools(["generate", "file_read", "stripe_acmeuni.read"], cfg)


def test_policy_rejects_a_capability_not_in_the_registry() -> None:
    cfg = load_policy()
    with pytest.raises(PolicyError, match="Unknown capability"):
        validate_tools(["stripe_acmeuni.wire_transfer"], cfg)


# --- grant store -------------------------------------------------------------


@pytest.fixture
def collab_store(tmp_path):
    """A collab store with two registered agents.

    agent_capabilities has a FOREIGN KEY onto agents(id), so a grant can only be
    written for an agent that exists -- a grant naming a nonexistent agent is not a
    thing the system should be able to represent.
    """
    from omniagentos.collab.contracts import Agent
    from omniagentos.collab.store import CollabStore

    store = CollabStore(db_path=str(tmp_path / "t.db"))
    store.register_agent(Agent(name="safe"))
    store.register_agent(Agent(name="risky"))
    return store


@pytest.fixture
def cap_store(collab_store) -> CapabilityStore:
    return CapabilityStore(collab_store._store)


@pytest.fixture
def agent_ids(collab_store) -> dict[str, str]:
    return {str(a["name"]): str(a["id"]) for a in collab_store.list_agents()}


def test_store_refuses_to_persist_an_unknown_capability(
    cap_store: CapabilityStore, agent_ids: dict[str, str]
) -> None:
    safe = agent_ids["safe"]
    with pytest.raises(ConnectorError, match="unknown capability"):
        cap_store.set_grant(safe, ["stripe_acmeuni.read", "stripe_acmeuni.nonsense"])
    # Validation runs before the transaction opens, so one bad id in the request
    # leaves the existing grant untouched rather than clearing it.
    assert cap_store.get_grant(safe) == []


def test_grant_for_a_nonexistent_agent_is_refused(cap_store: CapabilityStore) -> None:
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        cap_store.set_grant("agt_does_not_exist", ["piedpiper_acmeuni.read"])


def test_store_roundtrip_and_audit_log(
    cap_store: CapabilityStore, agent_ids: dict[str, str]
) -> None:
    safe = agent_ids["safe"]
    cap_store.set_grant(safe, ["piedpiper_acmeuni.read", "piedpiper_acmeuni.note_write"])
    # Grants come back sorted by capability id, not insertion order.
    assert cap_store.get_grant(safe) == ["piedpiper_acmeuni.note_write", "piedpiper_acmeuni.read"]

    cap_store.set_grant(safe, ["piedpiper_acmeuni.read"])  # revoke note_write
    assert cap_store.get_grant(safe) == ["piedpiper_acmeuni.read"]

    log = cap_store.grant_log(safe)
    actions = {(e["capability_id"], e["action"]) for e in log}
    assert ("piedpiper_acmeuni.note_write", "grant") in actions
    assert ("piedpiper_acmeuni.note_write", "revoke") in actions
    # The revoke did not erase the history of the grant.
    assert len(log) == 3


def test_token_names_the_agent_but_does_not_carry_the_grant(
    cap_store: CapabilityStore, agent_ids: dict[str, str]
) -> None:
    """The central containment property of the call path.

    An agent holding `shell` can read and rewrite its own environment. If the grant
    travelled in that environment, the agent could hand itself stripe_acmeuni.refund.
    So the token proves *identity* only; the grant is looked up server-side. There
    is nothing in the agent's environment to escalate.
    """
    safe = agent_ids["safe"]
    cap_store.set_grant(safe, ["piedpiper_acmeuni.read"])
    token = cap_store.issue_token("run_1", safe)

    identity = cap_store.resolve_token(token)
    assert identity == {"run_id": "run_1", "agent_id": safe}
    # The token is opaque: it says who, never what.
    assert "piedpiper_acmeuni" not in token
    assert "stripe" not in token

    # The server, not the caller, decides what that identity may do.
    assert cap_store.get_grant(identity["agent_id"]) == ["piedpiper_acmeuni.read"]


def test_unknown_expired_and_revoked_tokens_all_fail_closed(
    cap_store: CapabilityStore, agent_ids: dict[str, str]
) -> None:
    safe = agent_ids["safe"]
    assert cap_store.resolve_token("not-a-real-token") is None

    expired = cap_store.issue_token("run_2", safe, ttl_seconds=-1)
    assert cap_store.resolve_token(expired) is None, "an expired token must not resolve"

    live = cap_store.issue_token("run_3", safe)
    assert cap_store.resolve_token(live) is not None
    cap_store.revoke_tokens_for_run("run_3")
    assert cap_store.resolve_token(live) is None, "access must not outlive the run"


def test_denied_calls_are_logged_not_just_refused(
    cap_store: CapabilityStore, agent_ids: dict[str, str]
) -> None:
    """A refusal you cannot see is a refusal you cannot learn from."""
    risky = agent_ids["risky"]
    cap_store.log_call(
        "run_9",
        risky,
        "stripe_acmeuni.refund",
        method="POST",
        path="/v1/refunds",
        allowed=False,
        reason="requires_human_approval",
    )
    entry = cap_store.call_log("run_9")[0]
    assert entry["allowed"] == 0
    assert entry["reason"] == "requires_human_approval"
    assert entry["capability_id"] == "stripe_acmeuni.refund"


def test_store_holders_of_answers_who_can_refund(
    cap_store: CapabilityStore, agent_ids: dict[str, str]
) -> None:
    cap_store.set_grant(agent_ids["safe"], ["stripe_acmeuni.read"])
    cap_store.set_grant(agent_ids["risky"], ["stripe_acmeuni.read", "stripe_acmeuni.refund"])
    assert cap_store.holders_of("stripe_acmeuni.refund") == [agent_ids["risky"]]


def test_deleting_an_agent_revokes_its_grants(
    cap_store: CapabilityStore, collab_store, agent_ids: dict[str, str]
) -> None:
    """ON DELETE CASCADE: a deleted agent must not leave orphaned access behind."""
    risky = agent_ids["risky"]
    cap_store.set_grant(risky, ["stripe_acmeuni.read"])
    assert cap_store.holders_of("stripe_acmeuni.read") == [risky]

    collab_store._connection.execute("DELETE FROM agents WHERE id = ?", (risky,))
    collab_store._connection.commit()

    assert cap_store.get_grant(risky) == []
    assert cap_store.holders_of("stripe_acmeuni.read") == []
