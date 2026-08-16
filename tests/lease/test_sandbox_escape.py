"""The escape suite: what a leased child can and cannot ACTUALLY do (Track D, D4).

These are not unit tests of a profile string. Every assertion here launches a real
process under a real ``sandbox-exec`` profile and observes whether the OS refused
it, because a confinement claim that has only been proved against its own generator
has not been proved at all.

Deliberately, and importantly, NO TEST HERE TOUCHES THE INTERNET. The egress cases
target listeners this module binds on loopback, which is strictly stronger evidence
than reaching for a public host would be: under ``deny`` we assert a connection to a
listener we KNOW is up and one hop away still fails, so a pass cannot be an
accidental DNS failure or a flaky network.

The whole module skips cleanly when ``sandbox-exec`` is unavailable or its live
self-test does not demonstrate confinement (``sandbox.sandbox_available()``), which
is the same predicate the adapters' fail-closed floor uses.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from omniagentos.lease.models import LeaseCeilings
from omniagentos.lease.rlimits import make_preexec, rlimit_plan
from omniagentos.runner import sandbox

SBX = "/usr/bin/sandbox-exec"

pytestmark = pytest.mark.skipif(
    not (os.path.exists(SBX) and sandbox.sandbox_available()),
    reason="OS sandbox (sandbox-exec) unavailable or unproven on this host",
)

# A child that reports whether one TCP connect succeeded. Kept as source text (not
# a file) so the child needs no readable script outside the profile's allowances.
_CONNECT_PROBE = (
    "import socket, sys\n"
    "s = socket.socket()\n"
    "s.settimeout(4)\n"
    "try:\n"
    "    s.connect((sys.argv[1], int(sys.argv[2])))\n"
    "    print('CONNECT_OK')\n"
    "except Exception as exc:\n"
    "    print('CONNECT_FAIL', type(exc).__name__)\n"
)


def _run_confined(profile: str, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` under ``profile`` with a hard timeout; never hangs the suite."""
    return subprocess.run(
        [SBX, "-p", profile, *argv],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _accept_forever(sock: socket.socket) -> None:
    """Accept-and-close loop; exits when the socket is closed."""
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        conn.close()


def _non_loopback_ipv4() -> str | None:
    """This host's primary non-loopback IPv4, or None. Sends no packets.

    A connected UDP socket to TEST-NET-1 only performs a route lookup, so this
    discovers the interface address without touching the network.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


class _LoopbackListener:
    """An accept-and-close TCP listener on 127.0.0.1, for egress probes."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            conn.close()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def listener() -> object:
    """A live loopback listener that is definitely reachable when NOT confined."""
    server = _LoopbackListener()
    try:
        yield server
    finally:
        server.close()


# --------------------------------------------------------------------------- #
# Filesystem confinement (unchanged behavior, re-pinned from the lease's angle)
# --------------------------------------------------------------------------- #


def test_write_outside_lease_write_roots_is_denied(tmp_path: Path) -> None:
    """A write outside the granted roots fails at the OS boundary, not by policy."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    victim = tmp_path / "outside.txt"
    profile = sandbox.build_profile(str(workspace))

    _run_confined(profile, ["/bin/sh", "-c", f"echo pwned > {victim}"])

    assert not victim.exists(), "a write outside the workspace was NOT denied"


def test_write_inside_lease_write_roots_succeeds(tmp_path: Path) -> None:
    """The confinement is real but not vacuous: the granted root stays writable."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    keeper = workspace / "ok.txt"
    profile = sandbox.build_profile(str(workspace))

    _run_confined(profile, ["/bin/sh", "-c", f"echo fine > {keeper}"])

    assert keeper.exists(), "the granted write root was not writable"


def test_secret_directory_read_is_denied(tmp_path: Path) -> None:
    """Reading a registered secret store is refused even though reads default open.

    Uses the FIRST entry of the shared secret registry that exists on this host, so
    the test pins the registry the profile actually derives from rather than a
    hardcoded path.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    candidates = [root for root in sandbox.secret_read_deny_roots() if os.path.isdir(root)]
    if not candidates:
        pytest.skip("no registered secret directory exists on this host")
    secret_dir = candidates[0]
    profile = sandbox.build_profile(str(workspace))

    result = _run_confined(profile, ["/bin/sh", "-c", f"ls -1 {secret_dir!r} 2>&1; echo EXIT=$?"])

    assert "Operation not permitted" in result.stdout or "EXIT=0" not in result.stdout, (
        f"a registered secret directory was readable under the profile: {result.stdout!r}"
    )


# --------------------------------------------------------------------------- #
# Egress: the D3 network modes, proved against a local listener
# --------------------------------------------------------------------------- #


def test_default_profile_is_byte_identical_to_the_pre_lease_profile(tmp_path: Path) -> None:
    """THE regression that keeps this lane additive: default calls emit no egress rules.

    ``build_profile(ws)`` and ``build_profile(ws, net_mode="open")`` must be the same
    string, and neither may contain a network directive. If this ever fails, some
    call site that never asked for egress policy has silently acquired one.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()

    default = sandbox.build_profile(str(workspace))
    explicit_open = sandbox.build_profile(str(workspace), net_mode="open")

    assert default == explicit_open
    assert "network" not in default
    assert sandbox.network_rules() == ""


def test_deny_mode_blocks_a_live_loopback_listener(
    listener: _LoopbackListener, tmp_path: Path
) -> None:
    """Under ``deny`` even a loopback connect to a listening socket is refused.

    The loopback target is the point: macOS SBPL cannot express "loopback only" for
    outbound (its host filter is a no-op), so ``deny`` is a TOTAL deny and this test
    is what stops a future "helpful" loopback re-allow from silently reopening all
    egress.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.build_profile(str(workspace), net_mode="deny")

    result = _run_confined(
        profile, [sys.executable, "-c", _CONNECT_PROBE, "127.0.0.1", str(listener.port)]
    )

    assert "CONNECT_FAIL" in result.stdout, f"deny mode allowed egress: {result.stdout!r}"


def test_unconfined_control_reaches_the_same_listener(listener: _LoopbackListener) -> None:
    """Control: the listener IS reachable without a profile, so the deny above is real."""
    result = subprocess.run(
        [sys.executable, "-c", _CONNECT_PROBE, "127.0.0.1", str(listener.port)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "CONNECT_OK" in result.stdout


def test_proxy_mode_allows_only_the_declared_loopback_port(tmp_path: Path) -> None:
    """``proxy`` re-opens exactly the proxy's port and nothing else.

    Two live listeners, one allowlisted port. The allowed port must connect and the
    other must not — that asymmetry is the entire security value of proxy mode,
    since the domain policy is only enforceable if direct egress is impossible.
    """
    allowed = _LoopbackListener()
    other = _LoopbackListener()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        profile = sandbox.build_profile(
            str(workspace), net_mode="proxy", net_loopback_ports=(allowed.port,)
        )

        ok = _run_confined(
            profile, [sys.executable, "-c", _CONNECT_PROBE, "127.0.0.1", str(allowed.port)]
        )
        blocked = _run_confined(
            profile, [sys.executable, "-c", _CONNECT_PROBE, "127.0.0.1", str(other.port)]
        )
    finally:
        allowed.close()
        other.close()

    assert "CONNECT_OK" in ok.stdout, f"the declared proxy port was blocked: {ok.stdout!r}"
    assert "CONNECT_FAIL" in blocked.stdout, f"a non-proxy port was reachable: {blocked.stdout!r}"


def test_proxy_mode_residual_same_port_on_another_host_is_reachable(tmp_path: Path) -> None:
    """PIN THE RESIDUAL: the port allow is host-agnostic, so this is NOT a bypass-proof
    boundary — and the suite says so out loud instead of implying otherwise.

    macOS SBPL enforces only the PORT half of ``remote ip``. A child under proxy
    mode can therefore reach ANY host on the allowed port, and it knows that port
    because ``HTTPS_PROXY`` carries it. This test asserts that reality against a
    listener on a NON-loopback address of this machine. If a future macOS (or a
    different profile) ever made the host filter work, this test would fail — which
    is exactly the signal we want, because the honest-residual docs in
    ``network_rules`` and ``configs/lease.yaml`` would then be out of date.
    """
    non_loopback = _non_loopback_ipv4()
    if non_loopback is None:
        pytest.skip("no non-loopback IPv4 interface on this host")
    remote = socket.socket()
    remote.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    remote.bind((non_loopback, 0))
    remote.listen(4)
    port = remote.getsockname()[1]
    threading.Thread(target=lambda: _accept_forever(remote), daemon=True).start()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        profile = sandbox.build_profile(
            str(workspace), net_mode="proxy", net_loopback_ports=(port,)
        )
        result = _run_confined(
            profile, [sys.executable, "-c", _CONNECT_PROBE, non_loopback, str(port)]
        )
    finally:
        remote.close()

    assert "CONNECT_OK" in result.stdout, (
        "the host filter appears to WORK now -- update the residual documentation in "
        f"network_rules() and configs/lease.yaml. stdout={result.stdout!r}"
    )


def test_proxy_mode_without_ports_degrades_to_deny(
    listener: _LoopbackListener, tmp_path: Path
) -> None:
    """A proxy profile with no ports must fail CLOSED, never fall back to open."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    profile = sandbox.build_profile(str(workspace), net_mode="proxy", net_loopback_ports=())

    result = _run_confined(
        profile, [sys.executable, "-c", _CONNECT_PROBE, "127.0.0.1", str(listener.port)]
    )

    assert "CONNECT_FAIL" in result.stdout
    assert sandbox.network_rules("proxy", ()) == sandbox.network_rules("deny")


def test_unknown_net_mode_raises_rather_than_defaulting_open(tmp_path: Path) -> None:
    """An unrecognized egress mode aborts profile construction (and so the launch)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(ValueError):
        sandbox.build_profile(str(workspace), net_mode="permissive")


# --------------------------------------------------------------------------- #
# Ceilings: rlimits are visible in the child
# --------------------------------------------------------------------------- #


_RLIMIT_PROBE = "import resource, sys\nprint('CPU', resource.getrlimit(resource.RLIMIT_CPU))\n"


def test_cpu_ceiling_is_visible_in_the_child() -> None:
    """``make_preexec`` actually lowers RLIMIT_CPU in the spawned process."""
    ceilings = LeaseCeilings(cpu_s=123.0)
    preexec = make_preexec(ceilings)
    assert preexec is not None, "a positive cpu_s must produce a preexec closure"

    result = subprocess.run(
        [sys.executable, "-c", _RLIMIT_PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        preexec_fn=preexec,
    )

    assert "CPU (123, 128)" in result.stdout, result.stdout


def test_no_ceilings_means_no_preexec() -> None:
    """With every ceiling unset the spawn path is byte-identical (preexec_fn=None)."""
    assert make_preexec(LeaseCeilings()) is None
    assert rlimit_plan(LeaseCeilings()) == []


def test_rss_is_never_applied_as_an_rlimit() -> None:
    """rss_mb is DECLARED but never enforced: on Darwin RLIMIT_RSS aliases RLIMIT_AS.

    Capping address space would kill Node/JIT runtimes outright, so a plan built
    from an rss-only ceiling must be empty and must yield no preexec at all.
    """
    ceilings = LeaseCeilings(rss_mb=4096.0)
    assert rlimit_plan(ceilings) == []
    assert make_preexec(ceilings) is None


def test_a_ceiling_above_the_inherited_limit_never_raises_it() -> None:
    """MONOTONICITY: a ceiling may only tighten, never loosen (review BLOCKER #2).

    The dangerous shape is a lease asking for MORE than the parent's inherited
    SOFT limit. Clamping only against the HARD limit would then RAISE the soft
    limit, and enforce mode would end up less confined than off mode -- which
    inherits the low value untouched.

    Composed preexec: first drop the child to (50, 550) to simulate a restrictive
    inherited environment, then apply a lease ceiling of 450 on top. The correct
    result keeps soft at 50; a naive min(450, 550) would publish 450.
    """
    import resource

    ceiling = make_preexec(LeaseCeilings(cpu_s=450.0))
    assert ceiling is not None

    def restrictive_then_lease() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (50, 550))
        ceiling()

    result = subprocess.run(
        [sys.executable, "-c", _RLIMIT_PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        preexec_fn=restrictive_then_lease,
    )

    assert result.returncode == 0, result.stderr
    assert "CPU (50, 455)" in result.stdout, (
        "a ceiling ABOVE the inherited soft limit changed it -- enforce mode would "
        f"be looser than off mode. stdout={result.stdout!r}"
    )


def test_preexec_never_raises_on_an_impossible_ceiling() -> None:
    """An unapplicable ceiling degrades to telemetry; the child still runs.

    A ceiling far ABOVE the current hard limit would be EPERM if applied naively.
    The closure clamps instead, so the process must still start and exit cleanly.

    NOTE, said plainly because the previous version of this test overstated it
    (security review finding #15): passing here proves the LAUNCH survives, NOT
    that the ceiling was applied. v1 deliberately cannot fail closed on an
    unapplied rlimit -- raising between fork and exec kills the child with an
    opaque error -- which is why ``limit_report`` says "requested", not "enforced".
    """
    import resource

    _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    absurd = 10**9 if hard == resource.RLIM_INFINITY else int(hard) * 1000
    preexec = make_preexec(LeaseCeilings(cpu_s=float(absurd)))
    assert preexec is not None

    result = subprocess.run(
        [sys.executable, "-c", "print('ALIVE')"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        preexec_fn=preexec,
    )

    assert result.returncode == 0
    assert "ALIVE" in result.stdout


# --------------------------------------------------------------------------- #
# The pre-existing guarantee must still hold
# --------------------------------------------------------------------------- #


def test_sandbox_self_test_still_passes() -> None:
    """``sandbox_available()`` still proves confinement after the D3 changes."""
    sandbox.sandbox_available.cache_clear()
    try:
        assert sandbox.sandbox_available() is True
    finally:
        sandbox.sandbox_available.cache_clear()
