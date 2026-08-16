"""Google-style OAuth2 access-token minting for the broker.

This is the ONE auth scheme that fetches a credential over the network *before*
the real API call. It lives beside ``broker.py`` (not in a new top-level package)
and, like the broker, treats the three OAuth env vars as secret material: they are
read, POSTed to Google's token endpoint, and never logged or returned to a caller.

The long-lived REFRESH token is exchanged for a short-lived ACCESS token, which is
CACHED in-process keyed by the refresh-token env NAME (a stable, non-secret handle
-- never the value) until ~60s before it expires. A burst of API calls therefore
mints one token, not one per call. A lock guards the cache because the broker runs
under a threaded server (uvicorn dispatches sync handlers on a worker pool).

Fail-closed is the contract: any non-200 or malformed token response raises
``OAuthTokenError`` and NO access token is produced, so the broker refuses the API
call rather than issuing it unauthenticated. Expiry is measured with
``time.monotonic()`` (a steady clock immune to wall-clock jumps).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

# Google's OAuth2 token endpoint. The refresh_token grant is exchanged here.
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Refresh a cached token this many seconds BEFORE its stated expiry, so a token
# that is technically still valid but about to lapse is never handed to a call
# that might outlive it.
EXPIRY_SKEW_SECONDS = 60.0


class OAuthTokenError(RuntimeError):
    """The token endpoint would not mint an access token. The broker fails closed."""


@dataclass
class _CachedToken:
    access_token: str
    expiry_monotonic: float


# Keyed by the refresh-token ENV NAME, not its value. The name is a stable,
# non-secret handle; the value never appears as a dict key or in a log line.
_cache: dict[str, _CachedToken] = {}
_lock = threading.Lock()


def reset_cache() -> None:
    """Drop every cached token. Test hook only -- not part of the call path."""
    with _lock:
        _cache.clear()


def mint_bearer(
    refresh_env: str,
    client_id_env: str,
    client_secret_env: str,
    resolve_for_connector: Callable[[str], str],
    *,
    now: Callable[[], float] = time.monotonic,
    timeout: float = 30.0,
) -> str:
    """Return a valid Google access token for ``refresh_env``.

    Serves a still-fresh cached token when one exists; otherwise resolves the three
    secrets, POSTs ``grant_type=refresh_token`` to Google, caches the minted token
    keyed by ``refresh_env`` until ~60s before expiry, and returns it.

    Fails closed: raises ``OAuthTokenError`` on any transport error, non-200, or
    malformed/empty token body -- the caller never receives a token, so the outbound
    API call is refused. The injected resolver must be fenced to the caller's
    connector; a missing or out-of-scope name propagates its broker denial before
    any network request is made.
    """
    key = refresh_env
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached.expiry_monotonic > now():
            return cached.access_token

    # Resolve BEFORE the network call. A missing env raises through the scoped resolver
    # (broker's credential_missing denial), so we never POST a half-formed request.
    refresh_token = resolve_for_connector(refresh_env)
    client_id = resolve_for_connector(client_id_env)
    client_secret = resolve_for_connector(client_secret_env)

    import httpx

    try:
        response = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise OAuthTokenError(f"token endpoint transport error: {exc}") from exc

    if response.status_code != 200:
        # Fail closed: an error from the token endpoint means NO token. Do not leak
        # the response body (it may echo request material); the status is enough.
        raise OAuthTokenError(
            f"token endpoint returned HTTP {response.status_code}; "
            "refusing to proceed without an access token"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthTokenError("token endpoint returned a non-JSON body") from exc

    access_token = payload.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise OAuthTokenError("token endpoint response contained no access_token")

    try:
        ttl = float(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        ttl = 3600.0

    # Clamp to >= 0 so a tiny/absurd expires_in just means "re-mint next time".
    expiry = now() + max(0.0, ttl - EXPIRY_SKEW_SECONDS)
    with _lock:
        _cache[key] = _CachedToken(access_token=access_token, expiry_monotonic=expiry)
    return access_token


__all__ = ["TOKEN_ENDPOINT", "OAuthTokenError", "mint_bearer", "reset_cache"]
