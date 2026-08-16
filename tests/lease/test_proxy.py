"""The allowlisting CONNECT proxy, proved end-to-end against a LOCAL stub server.

No test here reaches the internet. ``localhost`` is used as the allowlisted
"domain" because it resolves without DNS, which lets the allow path be exercised
for real — a proxy test that only ever asserted denials would pass just as well if
the proxy refused everything.
"""

from __future__ import annotations

import socket
import threading

import pytest

from omniagentos.lease.proxy import (
    AllowlistProxy,
    domain_allowed,
    normalize_domain,
    proxy_env,
)


class _EchoServer:
    """A stub upstream: accepts, echoes one chunk back, closes."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port: int = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                data = conn.recv(1024)
                if data:
                    conn.sendall(b"UPSTREAM:" + data)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _connect_through(proxy: AllowlistProxy, target: str) -> tuple[str, socket.socket]:
    """Issue one CONNECT through ``proxy`` and return (status line, live socket)."""
    client = socket.create_connection(proxy.address, timeout=5)
    client.settimeout(5)
    client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    status = client.recv(4096).decode("latin-1", "replace").split("\r\n", 1)[0]
    return status, client


# --------------------------------------------------------------------------- #
# Allowlist matching
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("host", "patterns", "expected"),
    [
        ("api.anthropic.com", ["api.anthropic.com"], True),
        ("API.Anthropic.COM", ["api.anthropic.com"], True),
        ("api.anthropic.com.", ["api.anthropic.com"], True),
        # A bare pattern must NOT match subdomains -- an accidental suffix match is
        # an egress hole, so suffix intent has to be spelled out.
        ("evil.api.anthropic.com", ["api.anthropic.com"], False),
        ("api.anthropic.com.evil.test", ["api.anthropic.com"], False),
        ("evil.api.anthropic.com", ["*.api.anthropic.com"], True),
        ("api.anthropic.com", ["*.api.anthropic.com"], True),
        ("notapi.anthropic.com", ["*.api.anthropic.com"], False),
        ("api.anthropic.com", [], False),
        ("", ["api.anthropic.com"], False),
        ("*", ["*.anything"], False),
        ("192.168.1.5", ["api.anthropic.com"], False),
    ],
)
def test_domain_allowed(host: str, patterns: list[str], expected: bool) -> None:
    """Allowlist matching is exact-or-explicit-wildcard, case- and dot-insensitive."""
    assert domain_allowed(host, patterns) is expected


def test_normalize_domain_preserves_the_wildcard() -> None:
    """A wildcard pattern must survive normalization or it silently loses its meaning."""
    assert normalize_domain("  *.Example.COM. ") == "*.example.com"
    assert normalize_domain("Example.COM") == "example.com"
    assert normalize_domain("bad host") == ""
    assert normalize_domain("") == ""


def test_proxy_env_clears_no_proxy() -> None:
    """NO_PROXY is forced EMPTY: an inherited value would be a bypass."""
    env = proxy_env("http://127.0.0.1:9")
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9"
    assert env["https_proxy"] == "http://127.0.0.1:9"
    assert env["NO_PROXY"] == ""
    assert env["no_proxy"] == ""


# --------------------------------------------------------------------------- #
# End-to-end tunnelling
# --------------------------------------------------------------------------- #


def test_allowed_domain_tunnels_bytes_to_the_stub_upstream() -> None:
    """An allowlisted CONNECT establishes a blind, unmodified byte tunnel."""
    upstream = _EchoServer()
    proxy = AllowlistProxy(["localhost"], allowed_ports=(upstream.port,))
    proxy.start()
    try:
        status, client = _connect_through(proxy, f"localhost:{upstream.port}")
        assert "200" in status, status
        client.sendall(b"hello")
        assert client.recv(1024) == b"UPSTREAM:hello"
        client.close()
    finally:
        proxy.stop()
        upstream.close()

    assert proxy.stats.allowed == 1
    assert proxy.stats.denied == 0


def test_disallowed_domain_is_refused_with_403() -> None:
    """A domain outside the allowlist never reaches the upstream at all."""
    upstream = _EchoServer()
    proxy = AllowlistProxy(["api.anthropic.com"], allowed_ports=(upstream.port,))
    proxy.start()
    try:
        status, client = _connect_through(proxy, f"localhost:{upstream.port}")
        client.close()
    finally:
        proxy.stop()
        upstream.close()

    assert "403" in status, status
    assert proxy.stats.denied == 1
    assert proxy.stats.allowed == 0


def test_disallowed_port_is_refused_even_for_an_allowed_domain() -> None:
    """The port allowlist is independent of the domain allowlist."""
    upstream = _EchoServer()
    proxy = AllowlistProxy(["localhost"], allowed_ports=(1,))
    proxy.start()
    try:
        status, client = _connect_through(proxy, f"localhost:{upstream.port}")
        client.close()
    finally:
        proxy.stop()
        upstream.close()

    assert "403" in status, status


def test_empty_allowlist_denies_everything() -> None:
    """FAIL CLOSED: a proxy with no allowed domains is a proxy that allows nothing."""
    upstream = _EchoServer()
    proxy = AllowlistProxy([], allowed_ports=(upstream.port,))
    proxy.start()
    try:
        status, client = _connect_through(proxy, f"localhost:{upstream.port}")
        client.close()
    finally:
        proxy.stop()
        upstream.close()

    assert "403" in status, status


def test_non_connect_method_is_rejected() -> None:
    """Plain-HTTP proxying is out of scope for v1 and must not silently work."""
    proxy = AllowlistProxy(["localhost"])
    proxy.start()
    try:
        client = socket.create_connection(proxy.address, timeout=5)
        client.settimeout(5)
        client.sendall(b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n")
        status = client.recv(4096).decode("latin-1", "replace").split("\r\n", 1)[0]
        client.close()
    finally:
        proxy.stop()

    assert "405" in status, status


def test_start_and_stop_are_idempotent() -> None:
    """A double start returns the same address; a double stop never raises."""
    proxy = AllowlistProxy(["localhost"])
    first = proxy.start()
    second = proxy.start()
    assert first == second
    assert proxy.url == f"http://127.0.0.1:{proxy.port}"
    proxy.stop()
    proxy.stop()


def test_context_manager_releases_the_port() -> None:
    """The listening socket is genuinely released on exit (no descriptor leak)."""
    with AllowlistProxy(["localhost"]) as proxy:
        port = proxy.port
        assert port > 0
    probe = socket.socket()
    probe.settimeout(2)
    with pytest.raises(OSError):
        probe.connect(("127.0.0.1", port))
    probe.close()
