"""OVH auth scheme: the broker SIGNS each request instead of carrying a bearer.

Auth spec contract (pinned):

    ovh:OVH_APPLICATION_KEY:OVH_APPLICATION_SECRET:OVH_CONSUMER_KEY

means the broker reads those THREE env var NAMES and, per request, sets:

    X-Ovh-Application: <application key>
    X-Ovh-Consumer:    <consumer key>
    X-Ovh-Timestamp:   <unix time>
    X-Ovh-Signature:   "$1$" + sha1_hex(secret + "+" + consumer + "+" + METHOD
                        + "+" + FULL_URL + "+" + BODY + "+" + TIMESTAMP)

The application SECRET feeds the SHA1 digest but never leaves the broker: it is
never a header, a return value, or a log line. Signing covers the ACTUAL outbound
method/url/body, so a tampered request cannot reuse a signature.
"""

from __future__ import annotations

import hashlib

import pytest

from omniagentos.connectors import Capability, HttpSpec
from omniagentos.connectors.broker import (
    BrokerDenied,
    _auth_headers,
    _ovh_body_str,
    _ovh_signature,
)
from omniagentos.contracts import ActionClass

OVH_SPEC = "ovh:OVH_APPLICATION_KEY:OVH_APPLICATION_SECRET:OVH_CONSUMER_KEY"

# Deliberately non-secret placeholders -- no real OVH key material in the repo.
_APP_KEY = "app-key-pretend"
_APP_SECRET = "app-secret-pretend"
_CONSUMER_KEY = "consumer-key-pretend"


def _set_ovh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVH_APPLICATION_KEY", _APP_KEY)
    monkeypatch.setenv("OVH_APPLICATION_SECRET", _APP_SECRET)
    monkeypatch.setenv("OVH_CONSUMER_KEY", _CONSUMER_KEY)


def _ovh_read_cap() -> Capability:
    """A callable, read-only OVH capability using the ovh signing scheme."""
    return Capability(
        id="ovh.read",
        connector="ovh",
        group="infra",
        label="ovh read",
        action_class=ActionClass.READ_ONLY,
        http=HttpSpec(
            base_url="https://eu.api.ovh.com",
            methods=["GET"],
            auth=OVH_SPEC,
        ),
    )


# --- pure signer -------------------------------------------------------------


def test_signature_is_deterministic_for_a_known_input() -> None:
    """The signer is a pure function of its inputs -- same in, same digest out."""
    method = "GET"
    url = "https://eu.api.ovh.com/1.0/cloud/project/proj123/instance"
    body_str = ""
    timestamp = "1700000000"

    expected_raw = "+".join([_APP_SECRET, _CONSUMER_KEY, method, url, body_str, timestamp])
    expected = "$1$" + hashlib.sha1(expected_raw.encode("utf-8")).hexdigest()

    got = _ovh_signature(_APP_SECRET, _CONSUMER_KEY, method, url, body_str, timestamp)
    assert got == expected
    # Stable across calls.
    assert got == _ovh_signature(_APP_SECRET, _CONSUMER_KEY, method, url, body_str, timestamp)
    assert got.startswith("$1$")


def test_body_str_matches_httpx_wire_encoding() -> None:
    """A body signs to exactly what httpx puts on the wire (compact JSON)."""
    assert _ovh_body_str(None) == ""
    # httpx 0.28 uses separators=(",", ":") and ensure_ascii=False.
    assert _ovh_body_str({"name": "vm", "flavorId": "abc"}) == '{"name":"vm","flavorId":"abc"}'
    assert _ovh_body_str({"note": "café"}) == '{"note":"café"}'


def test_signature_covers_method_url_and_body() -> None:
    """Changing method, url, or body changes the digest -- the request is bound."""
    base = _ovh_signature(_APP_SECRET, _CONSUMER_KEY, "GET", "https://x/a", "", "1")
    assert base != _ovh_signature(_APP_SECRET, _CONSUMER_KEY, "POST", "https://x/a", "", "1")
    assert base != _ovh_signature(_APP_SECRET, _CONSUMER_KEY, "GET", "https://x/b", "", "1")
    assert base != _ovh_signature(_APP_SECRET, _CONSUMER_KEY, "GET", "https://x/a", "{}", "1")
    assert base != _ovh_signature(_APP_SECRET, _CONSUMER_KEY, "GET", "https://x/a", "", "2")


# --- header wiring -----------------------------------------------------------


def test_auth_headers_sets_the_four_ovh_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheme attaches all four X-Ovh-* headers and no query/cert."""
    _set_ovh_env(monkeypatch)
    monkeypatch.setattr("time.time", lambda: 1700000000.0)

    spec = HttpSpec(base_url="https://eu.api.ovh.com", auth=OVH_SPEC)
    url = "https://eu.api.ovh.com/1.0/cloud/project/proj123/instance"
    headers, params, cert = _auth_headers(spec, _ovh_read_cap(), method="GET", url=url, body=None)

    assert headers["X-Ovh-Application"] == _APP_KEY
    assert headers["X-Ovh-Consumer"] == _CONSUMER_KEY
    assert headers["X-Ovh-Timestamp"] == "1700000000"
    expected = _ovh_signature(_APP_SECRET, _CONSUMER_KEY, "GET", url, "", "1700000000")
    assert headers["X-Ovh-Signature"] == expected
    assert params == {} and cert is None


def test_signature_matches_the_real_outbound_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A POST body is signed as the compact JSON httpx will actually send."""
    _set_ovh_env(monkeypatch)
    monkeypatch.setattr("time.time", lambda: 1700000000.0)

    spec = HttpSpec(base_url="https://eu.api.ovh.com", auth=OVH_SPEC)
    url = "https://eu.api.ovh.com/1.0/cloud/project/proj123/instance"
    body = {"name": "prod-01", "flavorId": "f1", "imageId": "i1", "region": "GRA11"}
    headers, _, _ = _auth_headers(spec, _ovh_read_cap(), method="POST", url=url, body=body)

    wire_body = '{"name":"prod-01","flavorId":"f1","imageId":"i1","region":"GRA11"}'
    expected = _ovh_signature(_APP_SECRET, _CONSUMER_KEY, "POST", url, wire_body, "1700000000")
    assert headers["X-Ovh-Signature"] == expected


# --- secret hygiene ----------------------------------------------------------


def test_application_secret_never_appears_in_returned_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The application secret feeds the digest one-way and is never emitted."""
    _set_ovh_env(monkeypatch)
    monkeypatch.setattr("time.time", lambda: 1700000000.0)

    spec = HttpSpec(base_url="https://eu.api.ovh.com", auth=OVH_SPEC)
    url = "https://eu.api.ovh.com/1.0/cloud/project/proj123/instance"
    headers, params, cert = _auth_headers(spec, _ovh_read_cap(), method="GET", url=url, body=None)

    blob = repr(headers) + repr(params) + repr(cert)
    assert _APP_SECRET not in blob
    # The one-way digest is present, the pre-image secret is not.
    assert headers["X-Ovh-Signature"].startswith("$1$")


# --- form fail-closed --------------------------------------------------------


def test_form_payload_is_refused_not_signed_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A form payload is refused: signing it as an empty body would 401 at OVH.

    The signature folds in the JSON body only, but a form payload is transmitted
    as ``data=...``. Signing an empty pre-image while sending form bytes is a
    guaranteed mismatch, so the scheme fails closed before any network hop.
    """
    _set_ovh_env(monkeypatch)
    monkeypatch.setattr("time.time", lambda: 1700000000.0)

    spec = HttpSpec(base_url="https://eu.api.ovh.com", auth=OVH_SPEC)
    url = "https://eu.api.ovh.com/1.0/cloud/project/proj123/instance"
    with pytest.raises(BrokerDenied) as exc:
        _auth_headers(spec, _ovh_read_cap(), method="POST", url=url, form={"foo": "bar"})
    assert exc.value.reason == "form_not_supported"
    assert "form" in exc.value.detail


def test_json_body_still_signs_when_no_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """The JSON-body path is unaffected by the form guard -- it still signs."""
    _set_ovh_env(monkeypatch)
    monkeypatch.setattr("time.time", lambda: 1700000000.0)

    spec = HttpSpec(base_url="https://eu.api.ovh.com", auth=OVH_SPEC)
    url = "https://eu.api.ovh.com/1.0/cloud/project/proj123/instance"
    headers, _, _ = _auth_headers(
        spec, _ovh_read_cap(), method="POST", url=url, body={"name": "vm"}, form=None
    )
    wire_body = '{"name":"vm"}'
    expected = _ovh_signature(_APP_SECRET, _CONSUMER_KEY, "POST", url, wire_body, "1700000000")
    assert headers["X-Ovh-Signature"] == expected


# --- connector env scope -----------------------------------------------------


def test_connector_env_lists_only_the_three_signing_secrets() -> None:
    """OVH_PROJECT_ID is a caller-supplied path param, not a broker credential.

    `infra` is not in INJECTABLE_GROUPS, so infra env is never handed to an agent;
    the project id is not a secret and must arrive as a task parameter. Declaring
    it in `env` would be misleading, so the connector lists only the three secrets
    the signing scheme actually resolves.
    """
    from omniagentos.connectors import load_registry

    connector = load_registry().connectors["ovh"]
    assert connector.env == [
        "OVH_APPLICATION_KEY",
        "OVH_APPLICATION_SECRET",
        "OVH_CONSUMER_KEY",
    ]
    assert "OVH_PROJECT_ID" not in connector.env


# --- parsing -----------------------------------------------------------------


def test_ovh_spec_requires_exactly_three_env_names() -> None:
    """A malformed ovh spec is refused as a bad auth scheme, not silently run."""
    for bad in ("ovh:ONLY_ONE", "ovh:A:B", "ovh:A:B:C:D", "ovh:A::C"):
        spec = HttpSpec(base_url="https://eu.api.ovh.com", auth=bad)
        with pytest.raises(BrokerDenied) as exc:
            _auth_headers(spec, _ovh_read_cap())
        assert exc.value.reason == "bad_auth_scheme"
