"""Brokered U-N1 web fetch and web search with IP-pinned, per-hop SSRF refusal.

K2-14 COST CLASS BINDING:
  - web.fetch: FREE (paid=false; bandwidth only), auto-grantable to research workers.
  - web.search: METERED (paid=true), requires operator D-08 budget grant (Tavily API).

AUTHORIZATION: both entry points are store-backed. ``web.fetch`` cannot reach
the network until :func:`omniagentos.connectors.broker.authorize_with_grant`
has loaded the caller's STANDING grant out of the capability store; ``granted``
is not merely ignored, it is a typed refusal (``caller_supplied_grant``),
because a capability list a caller hands in is the caller vouching for itself
(the S1 privilege-escalation shape). ``web.search`` reaches the same decision
through ``broker.call`` with the same holder-backed arguments.

SSRF GUARD: CHECK-BEFORE-EACH-HOP, AND CONNECT TO WHAT WAS CHECKED. Fetch
disables httpx's automatic redirects and manually loops, checking EVERY hop's
target via :func:`_check_url` BEFORE issuing the next request. The address that
check validated is then PINNED: the request is issued against that IP literal
with the original hostname carried in the ``Host`` header and in the TLS
``sni_hostname`` extension (which httpcore also uses as the certificate's
``server_hostname``, so name verification still binds to the real hostname).

DNS REBINDING IS CLOSED, NOT A CAVEAT. An earlier revision of this module
resolved the hostname, checked the answer, and then handed the HOSTNAME to
httpx — which performed its own, independent ``getaddrinfo``. Those are two
different lookups, so an attacker serving a 0-TTL public answer followed by
``169.254.169.254`` reached cloud metadata deterministically; no timing
precision was involved and calling it "inherently racy / outside scope" was
simply wrong. Pinning the checked address removes the second lookup, so there
is only one resolution and it is the one that was validated.

Defended ranges:
  - Loopback: 127.0.0.0/8, ::1, localhost, *.localhost
  - Link-local: 169.254.0.0/16, fe80::/10 (AWS/ECS metadata endpoints)
  - RFC1918 private: 10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12, fc00::/7
  - Reserved/unspecified: 0.0.0.0/8, ::, and others
  - Non-http(s) schemes (file://, gopher://, etc.)
  - URL userinfo (``https://user:pass@host/``), which is a credential travelling
    on egress by another door than the ``Authorization`` header this refuses.

AUDIT: ``web.fetch`` is credential-free, so ``broker.call`` would take its
unlogged path and record nothing. This module therefore owns the U-A1 pair
itself and writes it the way the broker does: a durable INTENT row before any
egress (an unwritable intent is ``audit_unavailable`` and the fetch does not
happen), then exactly one terminal row — ``allowed``, ``denied``, ``failed`` or
``unavailable``. Refusals are recorded AS refusals, carrying the URL that was
refused, and an audit-write failure is never swallowed.

BOUNDS (U-N1 spec):
  - Response size: truncated at 1 MiB (never refused, never streamed unbounded)
  - Timeout: 10 seconds per request
  - Redirects: max 5 hops; exceeding is a typed refusal, not a silent drop
  - Redirect loops: detected and refused

All response content is sanitized via sanitize_for_persistence() before storage.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from omniagentos.connectors.broker import (
    AuditContext,
    BrokerDenied,
    _audit_capability,
    _audit_failure_reason,
    _audit_row,
    _audit_store_scope,
    _emit_audit_health,
    _reached_provider,
    _terminal_decision,
    authorize_with_grant,
    validate_request,
)
from omniagentos.connectors.broker import call as broker_call
from omniagentos.contracts import digest, new_id
from omniagentos.security.sanitize import sanitize_for_persistence

FETCH_CAPABILITY = "web.fetch"
SEARCH_CAPABILITY = "web.search"
FETCH_TIMEOUT_SECONDS = 10.0
FETCH_MAX_REDIRECTS = 5
FETCH_MAX_RESPONSE_BYTES = 1_048_576  # U-N1: truncate, never return more than 1 MiB.
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
#: The registry path this capability is authorized against. The real URL is
#: chosen per call and is checked by the SSRF guard, not by the path allowlist.
_REGISTRY_PATH = "/"
#: Ceiling on the path recorded in an audit row. Long enough to identify a
#: resource, short enough that a hostile URL cannot bloat the spine.
_AUDIT_PATH_MAX = 512


@dataclass(frozen=True, slots=True)
class _PinnedTarget:
    """One hop that passed the guard, plus the address the guard validated.

    ``url`` is what the caller (or a ``Location`` header) asked for and is what
    a relative redirect resolves against. ``connect_url`` is the same request
    aimed at the validated IP literal, so no second name resolution can occur
    between the check and the connection.
    """

    url: str
    connect_url: str
    host: str
    host_header: str
    address: str


def _refused_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    # Explicitly covers loopback, link-local, RFC1918/IPv6 ULA, reserved and
    # unspecified ranges; ``not is_global`` also fails closed for remaining
    # non-public ranges.
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def _refused_host(host: str) -> bool:
    name = host.strip().lower().rstrip(".")
    if not name or name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return _refused_ip(name)


def _public_dns(host: str) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BrokerDenied("host_resolution_failed", FETCH_CAPABILITY, str(exc)) from exc
    addresses = tuple(sorted({str(record[4][0]) for record in records if record[4]}))
    if not addresses:
        raise BrokerDenied(
            "host_resolution_failed", FETCH_CAPABILITY, "host resolved to no addresses"
        )
    return addresses


def _literal(address: str) -> str:
    """Render one validated address as it must appear in a URL netloc."""
    ip = ipaddress.ip_address(address)
    return f"[{ip.compressed}]" if ip.version == 6 else ip.compressed


def _check_url(
    url: str, resolver: Callable[[str], Iterable[str]] = _public_dns
) -> _PinnedTarget:
    """Fail closed on unsafe scheme, host, or DNS answer, and pin what passed.

    Returning the pinned address is what makes the check binding. Returning only
    the URL — as this did before — left the connection free to resolve the name
    a second time and reach an address this function never saw, which is DNS
    rebinding with no race to win.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise BrokerDenied("invalid_url", FETCH_CAPABILITY, str(exc)) from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise BrokerDenied("unsupported_scheme", FETCH_CAPABILITY, "use an http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise BrokerDenied(
            "invalid_url",
            FETCH_CAPABILITY,
            "URL userinfo is a credential on egress; supply a URL without one",
        )
    host = parts.hostname
    if not host:
        raise BrokerDenied("invalid_url", FETCH_CAPABILITY, "URL must include a host")
    try:
        port = parts.port
    except ValueError as exc:
        raise BrokerDenied("invalid_url", FETCH_CAPABILITY, "URL port is not a number") from exc
    if _refused_host(host):
        raise BrokerDenied(
            "ssrf_refused", FETCH_CAPABILITY, f"refused non-public host {host!r}"
        )
    try:
        ipaddress.ip_address(host)
        address = host
    except ValueError:
        addresses = tuple(resolver(host))
        if not addresses:
            raise BrokerDenied(
                "host_resolution_failed", FETCH_CAPABILITY, "host resolved to no addresses"
            ) from None
        blocked = [candidate for candidate in addresses if _refused_ip(candidate)]
        if blocked:
            raise BrokerDenied(
                "ssrf_refused",
                FETCH_CAPABILITY,
                f"host {host!r} resolved to non-public address {blocked[0]!r}",
            ) from None
        # Every answer was validated, so any of them is safe to pin; taking one
        # is what stops a second lookup from returning an unvalidated sixth.
        address = addresses[0]

    literal = _literal(address)
    suffix = f":{port}" if port is not None else ""
    connect_netloc = f"{literal}{suffix}"
    name = f"[{host}]" if ":" in host else host
    # The fragment never travels on the wire; dropping it keeps the pinned URL
    # byte-comparable with what is actually sent.
    connect_url = urlunsplit((scheme, connect_netloc, parts.path, parts.query, ""))
    return _PinnedTarget(
        url=url,
        connect_url=connect_url,
        host=host,
        host_header=f"{name}{suffix}",
        address=address,
    )


def _audit_facts(url: str) -> tuple[str, str, str]:
    """Return (target_host, path, url_digest) for one audit row.

    The row records WHERE the call went, never WHAT was in it: host and path are
    metadata by the same rule that lets ``target_host`` exist for every other
    capability, and the query string — which is where a URL carries values — is
    represented only by a non-reversible digest of the whole URL. That digest
    still answers "is this the URL I think it is?" for an operator holding a
    candidate, without putting a token that was pasted into a query into the
    audit spine.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "", "", digest(url)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    target_host = f"{host}:{port}" if port is not None else host
    return target_host, (parts.path or "/")[:_AUDIT_PATH_MAX], digest(url)


def _bounded_bytes(response: httpx.Response) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        remaining = FETCH_MAX_RESPONSE_BYTES - size
        if remaining <= 0:
            return b"".join(chunks), True
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


class _Trace:
    """What this fetch attempted, readable after it raised.

    ``url`` is the last hop attempted. A denial has to be audited against the
    URL that caused it — a redirect to ``169.254.169.254`` is a different fact
    from the public URL the call started at, and recording only the latter hides
    the interesting half.

    ``sent`` records whether any request has left this process. ``broker.
    _reached_provider`` answers that question from the exception alone, which is
    right for the broker (every denial but ``transport_error`` is decided before
    the request is built) and wrong here: an ``ssrf_refused`` on hop two is
    raised *after* hop one was issued and answered.
    """

    __slots__ = ("sent", "url")

    def __init__(self, url: str) -> None:
        self.url = url
        self.sent = False


def _fetch_executor(
    url: str,
    method: str,
    content: bytes | str | None,
    headers: Mapping[str, str] | None,
    transport: httpx.BaseTransport | None,
    resolver: Callable[[str], Iterable[str]],
    trace: _Trace,
) -> dict[str, Any]:
    """Perform U-N1 transport after broker grant validation + intent audit.

    Called only from :func:`fetch`, and only after ``authorize_with_grant`` has
    approved the capability against a store-backed standing grant and the intent
    row is durable.
    """
    request_method = method.upper()
    if request_method not in {"GET", "POST"}:
        raise BrokerDenied(
            "method_not_allowed", FETCH_CAPABILITY, "web.fetch permits GET or POST only"
        )
    base_headers = dict(headers or {})
    if any(name.lower() in _SENSITIVE_HEADERS for name in base_headers):
        raise BrokerDenied(
            "request_headers_refused",
            FETCH_CAPABILITY,
            "credential-bearing headers are refused",
        )
    # A caller-supplied Host would decouple the header from the pinned address
    # and hand back the very indirection the pin exists to remove.
    base_headers = {name: value for name, value in base_headers.items() if name.lower() != "host"}
    started = time.monotonic()
    current = url
    request_content = content
    with httpx.Client(
        transport=transport,
        follow_redirects=False,
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
    ) as client:
        for redirects in range(FETCH_MAX_REDIRECTS + 1):
            trace.url = current
            target = _check_url(current, resolver)
            request = client.build_request(
                request_method,
                target.connect_url,
                headers={**base_headers, "Host": target.host_header},
                content=request_content,
                # httpcore reads this as the TLS ``server_hostname``, so the
                # certificate is still verified against the real name even
                # though the socket is opened to the validated address.
                extensions={"sni_hostname": target.host},
            )
            try:
                trace.sent = True
                response = client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise BrokerDenied("transport_error", FETCH_CAPABILITY, str(exc)) from exc
            try:
                location = response.headers.get("location")
                if response.status_code in _REDIRECTS and location:
                    if redirects == FETCH_MAX_REDIRECTS:
                        raise BrokerDenied(
                            "redirect_limit_exceeded",
                            FETCH_CAPABILITY,
                            f"redirect limit is {FETCH_MAX_REDIRECTS} hops",
                        )
                    # Resolve against the LOGICAL url so a relative Location
                    # lands on the host that sent it, then re-check and re-pin.
                    current = urljoin(target.url, location)
                    if response.status_code in {301, 302, 303} and request_method == "POST":
                        request_method, request_content = "GET", None
                    continue
                raw, truncated = _bounded_bytes(response)
                scrubbed, redactions, scrubber_version = sanitize_for_persistence(
                    "web_fetch_response", {"content": raw.decode("utf-8", errors="replace")}
                )
                assert isinstance(scrubbed, Mapping)
                receipt = {
                    "url": target.url,
                    "resolved_ip": target.address,
                    "status_code": response.status_code,
                    "size_bytes": len(raw),
                    "timing_ms": round((time.monotonic() - started) * 1000),
                    "redirect_count": redirects,
                    "redactions": redactions,
                    "scrubber_version": scrubber_version,
                    "truncated": truncated,
                    "allowed": True,
                    "denied": False,
                }
                return {
                    "capability": FETCH_CAPABILITY,
                    "status": response.status_code,
                    "ok": response.is_success,
                    "receipt": receipt,
                    "body": {
                        "content": str(scrubbed.get("content", "")),
                        "receipt": receipt,
                    },
                }
            finally:
                response.close()
    raise AssertionError("redirect loop exhausted unexpectedly")


def fetch(
    url: str,
    *,
    granted: list[str] | None = None,
    method: str = "GET",
    body: bytes | str | None = None,
    headers: Mapping[str, str] | None = None,
    grant_store: Any = None,
    grant_holder: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    audit_store: Any = None,
    audit_context: AuditContext | None = None,
    transport: httpx.BaseTransport | None = None,
    host_resolver: Callable[[str], Iterable[str]] = _public_dns,
) -> dict[str, Any]:
    """Return sanitized public content and a receipt through the broker seam.

    Egress happens only after ``authorize_with_grant`` has approved
    ``web.fetch`` against the standing grant that ``grant_store`` holds for
    ``grant_holder``. There is no unauthorized path: no store, no holder, or a
    caller-supplied ``granted`` list are each a typed refusal recorded in the
    audit spine, not a quiet pass.

    ``granted`` survives as a parameter for exactly one reason — so that a
    caller still passing one gets a loud ``caller_supplied_grant`` denial
    instead of having its list silently ignored while the call proceeds, which
    is what this module did before and what made the parameter dangerous.
    """
    context = (audit_context or AuditContext()).normalized()
    capability = _audit_capability(FETCH_CAPABILITY)
    if capability is None:
        raise BrokerDenied(
            "unknown_capability",
            FETCH_CAPABILITY,
            "web.fetch is not declared in the connector registry",
        )
    call_id = new_id("bcl")
    started = time.monotonic()
    trace = _Trace(url)
    intent_host, intent_path, url_hash = _audit_facts(url)
    reached = False

    try:
        with _audit_store_scope(audit_store) as sink:
            try:
                _audit_row(
                    sink,
                    context,
                    capability,
                    call_id=call_id,
                    method=method,
                    decision="intent",
                    request_schema_hash=url_hash,
                    agent_id=agent_id or "",
                    path=intent_path,
                    target_host=intent_host,
                )
            except Exception as exc:
                # No durable intent, no egress. A free capability is still a
                # capability that reaches the internet from this machine.
                _emit_audit_health(call_id, FETCH_CAPABILITY)
                raise BrokerDenied(
                    "audit_unavailable",
                    FETCH_CAPABILITY,
                    "durable web.fetch intent could not be written",
                ) from exc

            try:
                if granted is not None:
                    raise BrokerDenied(
                        "caller_supplied_grant",
                        FETCH_CAPABILITY,
                        "web.fetch never accepts a caller-supplied capability list; "
                        "thread grant_store + grant_holder instead",
                    )
                if grant_store is None or not grant_holder:
                    raise BrokerDenied(
                        "grant_store_unavailable",
                        FETCH_CAPABILITY,
                        "web.fetch requires a store-backed standing grant "
                        "(grant_store and grant_holder)",
                    )
                cap = authorize_with_grant(
                    FETCH_CAPABILITY,
                    None,
                    grant_store,
                    grant_holder=grant_holder,
                    agent_id=agent_id,
                    method=method,
                    path=_REGISTRY_PATH,
                    project_id=project_id,
                )
                validate_request(cap, method, _REGISTRY_PATH)
                result = _fetch_executor(
                    url, method, body, headers, transport, host_resolver, trace
                )
            except BaseException as exc:
                reached = _reached_provider(exc) or trace.sent
                decision, reason = _terminal_decision(exc)
                denied_host, denied_path, denied_hash = _audit_facts(trace.url)
                try:
                    _audit_row(
                        sink,
                        context,
                        capability,
                        call_id=call_id,
                        method=method,
                        decision=decision,
                        reason_code=reason,
                        latency_ms=round((time.monotonic() - started) * 1000),
                        request_schema_hash=denied_hash,
                        agent_id=agent_id or "",
                        path=denied_path,
                        target_host=denied_host,
                    )
                except Exception as audit_exc:
                    _emit_audit_health(call_id, FETCH_CAPABILITY)
                    raise BrokerDenied(
                        _audit_failure_reason(reached),
                        FETCH_CAPABILITY,
                        "web.fetch finalization could not be written",
                    ) from audit_exc
                raise

            reached = True
            final_host, final_path, final_hash = _audit_facts(
                str(result["receipt"].get("url") or trace.url)
            )
            try:
                _audit_row(
                    sink,
                    context,
                    capability,
                    call_id=call_id,
                    method=method,
                    decision="allowed",
                    latency_ms=round((time.monotonic() - started) * 1000),
                    status=int(result["status"]),
                    request_schema_hash=final_hash,
                    agent_id=agent_id or "",
                    path=final_path,
                    target_host=final_host,
                )
            except Exception as exc:
                _emit_audit_health(call_id, FETCH_CAPABILITY)
                raise BrokerDenied(
                    "audit_finalization_failed",
                    FETCH_CAPABILITY,
                    "web.fetch finalization could not be written",
                ) from exc
            return result
    except BrokerDenied:
        raise
    except Exception as exc:
        _emit_audit_health(call_id, FETCH_CAPABILITY)
        raise BrokerDenied(
            _audit_failure_reason(reached),
            FETCH_CAPABILITY,
            "web.fetch audit store is unavailable",
        ) from exc


def search(
    query: str,
    *,
    granted: list[str] | None = None,
    limit: int = 5,
    grant_store: Any = None,
    grant_holder: str | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
    audit_store: Any = None,
    audit_context: AuditContext | None = None,
) -> dict[str, Any]:
    """Call Tavily basic search after operator grant + D-08 budget admission.

    Like :func:`fetch`, the capability list is never taken from the caller. A
    ``granted`` argument is refused rather than forwarded, because forwarding it
    is precisely the self-vouching path ``broker.call`` still keeps for legacy
    campaign callers (``_call_unlogged`` falls through to ``authorize(cap_id,
    granted or [])`` when no holder is threaded) and precisely the escalation
    S1 closed in the toolplane.
    """
    if granted is not None:
        raise BrokerDenied(
            "caller_supplied_grant",
            SEARCH_CAPABILITY,
            "web.search never accepts a caller-supplied capability list; "
            "thread grant_store + grant_holder instead",
        )
    if grant_store is None or not grant_holder:
        raise BrokerDenied(
            "grant_store_unavailable",
            SEARCH_CAPABILITY,
            "web.search requires a store-backed standing grant (grant_store and grant_holder)",
        )
    if not audit_context or not audit_context.budget_receipt_id:
        raise BrokerDenied(
            "budget_envelope_required",
            SEARCH_CAPABILITY,
            "web.search is paid; attach a D-08 budget-envelope receipt",
        )
    try:
        max_results = min(max(int(limit), 1), 10)
    except (TypeError, ValueError) as exc:
        raise BrokerDenied(
            "invalid_search_request", SEARCH_CAPABILITY, "limit must be an integer"
        ) from exc
    started = time.monotonic()
    result = broker_call(
        SEARCH_CAPABILITY,
        None,
        method="POST",
        path="/search",
        body={
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",  # One Tavily credit; never auto-upgrade.
            "include_raw_content": False,
        },
        grant_store=grant_store,
        grant_holder=grant_holder,
        agent_id=agent_id,
        project_id=project_id,
        timeout=FETCH_TIMEOUT_SECONDS,
        audit_store=audit_store,
        audit_context=audit_context,
    )
    scrubbed, redactions, scrubber_version = sanitize_for_persistence(
        "web_search_response", result["body"]
    )
    return {
        "results": scrubbed,
        "receipt": {
            "status_code": int(result["status"]),
            "timing_ms": round((time.monotonic() - started) * 1000),
            "redactions": redactions,
            "scrubber_version": scrubber_version,
            "allowed": True,
            "denied": False,
            "provider": "tavily",
        },
    }


__all__ = [
    "FETCH_CAPABILITY",
    "FETCH_MAX_REDIRECTS",
    "FETCH_MAX_RESPONSE_BYTES",
    "FETCH_TIMEOUT_SECONDS",
    "SEARCH_CAPABILITY",
    "fetch",
    "search",
]
