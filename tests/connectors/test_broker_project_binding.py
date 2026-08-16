"""C11 / D-31 — a capability grant authorizes ONE project, and only that one.

The defect this pins: ``agent_capabilities`` named a holder and nothing else, so
``lane:worker`` holding a send capability for project A held it for project B as
well. Not "allowed by policy" — INVISIBLE, because the fact that would have made
the question answerable was not recorded anywhere. Migration 115 records it and
``broker.authorize`` enforces it.

Three distinguishable refusals, because three different people fix three
different things (U-R3):

* ``grant_project_mismatch``  — the grant belongs to another project.
* ``grant_project_unbound``   — nothing durable binds this call to a project.
* ``call_project_unknown``    — a bound grant, presented by an unscoped caller.

Everything here is offline: no network, no credential, no provider.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.connectors.broker import (
    _DENIAL_NEXT_ACTIONS,
    BrokerDenied,
    authorize,
    authorize_with_grant,
)
from omniagentos.connectors.store import CapabilityStore
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.grants import GrantsStore
from omniagentos.provision.capability_requests import (
    CapabilityRequest,
    CapabilityRequestService,
    ToolRequest,
)

HOLDER = "lane:swarm.worker.c11"
READ_CAP = "piedpiper_acmeuni.read"
SEND_CAP = "gmail.send"
PROJECT_A = "proj_c11_a"
PROJECT_B = "proj_c11_b"
EXPIRES = "2099-01-01T00:00:00+00:00"
RATIONALE = "prove the project binding travels from the ask to the grant"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    """A migrated control-plane DB holding one agent and two real projects."""
    db_path = str(tmp_path / "c11.sqlite3")
    migrate(db_path)
    raw = SqliteStore(db_path)
    raw._connection.execute(
        "INSERT INTO agents (id, name, lineage, model, expertise_json, trust_level, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            HOLDER,
            HOLDER,
            "test.c11",
            None,
            "[]",
            "T1",
            "idle",
            "2026-08-04T00:00:00+00:00",
            "2026-08-04T00:00:00+00:00",
        ),
    )
    for project in (PROJECT_A, PROJECT_B):
        raw._connection.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
            (project, project, "2026-08-04T00:00:00+00:00"),
        )
    raw._connection.commit()
    try:
        yield raw
    finally:
        raw.close()


@pytest.fixture
def caps(store: SqliteStore) -> CapabilityStore:
    return CapabilityStore(store)


@pytest.fixture
def grants(store: SqliteStore) -> GrantsStore:
    return GrantsStore(store)


def _mint_campaign(grants: GrantsStore, **overrides: object) -> dict:
    base: dict = {
        "approval_id": "apr_c11",
        "max_actions": 2,
        "max_spend_usd": 100.0,
        "expires_at": EXPIRES,
        # SEND_CAP ("gmail.send") is broadcast-capable (grant-audience-bound
        # fix): create_grant requires a non-empty target_set (the audience
        # snapshot) for it -- an empty/unbounded audience is refused at mint.
        "target_set": ["a@x.com"],
        "metadata": {"generation": 0, "action_class": "consequential"},
    }
    base.update(overrides)
    return grants.create_grant(SEND_CAP, **base)


def _gmail_raw_mime(recipients: list[str]) -> str:
    """A minimal base64url-encoded MIME body naming ``recipients`` as To:."""
    message = f"To: {', '.join(recipients)}\r\nSubject: hi\r\n\r\nbody"
    return base64.urlsafe_b64encode(message.encode("utf-8")).decode("ascii").rstrip("=")


# ------------------------------------------------------------------ the column


def test_migration_115_records_the_binding_on_both_tables(store: SqliteStore) -> None:
    """The fact has to exist before anything can enforce it."""
    for table in ("agent_capabilities", "capability_requests"):
        columns = {
            str(row["name"])
            for row in store._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert "project_id" in columns, f"{table} still cannot record a project binding"


def test_issue_scoped_grant_persists_the_project_binding(caps: CapabilityStore) -> None:
    caps.issue_scoped_grant(HOLDER, [READ_CAP], project_id=PROJECT_A)
    row = caps.get_grant_row(HOLDER, READ_CAP)
    assert row is not None
    assert row["project_id"] == PROJECT_A


def test_reissuing_for_another_project_rebinds_rather_than_widens(caps: CapabilityStore) -> None:
    """One row per (agent, capability): a rebind MOVES the grant, never adds one."""
    caps.issue_scoped_grant(HOLDER, [READ_CAP], project_id=PROJECT_A)
    caps.issue_scoped_grant(HOLDER, [READ_CAP], project_id=PROJECT_B)
    row = caps.get_grant_row(HOLDER, READ_CAP)
    assert row is not None
    assert row["project_id"] == PROJECT_B
    assert caps.get_grant(HOLDER) == [READ_CAP]


# ------------------------------------------------- the standing-grant decision


def test_same_project_grant_authorizes(caps: CapabilityStore) -> None:
    caps.issue_scoped_grant(HOLDER, [READ_CAP], project_id=PROJECT_A)

    cap = authorize(
        READ_CAP,
        caps.get_grant(HOLDER),
        grant_store=caps,
        agent_id=HOLDER,
        project_id=PROJECT_A,
    )

    assert cap.id == READ_CAP


def test_cross_project_grant_is_denied_with_its_own_code(caps: CapabilityStore) -> None:
    """The headline: a project-A grant is NOT usable by project-B work."""
    caps.issue_scoped_grant(HOLDER, [READ_CAP], project_id=PROJECT_A)

    with pytest.raises(BrokerDenied) as denied:
        authorize(
            READ_CAP,
            caps.get_grant(HOLDER),
            grant_store=caps,
            agent_id=HOLDER,
            project_id=PROJECT_B,
        )

    assert denied.value.reason == "grant_project_mismatch"
    assert denied.value.payload()["reason_code"] == "grant_project_mismatch"
    # Diagnosable: the denial names both sides so an operator does not have to
    # go and read two tables to find out which boundary was crossed.
    assert PROJECT_A in denied.value.detail
    assert PROJECT_B in denied.value.detail
    assert denied.value.next_action


def test_a_legacy_null_project_grant_never_silently_authorizes(caps: CapabilityStore) -> None:
    """A pre-115 row proves no binding, so it authorizes no project-scoped call."""
    caps.issue_scoped_grant(HOLDER, [READ_CAP])  # no project: the legacy shape
    assert caps.get_grant_row(HOLDER, READ_CAP)["project_id"] is None  # type: ignore[index]

    with pytest.raises(BrokerDenied) as denied:
        authorize(
            READ_CAP,
            caps.get_grant(HOLDER),
            grant_store=caps,
            agent_id=HOLDER,
            project_id=PROJECT_B,
        )

    assert denied.value.reason == "grant_project_unbound"


def test_a_bound_grant_refuses_a_caller_that_names_no_project(caps: CapabilityStore) -> None:
    """ "I did not say which project I am in" must not mean "therefore any of them"."""
    caps.issue_scoped_grant(HOLDER, [READ_CAP], project_id=PROJECT_A)

    with pytest.raises(BrokerDenied) as denied:
        authorize(READ_CAP, caps.get_grant(HOLDER), grant_store=caps, agent_id=HOLDER)

    assert denied.value.reason == "call_project_unknown"


def test_an_unbound_grant_and_an_unscoped_call_are_left_alone(caps: CapabilityStore) -> None:
    """Landing ahead of the D-31 switch may not flag-day today's callers."""
    caps.issue_scoped_grant(HOLDER, [READ_CAP])

    cap = authorize(READ_CAP, caps.get_grant(HOLDER), grant_store=caps, agent_id=HOLDER)

    assert cap.id == READ_CAP


def test_naming_a_project_with_no_durable_record_fails_closed() -> None:
    """No store, no row, no proof — and a caller's own list is not proof."""
    with pytest.raises(BrokerDenied) as denied:
        authorize(READ_CAP, [READ_CAP], project_id=PROJECT_A)

    assert denied.value.reason == "grant_project_unbound"


# ---------------------------------------------------- the holder (no agent_id)


def test_holder_backed_authorization_enforces_the_binding(caps: CapabilityStore) -> None:
    """``authorize_with_grant`` must not lose the check when no agent_id is named."""
    caps.issue_scoped_grant(HOLDER, [READ_CAP], project_id=PROJECT_A)

    cap = authorize_with_grant(READ_CAP, None, caps, grant_holder=HOLDER, project_id=PROJECT_A)
    assert cap.id == READ_CAP

    with pytest.raises(BrokerDenied) as denied:
        authorize_with_grant(READ_CAP, None, caps, grant_holder=HOLDER, project_id=PROJECT_B)
    assert denied.value.reason == "grant_project_mismatch"


def test_holder_backed_legacy_grant_is_unbound_not_allowed(caps: CapabilityStore) -> None:
    caps.issue_scoped_grant(HOLDER, [READ_CAP])

    with pytest.raises(BrokerDenied) as denied:
        authorize_with_grant(READ_CAP, None, caps, grant_holder=HOLDER, project_id=PROJECT_A)

    assert denied.value.reason == "grant_project_unbound"


# ------------------------------------------------------ the bounded campaign grant


def test_campaign_grant_authorizes_its_own_project(grants: GrantsStore) -> None:
    # Unlike the other tests in this module, this one reaches all the way
    # through the hard-human/target-allowed/audience-bound gate to a
    # successful authorize(), so it needs a target AND a live recipient
    # surface (scoped_args) matching _mint_campaign's target_set.
    grant = _mint_campaign(grants, project_id=PROJECT_A)

    cap = authorize(
        SEND_CAP,
        [SEND_CAP],
        grant_id=grant["id"],
        grant_store=grants,
        generation=0,
        project_id=PROJECT_A,
        target="a@x.com",
        scoped_args={"raw": _gmail_raw_mime(["a@x.com"])},
    )

    assert cap.id == SEND_CAP


def test_campaign_grant_project_mismatch_is_refused_before_the_approval_gate(
    grants: GrantsStore,
) -> None:
    """Containment is decided first: a cross-project ask never gets to argue approval.

    ``gmail.send`` is CONSEQUENTIAL, so the only other way this call ends is the
    hard-human gate. Asserting the project code (and not ``invalid_approval_token``)
    is what pins the ORDER — a breakout must be reported as a breakout, not as a
    paperwork problem.
    """
    grant = _mint_campaign(grants, project_id=PROJECT_A)

    with pytest.raises(BrokerDenied) as denied:
        authorize(
            SEND_CAP,
            [SEND_CAP],
            grant_id=grant["id"],
            grant_store=grants,
            generation=0,
            project_id=PROJECT_B,
        )

    assert denied.value.reason == "grant_project_mismatch"


def test_campaign_grant_without_a_project_cannot_serve_one(grants: GrantsStore) -> None:
    grant = _mint_campaign(grants)

    with pytest.raises(BrokerDenied) as denied:
        authorize(
            SEND_CAP,
            [SEND_CAP],
            grant_id=grant["id"],
            grant_store=grants,
            generation=0,
            project_id=PROJECT_A,
        )

    assert denied.value.reason == "grant_project_unbound"


def test_authorize_with_grant_consumes_nothing_when_the_project_is_wrong(
    grants: GrantsStore,
) -> None:
    """A refused breakout may not spend the campaign's bounded actions."""
    grant = _mint_campaign(grants, project_id=PROJECT_A)

    with pytest.raises(BrokerDenied) as denied:
        authorize_with_grant(
            SEND_CAP,
            [SEND_CAP],
            grants,
            grant["id"],
            generation=0,
            project_id=PROJECT_B,
        )

    assert denied.value.reason == "grant_project_mismatch"
    assert grants.get_grant(grant["id"])["actions_used"] == 0  # type: ignore[index]


# ------------------------------------------------------------- the whole chain


def test_the_binding_travels_from_the_request_to_the_grant_to_the_broker(
    store: SqliteStore, caps: CapabilityStore
) -> None:
    """One project id threads ask -> decision -> standing grant -> authorization.

    ``echo.ping`` is the credential-free floor capability, so the auto-grant path
    runs offline. The project is supplied by the SERVICE (the authenticated
    channel), never by the envelope: a worker that could name the project on its
    own ask could ask for another project's reach.
    """
    service = CapabilityRequestService(store)
    envelope = CapabilityRequest(
        from_agent_id=HOLDER,
        from_lane=HOLDER,
        rationale=RATIONALE,
        request=ToolRequest("echo.ping"),
        request_id=str(uuid.uuid4()),
        requested_at="2026-08-04T12:00:00+00:00",
    )

    receipt = service.submit(envelope, channel_principal=HOLDER, project_id=PROJECT_A)
    assert receipt.state == "granted", receipt.reason_code

    assert service.get(envelope.request_id)["project_id"] == PROJECT_A  # type: ignore[index]
    row = caps.get_grant_row(HOLDER, "echo.ping")
    assert row is not None and row["project_id"] == PROJECT_A

    assert (
        authorize(
            "echo.ping",
            caps.get_grant(HOLDER),
            grant_store=caps,
            agent_id=HOLDER,
            project_id=PROJECT_A,
        ).id
        == "echo.ping"
    )
    with pytest.raises(BrokerDenied) as denied:
        authorize(
            "echo.ping",
            caps.get_grant(HOLDER),
            grant_store=caps,
            agent_id=HOLDER,
            project_id=PROJECT_B,
        )
    assert denied.value.reason == "grant_project_mismatch"


def test_a_replayed_request_id_cannot_carry_a_second_project(store: SqliteStore) -> None:
    """Replay is idempotency, not a project-crossing primitive."""
    from omniagentos.provision.capability_requests import EnvelopeRejected

    service = CapabilityRequestService(store)
    envelope = CapabilityRequest(
        from_agent_id=HOLDER,
        from_lane=HOLDER,
        rationale=RATIONALE,
        request=ToolRequest("echo.ping"),
        request_id=str(uuid.uuid4()),
        requested_at="2026-08-04T12:00:00+00:00",
    )
    service.submit(envelope, channel_principal=HOLDER, project_id=PROJECT_A)

    with pytest.raises(EnvelopeRejected) as rejected:
        service.submit(envelope, channel_principal=HOLDER, project_id=PROJECT_B)

    assert rejected.value.reason_code == "request_id_reuse"


# ------------------------------------------------------------------- taxonomy


def test_the_three_project_denials_route_to_three_different_remedies() -> None:
    """U-R3: a caller routes on the code alone, so no two may share a next action."""
    codes = ("grant_project_mismatch", "grant_project_unbound", "call_project_unknown")
    actions = [_DENIAL_NEXT_ACTIONS[code] for code in codes]
    assert all(actions)
    assert len(set(actions)) == len(codes)
    # And they stay distinct from every OTHER denial's remedy.
    assert len(set(_DENIAL_NEXT_ACTIONS.values())) == len(_DENIAL_NEXT_ACTIONS)
