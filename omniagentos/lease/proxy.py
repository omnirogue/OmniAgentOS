"""Localhost HTTP CONNECT proxy with a domain allowlist.

Why this exists:
macOS Seatbelt (`sandbox-exec`) CANNOT filter network egress by hostname. It CAN
filter by destination PORT (live-verified on this host: `(remote ip "localhost:*")`
allows ALL outbound because the host half of the filter is a no-op, but
`(remote ip "localhost:<PORT>")` genuinely restricts to that port number). So the only
way to get domain-level egress control for a sandboxed sub-CLI is:

1. deny all network in the SBPL profile,
2. re-allow exactly one ephemeral loopback PORT,
3. run this allowlisting CONNECT proxy on that port in the UNSANDBOXED parent, and
4. inject `HTTPS_PROXY`/`HTTP_PROXY` into the child so its HTTPS client tunnels
   through us.

The proxy is the policy point: it reads the CONNECT target host and refuses anything
outside the allowlist. It NEVER terminates TLS — it is a blind byte-for-byte tunnel
after the allow decision, so it can neither read nor alter provider traffic.
"""

from __future__ import annotations

import logging
import select
import socket
import socketserver
import threading
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "AllowlistProxy",
    "DEFAULT_ALLOWED_PORTS",
    "ProxyStats",
    "domain_allowed",
    "normalize_domain",
    "proxy_env",
]

DEFAULT_ALLOWED_PORTS: tuple[int, ...] = (443,)

_logger = logging.getLogger(__name__)


def normalize_domain(value: str) -> str:
    """Normalize a domain string for comparison or storage.

    Strips whitespace, casefolds to lowercase, strips a trailing dot, and strips
    a leading '*.' only. If the input is empty or invalid (garbage), returns "".
    """
    if not value or not isinstance(value, str):
        return ""
    val = value.strip().casefold()
    if val.endswith("."):
        val = val[:-1]

    allowed_chars = set("abcdefghijklmnopqrstuvwxyz0123456789-._*")
    if not val or not all(c in allowed_chars for c in val):
        return ""
    return val


def domain_allowed(host: str, patterns: Sequence[str]) -> bool:
    """Check if the given host is allowed by any of the patterns.

    Matching is case-insensitive. A pattern matches when:
    - it equals the host exactly, OR
    - it starts with "*." and the host equals the remainder or ends with "." + remainder.

    A bare pattern like 'example.com' matches ONLY 'example.com' (NOT subdomains).
    Empty or garbage host returns False. Empty pattern list returns False (FAIL CLOSED).
    """
    norm_host = normalize_domain(host)
    if not norm_host or "*" in norm_host:
        return False
    if not patterns:
        return False

    for pat in patterns:
        norm_pat = normalize_domain(pat)
        if not norm_pat:
            continue

        if norm_host == norm_pat:
            return True

        if norm_pat.startswith("*."):
            remainder = norm_pat[2:]
            if norm_host == remainder or norm_host.endswith("." + remainder):
                return True

    return False


def proxy_env(url: str) -> dict[str, str]:
    """Return environment variables configured to route traffic through the given proxy URL.

    The NO_PROXY and no_proxy variables are explicitly emptied to prevent the child process
    from bypassing the proxy using inherited environment settings.
    """
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "ALL_PROXY": url,
        "all_proxy": url,
        "NO_PROXY": "",
        "no_proxy": "",
    }


@dataclass
class ProxyStats:
    """Thread-safe stats counter for the AllowlistProxy."""

    allowed: int = 0
    denied: int = 0
    failed: int = 0
    rejected: int = 0


class _ProxyServer(socketserver.ThreadingTCPServer):
    """Custom ThreadingTCPServer holding the AllowlistProxy instance."""

    daemon_threads = True
    allow_reuse_address = True
    proxy_instance: AllowlistProxy


class _ProxyRequestHandler(socketserver.StreamRequestHandler):
    """Handler for the proxy connection.

    Plain-HTTP proxying is deliberately out of scope for v1: every provider API
    is HTTPS, and a forwarding proxy that touched plaintext bodies would be a new
    content-modifying surface.
    """

    server: _ProxyServer

    # This timeout bounds the header-read phase: byte LIMITS alone do not stop a
    # client that connects and sends nothing, or that drips one byte at a time, so
    # a sandboxed child could otherwise pin a parent thread indefinitely.
    # StreamRequestHandler applies this class attribute to self.connection before
    # handle() runs. Left UNANNOTATED so it stays a class-variable override of the
    # base class's ``timeout = None`` rather than a new instance variable.
    timeout = 20.0

    def handle_timeout(self) -> None:
        """Handle connection read timeouts securely.

        Overridden to do nothing and return, allowing the connection to close safely
        instead of propagating socketserver's default exception behavior.
        """
        return

    def handle(self) -> None:
        """Handle an incoming client request.

        Only the CONNECT method is supported. Plain-HTTP proxying is deliberately out
        of scope for v1: every provider API is HTTPS, and a forwarding proxy that
        touched plaintext bodies would be a new content-modifying surface.
        """
        proxy = self.server.proxy_instance
        acquired = proxy._slots.acquire(blocking=False)
        if not acquired:
            with proxy._stats_lock:
                proxy._stats.rejected += 1
            self._respond(503, "Service Unavailable", "503 Service Unavailable")
            return

        try:
            client_sock = self.connection
            upstream_sock: socket.socket | None = None
            try:
                # 1. Read the request line with a bounded reader (8192 bytes limit)
                request_line = self.rfile.readline(8193)
                if len(request_line) > 8192:
                    self._send_error(400, "Bad Request")
                    return
                if not request_line:
                    return

                # 2. Drain and check remaining request headers (32 KiB limit)
                total_header_bytes = len(request_line)
                while True:
                    line = self.rfile.readline(8193)
                    if len(line) > 8192:
                        self._send_error(400, "Bad Request")
                        return
                    total_header_bytes += len(line)
                    if total_header_bytes > 32768:
                        self._send_error(400, "Bad Request")
                        return
                    if line in (b"\r\n", b"\n", b""):
                        break

                # 3. Parse request line (Latin-1 is standard for HTTP headers parsing)
                try:
                    decoded_line = request_line.decode("latin-1").strip()
                except Exception:
                    self._send_error(400, "Bad Request")
                    return

                parts = decoded_line.split()
                if len(parts) != 3:
                    self._send_error(400, "Bad Request")
                    return

                method, target, version = parts

                # 4. Enforce that ONLY CONNECT is supported
                if method != "CONNECT":
                    self._send_error(405, "Method Not Allowed")
                    return

                # 5. Parse CONNECT target (host:port) supporting IPv6 literal brackets
                if target.startswith("["):
                    close_idx = target.find("]")
                    if close_idx == -1:
                        self._send_error(400, "Bad Request")
                        return
                    conn_host = target[1:close_idx]
                    remainder = target[close_idx + 1 :]
                    if not remainder.startswith(":"):
                        self._send_error(400, "Bad Request")
                        return
                    port_str = remainder[1:]
                    check_host = target[: close_idx + 1]  # Keep "[::1]" for domain check
                else:
                    if ":" not in target:
                        self._send_error(400, "Bad Request")
                        return
                    conn_host, _, port_str = target.rpartition(":")
                    check_host = conn_host

                # 6. Validate the port
                valid_port = False
                try:
                    port = int(port_str)
                    if 1 <= port <= 65535:
                        valid_port = True
                except ValueError:
                    pass

                allowed_ports = proxy._allowed_ports
                allowed_domains = proxy._allowed_domains

                # 7. Policy check: port must be in allowed_ports, and domain must be allowed
                if (
                    not valid_port
                    or port not in allowed_ports
                    or not domain_allowed(check_host, allowed_domains)
                ):
                    with proxy._stats_lock:
                        proxy._stats.denied += 1

                    # Single debug line on denial, without bodies/headers
                    _logger.debug("Connection denied: target %s", target)

                    self._respond(403, "Forbidden", f"Forbidden: {target}\n")
                    return

                # 8. Connect to the upstream host
                try:
                    upstream_sock = socket.create_connection(
                        (conn_host, port), timeout=proxy._connect_timeout
                    )
                except OSError:
                    with proxy._stats_lock:
                        proxy._stats.failed += 1
                    # Send 502 Bad Gateway
                    self._respond(502, "Bad Gateway", "502 Bad Gateway")
                    return

                upstream: socket.socket = upstream_sock
                # create_connection leaves the CONNECT timeout on the socket. Clear it:
                # the tunnel below is select-driven, and a lingering per-call timeout
                # would tear down a healthy but momentarily slow provider stream as if
                # the peer had closed it. Idle detection is the select timeout's job.
                upstream.settimeout(None)

                # 9. Send success status
                with proxy._stats_lock:
                    proxy._stats.allowed += 1

                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()

                # Clear the socket timeout on the client connection for the tunnel phase;
                # the relay is select-driven and has its own idle timeout.
                self.connection.settimeout(None)

                # 10. Bidirectional Relay with select.select
                socks = [client_sock, upstream]
                while True:
                    r, _, _ = select.select(socks, [], [], proxy._idle_timeout)
                    if not r:
                        # Timeout reached
                        break

                    closed = False
                    for s in r:
                        src = s
                        dest = upstream if s is client_sock else client_sock
                        try:
                            data = src.recv(8192)
                        except OSError:
                            closed = True
                            break
                        if not data:
                            closed = True
                            break
                        try:
                            dest.sendall(data)
                        except OSError:
                            closed = True
                            break
                    if closed:
                        break

            except (OSError, ValueError):
                # Swallow safely
                pass
            except Exception:  # noqa: BLE001
                # Catch other unforeseen errors to be completely exception-safe
                pass
            finally:
                if upstream_sock is not None:
                    try:
                        upstream_sock.close()
                    except OSError:
                        pass
                try:
                    client_sock.close()
                except OSError:
                    pass
        finally:
            proxy._slots.release()

    def _respond(self, code: int, reason: str, body_text: str) -> None:
        """Write one short plain-text HTTP response; never raises."""
        try:
            body = body_text.encode()
            head = (
                f"HTTP/1.1 {code} {reason}\r\n"
                "Connection: close\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            self.wfile.write(head + body)
            self.wfile.flush()
        except OSError:
            pass

    def _send_error(self, code: int, message: str) -> None:
        """Helper to safely send simple HTTP error responses."""
        self._respond(code, message, f"{code} {message}")


class AllowlistProxy:
    """An allowlisting localhost HTTP CONNECT proxy server.

    This proxy intercepts CONNECT requests from sandboxed processes and ensures
    that egress connections are only allowed to domains in the specified allowlist
    and target ports in the allowed ports list.
    """

    def __init__(
        self,
        allowed_domains: Sequence[str],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        allowed_ports: Sequence[int] = DEFAULT_ALLOWED_PORTS,
        connect_timeout: float = 10.0,
        idle_timeout: float = 300.0,
        max_connections: int = 64,
    ) -> None:
        """Initialize the AllowlistProxy server."""
        self._allowed_domains = allowed_domains
        self._host = host
        self._requested_port = port
        self._allowed_ports = allowed_ports
        self._connect_timeout = connect_timeout
        self._idle_timeout = idle_timeout
        self._max_connections: int = max_connections
        self._slots: threading.BoundedSemaphore = threading.BoundedSemaphore(max_connections)
        self._stats = ProxyStats()
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._server: _ProxyServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Return the actual bound port of the active proxy server, or 0 if not started."""
        if self._server is not None:
            return int(self._server.server_address[1])
        return 0

    @property
    def address(self) -> tuple[str, int]:
        """Return the (host, port) address of the active proxy server."""
        if self._server is not None:
            addr = self._server.server_address
            return (str(addr[0]), int(addr[1]))
        return (self._host, 0)

    @property
    def url(self) -> str:
        """Return the proxy's environment URL (e.g., 'http://127.0.0.1:port')."""
        return f"http://{self._host}:{self.port}"

    @property
    def stats(self) -> ProxyStats:
        """Return the proxy stats counter."""
        with self._stats_lock:
            return ProxyStats(
                allowed=self._stats.allowed,
                denied=self._stats.denied,
                failed=self._stats.failed,
                rejected=self._stats.rejected,
            )

    def start(self) -> tuple[str, int]:
        """Start the proxy server in a background daemon thread.

        This method is idempotent. It returns the bound address (host, port).
        """
        with self._lock:
            if self._server is not None:
                return self.address

            server = _ProxyServer((self._host, self._requested_port), _ProxyRequestHandler)
            server.proxy_instance = self
            self._server = server

            thread = threading.Thread(
                target=server.serve_forever, name="AllowlistProxyThread", daemon=True
            )
            thread.start()
            self._thread = thread

            return self.address

    def stop(self) -> None:
        """Stop the proxy server and close all sockets.

        This method is idempotent and never raises.
        """
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None

        if server is not None:
            try:
                # Close the listening socket promptly to refuse new connections immediately.
                server.socket.close()
            except Exception:
                pass
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass

            # We do not attempt to track or close individual live tunnel connections here.
            # Because daemon_threads = True, they cannot keep the process alive, and
            # forcibly closing a socket that another thread is actively reading or writing
            # introduces race conditions and is its own hazard.

        if thread is not None:
            try:
                thread.join(timeout=5.0)
            except Exception:
                pass

    def __enter__(self) -> AllowlistProxy:
        """Enter context manager, starting the proxy server."""
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Exit context manager, stopping the proxy server."""
        self.stop()
