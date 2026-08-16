"""Capability-derived credential resolution scope and counterfeit coverage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omniagentos.connectors import Capability, HttpSpec, broker
from omniagentos.connectors.broker import BrokerDenied
from omniagentos.contracts import ActionClass


def _cap(cap_id: str, connector: str) -> Capability:
    return Capability(
        id=cap_id,
        connector=connector,
        group="support",
        label=cap_id,
        action_class=ActionClass.READ_ONLY,
        http=HttpSpec(base_url=f"https://{connector}.test"),
    )


def test_resolve_for_returns_only_connector_declared_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jira = _cap("jira.read", "jira")
    registry = SimpleNamespace(
        connectors={
            "jira": SimpleNamespace(env=["JIRA_URL", "JIRA_TOKEN"]),
            "jira_admin": SimpleNamespace(env=["JIRA_ADMIN_TOKEN", "JIRA_ADMIN_URL"]),
        }
    )
    monkeypatch.setattr(broker, "load_registry", lambda: registry)
    monkeypatch.setenv("JIRA_URL", "https://jira.fixture")
    monkeypatch.setenv("JIRA_TOKEN", "fixture-jira-value")
    monkeypatch.setenv("JIRA_ADMIN_TOKEN", "fixture-admin-value")
    monkeypatch.setenv("JIRA_ADMIN_URL", "https://jira-admin.fixture")

    assert broker.resolve_for(jira) == {
        "JIRA_URL": "https://jira.fixture",
        "JIRA_TOKEN": "fixture-jira-value",
    }


def test_same_prefix_other_connector_name_is_out_of_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jira = _cap("jira.read", "jira")
    registry = SimpleNamespace(
        connectors={
            "jira": SimpleNamespace(env=["JIRA_URL", "JIRA_TOKEN"]),
            "jira_admin": SimpleNamespace(env=["JIRA_ADMIN_TOKEN", "JIRA_ADMIN_URL"]),
        }
    )
    monkeypatch.setattr(broker, "load_registry", lambda: registry)
    monkeypatch.setenv("JIRA_URL", "https://jira.fixture")
    monkeypatch.setenv("JIRA_TOKEN", "fixture-jira-value")
    monkeypatch.setenv("JIRA_ADMIN_TOKEN", "fixture-admin-value")

    resolve = broker._scoped_resolver(jira)
    with pytest.raises(BrokerDenied) as exc:
        resolve("JIRA_ADMIN_TOKEN")

    assert exc.value.reason == "env_name_out_of_scope"
    assert exc.value.cap_id == "jira.read"


def test_name_addressed_resolver_is_not_public() -> None:
    assert "resolve_for" in broker.__all__
    assert "resolve_secret" not in broker.__all__
    assert not hasattr(broker, "resolve_secret")
