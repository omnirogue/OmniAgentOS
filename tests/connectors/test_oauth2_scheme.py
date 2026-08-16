"""OAuth2 auth scheme: the broker mints a Bearer from a refresh token, caches it,
and fails closed when the token endpoint refuses.

Auth spec contract (pinned):

    oauth2:GOOGLE_OAUTH_REFRESH_TOKEN:GOOGLE_OAUTH_CLIENT_ID:GOOGLE_OAUTH_CLIENT_SECRET

means the broker reads those THREE env var NAMES, POSTs grant_type=refresh_token to
https://oauth2.googleapis.com/token, caches the minted access token in-process keyed
by the refresh-token env name until ~60s before expiry, and sets
``Authorization: Bearer <access_token>`` on the outbound API request. Credentials
remain behind the broker and are never handed to an agent.

Every test here MOCKS the token endpoint -- no test ever reaches real Google.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from omniagentos.connectors import Capability, HttpSpec, broker, oauth
from omniagentos.connectors.broker import (
    BrokerDenied,
    _auth_headers,
    _scoped_resolver,
    call,
)
from omniagentos.connectors.oauth import OAuthTokenError, mint_bearer
from omniagentos.contracts import ActionClass

OAUTH_SPEC = "oauth2:GOOGLE_OAUTH_REFRESH_TOKEN:GOOGLE_OAUTH_CLIENT_ID:GOOGLE_OAUTH_CLIENT_SECRET"


class _TokenResp:
    """A stand-in for Google's token endpoint response."""

    def __init__(
        self, *, status: int = 200, access_token: str = "ya29.fake", expires_in: Any = 3600
    ) -> None:
        self.status_code = status
        self._body = {"access_token": access_token, "expires_in": expires_in}

    def json(self) -> dict[str, Any]:
        return self._body


def _set_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "1//refresh-pretend")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id-pretend.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret-pretend")


def _google_read_cap() -> Capability:
    """A callable, read-only google-group capability using the oauth2 scheme."""
    return Capability(
        id="gmail.read",
        connector="gmail",
        group="google",
        label="gmail read",
        action_class=ActionClass.READ_ONLY,
        http=HttpSpec(
            base_url="https://gmail.googleapis.com",
            methods=["GET"],
            auth=OAUTH_SPEC,
        ),
    )


def _google_resolver() -> Callable[[str], str]:
    return _scoped_resolver(_google_read_cap())


@pytest.fixture(autouse=True)
def _clear_token_cache() -> Any:
    oauth.reset_cache()
    yield
    oauth.reset_cache()


# --- parsing -----------------------------------------------------------------


def test_oauth2_spec_requires_exactly_three_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed oauth2 spec is refused as a bad auth scheme, not silently run."""
    for bad in ("oauth2:ONLY_ONE", "oauth2:A:B", "oauth2:A:B:C:D", "oauth2:A::C"):
        spec = HttpSpec(base_url="https://x.test", auth=bad)
        with pytest.raises(BrokerDenied) as exc:
            _auth_headers(spec, _google_read_cap())
        assert exc.value.reason == "bad_auth_scheme"


# --- mint + attach -----------------------------------------------------------


def test_auth_headers_mint_and_attach_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The oauth2 scheme mints a token and sets Authorization: Bearer on the request."""
    _set_oauth_env(monkeypatch)
    posts: list[dict[str, Any]] = []

    def _fake_post(url: str, **kwargs: Any) -> _TokenResp:
        posts.append({"url": url, **kwargs})
        return _TokenResp(access_token="ya29.minted")

    monkeypatch.setattr(httpx, "post", _fake_post)

    spec = HttpSpec(base_url="https://gmail.googleapis.com", auth=OAUTH_SPEC)
    headers, params, cert = _auth_headers(spec, _google_read_cap())

    assert headers["Authorization"] == "Bearer ya29.minted"
    assert params == {} and cert is None
    # Exchanged the refresh token at Google's endpoint with the right grant + creds.
    assert posts[0]["url"] == "https://oauth2.googleapis.com/token"
    assert posts[0]["data"]["grant_type"] == "refresh_token"
    assert posts[0]["data"]["refresh_token"] == "1//refresh-pretend"
    assert posts[0]["data"]["client_id"].endswith(".apps.googleusercontent.com")
    assert posts[0]["data"]["client_secret"] == "client-secret-pretend"


def test_full_call_attaches_bearer_from_minted_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: broker.call mints a token and carries it on the outbound API call."""
    _set_oauth_env(monkeypatch)
    cap = _google_read_cap()
    monkeypatch.setattr(broker, "authorize", lambda *a, **k: cap)
    monkeypatch.setattr(broker, "validate_request", lambda *a, **k: None)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _TokenResp(access_token="ya29.call"))

    captured: dict[str, Any] = {}

    class _ApiResp:
        status_code = 200
        is_success = True

        def json(self) -> dict[str, Any]:
            return {"messages": []}

    def _fake_request(method: str, url: str, **kwargs: Any) -> _ApiResp:
        captured["headers"] = kwargs.get("headers")
        return _ApiResp()

    monkeypatch.setattr(httpx, "request", _fake_request)

    out = call("gmail.read", ["gmail.read"], method="GET", path="/gmail/v1/users/me/messages")
    assert out["ok"] is True
    assert captured["headers"]["Authorization"] == "Bearer ya29.call"


# --- caching + expiry --------------------------------------------------------


def test_token_is_cached_second_call_does_not_repost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second mint within the token's lifetime serves the cache -- no re-POST."""
    _set_oauth_env(monkeypatch)
    posts: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: (posts.append(url), _TokenResp())[1])

    clock = [1000.0]
    t1 = mint_bearer(
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        _google_resolver(),
        now=lambda: clock[0],
    )
    clock[0] += 100.0  # well within the ~3540s cached window
    t2 = mint_bearer(
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        _google_resolver(),
        now=lambda: clock[0],
    )
    assert t1 == t2 == "ya29.fake"
    assert len(posts) == 1  # minted once, reused once


def test_expired_token_is_re_minted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once past (expiry - skew), the next mint re-POSTs for a fresh token."""
    _set_oauth_env(monkeypatch)
    posts: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: (posts.append(url), _TokenResp())[1])

    clock = [1000.0]
    args = (
        "GOOGLE_OAUTH_REFRESH_TOKEN",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        _google_resolver(),
    )
    mint_bearer(*args, now=lambda: clock[0])
    clock[0] += 3600.0  # past expires_in(3600) - skew(60) => stale
    mint_bearer(*args, now=lambda: clock[0])
    assert len(posts) == 2  # re-minted after expiry


# --- fail closed -------------------------------------------------------------


def test_token_endpoint_400_fails_closed_no_outbound_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-200 from the token endpoint refuses the call and issues NO API request."""
    _set_oauth_env(monkeypatch)
    cap = _google_read_cap()
    monkeypatch.setattr(broker, "authorize", lambda *a, **k: cap)
    monkeypatch.setattr(broker, "validate_request", lambda *a, **k: None)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _TokenResp(status=400))

    sent: list[Any] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must never run
        sent.append((args, kwargs))
        raise AssertionError("no API call may be issued without a valid token")

    monkeypatch.setattr(httpx, "request", _boom)

    with pytest.raises(BrokerDenied) as exc:
        call("gmail.read", ["gmail.read"], method="GET", path="/gmail/v1/users/me/messages")
    assert exc.value.reason == "oauth_token_error"
    assert sent == []  # failed closed -- nothing hit the network


def test_token_endpoint_error_raises_oauth_error_from_minter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The minter itself signals failure via OAuthTokenError (broker maps to denial)."""
    _set_oauth_env(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _TokenResp(status=401))
    with pytest.raises(OAuthTokenError):
        mint_bearer(
            "GOOGLE_OAUTH_REFRESH_TOKEN",
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            _google_resolver(),
        )


def test_missing_refresh_env_fails_closed_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent refresh-token env refuses the call before any token POST is made."""
    # Only client id/secret are set; the refresh token is deliberately absent.
    monkeypatch.delenv("GOOGLE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id-pretend")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret-pretend")

    posted: list[Any] = []

    def _boom_post(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must never run
        posted.append((args, kwargs))
        raise AssertionError("must not POST to the token endpoint with a missing secret")

    monkeypatch.setattr(httpx, "post", _boom_post)

    with pytest.raises(BrokerDenied) as exc:
        mint_bearer(
            "GOOGLE_OAUTH_REFRESH_TOKEN",
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            _google_resolver(),
        )
    assert exc.value.reason == "credential_missing"
    assert posted == []  # never reached the network
