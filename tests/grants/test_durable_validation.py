"""Durable approval-token validation (H-02).

Uses only in-memory / tmp SQLite grant stores — never live money or customer
operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.grants import (
    GrantsStore,
    normalize_grant_ref,
    validate_approval_token,
)
from tests.support.db_template import make_store

CAP = "stripe_acmeuni.refund"
_EXPIRES = "2099-01-01T00:00:00+00:00"


def _mint(grants: GrantsStore, **kwargs: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "approval_id": "apr_val_1",
        "max_actions": 2,
        "max_spend_usd": 50.0,
        "expires_at": _EXPIRES,
        "target_set": ["cus_ok"],
        "metadata": {
            "generation": 1,
            "action_class": "consequential",
            "connector": "stripe_acmeuni",
            "tool": "refund",
            "scoped_args": {"currency": "usd"},
        },
    }
    base.update(kwargs)
    return grants.create_grant(CAP, **base)  # type: ignore[arg-type]


@pytest.fixture
def grants(tmp_path: Path) -> GrantsStore:
    return GrantsStore(make_store(SqliteStore, tmp_path / "durable.db"))


def test_missing_store_fails_closed() -> None:
    result = validate_approval_token("gnt_x", grant_store=None, capability=CAP)
    assert result.ok is False
    assert result.reason == "grant_store_unavailable"


def test_arbitrary_string_fails(grants: GrantsStore) -> None:
    result = validate_approval_token(
        "not-a-real-grant",
        grant_store=grants,
        capability=CAP,
    )
    assert result.ok is False
    assert result.reason == "token_not_found"


@pytest.mark.parametrize("token", ["", "   ", None])
def test_malformed_tokens_fail(grants: GrantsStore, token: str | None) -> None:
    result = validate_approval_token(token, grant_store=grants, capability=CAP)
    assert result.ok is False
    assert result.reason in {"token_malformed", "token_missing"}


def test_expired_token_fails(grants: GrantsStore) -> None:
    grant = _mint(grants, expires_at="2001-01-01T00:00:00+00:00")
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        generation=1,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "expired"


def test_revoked_token_fails(grants: GrantsStore) -> None:
    grant = _mint(grants)
    grants.revoke_grant(grant["id"], "stop")
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        generation=1,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "revoked"


def test_wrong_generation_fails(grants: GrantsStore) -> None:
    grant = _mint(grants)
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        action_class="consequential",
        generation=99,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "generation_mismatch"


def test_missing_generation_presentation_is_ambiguous(grants: GrantsStore) -> None:
    grant = _mint(grants)
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        action_class="consequential",
        generation=None,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "generation_ambiguous"


def test_missing_generation_pin_on_row_is_ambiguous(grants: GrantsStore) -> None:
    """Legacy/raw rows without a pin fail closed (mint would have refused them)."""
    grant = _mint(grants)
    grants._store._write(
        "UPDATE campaign_grants SET metadata_json = ? WHERE id = ?",
        ('{"action_class":"consequential","scoped_args":{"currency":"usd"}}', grant["id"]),
    )
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        action_class="consequential",
        generation=0,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "generation_ambiguous"


def test_mint_requires_generation_pin(grants: GrantsStore) -> None:
    with pytest.raises(ValueError, match="generation"):
        grants.create_grant(
            CAP,
            approval_id="apr_no_gen",
            max_actions=1,
            max_spend_usd=1.0,
            expires_at=_EXPIRES,
            metadata={},
        )


def test_mint_requires_action_class_pin_for_hard_human(grants: GrantsStore) -> None:
    with pytest.raises(ValueError, match="action_class"):
        grants.create_grant(
            CAP,
            approval_id="apr_no_action_class",
            max_actions=1,
            max_spend_usd=1.0,
            expires_at=_EXPIRES,
            metadata={"generation": 0},
        )


def test_mint_rejects_action_class_mismatch(grants: GrantsStore) -> None:
    with pytest.raises(ValueError, match="action_class"):
        grants.create_grant(
            CAP,
            approval_id="apr_wrong_action_class",
            max_actions=1,
            max_spend_usd=1.0,
            expires_at=_EXPIRES,
            metadata={"generation": 0, "action_class": "read_only"},
        )


def test_missing_action_class_pin_on_hard_human_row_fails(
    grants: GrantsStore,
) -> None:
    grant = _mint(grants)
    grants._store._write(
        "UPDATE campaign_grants SET metadata_json = ? WHERE id = ?",
        ('{"generation":1}', grant["id"]),
    )
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        action_class="consequential",
        connector="stripe_acmeuni",
        tool="refund",
        generation=1,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "action_class_ambiguous"


def test_wrong_action_fails(grants: GrantsStore) -> None:
    grant = _mint(grants)
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability="stripe_acmeuni.charge",
        generation=1,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "capability_mismatch"


def test_wrong_connector_fails(grants: GrantsStore) -> None:
    grant = _mint(grants)
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        connector="other_bank",
        generation=1,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "connector_mismatch"


def test_wrong_tool_fails(grants: GrantsStore) -> None:
    grant = _mint(grants)
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        tool="charge",
        generation=1,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "tool_mismatch"


def test_wrong_target_fails(grants: GrantsStore) -> None:
    grant = _mint(grants)
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        action_class="consequential",
        generation=1,
        target="cus_evil",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "target_breakout"


def test_ambiguous_target_with_nonempty_set_fails(grants: GrantsStore) -> None:
    grant = _mint(grants)
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        action_class="consequential",
        generation=1,
        target=None,
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "target_ambiguous"


@pytest.mark.parametrize("bad_set", ["", 0, False, {"cus": 1}, "cus_ok"])
def test_malformed_target_set_fails(grants: GrantsStore, bad_set: object) -> None:
    import json

    grant = _mint(grants)
    grants._store._write(
        "UPDATE campaign_grants SET target_set_json = ? WHERE id = ?",
        (json.dumps(bad_set), grant["id"]),
    )
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        action_class="consequential",
        generation=1,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is False
    assert result.reason == "target_set_ambiguous"


def test_mint_rejects_non_list_target_set(grants: GrantsStore) -> None:
    with pytest.raises(ValueError, match="target_set"):
        grants.create_grant(
            CAP,
            approval_id="apr_bad_ts",
            max_actions=1,
            max_spend_usd=1.0,
            expires_at=_EXPIRES,
            target_set="not-a-list",  # type: ignore[arg-type]
            metadata={"generation": 0},
        )


def test_scoped_args_mismatch_fails(grants: GrantsStore) -> None:
    grant = _mint(grants)
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        action_class="consequential",
        generation=1,
        target="cus_ok",
        scoped_args={"currency": "eur"},
    )
    assert result.ok is False
    assert result.reason == "scoped_args_mismatch"


def test_replay_after_consume_fails(grants: GrantsStore) -> None:
    grant = _mint(grants, max_actions=1)
    first = grants.try_consume(
        grant["id"],
        capability=CAP,
        target="cus_ok",
        generation=1,
        scoped_args={"currency": "usd"},
        action_class="consequential",
        connector="stripe_acmeuni",
        tool="refund",
    )
    assert first.ok is True
    second = grants.try_consume(
        grant["id"],
        capability=CAP,
        target="cus_ok",
        generation=1,
        scoped_args={"currency": "usd"},
        action_class="consequential",
        connector="stripe_acmeuni",
        tool="refund",
    )
    assert second.ok is False
    assert second.reason == "max_actions"


def test_audit_records_denial_detail(grants: GrantsStore) -> None:
    grant = _mint(grants, max_actions=2)
    result = grants.try_consume(
        grant["id"],
        capability=CAP,
        target="cus_evil",
        generation=1,
        scoped_args={"currency": "usd"},
        action_class="consequential",
        connector="stripe_acmeuni",
        tool="refund",
    )
    assert result.ok is False
    assert result.outcome == "broke_out"
    assert result.reason == "target_breakout"
    assert result.action is not None
    assert result.action["outcome"] == "broke_out"
    assert result.action["detail"] == "target_breakout"


def test_concurrent_exactly_one_consume(grants: GrantsStore) -> None:
    import concurrent.futures

    grant = _mint(grants, max_actions=1)
    results: list[bool] = []

    def _once() -> bool:
        res = grants.try_consume(
            grant["id"],
            capability=CAP,
            target="cus_ok",
            generation=1,
            scoped_args={"currency": "usd"},
            action_class="consequential",
            connector="stripe_acmeuni",
            tool="refund",
        )
        return res.ok

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_once) for _ in range(8)]
        results = [f.result() for f in futures]

    assert sum(1 for ok in results if ok) == 1
    assert grants.get_grant(grant["id"])["actions_used"] == 1


def test_positive_scoped_grant(grants: GrantsStore) -> None:
    grant = _mint(grants)
    result = validate_approval_token(
        grant["id"],
        grant_store=grants,
        capability=CAP,
        action_class="consequential",
        connector="stripe_acmeuni",
        tool="refund",
        generation=1,
        target="cus_ok",
        scoped_args={"currency": "usd"},
    )
    assert result.ok is True
    assert result.reason == "ok"
    assert result.grant is not None
    assert result.grant["id"] == grant["id"]


def test_normalize_conflicting_refs_fail() -> None:
    ref, reason = normalize_grant_ref("gnt_a", "gnt_b")
    assert ref is None
    assert reason == "token_grant_mismatch"


@pytest.mark.parametrize(
    ("token", "gid", "reason"),
    [
        ("", "gnt_x", "token_malformed"),
        ("   ", None, "token_malformed"),
        (None, "", "token_malformed"),
        (123, None, "token_malformed"),  # type: ignore[arg-type]
        (None, 99, "token_malformed"),  # type: ignore[arg-type]
    ],
)
def test_normalize_malformed_refs(token: object, gid: object, reason: str) -> None:
    ref, got = normalize_grant_ref(token, gid)  # type: ignore[arg-type]
    assert ref is None
    assert got == reason


def test_store_raising_fails_closed() -> None:
    class _Boom:
        def get_grant(self, _gid: str) -> dict[str, Any] | None:
            raise RuntimeError("unavailable")

    result = validate_approval_token("gnt_x", grant_store=_Boom(), capability=CAP)
    assert result.ok is False
    assert result.reason == "grant_store_error"
