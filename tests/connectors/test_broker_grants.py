"""H-02 / F-11: durable approval-token validation for hard-human gates.

Arbitrary truthy strings must never authorize CONSEQUENTIAL / IRREVERSIBLE
actions. Only a store-backed grant that passes existence, generation, expiry,
revocation, capability/connector/tool, target, and scoped-arg checks may pass.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Any

import pytest

from omniagentos.connectors import Capability, HttpSpec, load_registry
from omniagentos.connectors.broker import (
    BrokerDenied,
    authorize,
    authorize_with_grant,
    call,
)
from omniagentos.contracts import ActionClass
from omniagentos.db.store import SqliteStore
from omniagentos.grants import GrantsStore

CAP_ID = "stripe_acmeuni.refund"
_EXPIRES = "2099-01-01T00:00:00+00:00"


def _mint(grants: GrantsStore, **kwargs: object) -> dict:
    base: dict[str, Any] = {
        "approval_id": "apr_broker_1",
        "max_actions": 1,
        "max_spend_usd": 100.0,
        "expires_at": _EXPIRES,
        "target_set": [],
        "metadata": {"generation": 0, "action_class": "consequential"},
    }
    base.update(kwargs)
    return grants.create_grant(CAP_ID, **base)  # type: ignore[arg-type]


class _CallableHardHumanRegistry:
    def __init__(self) -> None:
        real = load_registry().capability(CAP_ID)
        self._cap = real.model_copy(
            update={
                "http": HttpSpec(
                    base_url="https://example.test",
                    methods=["POST"],
                    path_prefixes=["/refunds"],
                )
            }
        )

    def capability(self, cap_id: str):
        assert cap_id == CAP_ID
        return self._cap


@pytest.fixture(autouse=True)
def callable_hard_human(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _CallableHardHumanRegistry()
    monkeypatch.setattr("omniagentos.connectors.broker.load_registry", lambda: registry)


@pytest.fixture
def grants(tmp_path: Path) -> GrantsStore:
    return GrantsStore(SqliteStore(str(tmp_path / "broker-grants.db")))


def _stub_network(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []

    class _Resp:
        status_code = 200
        is_success = True

        def json(self) -> dict[str, Any]:
            return {"ok": True, "id": "re_1"}

    def _capture(*args: Any, **kwargs: Any) -> Any:
        sent.append((args, kwargs))
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "request", _capture)
    monkeypatch.setattr(
        "omniagentos.connectors.broker._auth_headers",
        lambda *a, **k: ({}, {}, None),
    )
    monkeypatch.setattr(
        "omniagentos.connectors.broker._auth_form",
        lambda spec, capability: {},
    )
    return sent


def test_hard_human_without_approval_or_grant_is_denied() -> None:
    with pytest.raises(BrokerDenied) as exc:
        authorize(CAP_ID, [CAP_ID])
    assert exc.value.reason == "requires_human_approval"


def test_hard_human_with_store_loaded_grant_authorizes(grants: GrantsStore) -> None:
    """Live grants must come from the store — never a fabricated grant_row."""
    grant = _mint(grants, max_actions=1)

    cap = authorize(
        CAP_ID,
        [CAP_ID],
        grant_id=grant["id"],
        grant_store=grants,
        generation=0,
    )

    assert cap.id == CAP_ID


def test_fabricated_grant_row_argument_is_rejected() -> None:
    """Regression: authorize() must not accept a caller-supplied grant_row dict.

    A money gate satisfied by a fabricated function argument is not a gate.
    """
    import inspect

    sig = inspect.signature(authorize)
    assert "grant_row" not in sig.parameters


def test_grant_id_without_store_or_checker_fails_closed() -> None:
    with pytest.raises(BrokerDenied) as exc:
        authorize(CAP_ID, [CAP_ID], grant_id="gnt_unvalidated")
    assert exc.value.reason == "invalid_approval_token"


def test_arbitrary_approval_token_string_is_rejected() -> None:
    """H-02: truthiness is not authorization."""
    with pytest.raises(BrokerDenied) as exc:
        authorize(CAP_ID, [CAP_ID], approval_token="one-shot-approval")
    assert exc.value.reason == "invalid_approval_token"


def test_approval_token_without_store_fails_closed() -> None:
    with pytest.raises(BrokerDenied) as exc:
        authorize(CAP_ID, [CAP_ID], approval_token="gnt_looks_real")
    assert exc.value.reason == "invalid_approval_token"


def test_truthy_checker_without_store_cannot_authorize() -> None:
    """A boolean callback cannot stand in for the complete durable grant contract."""
    with pytest.raises(BrokerDenied) as exc:
        authorize(
            CAP_ID,
            [CAP_ID],
            approval_token="gnt_unvalidated",
            grant_checker=lambda **_kwargs: True,
            generation=0,
        )
    assert exc.value.reason == "invalid_approval_token"
    assert "grant_store_unavailable" in (exc.value.detail or "")


def test_approval_token_validated_against_store_authorizes(grants: GrantsStore) -> None:
    grant = _mint(grants, max_actions=1)
    cap = authorize(
        CAP_ID,
        [CAP_ID],
        approval_token=grant["id"],
        grant_store=grants,
        generation=0,
    )
    assert cap.id == CAP_ID


def test_expired_token_is_rejected(grants: GrantsStore) -> None:
    grant = _mint(grants, expires_at="2000-01-01T00:00:00+00:00")
    with pytest.raises(BrokerDenied) as exc:
        authorize(
            CAP_ID,
            [CAP_ID],
            approval_token=grant["id"],
            grant_store=grants,
            generation=0,
        )
    assert exc.value.reason == "invalid_approval_token"


def test_revoked_token_is_rejected(grants: GrantsStore) -> None:
    grant = _mint(grants)
    grants.revoke_grant(grant["id"], "operator revoke")
    with pytest.raises(BrokerDenied) as exc:
        authorize(
            CAP_ID,
            [CAP_ID],
            approval_token=grant["id"],
            grant_store=grants,
            generation=0,
        )
    assert exc.value.reason == "invalid_approval_token"


def test_wrong_generation_is_rejected(grants: GrantsStore) -> None:
    grant = _mint(grants, metadata={"generation": 3, "action_class": "consequential"})
    with pytest.raises(BrokerDenied) as exc:
        authorize(
            CAP_ID,
            [CAP_ID],
            approval_token=grant["id"],
            grant_store=grants,
            generation=1,
        )
    assert exc.value.reason == "invalid_approval_token"


def test_missing_generation_presentation_is_rejected(grants: GrantsStore) -> None:
    grant = _mint(grants, metadata={"generation": 3, "action_class": "consequential"})
    with pytest.raises(BrokerDenied) as exc:
        authorize(
            CAP_ID,
            [CAP_ID],
            approval_token=grant["id"],
            grant_store=grants,
        )
    assert exc.value.reason == "invalid_approval_token"


def test_wrong_action_class_pin_is_rejected(grants: GrantsStore) -> None:
    grant = _mint(grants)
    grants._store._write(
        "UPDATE campaign_grants SET metadata_json = ? WHERE id = ?",
        ('{"generation":0,"action_class":"read_only"}', grant["id"]),
    )
    with pytest.raises(BrokerDenied) as exc:
        authorize(
            CAP_ID,
            [CAP_ID],
            approval_token=grant["id"],
            grant_store=grants,
            generation=0,
        )
    assert exc.value.reason == "invalid_approval_token"


def test_wrong_target_is_rejected(grants: GrantsStore) -> None:
    grant = _mint(grants, target_set=["cus_allowed"])
    with pytest.raises(BrokerDenied) as exc:
        authorize(
            CAP_ID,
            [CAP_ID],
            approval_token=grant["id"],
            grant_store=grants,
            target="cus_other",
            generation=0,
        )
    assert exc.value.reason == "invalid_approval_token"


def test_missing_store_on_scoped_token_fails_closed(grants: GrantsStore) -> None:
    grant = _mint(grants)
    with pytest.raises(BrokerDenied) as exc:
        authorize(CAP_ID, [CAP_ID], approval_token=grant["id"], generation=0)
    assert exc.value.reason == "invalid_approval_token"


def test_authorize_with_grant_consumes_and_then_refuses_exhausted_grant(
    grants: GrantsStore,
) -> None:
    grant = _mint(grants, max_actions=1)

    cap = authorize_with_grant(CAP_ID, [CAP_ID], grants, grant["id"], generation=0)
    assert cap.id == CAP_ID
    assert grants.get_grant(grant["id"])["actions_used"] == 1

    with pytest.raises(BrokerDenied) as exc:
        authorize_with_grant(CAP_ID, [CAP_ID], grants, grant["id"], generation=0)
    assert exc.value.reason in {
        "grant_refused",
        "requires_human_approval",
        "invalid_approval_token",
        "grant_broke_out",
    }


def test_replay_of_consumed_one_shot_grant_is_rejected(grants: GrantsStore) -> None:
    grant = _mint(grants, max_actions=1, target_set=["cus_1"])
    first = authorize_with_grant(
        CAP_ID,
        [CAP_ID],
        grants,
        grant["id"],
        target="cus_1",
        generation=0,
    )
    assert first.id == CAP_ID
    with pytest.raises(BrokerDenied) as exc:
        authorize_with_grant(
            CAP_ID,
            [CAP_ID],
            grants,
            grant["id"],
            target="cus_1",
            generation=0,
        )
    assert exc.value.reason in {"grant_refused", "invalid_approval_token"}


def test_positive_scoped_grant_authorizes_matching_target_and_args(
    grants: GrantsStore,
) -> None:
    grant = _mint(
        grants,
        max_actions=2,
        target_set=["cus_allowed"],
        metadata={
            "generation": 7,
            "action_class": "consequential",
            "connector": "stripe_acmeuni",
            "tool": "refund",
            "scoped_args": {"reason": "duplicate_charge"},
        },
    )
    cap = authorize(
        CAP_ID,
        [CAP_ID],
        approval_token=grant["id"],
        grant_store=grants,
        target="cus_allowed",
        generation=7,
        scoped_args={"reason": "duplicate_charge", "note": "ok extra"},
    )
    assert cap.id == CAP_ID


def test_money_write_rejects_truthy_token_without_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """call() money gate must not treat a free-form string as approval."""
    sent: list[Any] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        sent.append((args, kwargs))
        raise AssertionError("money write must never reach the network")

    import httpx

    monkeypatch.setattr(httpx, "request", _boom)

    with pytest.raises(BrokerDenied) as exc:
        call(
            CAP_ID,
            [CAP_ID],
            method="POST",
            path="/refunds",
            approval_token="please-approve",
        )
    assert exc.value.reason == "invalid_approval_token"
    assert sent == []


def test_store_error_fails_closed_on_authorize() -> None:
    class _Broken:
        def get_grant(self, _grant_id: str) -> dict[str, Any] | None:
            raise RuntimeError("db down")

    with pytest.raises(BrokerDenied) as exc:
        authorize(
            CAP_ID,
            [CAP_ID],
            approval_token="gnt_x",
            grant_store=_Broken(),
            generation=0,
        )
    assert exc.value.reason == "invalid_approval_token"


def test_store_error_wrapped_on_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """call() surfaces store faults as grant_store_unavailable, no network."""
    sent = _stub_network(monkeypatch)

    class _Broken:
        def get_grant(self, _grant_id: str) -> dict[str, Any] | None:
            raise RuntimeError("db down")

    with pytest.raises(BrokerDenied) as exc:
        call(
            CAP_ID,
            [CAP_ID],
            method="POST",
            path="/refunds",
            body={"amount": 1},
            approval_token="gnt_x",
            grant_store=_Broken(),
            generation=0,
        )
    assert exc.value.reason == "grant_store_unavailable"
    assert sent == []


def test_call_body_scoped_args_mismatch_denies_without_network(
    monkeypatch: pytest.MonkeyPatch,
    grants: GrantsStore,
) -> None:
    """Scoped pins bind to the request body — not a caller-side claim."""
    sent = _stub_network(monkeypatch)
    grant = _mint(
        grants,
        max_actions=1,
        metadata={
            "generation": 0,
            "action_class": "consequential",
            "scoped_args": {"currency": "usd"},
        },
    )
    with pytest.raises(BrokerDenied) as exc:
        call(
            CAP_ID,
            [CAP_ID],
            method="POST",
            path="/refunds",
            body={"currency": "eur", "amount": 5},
            approval_token=grant["id"],
            grant_store=grants,
            generation=0,
        )
    assert exc.value.reason in {"grant_refused", "invalid_approval_token"}
    assert "scoped_args" in (exc.value.detail or "")
    assert sent == []
    assert grants.get_grant(grant["id"])["actions_used"] == 0


def test_call_body_scoped_args_match_allows_once(
    monkeypatch: pytest.MonkeyPatch,
    grants: GrantsStore,
) -> None:
    sent = _stub_network(monkeypatch)
    grant = _mint(
        grants,
        max_actions=1,
        metadata={
            "generation": 0,
            "action_class": "consequential",
            "scoped_args": {"currency": "usd"},
        },
    )
    out = call(
        CAP_ID,
        [CAP_ID],
        method="POST",
        path="/refunds",
        body={"currency": "usd", "amount": 5},
        approval_token=grant["id"],
        grant_store=grants,
        generation=0,
    )
    assert out["ok"] is True
    assert len(sent) == 1


def test_call_replay_no_second_network(
    monkeypatch: pytest.MonkeyPatch,
    grants: GrantsStore,
) -> None:
    """End-to-end: one-shot grant allows exactly one network hop."""
    sent = _stub_network(monkeypatch)
    grant = _mint(grants, max_actions=1)
    first = call(
        CAP_ID,
        [CAP_ID],
        method="POST",
        path="/refunds",
        body={"amount": 1},
        approval_token=grant["id"],
        grant_store=grants,
        generation=0,
    )
    assert first["ok"] is True
    with pytest.raises(BrokerDenied) as exc:
        call(
            CAP_ID,
            [CAP_ID],
            method="POST",
            path="/refunds",
            body={"amount": 1},
            approval_token=grant["id"],
            grant_store=grants,
            generation=0,
        )
    assert exc.value.reason in {
        "grant_refused",
        "invalid_approval_token",
        "requires_human_approval",
    }
    assert len(sent) == 1


def test_concurrent_authorize_with_grant_exactly_one(grants: GrantsStore) -> None:
    grant = _mint(grants, max_actions=1)
    outcomes: list[str] = []

    def _once() -> str:
        try:
            authorize_with_grant(CAP_ID, [CAP_ID], grants, grant["id"], generation=0)
            return "ok"
        except BrokerDenied as exc:
            return exc.reason

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = [f.result() for f in (pool.submit(_once) for _ in range(6))]

    assert outcomes.count("ok") == 1
    assert grants.get_grant(grant["id"])["actions_used"] == 1


def test_soft_danger_write_rejects_truthy_token_without_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Danger-group WRITE with a soft action_class still needs durable proof."""
    soft = Capability(
        id="fake_ads.launch",
        connector="fake_ads",
        group="ads",
        label="soft danger write",
        action_class=ActionClass.INTERNAL_REVERSIBLE,
        http=HttpSpec(base_url="https://example.test", methods=["POST"]),
    )

    class _Reg:
        def capability(self, cap_id: str) -> Capability:
            assert cap_id == soft.id
            return soft

    monkeypatch.setattr("omniagentos.connectors.broker.load_registry", lambda: _Reg())
    monkeypatch.setattr(
        "omniagentos.connectors.broker._danger_groups",
        lambda: frozenset({"ads"}),
    )

    sent: list[Any] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        sent.append((args, kwargs))
        raise AssertionError("danger write must never reach the network")

    import httpx

    monkeypatch.setattr(httpx, "request", _boom)

    with pytest.raises(BrokerDenied) as exc:
        call(
            soft.id,
            [soft.id],
            method="POST",
            path="/launch",
            approval_token="looks-approved",
        )
    # Soft class skips hard-human; danger gate still fires without durable_ok.
    assert exc.value.reason == "dangerous_write_requires_approval"
    assert sent == []


def test_malformed_conflicting_refs_denied_on_call(
    monkeypatch: pytest.MonkeyPatch,
    grants: GrantsStore,
) -> None:
    sent = _stub_network(monkeypatch)
    grant = _mint(grants)
    with pytest.raises(BrokerDenied) as exc:
        call(
            CAP_ID,
            [CAP_ID],
            method="POST",
            path="/refunds",
            body={"amount": 1},
            approval_token=grant["id"],
            grant_id="gnt_other",
            grant_store=grants,
            generation=0,
        )
    assert exc.value.reason == "invalid_approval_token"
    assert sent == []
