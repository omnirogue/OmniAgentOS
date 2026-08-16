"""Campaign grants — bounded minting + consume (HANDOFF Task 3)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from omniagentos.connectors.extractors import audience_snapshot_hash
from omniagentos.db.store import SqliteStore
from omniagentos.grants import GrantsStore
from omniagentos.grants.store import is_grant_live

# Far-future expiry used by mint helpers in this module.
_EXPIRES = "2099-01-01T00:00:00+00:00"


def _mint(
    grants: GrantsStore,
    capability: str = "gmail.send",
    **kwargs: object,
) -> dict:
    base = {
        "approval_id": "apr_test_1",
        "max_actions": 5,
        "max_spend_usd": 10.0,
        "expires_at": _EXPIRES,
        "target_set": ["a@x.com"],
        "metadata": {"generation": 0, "action_class": "consequential"},
    }
    # Allow callers to replace metadata while retaining required durable pins.
    if "metadata" in kwargs and isinstance(kwargs["metadata"], dict):
        meta = dict(kwargs["metadata"])  # type: ignore[arg-type]
        meta.setdefault("generation", 0)
        meta.setdefault("action_class", "consequential")
        kwargs = {**kwargs, "metadata": meta}
    base.update(kwargs)
    return grants.create_grant(capability, **base)  # type: ignore[arg-type]


@pytest.fixture
def grants(tmp_path: Path) -> GrantsStore:
    return GrantsStore(SqliteStore(str(tmp_path / "grants.db")))


def test_create_rejects_missing_bounds(grants: GrantsStore) -> None:
    with pytest.raises(ValueError, match="approval_id"):
        grants.create_grant("gmail.send", max_actions=1, max_spend_usd=1.0, expires_at=_EXPIRES)
    with pytest.raises(ValueError, match="max_actions"):
        grants.create_grant("gmail.send", approval_id="a", max_spend_usd=1.0, expires_at=_EXPIRES)
    with pytest.raises(ValueError, match="max_spend"):
        grants.create_grant("gmail.send", approval_id="a", max_actions=1, expires_at=_EXPIRES)
    with pytest.raises(ValueError, match="expires_at"):
        grants.create_grant("gmail.send", approval_id="a", max_actions=1, max_spend_usd=1.0)
    with pytest.raises(ValueError, match="generation"):
        grants.create_grant(
            "gmail.send",
            approval_id="a",
            max_actions=1,
            max_spend_usd=1.0,
            expires_at=_EXPIRES,
            metadata={},
        )


def test_create_get_list_active_and_record_action(grants: GrantsStore) -> None:
    created = _mint(
        grants,
        label="Welcome campaign",
        project_id="project-1",
        metadata={"campaign": "welcome", "generation": 0},
        max_actions=5,
    )

    assert created["id"].startswith("gnt_")
    assert created["target_set"] == ["a@x.com"]
    assert created["plan_approval_state"] == "approved"
    # "gmail.send" is broadcast-capable: create_grant additionally records the
    # audience bound (max_recipients + an approval-time snapshot hash/count of
    # target_set) on metadata (grant-audience-bound fix).
    assert created["metadata"] == {
        "action_class": "consequential",
        "campaign": "welcome",
        "generation": 0,
        "max_recipients": 1,
        "audience_snapshot_hash": audience_snapshot_hash(["a@x.com"]),
        "audience_snapshot_count": 1,
    }
    assert grants.get_grant(created["id"]) == created
    assert [row["id"] for row in grants.list_active_grants(capability="gmail.send")] == [
        created["id"]
    ]
    assert grants.list_active_grants(project_id="other-project") == []

    action = grants.record_action(
        created["id"],
        "gmail.send",
        "a@x.com",
        0,
        "failed",
        detail="provider rejected",
    )
    assert action["outcome"] == "failed"
    assert action["detail"] == "provider rejected"


def test_not_approved_is_not_live(grants: GrantsStore) -> None:
    # Bypass create_grant validation via raw SQL to simulate a bad row.
    g = _mint(grants)
    grants._store._write(
        "UPDATE campaign_grants SET plan_approval_state = ? WHERE id = ?",
        ("pending", g["id"]),
    )
    row = grants.get_grant(g["id"])
    assert row is not None
    live, reason = is_grant_live(row, capability="gmail.send")
    assert live is False
    assert reason == "not_approved"


def test_try_consume_exhausts_max_actions(grants: GrantsStore) -> None:
    grant = _mint(grants, max_actions=2)
    # Matches _mint's default target_set=["a@x.com"] -- a broadcast-capable
    # grant now also re-checks the live recipient surface at consume time.
    scoped_args = {"raw": _gmail_raw_mime(["a@x.com"])}

    assert grants.try_consume(
        grant["id"],
        capability="gmail.send",
        target="a@x.com",
        generation=0,
        action_class="consequential",
        scoped_args=scoped_args,
    ).ok
    assert grants.try_consume(
        grant["id"],
        capability="gmail.send",
        target="a@x.com",
        generation=0,
        action_class="consequential",
        scoped_args=scoped_args,
    ).ok
    refused = grants.try_consume(
        grant["id"],
        capability="gmail.send",
        target="a@x.com",
        generation=0,
        action_class="consequential",
        scoped_args=scoped_args,
    )

    assert refused.ok is False
    assert refused.outcome == "refused"
    assert refused.reason == "max_actions"
    assert grants.get_grant(grant["id"])["actions_used"] == 2


def test_expired_grant_is_refused_and_not_active(grants: GrantsStore) -> None:
    grant = _mint(grants, expires_at="2000-01-01T00:00:00Z")

    result = grants.try_consume(
        grant["id"],
        target="a@x.com",
        generation=0,
        action_class="consequential",
    )

    assert result.ok is False
    assert result.reason == "expired"
    assert grants.list_active_grants() == []


def test_revoked_grant_is_refused(grants: GrantsStore) -> None:
    grant = _mint(grants)

    revoked = grants.revoke_grant(grant["id"], "campaign stopped")
    result = grants.try_consume(
        grant["id"],
        target="a@x.com",
        generation=0,
        action_class="consequential",
    )

    assert revoked is not None
    assert revoked["revoked_at"]
    assert revoked["revoke_reason"] == "campaign stopped"
    assert result.ok is False
    assert result.reason == "revoked"


def test_target_breakout_does_not_consume_then_in_set_target_does(
    grants: GrantsStore,
) -> None:
    grant = _mint(grants, target_set=["a@x.com"], max_actions=1)

    breakout = grants.try_consume(
        grant["id"],
        target="b@y.com",
        generation=0,
        action_class="consequential",
    )
    after_breakout = grants.get_grant(grant["id"])
    allowed = grants.try_consume(
        grant["id"],
        target="a@x.com",
        generation=0,
        action_class="consequential",
        scoped_args={"raw": _gmail_raw_mime(["a@x.com"])},
    )

    assert breakout.ok is False
    assert breakout.outcome == "broke_out"
    assert breakout.reason == "target_breakout"
    assert breakout.action is not None
    assert breakout.action["outcome"] == "broke_out"
    assert after_breakout is not None
    assert after_breakout["actions_used"] == 0
    assert after_breakout["spend_used_usd"] == 0
    assert allowed.ok is True
    assert grants.get_grant(grant["id"])["actions_used"] == 1


def test_empty_target_set_allows_any_target_for_a_non_broadcast_capability(
    grants: GrantsStore,
) -> None:
    """Empty target_set still means 'no audience restriction' for a capability

    with no customer-message audience (e.g. a refund) -- this is unchanged by
    the grant-audience-bound fix, which only constrains broadcast-capable
    capabilities (``gmail*.send`` / ``piedpiper_*.conversation_send`` /
    ``customerio_*.trigger_broadcast``). Broker.call additionally fails closed
    when a non-empty target_set is present and recipients are unknown — that
    path is covered in connector tests.
    """
    grant = _mint(
        grants,
        capability="stripe_acmeuni.refund",
        target_set=[],
        metadata={"generation": 0, "action_class": "consequential"},
    )

    result = grants.try_consume(
        grant["id"],
        capability="stripe_acmeuni.refund",
        target="anyone@example.com",
        generation=0,
        action_class="consequential",
    )

    assert result.ok is True
    assert grants.get_grant(grant["id"])["actions_used"] == 1


# --------------------------------------------------------------------------
# Grant-audience-bound (max_recipients / audience snapshot) — approved
# customer-message grants must be bound to a concrete, capped audience.
# --------------------------------------------------------------------------


def test_mint_rejects_unbounded_audience_for_broadcast_capability(
    grants: GrantsStore,
) -> None:
    """A broadcast-capable grant with no target_set (unbounded audience) is

    refused at mint — an approved customer-message grant may never default to
    an unbounded audience.
    """
    with pytest.raises(ValueError, match="target_set"):
        _mint(grants, target_set=[])
    with pytest.raises(ValueError, match="target_set"):
        _mint(grants, target_set=None)


def test_mint_rejects_max_recipients_below_the_snapshot(grants: GrantsStore) -> None:
    with pytest.raises(ValueError, match="max_recipients"):
        _mint(grants, target_set=["a@x.com", "b@x.com"], max_recipients=1)


def test_mint_records_audience_bound_and_send_path_rejects_expanded_audience(
    grants: GrantsStore,
) -> None:
    """The falsifier: a grant approved against N recipients cannot be

    exercised unmodified against a materially larger recipient set.
    """
    grant = _mint(grants, target_set=["a@x.com", "b@x.com"], max_actions=5)

    assert grant["metadata"]["max_recipients"] == 2
    assert grant["metadata"]["audience_snapshot_count"] == 2
    assert grant["metadata"]["audience_snapshot_hash"] == audience_snapshot_hash(
        ["a@x.com", "b@x.com"]
    )

    # Bounded: the live call's own recipient surface matches the approved
    # snapshot exactly -- accepted, and the grant's actions_used advances.
    accepted = grants.try_consume(
        grant["id"],
        capability="gmail.send",
        target="a@x.com",
        generation=0,
        action_class="consequential",
        scoped_args={"raw": _gmail_raw_mime(["a@x.com", "b@x.com"])},
    )
    assert accepted.ok is True
    assert grants.get_grant(grant["id"])["actions_used"] == 1

    # Expanded: the SAME grant is exercised again, but this time the live send
    # body carries a materially larger recipient set than what was approved.
    # Refused, and the grant's bounded actions are NOT spent by the refusal.
    expanded = grants.try_consume(
        grant["id"],
        capability="gmail.send",
        target="a@x.com",
        generation=0,
        action_class="consequential",
        scoped_args={"raw": _gmail_raw_mime(["a@x.com", "b@x.com", "c@x.com", "d@x.com"])},
    )
    assert expanded.ok is False
    assert expanded.reason in {"max_recipients_exceeded", "audience_drift"}
    assert grants.get_grant(grant["id"])["actions_used"] == 1


def test_legacy_grant_missing_audience_bound_fails_closed(grants: GrantsStore) -> None:
    """A row that predates this fix (or was hand-edited) has no recorded

    max_recipients/audience_snapshot_hash. Validation must reject it rather
    than default to an unbounded audience.
    """
    grant = _mint(grants, target_set=["a@x.com"])
    stripped = dict(grant["metadata"])
    stripped.pop("max_recipients", None)
    stripped.pop("audience_snapshot_hash", None)
    stripped.pop("audience_snapshot_count", None)
    grants._store._write(
        "UPDATE campaign_grants SET metadata_json = ? WHERE id = ?",
        (json.dumps(stripped), grant["id"]),
    )

    row = grants.get_grant(grant["id"])
    assert row is not None
    live, reason = is_grant_live(row, capability="gmail.send")
    assert live is False
    assert reason == "no_audience_bound"

    result = grants.try_consume(
        grant["id"],
        capability="gmail.send",
        target="a@x.com",
        generation=0,
        action_class="consequential",
    )
    assert result.ok is False
    assert result.reason == "no_audience_bound"


def test_broadcast_grant_with_empty_target_set_is_rejected_at_consumption(
    grants: GrantsStore,
) -> None:
    """A broadcast-capable grant somehow bearing an empty target_set -- the

    exact pre-fix vulnerable shape (unbounded audience, no cap), reconstructed
    here by bypassing ``create_grant``'s mint-time guard via raw SQL, as a
    pre-existing or hand-edited row would -- is refused at the CONSUMPTION
    boundary (``try_consume`` / ``validate_approval_token``) too, not merely
    prevented at mint. Audience-unbounded is never let through just because
    something other than ``create_grant`` wrote the row.
    """
    grant = _mint(grants, target_set=["a@x.com"])
    unbound_meta = {
        key: value
        for key, value in grant["metadata"].items()
        if key not in {"max_recipients", "audience_snapshot_hash", "audience_snapshot_count"}
    }
    grants._store._write(
        "UPDATE campaign_grants SET target_set_json = '[]', metadata_json = ? WHERE id = ?",
        (json.dumps(unbound_meta), grant["id"]),
    )

    row = grants.get_grant(grant["id"])
    assert row is not None
    assert row["target_set"] == []

    result = grants.try_consume(
        grant["id"],
        capability="gmail.send",
        target="anyone@example.com",
        generation=0,
        action_class="consequential",
    )
    assert result.ok is False
    assert result.reason == "no_audience_bound"


def _gmail_raw_mime(recipients: list[str]) -> str:
    """A minimal base64url-encoded MIME body naming ``recipients`` as To:."""
    message = f"To: {', '.join(recipients)}\r\nSubject: hi\r\n\r\nbody"
    return base64.urlsafe_b64encode(message.encode("utf-8")).decode("ascii").rstrip("=")
