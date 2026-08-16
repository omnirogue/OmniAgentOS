"""Offline U-N1 web-read counterfeits: no live network is ever used.

Every fetch in this file is authorized the way production authorizes it — a
store-backed standing grant loaded by holder — because the alternative (handing
``fetch`` a capability list) is now a refusal, which is the point.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.connectors.broker import AuditContext, BrokerDenied
from omniagentos.connectors.store import CapabilityStore
from omniagentos.connectors.web_read import FETCH_MAX_RESPONSE_BYTES, fetch, search
from omniagentos.db.store import SqliteStore

HOLDER = "lane:research.formation"
CONTEXT = AuditContext(holder=HOLDER, run_id="run-web-read", request_id="req-web-read")
PUBLIC_IP = "93.184.216.34"


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_call(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append({"args": args, **kwargs})


class _FormationGrant:
    """The standing grant store: it, not the caller, says what the holder holds."""

    def __init__(self, capabilities: tuple[str, ...] = ("web.fetch",)) -> None:
        self.capabilities = list(capabilities)
        self.lookups: list[str] = []

    def get_grant(self, holder: str) -> list[str]:
        self.lookups.append(holder)
        assert holder == HOLDER
        return list(self.capabilities)


def _public_dns(_host: str) -> tuple[str, ...]:
    return (PUBLIC_IP,)


def _call(url: str, handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> dict[str, Any]:
    return fetch(
        url,
        grant_store=kwargs.pop("grant_store", _FormationGrant()),
        grant_holder=kwargs.pop("grant_holder", HOLDER),
        audit_store=kwargs.pop("audit_store", _Audit()),
        audit_context=CONTEXT,
        transport=httpx.MockTransport(handler),
        host_resolver=kwargs.pop("host_resolver", _public_dns),
        **kwargs,
    )


@pytest.fixture
def audit_store(tmp_path: Path) -> tuple[SqliteStore, CapabilityStore]:
    raw = SqliteStore(str(tmp_path / "web-read.sqlite3"))
    try:
        yield raw, CapabilityStore(raw)
    finally:
        raw.close()


def test_granted_research_formation_fetch_is_scrubbed_bounded_receipted_and_audited(
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    _raw, store = audit_store
    grants = _FormationGrant()

    def handler(request: httpx.Request) -> httpx.Response:
        # The socket is opened to the address the guard validated, while the
        # request still identifies the real host to the server and to TLS.
        assert request.url.host == PUBLIC_IP
        assert request.headers["host"] == "fixture.example"
        assert request.extensions["sni_hostname"] == "fixture.example"
        return httpx.Response(200, content=b'{"api_key":"must-not-return","ok":true}', request=request)

    result = fetch(
        "https://fixture.example/article",
        grant_store=grants,
        grant_holder=HOLDER,
        audit_store=store,
        audit_context=CONTEXT,
        transport=httpx.MockTransport(handler),
        host_resolver=_public_dns,
    )
    assert "must-not-return" not in result["body"]["content"]
    assert result["body"]["receipt"]["size_bytes"] <= FETCH_MAX_RESPONSE_BYTES
    assert result["body"]["receipt"]["redactions"] >= 1
    assert result["body"]["receipt"]["allowed"] is True
    assert result["body"]["receipt"]["resolved_ip"] == PUBLIC_IP
    # The grant was actually loaded from the store; this is what the old
    # "granted" test only appeared to assert.
    assert grants.lookups == [HOLDER]
    rows = list(reversed(store.call_log(run_id="run-web-read")))
    assert [row["decision"] for row in rows] == ["intent", "allowed"]
    assert len({row["call_id"] for row in rows}) == 1


# ---------------------------------------------------------------------------
# BLOCKER 1 — no authorization, no egress.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({}, "grant_store_unavailable"),
        ({"grant_holder": HOLDER}, "grant_store_unavailable"),
        ({"grant_store": _FormationGrant()}, "grant_store_unavailable"),
        ({"grant_store": _FormationGrant(), "grant_holder": ""}, "grant_store_unavailable"),
    ],
)
def test_ungranted_caller_is_refused_and_never_reaches_the_network(
    kwargs: dict[str, Any], code: str
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"SECRET-PAGE-BODY", request=request)

    with pytest.raises(BrokerDenied) as refused:
        fetch(
            "https://fixture.example/secret",
            audit_store=_Audit(),
            audit_context=CONTEXT,
            transport=httpx.MockTransport(handler),
            host_resolver=_public_dns,
            **kwargs,
        )
    assert refused.value.reason == code
    assert seen == []


@pytest.mark.parametrize("granted", [[], ["web.fetch"], ["nothing.at.all"]])
def test_caller_supplied_capability_list_is_refused_not_ignored(granted: list[str]) -> None:
    """The pre-S1 shape: a caller vouching for itself. Refused, loudly."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"SECRET-PAGE-BODY", request=request)

    with pytest.raises(BrokerDenied) as refused:
        fetch(
            "https://fixture.example/secret",
            granted=granted,
            grant_store=_FormationGrant(),
            grant_holder=HOLDER,
            audit_store=_Audit(),
            audit_context=CONTEXT,
            transport=httpx.MockTransport(handler),
            host_resolver=_public_dns,
        )
    assert refused.value.reason == "caller_supplied_grant"
    assert seen == []


def test_holder_without_the_capability_in_the_store_is_refused() -> None:
    """The store is decisive: a holder whose row lacks web.fetch cannot fetch."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"SECRET-PAGE-BODY", request=request)

    with pytest.raises(BrokerDenied) as refused:
        _call(
            "https://fixture.example/secret",
            handler,
            grant_store=_FormationGrant(capabilities=("slack.read",)),
        )
    assert refused.value.reason == "not_granted"
    assert seen == []


def test_search_refuses_a_caller_supplied_capability_list() -> None:
    with pytest.raises(BrokerDenied) as refused:
        search(
            "anything",
            granted=["web.search"],
            grant_store=_FormationGrant(),
            grant_holder=HOLDER,
            audit_context=AuditContext(holder=HOLDER, budget_receipt_id="rcp-1"),
        )
    assert refused.value.reason == "caller_supplied_grant"


def test_search_without_a_store_backed_grant_is_refused() -> None:
    with pytest.raises(BrokerDenied) as refused:
        search(
            "anything",
            audit_context=AuditContext(holder=HOLDER, budget_receipt_id="rcp-1"),
        )
    assert refused.value.reason == "grant_store_unavailable"


def test_search_still_requires_a_budget_envelope_receipt() -> None:
    with pytest.raises(BrokerDenied) as refused:
        search(
            "anything",
            grant_store=_FormationGrant(),
            grant_holder=HOLDER,
            audit_context=AuditContext(holder=HOLDER),
        )
    assert refused.value.reason == "budget_envelope_required"


def test_a_method_outside_the_registry_allowlist_is_refused_before_egress() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        seen.append(str(request.url))
        return httpx.Response(200, request=request)

    with pytest.raises(BrokerDenied) as refused:
        _call("https://fixture.example/x", handler, method="DELETE")
    assert refused.value.reason == "method_not_allowed"
    assert seen == []


def test_credential_bearing_request_headers_are_refused_before_egress() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        seen.append(str(request.url))
        return httpx.Response(200, request=request)

    with pytest.raises(BrokerDenied) as refused:
        _call("https://fixture.example/x", handler, headers={"Authorization": "Bearer x"})
    assert refused.value.reason == "request_headers_refused"
    assert seen == []


def test_search_source_never_forwards_a_caller_list_to_the_broker() -> None:
    """Structural: no path in search() hands ``granted`` to broker.call."""
    source = Path("omniagentos/connectors/web_read.py").read_text(encoding="utf-8")
    body = source[source.index("def search("):]
    assert "granted=granted" not in body
    assert "        granted,\n" not in body


# ---------------------------------------------------------------------------
# BLOCKER 2 — the checked address is the connected address.
# ---------------------------------------------------------------------------


_REBIND_SECRET = b"INTERNAL-CONTROL-PLANE-SECRET"


class _LoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(_REBIND_SECRET)))
        self.end_headers()
        self.wfile.write(_REBIND_SECRET)

    def log_message(self, *args: Any) -> None:
        return


def test_dns_rebinding_cannot_reach_a_real_loopback_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile resolver answers public; the pin means httpx never re-resolves.

    This goes through the real httpx + httpcore + ``socket`` stack, which is the
    layer that used to perform the SECOND lookup. ``localtest.me`` resolves to
    127.0.0.1 in real DNS, and a real server is listening there holding a
    secret. Before the pin, the guard checked one answer and httpx looked the
    same name up again and connected to the other — deterministically, no timing
    precision required, and the secret came back with a 200.

    The socket layer is stubbed at ``connect`` only so the offline lane stays
    offline: the assertion is about the ADDRESS the OS was asked to connect to,
    which is the whole disagreement.
    """
    server = HTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    connects: list[Any] = []
    resolved_names: list[str] = []
    real_getaddrinfo = socket.getaddrinfo

    def spy_connect(self: socket.socket, address: Any) -> None:
        connects.append(address)
        raise ConnectionRefusedError(61, "connection refused by the offline lane")

    def spy_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        resolved_names.append(str(host))
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    monkeypatch.setattr(socket, "getaddrinfo", spy_getaddrinfo)
    try:
        with pytest.raises(BrokerDenied) as refused:
            fetch(
                f"http://localtest.me:{port}/",
                grant_store=_FormationGrant(),
                grant_holder=HOLDER,
                audit_store=_Audit(),
                audit_context=CONTEXT,
                host_resolver=lambda _host: (PUBLIC_IP,),
            )
        assert refused.value.reason == "transport_error"
        assert _REBIND_SECRET.decode() not in str(refused.value.detail)
    finally:
        monkeypatch.undo()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # The connection went to the address the guard validated, and to nothing
    # else. Loopback — where the secret actually lives — was never dialled.
    assert connects == [(PUBLIC_IP, port)]
    # And the transport never looked the hostname up a second time; the only
    # name it resolved is the IP literal it was handed.
    assert "localtest.me" not in resolved_names


def test_a_second_resolution_can_never_be_used_because_the_first_is_pinned() -> None:
    """The resolver is called once per hop and its answer is what is connected to."""
    answers = iter([("93.184.216.34",), ("169.254.169.254",)])
    calls: list[str] = []

    def rebinding_resolver(host: str) -> tuple[str, ...]:
        calls.append(host)
        return next(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        # The metadata address from the SECOND resolution is never reachable:
        # the request carries the address from the first.
        assert request.url.host == "93.184.216.34"
        return httpx.Response(200, content=b"public", request=request)

    result = _call(
        "http://rebind.example/",
        handler,
        host_resolver=rebinding_resolver,
    )
    assert calls == ["rebind.example"]
    assert result["receipt"]["resolved_ip"] == "93.184.216.34"


def test_each_redirect_hop_is_re_resolved_and_re_pinned() -> None:
    resolved: dict[str, str] = {"start.example": "93.184.216.34", "final.example": "23.215.0.136"}
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url.host), request.headers["host"]))
        if request.headers["host"] == "start.example":
            return httpx.Response(
                302, headers={"location": "https://final.example/article"}, request=request
            )
        return httpx.Response(200, content=b"final", request=request)

    result = _call(
        "https://start.example/page",
        handler,
        host_resolver=lambda host: (resolved[host],),
    )
    assert seen == [("93.184.216.34", "start.example"), ("23.215.0.136", "final.example")]
    assert result["receipt"]["resolved_ip"] == "23.215.0.136"
    assert result["receipt"]["url"] == "https://final.example/article"


def test_url_userinfo_is_refused_as_a_credential_on_egress() -> None:
    with pytest.raises(BrokerDenied) as refused:
        _call(
            "https://user:secret@fixture.example/x",
            lambda request: httpx.Response(200, request=request),
        )
    assert refused.value.reason == "invalid_url"


def test_caller_supplied_host_header_cannot_unpin_the_connection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == PUBLIC_IP
        assert request.headers["host"] == "fixture.example"
        return httpx.Response(200, content=b"ok", request=request)

    result = _call(
        "https://fixture.example/x",
        handler,
        headers={"Host": "169.254.169.254", "X-Trace": "keep-me"},
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# BLOCKER 3 — the audit spine records denials as denials, with the URL.
# ---------------------------------------------------------------------------


def test_ssrf_denial_writes_an_intent_and_a_denied_row_carrying_the_url(
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    _raw, store = audit_store

    with pytest.raises(BrokerDenied) as refused:
        fetch(
            "http://169.254.169.254/latest/meta-data/iam/",
            grant_store=_FormationGrant(),
            grant_holder=HOLDER,
            audit_store=store,
            audit_context=CONTEXT,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, request=r)),
            host_resolver=_public_dns,
        )
    assert refused.value.reason == "ssrf_refused"

    rows = list(reversed(store.call_log(run_id="run-web-read")))
    assert [row["decision"] for row in rows] == ["intent", "denied"]
    assert [row["allowed"] for row in rows] == [0, 0]
    assert len({row["call_id"] for row in rows}) == 1
    assert rows[-1]["reason_code"] == "ssrf_refused"
    # The whole point: the log can answer "what URL did this agent try to fetch?"
    assert all(row["target_host"] == "169.254.169.254" for row in rows)
    assert all(row["path"] == "/latest/meta-data/iam/" for row in rows)


def test_a_refused_redirect_is_audited_against_the_hop_that_caused_it(
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    _raw, store = audit_store

    with pytest.raises(BrokerDenied):
        fetch(
            "https://fixture.example/start",
            grant_store=_FormationGrant(),
            grant_holder=HOLDER,
            audit_store=store,
            audit_context=CONTEXT,
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    302,
                    headers={"location": "http://169.254.169.254/latest/meta-data/"},
                    request=r,
                )
            ),
            host_resolver=_public_dns,
        )

    rows = list(reversed(store.call_log(run_id="run-web-read")))
    assert [row["decision"] for row in rows] == ["intent", "denied"]
    assert rows[0]["target_host"] == "fixture.example"
    assert rows[0]["path"] == "/start"
    assert rows[-1]["target_host"] == "169.254.169.254"
    assert rows[-1]["path"] == "/latest/meta-data/"
    assert rows[-1]["reason_code"] == "ssrf_refused"


def test_an_ungranted_call_is_recorded_as_a_denial(
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    _raw, store = audit_store

    with pytest.raises(BrokerDenied):
        fetch(
            "https://fixture.example/secret",
            granted=["web.fetch"],
            grant_store=_FormationGrant(),
            grant_holder=HOLDER,
            audit_store=store,
            audit_context=CONTEXT,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, request=r)),
            host_resolver=_public_dns,
        )

    rows = list(reversed(store.call_log(run_id="run-web-read")))
    assert [row["decision"] for row in rows] == ["intent", "denied"]
    assert rows[-1]["reason_code"] == "caller_supplied_grant"


def test_three_ssrf_probes_and_one_fetch_produce_three_denials(
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    """The reviewer's measurement, as an assertion: 5 rows / 0 denials was the bug."""
    _raw, store = audit_store
    grants = _FormationGrant()
    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/latest/meta-data/iam/",
        "http://127.0.0.1:8485/api/capability",
    ):
        with pytest.raises(BrokerDenied):
            fetch(
                url,
                grant_store=grants,
                grant_holder=HOLDER,
                audit_store=store,
                audit_context=CONTEXT,
                transport=httpx.MockTransport(lambda r: httpx.Response(200, request=r)),
                host_resolver=_public_dns,
            )
    fetch(
        "https://fixture.example/real",
        grant_store=grants,
        grant_holder=HOLDER,
        audit_store=store,
        audit_context=CONTEXT,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"ok", request=r)),
        host_resolver=_public_dns,
    )

    rows = store.call_log(run_id="run-web-read")
    decisions = [row["decision"] for row in rows]
    assert decisions.count("intent") == 4
    assert decisions.count("denied") == 3
    assert decisions.count("allowed") == 1
    assert len(rows) == 8


def test_an_unwritable_intent_row_refuses_the_fetch_instead_of_silently_egressing() -> None:
    seen: list[str] = []

    class _BrokenAudit:
        def log_call(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("audit sink is down")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"SECRET-PAGE-BODY", request=request)

    with pytest.raises(BrokerDenied) as refused:
        fetch(
            "https://fixture.example/x",
            grant_store=_FormationGrant(),
            grant_holder=HOLDER,
            audit_store=_BrokenAudit(),
            audit_context=CONTEXT,
            transport=httpx.MockTransport(handler),
            host_resolver=_public_dns,
        )
    assert refused.value.reason == "audit_unavailable"
    assert seen == []


def test_an_unwritable_terminal_row_is_reported_not_swallowed() -> None:
    class _IntentOnlyAudit:
        def __init__(self) -> None:
            self.calls = 0

        def log_call(self, *args: Any, **kwargs: Any) -> None:
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("audit sink died mid-call")

    with pytest.raises(BrokerDenied) as refused:
        fetch(
            "https://fixture.example/x",
            grant_store=_FormationGrant(),
            grant_holder=HOLDER,
            audit_store=_IntentOnlyAudit(),
            audit_context=CONTEXT,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"ok", request=r)),
            host_resolver=_public_dns,
        )
    assert refused.value.reason == "audit_finalization_failed"


# ---------------------------------------------------------------------------
# SSRF encodings and redirect bounds (kept from the repair branch).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://127.0.0.1/x", "ssrf_refused"),
        ("http://192.168.1.1/x", "ssrf_refused"),
        ("http://169.254.169.254/latest/meta-data/", "ssrf_refused"),
        ("file:///etc/passwd", "unsupported_scheme"),
        ("gopher://example.com/x", "unsupported_scheme"),
    ],
)
def test_counterfeit_url_is_refused_with_actionable_typed_code(url: str, code: str) -> None:
    with pytest.raises(BrokerDenied) as refused:
        _call(url, lambda request: httpx.Response(200, request=request))
    assert refused.value.reason == code
    assert refused.value.next_action


def test_redirect_to_private_host_is_checked_before_private_request() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "http://127.0.0.1/internal"}, request=request)

    with pytest.raises(BrokerDenied) as refused:
        _call("https://fixture.example/start", handler)
    assert refused.value.reason == "ssrf_refused"
    assert refused.value.next_action
    assert seen == ["fixture.example"]


def test_oversized_response_is_truncated_at_one_mebibyte() -> None:
    result = _call(
        "https://fixture.example/large",
        lambda request: httpx.Response(200, content=b"x" * (FETCH_MAX_RESPONSE_BYTES + 1), request=request),
    )
    assert result["body"]["receipt"]["truncated"] is True
    assert result["receipt"]["size_bytes"] == FETCH_MAX_RESPONSE_BYTES


def test_redirect_limit_is_typed_and_actionable() -> None:
    with pytest.raises(BrokerDenied) as refused:
        _call(
            "https://fixture.example/loop",
            lambda request: httpx.Response(302, headers={"location": "/loop"}, request=request),
        )
    assert refused.value.reason == "redirect_limit_exceeded"
    assert refused.value.next_action


def test_no_toolplane_import() -> None:
    source = Path("omniagentos/connectors/web_read.py").read_text(encoding="utf-8")
    assert "from omniagentos.toolplane" not in source
    assert "import omniagentos.toolplane" not in source


def test_redirect_to_metadata_endpoint_is_refused_request_never_sent() -> None:
    """Counterfeit (a): external → 302 → 169.254.169.254/metadata, request NEVER issued."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        # This response will never be sent; the 302 to metadata is caught
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"}, request=request)

    with pytest.raises(BrokerDenied) as refused:
        _call("https://fixture.example/start", handler)
    assert refused.value.reason == "ssrf_refused"
    # Proof: only the initial external URL was requested, not the metadata endpoint
    assert seen == ["fixture.example"]


def test_redirect_to_control_plane_loopback_is_refused_request_never_sent() -> None:
    """Counterfeit (b): external → 302 → 127.0.0.1:8485 (OmniAgentOS control plane), request NEVER issued."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8485/api/capability"}, request=request)

    with pytest.raises(BrokerDenied) as refused:
        _call("https://fixture.example/start", handler)
    assert refused.value.reason == "ssrf_refused"
    assert seen == ["fixture.example"]


def test_redirect_to_rfc1918_is_refused_request_never_sent() -> None:
    """Counterfeit (c): external → 302 → RFC1918 private range, request NEVER issued."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "http://192.168.1.100/admin"}, request=request)

    with pytest.raises(BrokerDenied) as refused:
        _call("https://fixture.example/start", handler)
    assert refused.value.reason == "ssrf_refused"
    assert seen == ["fixture.example"]


def test_redirect_chain_exceeding_max_hops_is_typed_and_actionable() -> None:
    """Counterfeit (d): redirect chain > 5 hops, typed refusal, redirect_count is truthful."""
    hop_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hop_count
        hop_count += 1
        # Each hop redirects to the next external URL (all pass SSRF checks)
        next_hop = f"https://redirect-{hop_count}.example/page"
        return httpx.Response(302, headers={"location": next_hop}, request=request)

    with pytest.raises(BrokerDenied) as refused:
        _call("https://fixture.example/start", handler)
    assert refused.value.reason == "redirect_limit_exceeded"
    assert refused.value.next_action
    # hop_count should be 6 (tried to make the 6th hop, which exceeds the limit of 5)
    assert hop_count == 6


def test_redirect_cycle_is_refused_without_hanging() -> None:
    """Counterfeit (e): redirect cycle (A→B→A), refused without infinite loop."""
    hop_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hop_count
        hop_count += 1
        # Cycle between two external URLs
        if "page-1" in str(request.url):
            next_url = "https://fixture.example/page-2"
        else:
            next_url = "https://fixture.example/page-1"
        return httpx.Response(302, headers={"location": next_url}, request=request)

    with pytest.raises(BrokerDenied) as refused:
        _call("https://fixture.example/page-1", handler)
    # Should be caught by redirect limit, not infinite loop
    assert refused.value.reason == "redirect_limit_exceeded"
    assert hop_count == 6  # Tried to follow 6 hops (the limit is 5)


def test_legitimate_external_to_external_redirect_is_allowed_with_count() -> None:
    """Counterfeit (f): legitimate external→external redirect is ALLOWED, redirect_count==1."""
    request_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_log.append(request.headers["host"])
        # First request (to start.example) redirects to another external URL
        if request.headers["host"] == "start.example":
            return httpx.Response(302, headers={"location": "https://final.example/article"}, request=request)
        # Final request returns 200
        return httpx.Response(200, content=b"Final content", request=request)

    result = _call("https://start.example/page", handler)
    assert result["body"]["receipt"]["allowed"] is True
    assert result["body"]["receipt"]["redirect_count"] == 1
    # Both requests should have been issued (redirect was legitimate)
    assert request_log == ["start.example", "final.example"]
