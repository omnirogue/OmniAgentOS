"""Teller mutual-TLS (mTLS) client-certificate support for banking integration.

Tests verify:
- Client cert is attached to Teller HTTP requests
- HTTP Basic auth is set with the access token as username, empty password
- Only GET methods are used (read-only guarantee)
- Graceful degradation when cert/token are missing
- Teller accounts and transactions are collected when all auth material is present
"""

from __future__ import annotations

import base64
from datetime import date
from typing import Any

import pytest

from omniagentos.banking.collect import (
    BROKER_SUPPORTS_CLIENT_CERT,
    collect_day,
)
from omniagentos.connectors import broker
from omniagentos.connectors.broker import BrokerDenied, _auth_headers, validate_request

_DAY = date(2026, 7, 10)


def _teller_capability(auth: str):
    """The real registry capability for teller.read, with a scheme under test.

    U-R4 made credential resolution capability-addressed: ``_auth_headers`` now
    resolves through a resolver fenced to the connector that owns the name, so
    the capability is part of the call rather than an ambient env lookup.
    Building it from the LIVE registry keeps the six declared Teller env names
    in the picture — three of which the mTLS scheme never reads.
    """
    from omniagentos.connectors import load_registry

    capability = load_registry().capability("teller.read")
    assert capability.http is not None
    return capability.model_copy(update={"http": capability.http.model_copy(update={"auth": auth})})


class TestMtlsAuthScheme:
    """Test the mtls auth scheme in broker._auth_headers."""

    def test_mtls_auth_parses_cert_paths_and_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mtls scheme correctly extracts cert paths and access token from env."""
        capability = _teller_capability(
            "mtls:TELLER_CERT_PATH:TELLER_CERT_KEY_PATH:TELLER_ACCESS_TOKEN"
        )
        monkeypatch.setenv("TELLER_CERT_PATH", "/path/to/cert.pem")
        monkeypatch.setenv("TELLER_CERT_KEY_PATH", "/path/to/key.pem")
        monkeypatch.setenv("TELLER_ACCESS_TOKEN", "access_token_value_12345")
        # Deliberately NOT set: the three declared names this scheme never reads.
        # A resolver that eagerly resolved the whole connector would fail here,
        # which is exactly how every live Teller read broke.
        for unused in ("TELLER_APPLICATION_ID", "TELLER_ENV", "TELLER_API_BASE"):
            monkeypatch.delenv(unused, raising=False)

        headers, params, cert = _auth_headers(capability.http, capability)

        # Verify client cert tuple is returned
        assert cert == ("/path/to/cert.pem", "/path/to/key.pem"), (
            "cert tuple should contain paths"
        )

        # Verify Basic auth is set with token as username
        assert "Authorization" in headers
        auth_header = headers["Authorization"]
        assert auth_header.startswith("Basic "), "should use Basic auth"

        # Decode and verify the auth value
        encoded_part = auth_header.replace("Basic ", "")
        decoded = base64.b64decode(encoded_part).decode()
        assert decoded == "access_token_value_12345:", (
            "username should be token, password empty"
        )

    def test_mtls_auth_missing_cert_path_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing cert path env var raises BrokerDenied."""
        capability = _teller_capability(
            "mtls:TELLER_CERT_PATH:TELLER_CERT_KEY_PATH:TELLER_ACCESS_TOKEN"
        )
        monkeypatch.delenv("TELLER_CERT_PATH", raising=False)
        monkeypatch.setenv("TELLER_CERT_KEY_PATH", "/path/to/key.pem")
        monkeypatch.setenv("TELLER_ACCESS_TOKEN", "access_token_value_12345")

        with pytest.raises(BrokerDenied) as exc:
            _auth_headers(capability.http, capability)
        assert exc.value.reason == "credential_missing"

    def test_mtls_auth_name_outside_the_connector_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """U-R4: a scheme cannot name a credential Teller does not declare."""
        capability = _teller_capability(
            "mtls:STRIPE_SECRET_KEY:TELLER_CERT_KEY_PATH:TELLER_ACCESS_TOKEN"
        )
        monkeypatch.setenv("STRIPE_SECRET_KEY", "generated-in-test")

        with pytest.raises(BrokerDenied) as exc:
            _auth_headers(capability.http, capability)
        assert exc.value.reason == "env_name_out_of_scope"
        assert exc.value.cap_id == "teller.read"

    def test_mtls_auth_malformed_scheme_raises(self) -> None:
        """Malformed mtls scheme (missing parts) raises BrokerDenied."""
        capability = _teller_capability("mtls:ONLY_ONE_PART")

        with pytest.raises(BrokerDenied) as exc:
            _auth_headers(capability.http, capability)
        assert exc.value.reason == "bad_auth_scheme"


class TestTellerBrokerCall:
    """Test broker.call() with Teller's mTLS auth."""

    def test_broker_call_attaches_client_cert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """broker.call() passes the client cert to httpx.Client()."""

        # Mock httpx.Client to capture cert parameter and mock responses
        captured_calls = []

        class MockClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured_calls.append({"client_init": kwargs})
                self.kwargs = kwargs

            def __enter__(self) -> MockClient:
                return self

            def __exit__(self, *args: Any) -> None:
                pass

            def request(self, method: str, url: str, **kwargs: Any) -> Any:
                captured_calls.append({"method": method, "url": url, "kwargs": kwargs})

                class MockResponse:
                    status_code = 200
                    is_success = True

                    def json(self) -> dict[str, Any]:
                        return {"accounts": [{"id": "acc_1", "name": "Test"}]}

                return MockResponse()

        monkeypatch.setattr("httpx.Client", MockClient)

        # Mock env vars
        monkeypatch.setenv("TELLER_CERT_PATH", "/test/cert.pem")
        monkeypatch.setenv("TELLER_CERT_KEY_PATH", "/test/key.pem")
        monkeypatch.setenv("TELLER_ACCESS_TOKEN", "test_token_123")

        # Call the broker
        broker.call(
            "teller.read",
            granted=["teller.read"],
            method="GET",
            path="/accounts",
        )

        # Verify httpx.Client was initialized with cert
        assert len(captured_calls) >= 1
        client_init = captured_calls[0]
        assert "client_init" in client_init
        assert client_init["client_init"]["cert"] == ("/test/cert.pem", "/test/key.pem")

        # Verify the request was made with correct method and path
        request_call = next(c for c in captured_calls if "method" in c)
        assert request_call["method"] == "GET"
        assert request_call["url"] == "https://api.teller.io/accounts"

        # Verify Basic auth header is set
        assert "headers" in request_call["kwargs"]
        headers = request_call["kwargs"]["headers"]
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Basic ")

        # Decode and verify
        encoded = headers["Authorization"].replace("Basic ", "")
        decoded = base64.b64decode(encoded).decode()
        assert decoded == "test_token_123:"

    def test_broker_call_only_allows_get_on_teller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Teller.read capability is read-only: only GET method is allowed."""
        from omniagentos.connectors.broker import BrokerDenied

        monkeypatch.setenv("TELLER_CERT_PATH", "/test/cert.pem")
        monkeypatch.setenv("TELLER_CERT_KEY_PATH", "/test/key.pem")
        monkeypatch.setenv("TELLER_ACCESS_TOKEN", "test_token")

        # POST should be refused
        with pytest.raises(BrokerDenied) as exc:
            broker.call(
                "teller.read",
                granted=["teller.read"],
                method="POST",
                path="/accounts",
            )
        assert exc.value.reason == "method_not_allowed"


class TestTellerCollection:
    """Test Teller account and transaction collection."""

    def test_teller_collection_when_all_env_vars_present(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teller uses per-account paths and preserves decimal-dollar amounts."""
        monkeypatch.setenv("TELLER_CERT_PATH", "/test/cert.pem")
        monkeypatch.setenv("TELLER_CERT_KEY_PATH", "/test/key.pem")
        monkeypatch.setenv("TELLER_ACCESS_TOKEN", "test_token")

        # Clear Slash to focus on Teller
        for key in ("SLASH_API_KEY", "SLASH_API_KEY_ACMEUNI", "SLASH_API_KEY_INITECH"):
            monkeypatch.delenv(key, raising=False)

        def mock_broker_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
            assert cap == "teller.read"
            assert kwargs["method"] == "GET"
            if kwargs["path"] == "/accounts":
                return {
                    "ok": True,
                    "status": 200,
                    "body": [
                        {
                            "id": "teller_acc_1",
                            "name": "Linked Bank",
                            "last_four": "1234",
                            "type": "checking",
                            "currency": "USD",
                        },
                        {
                            "id": "teller_acc_2",
                            "name": "Reserve",
                            "last_four": "9876",
                            "type": "savings",
                            "currency": "USD",
                        },
                    ],
                }
            if kwargs["path"] == "/accounts/teller_acc_1/balances":
                return {
                    "ok": True,
                    "status": 200,
                    "body": {"available": "500.00", "ledger": "512.34"},
                }
            if kwargs["path"] == "/accounts/teller_acc_2/balances":
                return {
                    "ok": True,
                    "status": 200,
                    "body": {"available": "1200.25", "ledger": "1200.25"},
                }
            if kwargs["path"] == "/accounts/teller_acc_1/transactions":
                return {
                    "ok": True,
                    "status": 200,
                    "body": [
                        {
                            "id": "tx1",
                            "account_id": "teller_acc_1",
                            "amount": "1000.50",
                            "date": "2026-07-10",
                            "description": "Deposit",
                            "details": {"category": "income"},
                            "status": "posted",
                        },
                        {
                            "id": "tx2",
                            "account_id": "teller_acc_1",
                            "amount": "-12.34",
                            "date": "2026-07-10",
                            "description": "Lunch",
                            "details": {"category": "dining"},
                            "status": "posted",
                        },
                        {
                            "id": "tx-old",
                            "account_id": "teller_acc_1",
                            "amount": "-99.00",
                            "date": "2026-07-09",
                            "description": "Prior day",
                            "details": {},
                            "status": "posted",
                        },
                    ],
                }
            if kwargs["path"] == "/accounts/teller_acc_2/transactions":
                return {"ok": True, "status": 200, "body": []}
            raise AssertionError(f"unexpected path {kwargs['path']}")

        import omniagentos.banking.collect as collect_module

        monkeypatch.setattr(collect_module.broker, "call", mock_broker_call)

        result = collect_day(store, target_day=_DAY)

        # Verify Teller account was collected
        teller_accounts = [ac for ac in result.accounts if ac.account.provider == "teller"]
        assert len(teller_accounts) == 2
        teller_ac = next(ac for ac in teller_accounts if ac.account.id == "teller:teller_acc_1")
        assert teller_ac.account.name == "Linked Bank"
        assert teller_ac.fact.balance_usd == 500.0
        assert teller_ac.fact.deposits_usd == 1000.5
        assert teller_ac.fact.expenses_usd == 12.34
        assert teller_ac.fact.net_flow_usd == 988.16
        assert teller_ac.fact.txn_count == 2
        assert teller_ac.fact.meta["source_status"] == "ok"
        assert [txn.amount_usd for txn in teller_ac.transactions] == [1000.5, -12.34]

    def test_teller_shape_mismatch_degrades_to_zero_with_note(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key, value in (
            ("TELLER_CERT_PATH", "/test/cert.pem"),
            ("TELLER_CERT_KEY_PATH", "/test/key.pem"),
            ("TELLER_ACCESS_TOKEN", "test_token"),
        ):
            monkeypatch.setenv(key, value)
        for key in ("SLASH_API_KEY", "SLASH_API_KEY_ACMEUNI", "SLASH_API_KEY_INITECH"):
            monkeypatch.delenv(key, raising=False)

        def mock_broker_call(cap: str, _granted: list[str], **kwargs: Any) -> dict[str, Any]:
            assert cap == "teller.read"
            if kwargs["path"] == "/accounts":
                return {"ok": True, "status": 200, "body": [{"id": "bad", "name": "Bad"}]}
            if kwargs["path"].endswith("/balances"):
                return {"ok": True, "status": 200, "body": {"balance": "500.00"}}
            return {"ok": True, "status": 200, "body": {"unexpected": []}}

        import omniagentos.banking.collect as collect_module

        monkeypatch.setattr(collect_module.broker, "call", mock_broker_call)
        result = collect_day(store, target_day=_DAY)

        teller_ac = next(ac for ac in result.accounts if ac.account.provider == "teller")
        assert teller_ac.fact.balance_usd == 0.0
        assert teller_ac.fact.deposits_usd == 0.0
        assert teller_ac.fact.expenses_usd == 0.0
        assert teller_ac.fact.meta["source_status"] == "partial"
        assert len(teller_ac.fact.meta["data_quality"]) == 2
        assert any("source_status=partial" in note.message for note in result.notes)

    def test_teller_collection_skips_when_cert_missing(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When cert/token are missing, Teller is skipped with a data_quality note."""
        # Clear all Teller env vars
        for key in ("TELLER_CERT_PATH", "TELLER_CERT_KEY_PATH", "TELLER_ACCESS_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        # Clear Slash to focus on Teller
        for key in ("SLASH_API_KEY", "SLASH_API_KEY_ACMEUNI", "SLASH_API_KEY_INITECH"):
            monkeypatch.delenv(key, raising=False)

        def mock_broker_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("no broker call should happen when cert is missing")

        import omniagentos.banking.collect as collect_module

        monkeypatch.setattr(collect_module.broker, "call", mock_broker_call)

        result = collect_day(store, target_day=_DAY)

        # Verify Teller was skipped
        assert not any(ac.account.provider == "teller" for ac in result.accounts)
        assert any("Teller" in n.message for n in result.notes)

    def test_teller_collection_skips_when_token_missing(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When access token is missing, Teller is skipped even if cert is present."""
        monkeypatch.setenv("TELLER_CERT_PATH", "/test/cert.pem")
        monkeypatch.setenv("TELLER_CERT_KEY_PATH", "/test/key.pem")
        # Missing: TELLER_ACCESS_TOKEN

        # Clear Slash
        for key in ("SLASH_API_KEY", "SLASH_API_KEY_ACMEUNI", "SLASH_API_KEY_INITECH"):
            monkeypatch.delenv(key, raising=False)

        def mock_broker_call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("no broker call should happen when token is missing")

        import omniagentos.banking.collect as collect_module

        monkeypatch.setattr(collect_module.broker, "call", mock_broker_call)

        result = collect_day(store, target_day=_DAY)

        # Verify Teller was skipped
        assert not any(ac.account.provider == "teller" for ac in result.accounts)
        assert any("Teller" in n.message and "not configured" in n.message for n in result.notes)


class TestTellerReadOnlyGuarantee:
    """Test that Teller remains read-only: no write operations."""

    def test_teller_read_capability_is_get_only(self) -> None:
        """Teller.read is declared with GET-only methods."""
        from omniagentos.connectors import load_registry

        registry = load_registry()
        teller_read = registry.capability("teller.read")

        # Verify read-only
        assert teller_read.action_class.value == "read_only"
        assert teller_read.http.methods == ["GET"]
        assert teller_read.http.path_prefixes == []
        assert teller_read.http.path_regex == (
            "^/accounts$|^/accounts/[^/]+/balances$|^/accounts/[^/]+/transactions$"
        )

        for path in (
            "/accounts",
            "/accounts/acc_1/balances",
            "/accounts/acc_1/transactions",
        ):
            validate_request(teller_read, "GET", path)
        for path in (
            "/accounts/acc_1",
            "/accounts/acc_1/balances/details",
            "/accounts/acc_1/routing",
            "/accounts-extra",
        ):
            with pytest.raises(BrokerDenied, match="path_not_allowed"):
                validate_request(teller_read, "GET", path)

    def test_no_teller_write_capability_exists(self) -> None:
        """Teller has no write, transfer, or payment capabilities."""
        from omniagentos.connectors import ConnectorError, load_registry

        registry = load_registry()

        # Verify no write capabilities are declared
        for cap_id in ["teller.write", "teller.transfer", "teller.payment"]:
            with pytest.raises(ConnectorError):
                registry.capability(cap_id)


class TestBrokerSupportsFlagUpdate:
    """Test that BROKER_SUPPORTS_CLIENT_CERT flag is now True."""

    def test_broker_supports_client_cert_is_true(self) -> None:
        """BROKER_SUPPORTS_CLIENT_CERT should be True after implementation."""
        assert BROKER_SUPPORTS_CLIENT_CERT is True, (
            "broker should now support client certificates after mtls implementation"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
