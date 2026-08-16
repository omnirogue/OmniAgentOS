#!/usr/bin/env python3
"""probes.py — read-only liveness/auth probes for the revenue-critical services.

Scope and safety contract
-------------------------
Every probe in this module is READ-ONLY: balance, identity, count, verify, and
health endpoints only.  No probe may charge, refund, mutate, subscribe, send, or
create anything.  A service that has no safe read endpoint is registered with
``verification: {"type": "unset"}`` and stays UNVERIFIED forever rather than
being given an invented write probe (``slack-webhook-ops-alert`` is the worked
example: posting to a webhook is a send, so it is not probed).

Credentials are referenced by VARIABLE NAME only.  A probe result records the
variable names it consulted, the HTTP class it observed, and a detail string
drawn from a fixed vocabulary built in this file.  No credential value, and no
vendor response body, is ever placed in a result, printed, or logged.

State vocabulary (matches ~/.omniagentos/ops/bin/integrations-probe.py, plus UNVERIFIED)
-------------------------------------------------------------------------------
UP          the service answered and the credential was accepted
DOWN        network failure, timeout, or 5xx — the vendor or the path is broken
SUSPENDED   401/403 (credential/account) or 402 (BILLING, flagged separately)
NO-CRED     the credential variable is absent from connections.env
UNVERIFIED  the probe could not evaluate: rate limited, an unexpected 4xx that
            indicts the probe rather than the service, or this host being
            outside a vendor IP allowlist.  UNVERIFIED never reads as green.

Exit codes (consumed by the capmap registry's exit_semantics)
-------------------------------------------------------------
0  UP                     -> ok
2  DOWN                   -> down
3  SUSPENDED (credential) -> down for payment/auth capabilities, degraded otherwise
4  SUSPENDED (BILLING/402)-> down for payment/auth capabilities, degraded otherwise
5  NO-CRED                -> cannot_evaluate
6  UNVERIFIED             -> deliberately UNMAPPED in exit_semantics, so the store
                             resolves it to UNVERIFIED and it can never be green.

Two deliberate design decisions, both learned from measured estate incidents:

1. An explicit User-Agent is set on EVERY request.  All three PiedPiper
   integrations sat SUSPENDED for weeks because the default ``Python-urllib``
   UA drew a 403 from the vendor edge — an instrument error rendered as an
   account failure.  A probe must never blame a service for its own fingerprint.

2. Several vendors report authentication failure with HTTP 200 (Vandelay
   ``result=ERROR``, Slack ``ok=false``, NMI ``<error_response>``) or with an
   HTTP 400 (Meta's OAuthException 190 for an expired token).  Status-code-only
   classification would have reported all of those as healthy.  Each such probe
   therefore inspects a NAMED FIELD of the response and maps it explicitly; the
   body itself is never retained.

Every verdict in this file was validated against a live control before shipping:
for each refusal, the SAME probe code, the SAME User-Agent, and the SAME second
was run with a different credential for that vendor, and returned 2xx.  A
refusal that survives that control is the account, not the instrument.
"""

import argparse
import base64
import datetime
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = "/Users/youruser"
ENV_FILE = HOME + "/.config/omni/connections.env"
USER_AGENT = "omni-capmap-probe/1.0 (+estate-capability-map; read-only)"
TIMEOUT_SECONDS = 8
MAX_BODY_BYTES = 65536
PROBES_PATH = "/Users/youruser/OmniAgentOS/tools/capmap/probes.py"

UP = "UP"
DOWN = "DOWN"
SUSPENDED = "SUSPENDED"
NO_CRED = "NO-CRED"
UNVERIFIED = "UNVERIFIED"

EXIT_UP = 0
EXIT_DOWN = 2
EXIT_SUSPENDED = 3
EXIT_SUSPENDED_BILLING = 4
EXIT_NO_CRED = 5
EXIT_UNVERIFIED = 6

# Worst-first, for --all's rollup exit code.
_SEVERITY = {EXIT_SUSPENDED_BILLING: 5, EXIT_SUSPENDED: 4, EXIT_DOWN: 3,
             EXIT_NO_CRED: 2, EXIT_UNVERIFIED: 1, EXIT_UP: 0}

# Payment and identity capabilities: a refused credential means the money or
# login path is not working, so SUSPENDED resolves to "down" rather than
# "degraded".  Supporting services keep "degraded".
SEMANTICS_CRITICAL = {"0": "ok", "2": "down", "3": "down", "4": "down",
                      "5": "cannot_evaluate"}
SEMANTICS_SUPPORTING = {"0": "ok", "2": "down", "3": "degraded", "4": "degraded",
                        "5": "cannot_evaluate"}
SEMANTICS_NOTE = ("Exit 6 (UNVERIFIED: rate limited, probe-side 4xx, or this host "
                  "outside a vendor IP allowlist) is intentionally unmapped so the "
                  "store resolves it to UNVERIFIED and it can never read as green.")


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------

def load_env(path=ENV_FILE):
    """Return connections.env as a mapping, with os.environ as a fallback layer.

    Values are never printed; only ``.get`` presence and length are ever used
    by callers in this module.
    """
    env = dict(os.environ)
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                env[name.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


# --------------------------------------------------------------------------
# transport (injectable; tests never touch the network)
# --------------------------------------------------------------------------

class HttpRequest:
    """A read-only HTTP request. ``method`` is GET unless a vendor demands POST."""

    def __init__(self, url, method="GET", headers=None, body=None, client_cert=None):
        self.url = url
        self.method = method
        self.headers = dict(headers or {})
        self.headers.setdefault("User-Agent", USER_AGENT)
        self.body = body
        self.client_cert = client_cert  # (cert_path, key_path) for mTLS probes

    def __repr__(self):  # pragma: no cover - debugging aid, never logs headers
        return f"HttpRequest({self.method} {self.url})"


class HttpResponse:
    """``status`` is None exactly when the request never produced one."""

    def __init__(self, status=None, body=b"", error=None):
        self.status = status
        self.body = body or b""
        self.error = error


def urllib_transport(request, timeout=TIMEOUT_SECONDS):
    """Perform one request.  Transport failures become ``error``, never an exception."""
    context = ssl.create_default_context()
    if request.client_cert:
        try:
            context.load_cert_chain(request.client_cert[0], request.client_cert[1])
        except (OSError, ssl.SSLError) as exc:
            return HttpResponse(error="client_cert_" + type(exc).__name__)
    body = request.body.encode("utf-8") if isinstance(request.body, str) else request.body
    native = urllib.request.Request(request.url, data=body, headers=request.headers,
                                    method=request.method)
    try:
        with urllib.request.urlopen(native, timeout=timeout, context=context) as response:
            return HttpResponse(status=response.status, body=response.read(MAX_BODY_BYTES))
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(MAX_BODY_BYTES)
        except OSError:
            payload = b""
        return HttpResponse(status=exc.code, body=payload)
    except Exception as exc:  # noqa: BLE001 - any transport failure is DOWN, not a crash
        return HttpResponse(error=type(exc).__name__)


# --------------------------------------------------------------------------
# result construction
# --------------------------------------------------------------------------

def _result(spec, state, detail, exit_code, http_status=None, billing=False):
    return {
        "probe": spec["id"],
        "service": spec["service"],
        "company": spec["company"],
        "state": state,
        "detail": detail,
        "exit_code": exit_code,
        "billing": bool(billing),
        "http_status": http_status,
        "credential_vars": list(spec["vars"]),
    }


def no_cred(spec, missing):
    return _result(spec, NO_CRED, "credential variable unset: " + ",".join(sorted(missing)),
                   EXIT_NO_CRED)


def unverified(spec, detail, http_status=None):
    return _result(spec, UNVERIFIED, detail, EXIT_UNVERIFIED, http_status)


def classify(spec, response):
    """Default status-class mapping.  Body-sensitive probes refine this."""
    if response.status is None:
        return _result(spec, DOWN, "network %s" % (response.error or "unknown"), EXIT_DOWN)
    status = response.status
    if 200 <= status < 300:
        return _result(spec, UP, f"http {status}", EXIT_UP, status)
    if status == 402:
        return _result(spec, SUSPENDED, "http 402 billing", EXIT_SUSPENDED_BILLING,
                       status, billing=True)
    if status in (401, 403):
        return _result(spec, SUSPENDED, f"http {status} credential", EXIT_SUSPENDED, status)
    if status == 429:
        return unverified(spec, "http 429 rate limited", status)
    if status >= 500:
        return _result(spec, DOWN, f"http {status}", EXIT_DOWN, status)
    return unverified(spec, f"http {status} unexpected: probe endpoint suspect", status)


def _json_body(response):
    try:
        parsed = json.loads(response.body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _text_body(response):
    return response.body.decode("utf-8", "replace")


def _creds(spec, env, names=None):
    """Return (values, missing) without ever exposing a value to a caller's output."""
    names = names or spec["vars"]
    values = {}
    missing = []
    for name in names:
        value = env.get(name)
        if value:
            values[name] = value
        else:
            missing.append(name)
    return values, missing


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------

def probe_stripe(spec, env, transport):
    """GET /v1/balance — read-only; never touches charges, refunds, or customers."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    key = creds[spec["vars"][0]]
    response = transport(HttpRequest("https://api.stripe.com/v1/balance",
                                     headers={"Authorization": "Bearer " + key}))
    return classify(spec, response)


def probe_clerk(spec, env, transport):
    """GET /v1/users/count — a scalar count, no user records are read."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    key = creds[spec["vars"][0]]
    response = transport(HttpRequest("https://api.clerk.com/v1/users/count",
                                     headers={"Authorization": "Bearer " + key}))
    return classify(spec, response)


def probe_meta(spec, env, transport):
    """GET /{version}/me — identity only.

    Meta reports an expired or revoked token as HTTP 400 with an OAuthException
    body, not as 401.  Left to the default classifier that would read as
    "probe endpoint suspect"; it is mapped explicitly to SUSPENDED here, which
    is the entire reason ad-token expiry is currently invisible.
    """
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    token = creds[spec["vars"][0]]
    version = env.get("META_GRAPH_VERSION") or "v19.0"
    url = f"https://graph.facebook.com/{urllib.parse.quote(version)}/me?fields=id"
    response = transport(HttpRequest(url, headers={"Authorization": "Bearer " + token}))
    if response.status == 400:
        payload = _json_body(response) or {}
        error = payload.get("error") or {}
        if error.get("type") == "OAuthException" or error.get("code") == 190:
            return _result(spec, SUSPENDED, "http 400 oauth token invalid or expired",
                           EXIT_SUSPENDED, 400)
    return classify(spec, response)


def probe_paypal(spec, env, transport):
    """POST /v1/oauth2/token — issues a read scope token; moves no money."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    client_id = creds[spec["vars"][0]]
    secret = creds[spec["vars"][1]]
    basic = base64.b64encode((f"{client_id}:{secret}").encode()).decode("ascii")
    response = transport(HttpRequest(
        "https://api-m.paypal.com/v1/oauth2/token", method="POST",
        headers={"Authorization": "Basic " + basic, "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        body="grant_type=client_credentials"))
    return classify(spec, response)


def probe_vandelay(spec, env, transport):
    """GET /campaign/query/ — a read query.  Vandelay answers HTTP 200 for
    failures too, so the JSON ``result`` field is authoritative, not the status."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    query = urllib.parse.urlencode({"loginId": creds[spec["vars"][0]],
                                    "password": creds[spec["vars"][1]]})
    response = transport(HttpRequest("https://api.vandelay.example/campaign/query/?" + query))
    if response.status == 200:
        payload = _json_body(response) or {}
        result = str(payload.get("result", "")).upper()
        message = str(payload.get("message", "")).lower()
        if result == "SUCCESS":
            return _result(spec, UP, "http 200 result=success", EXIT_UP, 200)
        if "whitelist" in message or "ip must be" in message:
            return unverified(spec, "http 200 result=error: probe host not ip allowlisted", 200)
        if result == "ERROR":
            return _result(spec, SUSPENDED, "http 200 result=error credential",
                           EXIT_SUSPENDED, 200)
        return unverified(spec, "http 200 unrecognised result envelope", 200)
    return classify(spec, response)


def probe_nmi(spec, env, transport):
    """POST query.php — the NMI READ API.

    Guarded twice: the configured URL must be a ``query.php`` endpoint (never
    ``transact.php``, which charges), and the query window is pinned to a future
    date so no transaction data is returned.  NMI answers HTTP 200 with an
    ``<error_response>`` element for a bad key, so the body is authoritative.
    """
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    url = creds[spec["vars"][0]]
    key = creds[spec["vars"][1]]
    if not url.rstrip("/").endswith("query.php"):
        return unverified(spec, "configured url is not a query.php read endpoint")
    body = urllib.parse.urlencode({"security_key": key, "start_date": "20990101000000"})
    response = transport(HttpRequest(
        url, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body))
    if response.status == 200:
        text = _text_body(response)
        if "<error_response>" in text:
            return _result(spec, SUSPENDED, "http 200 error_response: api key rejected",
                           EXIT_SUSPENDED, 200)
        if "<nm_response>" in text:
            return _result(spec, UP, "http 200 nm_response", EXIT_UP, 200)
        return unverified(spec, "http 200 unrecognised response envelope", 200)
    return classify(spec, response)


def probe_fanbasis_liveness(spec, env, transport):
    """GET /api/health — LIVENESS ONLY.

    Fanbasis exposes no documented unauthenticated-safe endpoint that validates
    the API key, so this proves the API is serving and nothing more.  The API
    key variable is recorded for provenance but its validity is NOT asserted;
    the capability entry says so in what_it_does rather than overclaiming.
    """
    creds, missing = _creds(spec, env, [spec["vars"][0]])
    if missing:
        return no_cred(spec, missing)
    base = creds[spec["vars"][0]].rstrip("/")
    response = transport(HttpRequest(base + "/api/health",
                                     headers={"Accept": "application/json"}))
    if response.status == 200:
        payload = _json_body(response) or {}
        if str(payload.get("status", "")).lower() == "ok":
            return _result(spec, UP, "http 200 health status=ok", EXIT_UP, 200)
        return unverified(spec, "http 200 unrecognised health envelope", 200)
    return classify(spec, response)


def probe_teller(spec, env, transport):
    """GET /accounts over mutual TLS — read-only bank feed identity check."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    cert = creds[spec["vars"][0]]
    key = creds[spec["vars"][1]]
    token = creds[spec["vars"][2]]
    base = (env.get("TELLER_API_BASE") or "https://api.teller.io").rstrip("/")
    basic = base64.b64encode((token + ":").encode("utf-8")).decode("ascii")
    response = transport(HttpRequest(base + "/accounts",
                                     headers={"Authorization": "Basic " + basic,
                                              "Accept": "application/json"},
                                     client_cert=(cert, key)))
    if response.status is None and str(response.error or "").startswith("client_cert_"):
        return unverified(spec, "client certificate could not be loaded")
    return classify(spec, response)


def _decode_cert_not_after(path):
    """Return the certificate's notAfter as an aware UTC datetime, or None."""
    try:
        decoded = ssl._ssl._test_decode_cert(path)  # noqa: SLF001 - stdlib x509 reader
    except Exception:  # noqa: BLE001 - unreadable/invalid cert is handled by the caller
        return None
    raw = (decoded or {}).get("notAfter")
    if not raw:
        return None
    text = raw.replace(" GMT", "").strip()
    for fmt in ("%b %d %H:%M:%S %Y", "%b %d %H:%M:%S %Y"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def probe_teller_cert(spec, env, transport, now=None):
    """Local expiry check on the Teller client certificate.

    A silent certificate expiry zeroes the banking dashboards with no API error
    until the moment it stops working, so expiry is a first-class capability
    with its own warning band rather than a footnote on the API probe.
    """
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    path = creds[spec["vars"][0]]
    if not os.path.exists(path):
        return no_cred(spec, [spec["vars"][0]])
    not_after = _decode_cert_not_after(path)
    if not_after is None:
        return unverified(spec, "certificate present but notAfter could not be decoded")
    current = now or datetime.datetime.now(datetime.timezone.utc)
    days = int((not_after - current).total_seconds() // 86400)
    if days < 0:
        return _result(spec, DOWN, f"client certificate expired {abs(days)} days ago",
                       EXIT_DOWN)
    if days <= 21:
        return _result(spec, SUSPENDED, f"client certificate expires in {days} days",
                       EXIT_SUSPENDED)
    return _result(spec, UP, f"client certificate valid for {days} days", EXIT_UP)


def probe_cloudflare(spec, env, transport):
    """GET /user/tokens/verify — the vendor's own read-only token check."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    token = creds[spec["vars"][0]]
    response = transport(HttpRequest("https://api.cloudflare.com/client/v4/user/tokens/verify",
                                     headers={"Authorization": "Bearer " + token,
                                              "Accept": "application/json"}))
    payload = _json_body(response) or {}
    if response.status == 200 and payload.get("success") is True:
        return _result(spec, UP, "http 200 token active", EXIT_UP, 200)
    if response.status in (400, 401, 403) and payload.get("success") is False:
        return _result(spec, SUSPENDED, f"http {response.status} token rejected",
                       EXIT_SUSPENDED, response.status)
    return classify(spec, response)


def probe_zendesk(spec, env, transport):
    """GET /api/v2/users/me.json — identity of the API user only."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    subdomain, email, token = (creds[name] for name in spec["vars"])
    basic = base64.b64encode((f"{email}/token:{token}").encode()).decode("ascii")
    response = transport(HttpRequest(
        f"https://{urllib.parse.quote(subdomain)}.zendesk.com/api/v2/users/me.json",
        headers={"Authorization": "Basic " + basic, "Accept": "application/json"}))
    return classify(spec, response)


def probe_customerio(spec, env, transport):
    """GET /v1/segments — App API read; sends no events and mutates no people."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    response = transport(HttpRequest("https://api.customer.io/v1/segments",
                                     headers={"Authorization": "Bearer " + creds[spec["vars"][0]],
                                              "Accept": "application/json"}))
    return classify(spec, response)


def probe_clickup(spec, env, transport):
    """GET /api/v2/user — identity only."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    response = transport(HttpRequest("https://api.clickup.com/api/v2/user",
                                     headers={"Authorization": creds[spec["vars"][0]],
                                              "Accept": "application/json"}))
    return classify(spec, response)


def probe_jira(spec, env, transport):
    """GET /rest/api/3/myself — identity only."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    base, email, token = (creds[name] for name in spec["vars"])
    basic = base64.b64encode((f"{email}:{token}").encode()).decode("ascii")
    response = transport(HttpRequest(base.rstrip("/") + "/rest/api/3/myself",
                                     headers={"Authorization": "Basic " + basic,
                                              "Accept": "application/json"}))
    return classify(spec, response)


def probe_slack_bot(spec, env, transport):
    """GET auth.test — identity only.  Slack answers HTTP 200 with ``ok:false``
    for a revoked or invalid token, so the ``ok`` field is authoritative."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    response = transport(HttpRequest("https://slack.com/api/auth.test",
                                     headers={"Authorization": "Bearer " + creds[spec["vars"][0]],
                                              "Accept": "application/json"}))
    if response.status == 200:
        payload = _json_body(response) or {}
        if payload.get("ok") is True:
            return _result(spec, UP, "http 200 ok=true", EXIT_UP, 200)
        error = str(payload.get("error", "")).lower()
        if error in ("invalid_auth", "not_authed", "account_inactive", "token_revoked",
                     "token_expired", "no_permission"):
            return _result(spec, SUSPENDED, "http 200 ok=false auth", EXIT_SUSPENDED, 200)
        return unverified(spec, "http 200 ok=false non-auth", 200)
    return classify(spec, response)


def probe_bunny(spec, env, transport):
    """GET /pullzone (first page, one item) — read-only inventory."""
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    response = transport(HttpRequest("https://api.bunny.net/pullzone?page=1&perPage=1",
                                     headers={"AccessKey": creds[spec["vars"][0]],
                                              "Accept": "application/json"}))
    return classify(spec, response)


def probe_vultr(spec, env, transport):
    """GET /v2/account — read-only.

    Vultr enforces an API IP allowlist and refuses a non-allowlisted source with
    401 "Unauthorized IP address" even when the key is valid.  That is an
    instrument condition of the probe host, not a dead credential, so it
    resolves UNVERIFIED — reporting it as SUSPENDED would send whoever responds
    to rotate a perfectly good key.
    """
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    response = transport(HttpRequest("https://api.vultr.com/v2/account",
                                     headers={"Authorization": "Bearer " + creds[spec["vars"][0]],
                                              "Accept": "application/json"}))
    if response.status == 401:
        payload = _json_body(response) or {}
        if "unauthorized ip" in str(payload.get("error", "")).lower():
            return unverified(spec, "http 401 probe host not ip allowlisted", 401)
    return classify(spec, response)


def probe_ovh(spec, env, transport):
    """GET /me on the OVH EU API — identity only.

    OVH signs each call with a timestamp.  The unauthenticated ``/auth/time``
    endpoint is used for it rather than the local clock, because clock skew
    would otherwise manufacture a false credential failure; that preflight is
    unauthenticated and does not count against the service's rate budget.
    """
    creds, missing = _creds(spec, env)
    if missing:
        return no_cred(spec, missing)
    application_key, application_secret, consumer_key = (creds[name] for name in spec["vars"])
    region = (env.get("OVH_API_REGION") or "eu").strip().lower()
    base = f"https://{urllib.parse.quote(region)}.api.ovh.com/1.0"
    clock = transport(HttpRequest(base + "/auth/time"))
    if clock.status != 200:
        return unverified(spec, "ovh time preflight failed", clock.status)
    timestamp = _text_body(clock).strip()
    if not timestamp.isdigit():
        return unverified(spec, "ovh time preflight returned a non-numeric clock")
    url = base + "/me"
    import hashlib  # local import: only this probe needs a digest

    raw = "+".join([application_secret, consumer_key, "GET", url, "", timestamp])
    signature = "$1$" + hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324 - vendor spec
    response = transport(HttpRequest(url, headers={
        "X-Ovh-Application": application_key, "X-Ovh-Consumer": consumer_key,
        "X-Ovh-Timestamp": timestamp, "X-Ovh-Signature": signature,
        "Accept": "application/json"}))
    return classify(spec, response)


def probe_unset(spec, env, transport):
    """No safe read endpoint exists — never returns anything but UNVERIFIED."""
    return unverified(spec, spec.get("unverified_reason", "no safe read-only probe exists"))


# --------------------------------------------------------------------------
# probe registry — one entry per capability
# --------------------------------------------------------------------------

def _stripe(id, company, var, what):
    return {"id": id, "service": "stripe", "company": company, "vars": [var],
            "fn": probe_stripe, "critical": True, "tier": "critical",
            "what": what,
            "why": ("A revoked or expired Stripe secret key stops card revenue and every "
                    "billing automation that depends on it, with no vendor notification."),
            "blast": "Card payments, subscriptions, and billing automation for this account."}


def _clerk(id, company, var, what):
    return {"id": id, "service": "clerk", "company": company, "vars": [var],
            "fn": probe_clerk, "critical": True, "tier": "critical",
            "what": what,
            "why": ("An invalid Clerk secret key locks every user out of the product; the "
                    "failure is invisible server-side until customers report it."),
            "blast": "Sign-in, session validation, and every authenticated route for this app."}


def _meta(id, company, var):
    return {"id": id, "service": "meta-ads", "company": company, "vars": [var],
            "fn": probe_meta, "critical": True, "tier": "critical",
            "what": (f"Checks the {company} Meta access token with a Graph API /me identity read, "
                     "mapping Meta's HTTP 400 OAuthException to SUSPENDED."),
            "why": ("Meta tokens expire silently; ad reporting and CAPI conversion tracking "
                    "keep returning empty rather than failing loudly."),
            "blast": "Ad spend reporting and server-side conversion tracking for this brand."}


PROBE_SPECS = [
    _stripe("stripe-estate-primary", "estate", "STRIPE_PRIMARY_SECRET_KEY",
            "Reads the Stripe balance for the estate primary account (STRIPE_PRIMARY_SECRET_KEY)."),
    _stripe("stripe-estate-secondary", "estate", "STRIPE_SECONDARY_SECRET_KEY",
            "Reads the Stripe balance for the estate secondary account (STRIPE_SECONDARY_SECRET_KEY)."),
    _stripe("stripe-globex", "globex", "GLOBEX_STRIPE_SECRET_KEY",
            "Reads the Stripe balance for the Globex account (GLOBEX_STRIPE_SECRET_KEY)."),
    _stripe("stripe-initech-primary", "initech", "INITECH_STRIPE_PRIMARY_SECRET_KEY",
            "Reads the Stripe balance for the Initech primary account (INITECH_STRIPE_PRIMARY_SECRET_KEY)."),
    _stripe("stripe-acmeuni-agentpro", "acmeuni", "ACMEUNI_AGENTPRO_STRIPE_SECRET_KEY",
            "Reads the Stripe balance for the AcmeUni AgentPro account (ACMEUNI_AGENTPRO_STRIPE_SECRET_KEY)."),
    _stripe("stripe-email-replier-primary", "initech", "EMAIL_REPLIER_STRIPE_PRIMARY_SECRET_KEY",
            "Reads the Stripe balance used by email-replier billing (EMAIL_REPLIER_STRIPE_PRIMARY_SECRET_KEY)."),

    _clerk("clerk-crm-prod", "initech", "CRM_PROD_CLERK_SECRET_KEY",
           "Checks the shared CRM production Clerk secret key with a users/count read."),
    _clerk("clerk-crm-prod-initech", "initech", "CRM_PROD_INITECH_CLERK_SECRET_KEY",
           "Checks the Initech CRM production Clerk secret key with a users/count read."),
    _clerk("clerk-crm-prod-acmeuni", "acmeuni", "CRM_PROD_ACMEUNI_CLERK_SECRET_KEY",
           "Checks the AcmeUni CRM production Clerk secret key with a users/count read."),
    _clerk("clerk-initech-frontend", "initech", "INITECH_FRONTEND_CLERK_SECRET_KEY",
           "Checks the Initech frontend Clerk secret key with a users/count read."),
    _clerk("clerk-globex-adsmanager", "globex", "LOCAL_GLOBEX_ADSMANAGER_CLERK_SECRET_KEY",
           "Checks the Globex ads-manager Clerk secret key with a users/count read."),

    _meta("meta-ads-initech", "initech", "INITECH_META_ACCESS_TOKEN"),
    _meta("meta-ads-globex", "globex", "GLOBEX_META_ACCESS_TOKEN"),
    _meta("meta-ads-acmeuni", "acmeuni", "ACMEUNI_META_ACCESS_TOKEN"),

    {"id": "paypal-readonly", "service": "paypal", "company": "estate",
     "vars": ["PAYPAL_READONLY_CLIENT_ID", "PAYPAL_READONLY_CLIENT_SECRET"],
     "fn": probe_paypal, "critical": True, "tier": "critical",
     "what": "Requests a client-credentials token from PayPal live with the read-only client pair.",
     "why": "A dead PayPal client pair silently stops PayPal order and dispute reads.",
     "blast": "PayPal payment reconciliation and dispute visibility."},

    {"id": "vandelay", "service": "checkout-champ", "company": "initech",
     "vars": ["VANDELAY_LOGIN_ID", "VANDELAY_PASSWORD"],
     "fn": probe_vandelay, "critical": True, "tier": "critical",
     "what": ("Runs a read-only campaign query against Vandelay and grades the JSON "
              "result field, because the vendor returns HTTP 200 for auth failures."),
     "why": "Vandelay is a live order-processing path; a rejected login stops order sync.",
     "blast": "Vandelay order ingestion and CRM order reconciliation."},

    {"id": "nmi-fortpoint", "service": "nmi", "company": "initech",
     "vars": ["FORTPOINT_NMI_QUERY_URL", "FORTPOINT_NMI_SECURITY_KEY"],
     "fn": probe_nmi, "critical": True, "tier": "critical",
     "what": ("Queries the Fortpoint NMI gateway read API (query.php only, future-dated window) "
              "and grades the XML error_response element rather than the HTTP status."),
     "why": "A rejected NMI security key stops card processing on the Fortpoint gateway.",
     "blast": "Fortpoint gateway card processing and transaction reporting."},

    {"id": "nmi-glacier", "service": "nmi", "company": "initech",
     "vars": ["GLACIER_NMI_QUERY_URL", "GLACIER_NMI_SECURITY_KEY"],
     "fn": probe_nmi, "critical": True, "tier": "critical",
     "what": ("Queries the Glacier NMI gateway read API (query.php only, future-dated window) "
              "and grades the XML error_response element rather than the HTTP status."),
     "why": "A rejected NMI security key stops card processing on the Glacier gateway.",
     "blast": "Glacier gateway card processing and transaction reporting."},

    {"id": "fanbasis-initech-prod-liveness", "service": "fanbasis", "company": "initech",
     "vars": ["INITECH_FANBASIS_PRODUCTION_API_URL", "INITECH_FANBASIS_PRODUCTION_API_KEY"],
     "fn": probe_fanbasis_liveness, "critical": True, "tier": "critical",
     "what": ("LIVENESS ONLY: reads the Fanbasis production /api/health endpoint. This proves "
              "the API is serving; it does NOT validate INITECH_FANBASIS_PRODUCTION_API_KEY, "
              "because Fanbasis exposes no safe read endpoint that does."),
     "why": "Fanbasis carries live checkout traffic; an unserving API stops those payments.",
     "blast": "Fanbasis production checkout and webhook delivery."},

    {"id": "teller-api", "service": "teller", "company": "estate",
     "vars": ["TELLER_CERT_PATH", "TELLER_CERT_KEY_PATH", "TELLER_ACCESS_TOKEN"],
     "fn": probe_teller, "critical": True, "tier": "critical",
     "what": "Reads the Teller /accounts list over mutual TLS with the estate client certificate.",
     "why": "Teller is the bank feed; a broken connection zeroes the banking dashboards silently.",
     "blast": "Bank balance and transaction feeds across estate financial dashboards."},

    {"id": "teller-client-cert", "service": "teller", "company": "estate",
     "vars": ["TELLER_CERT_PATH"],
     "fn": probe_teller_cert, "critical": True, "tier": "critical",
     "what": ("Decodes the Teller client certificate locally and reports days to expiry; "
              "warns at 21 days and reports expired as down."),
     "why": ("A certificate expiry produces no vendor warning: the bank feed simply stops, "
             "and dashboards render zeroes as if the accounts were empty."),
     "blast": "Every Teller-backed bank feed, at a known future date."},

    {"id": "cloudflare-api", "service": "cloudflare", "company": "estate",
     "vars": ["CLOUDFLARE_API_TOKEN"], "fn": probe_cloudflare, "critical": False, "tier": "high",
     "what": "Verifies CLOUDFLARE_API_TOKEN with Cloudflare's own read-only token verify endpoint.",
     "why": "DNS and zone automation fails closed on a dead token, blocking deploy and DNS changes.",
     "blast": "DNS, zone, and edge configuration automation."},

    {"id": "zendesk-initech", "service": "zendesk", "company": "initech",
     "vars": ["ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN"],
     "fn": probe_zendesk, "critical": False, "tier": "high",
     "what": "Reads the Zendesk API user identity (users/me.json) for the Initech subdomain.",
     "why": "A dead Zendesk token stops support ticket automation and reporting.",
     "blast": "Support ticket ingestion, automation, and reporting."},

    {"id": "customerio-app", "service": "customer.io", "company": "estate",
     "vars": ["CUSTOMERIO_APP_API_KEY"], "fn": probe_customerio, "critical": False, "tier": "high",
     "what": "Reads the Customer.io App API segment list to verify CUSTOMERIO_APP_API_KEY.",
     "why": "A dead App API key stops lifecycle email segmentation and campaign automation.",
     "blast": "Lifecycle messaging and segmentation."},

    {"id": "clickup", "service": "clickup", "company": "estate",
     "vars": ["CLICKUP_API_TOKEN"], "fn": probe_clickup, "critical": False, "tier": "medium",
     "what": "Reads the ClickUp authenticated user to verify CLICKUP_API_TOKEN.",
     "why": "Task automation writing into ClickUp lists fails silently on a dead token.",
     "blast": "Task and delivery tracking automation."},

    {"id": "jira-initech", "service": "jira", "company": "initech",
     "vars": ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"],
     "fn": probe_jira, "critical": False, "tier": "medium",
     "what": "Reads the Jira /myself identity for the Initech Atlassian site.",
     "why": "Issue automation and the Jira/Slack bridge fail on an expired Atlassian token.",
     "blast": "Engineering issue automation and the Jira-Slack bridge."},

    {"id": "slack-bot", "service": "slack", "company": "estate",
     "vars": ["SLACK_BOT_TOKEN"], "fn": probe_slack_bot, "critical": False, "tier": "high",
     "what": ("Reads Slack auth.test and grades the JSON ok field, because Slack returns "
              "HTTP 200 with ok=false for a revoked token."),
     "why": ("The Slack bot token is a capmap alert delivery path; if it dies, alert delivery "
             "dies quietly with it."),
     "blast": "Slack bot messaging, including capmap alert delivery."},

    {"id": "slack-webhook-ops-alert", "service": "slack", "company": "estate",
     "vars": ["OPS_ALERT_SLACK_WEBHOOK_URL"], "fn": probe_unset, "critical": False, "tier": "high",
     "unverified_reason": "probing an incoming webhook would post a message, which is a write",
     "no_safe_probe": True,
     "what": ("NOT PROBED BY DESIGN. The only way to exercise an incoming Slack webhook is to "
              "post to it, which is a write and would page the operator; this capability is registered "
              "so the gap is visible instead of absent."),
     "why": "Alert delivery depends on this URL, and an unmonitored gap must still be counted.",
     "blast": "Capmap alert delivery via the ops-alert Slack webhook."},

    {"id": "bunny-cdn", "service": "bunny", "company": "estate",
     "vars": ["BUNNY_API_KEY"], "fn": probe_bunny, "critical": False, "tier": "medium",
     "what": "Reads one Bunny pull zone to verify BUNNY_API_KEY.",
     "why": "A dead Bunny key stops CDN and storage automation for hosted assets.",
     "blast": "CDN pull zones and asset storage automation."},

    {"id": "vultr-api", "service": "vultr", "company": "estate",
     "vars": ["VULTR_API_KEY"], "fn": probe_vultr, "critical": False, "tier": "medium",
     "what": ("Reads the Vultr account endpoint, distinguishing a genuinely rejected key from "
              "this host not being on the Vultr API IP allowlist (which is UNVERIFIED)."),
     "why": "Host provisioning automation fails on a dead Vultr key.",
     "blast": "Vultr instance provisioning and management automation."},

    {"id": "ovh-api", "service": "ovh", "company": "estate",
     "vars": ["OVH_APPLICATION_KEY", "OVH_APPLICATION_SECRET", "OVH_CONSUMER_KEY"],
     "fn": probe_ovh, "critical": False, "tier": "medium",
     "what": "Reads the OVH /me identity using a signed request against the OVH EU API.",
     "why": "OVH hosts estate infrastructure; a dead application key blocks all OVH automation.",
     "blast": "OVH infrastructure and DNS automation."},
]

PROBES = {spec["id"]: spec for spec in PROBE_SPECS}
if len(PROBES) != len(PROBE_SPECS):  # pragma: no cover - guards a copy/paste duplicate id
    raise RuntimeError("duplicate probe id in PROBE_SPECS")


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------

def run_probe(probe_id, env=None, transport=None):
    """Run one probe and return its result mapping.  Never raises for a probe fault."""
    spec = PROBES.get(probe_id)
    if spec is None:
        raise KeyError(probe_id)
    env = load_env() if env is None else env
    transport = transport or urllib_transport
    started = time.monotonic()
    try:
        result = spec["fn"](spec, env, transport)
    except Exception as exc:  # noqa: BLE001 - a probe bug is UNVERIFIED, never green
        result = unverified(spec, f"probe raised {type(exc).__name__}")
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


def run_all(env=None, transport=None, ids=None):
    env = load_env() if env is None else env
    return [run_probe(probe_id, env, transport) for probe_id in (ids or sorted(PROBES))]


# --------------------------------------------------------------------------
# capability documents
# --------------------------------------------------------------------------

def capability_document(spec):
    """Render the capmap registry document for one probe spec."""
    no_safe_probe = bool(spec.get("no_safe_probe"))
    if no_safe_probe:
        verification = {"type": "unset",
                        "reason": spec.get("unverified_reason", "no safe read-only probe exists"),
                        "credential_vars": list(spec["vars"])}
        semantics = {}
    else:
        verification = {
            "type": "command",
            "argv": ["/usr/bin/env", "python3", PROBES_PATH, spec["id"]],
            "timeout_seconds": 20,
            "read_only": True,
            "credential_vars": list(spec["vars"]),
            "note": SEMANTICS_NOTE,
        }
        semantics = dict(SEMANTICS_CRITICAL if spec.get("critical") else SEMANTICS_SUPPORTING)
    return {
        "id": spec["id"],
        "company": spec["company"],
        "kind": "external-service",
        "host": "owner-mac",
        "repo": "/Users/youruser/OmniAgentOS",
        "what_it_does": spec["what"],
        "why_it_matters": spec["why"],
        "blast_radius": spec["blast"],
        "verification": verification,
        "exit_semantics": semantics,
        "cadence_seconds": 900,
        "staleness_slo_seconds": 2700,
        "escalation_tier": spec.get("tier", "medium"),
        "owner": "{}-ops".format(spec["company"]),
        "last_verified": None,
        "last_status": "UNVERIFIED",
    }


def emit_capabilities(directory, overwrite=False):
    """Write one capability JSON per probe.  Existing files are left alone."""
    os.makedirs(directory, exist_ok=True)
    written, skipped = [], []
    for spec in PROBE_SPECS:
        path = os.path.join(directory, spec["id"].replace("/", "-") + ".json")
        if os.path.exists(path) and not overwrite:
            skipped.append(path)
            continue
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(capability_document(spec), handle, indent=2, sort_keys=True)
            handle.write("\n")
        written.append(path)
    return written, skipped


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _render(results):
    headers = ("probe", "company", "state", "detail")
    rows = [[str(r["probe"]), str(r["company"]), str(r["state"]), str(r["detail"])]
            for r in results]
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row)]
    lines = [" | ".join(h.ljust(w) for h, w in zip(headers, widths)),
             "-+-".join("-" * w for w in widths)]
    lines.extend(" | ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="probes", description="Read-only capability probes (no writes, ever).")
    parser.add_argument("probe", nargs="?", help="probe id to run")
    parser.add_argument("--list", action="store_true", help="list probe ids")
    parser.add_argument("--all", action="store_true", help="run every probe")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--emit-capabilities", metavar="DIR",
                        help="write capability registry documents into DIR")
    args = parser.parse_args(argv)

    if args.emit_capabilities:
        written, skipped = emit_capabilities(args.emit_capabilities)
        for path in written:
            print(f"wrote {path}")
        for path in skipped:
            print(f"skipped (exists) {path}")
        return EXIT_UP

    if args.list:
        for spec in PROBE_SPECS:
            print(f"{spec['id']:<34} {spec['company']:<14} {spec['service']}")
        return EXIT_UP

    if args.all:
        results = run_all()
        print(json.dumps(results, indent=2, sort_keys=True) if args.json else _render(results))
        return max(results, key=lambda r: _SEVERITY.get(r["exit_code"], 0))["exit_code"] \
            if results else EXIT_UNVERIFIED

    if not args.probe:
        parser.error("a probe id, --all, --list, or --emit-capabilities is required")
    try:
        result = run_probe(args.probe)
    except KeyError:
        print(f"probes: unknown probe {args.probe!r}", file=sys.stderr)
        return 64
    print(json.dumps(result, sort_keys=True))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
