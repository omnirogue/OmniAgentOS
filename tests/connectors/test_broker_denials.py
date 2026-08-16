"""Machine-routable broker denial codes and their actionable next moves."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omniagentos.api.routes import access
from omniagentos.api.routes.access import BrokerCallRequest
from omniagentos.api.services import ApiError
from omniagentos.connectors import Capability, HttpSpec, broker
from omniagentos.connectors.broker import BrokerDenied
from omniagentos.contracts import ActionClass


def _cap(*, http: HttpSpec | None) -> Capability:
    return Capability(
        id="fixture.read",
        connector="fixture",
        group="support",
        label="fixture read",
        action_class=ActionClass.READ_ONLY,
        http=http,
    )


def _registry(capability: Capability) -> SimpleNamespace:
    return SimpleNamespace(
        capability=lambda cap_id: capability,
        connectors={
            "fixture": SimpleNamespace(
                env=["FIXTURE_ACCESS_TOKEN", "FIXTURE_SECONDARY_TOKEN"]
            )
        },
    )


def test_four_denial_states_have_distinct_codes_and_next_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = _cap(
        http=HttpSpec(
            base_url="https://fixture.test",
            auth="bearer:FIXTURE_ACCESS_TOKEN",
        )
    )

    monkeypatch.setattr(broker, "load_registry", lambda: _registry(reviewed))
    monkeypatch.setenv("FIXTURE_ACCESS_TOKEN", "fixture-value")
    with pytest.raises(BrokerDenied) as not_granted:
        broker.authorize(reviewed.id, [])

    no_path_cap = _cap(http=None)
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(no_path_cap))
    with pytest.raises(BrokerDenied) as no_path:
        broker.authorize(no_path_cap.id, [no_path_cap.id])

    monkeypatch.setattr(broker, "load_registry", lambda: _registry(reviewed))
    monkeypatch.delenv("FIXTURE_ACCESS_TOKEN")
    monkeypatch.delenv("FIXTURE_SECONDARY_TOKEN", raising=False)
    with pytest.raises(BrokerDenied) as unprovisioned:
        broker.resolve_for(reviewed)

    monkeypatch.setenv("FIXTURE_SECONDARY_TOKEN", "fixture-secondary")
    with pytest.raises(BrokerDenied) as missing:
        broker.resolve_for(reviewed)

    denials = (not_granted.value, missing.value, unprovisioned.value, no_path.value)
    assert [denial.reason for denial in denials] == [
        "not_granted",
        "credential_missing",
        "capability_unprovisioned",
        "no_call_path",
    ]
    assert [denial.next_action for denial in denials] == [
        "request via CapabilityRequest",
        "operator must provision this credential name",
        "operator must provision this connector",
        "no http path reviewed for this capability",
    ]
    assert len({denial.reason for denial in denials}) == 4
    assert unprovisioned.value.reason != missing.value.reason


class _Caps:
    def resolve_token(self, token: str) -> dict[str, str]:
        assert token == "valid"
        return {"run_id": "run-fixture", "agent_id": "lane:runner.step"}

    def get_grant(self, agent_id: str) -> list[str]:
        assert agent_id == "lane:runner.step"
        return ["fixture.read"]

    def log_call(self, *args: object, **kwargs: object) -> None:
        return None


@pytest.mark.parametrize(
    ("reason", "next_action"),
    [
        ("not_granted", "request via CapabilityRequest"),
        ("credential_missing", "operator must provision this credential name"),
        ("capability_unprovisioned", "operator must provision this connector"),
        ("no_call_path", "no http path reviewed for this capability"),
    ],
)
def test_api_broker_error_payload_is_routable_without_text_parsing(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    next_action: str,
) -> None:
    def _deny(*args: object, **kwargs: object) -> None:
        raise BrokerDenied(reason, "fixture.read", "human-readable context")

    monkeypatch.setattr(access.broker, "call", _deny)

    with pytest.raises(ApiError) as caught:
        access.broker_call(
            BrokerCallRequest(capability="fixture.read"),
            _Caps(),  # type: ignore[arg-type]
            authorization="Bearer valid",
        )

    assert caught.value.code == reason
    assert caught.value.message == next_action
    assert caught.value.detail == {
        "capability_id": "fixture.read",
        "reason_code": reason,
        "next_action": next_action,
    }


def test_no_remedy_tells_an_agent_to_retry_a_provisioning_gap() -> None:
    """K8 / the balance rule: a typed refusal must be ESCAPABLE, not a loop.

    An unset environment variable never becomes set on its own, so
    "transient fault, retry after availability" is an instruction to spin
    forever — the 'stuck' outcome the balance rule forbids. Retry wording is
    reserved for the conditions that genuinely do recover without an operator:
    the local audit store coming back, and transport.
    """
    remedies = dict(broker._DENIAL_NEXT_ACTIONS)

    assert remedies["credential_missing"].startswith("operator must")
    assert remedies["capability_unprovisioned"].startswith("operator must")
    assert remedies["credential_unavailable"].startswith("operator must")

    retryable = {reason for reason, text in remedies.items() if "retry" in text.lower()}
    assert retryable == {"audit_unavailable", "audit_finalization_failed"}
    # ...and the post-request one tells the caller NOT to retry blindly.
    assert "reconcile" in remedies["audit_finalization_failed"]

    # Every distinguishable denial keeps a distinguishable remedy: a caller that
    # routes on the code alone must learn something different from each.
    assert len(set(remedies.values())) == len(remedies)
