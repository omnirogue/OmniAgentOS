"""LS-003 / D-1 — the deployment half of the dashboard's trusted-hop boundary.

`dashboard/src/middleware.ts` and `serverProxy.ts::requireTrustedHop` are
correct and fail closed by design. The outage was that NOTHING generated,
exported or injected `OMNIAGENTOS_TRUSTED_HOP_SECRET`, so every `/api/**`
request 403'd and the dashboard was dark for users.

These tests pin the carriers of that one value end to end: generated once,
stored 0600, exported to the dashboard, exported to caddy, injected by the
Caddyfile with any inbound copy stripped, and supervised so a missing front
door is NAMED rather than silent.
"""

from __future__ import annotations

import http.client
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "scripts" / "launch-supervised.sh"
CADDYFILE = REPO_ROOT / "configs" / "dashboard-caddy" / "Caddyfile"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from process_supervisor import (  # noqa: E402
    CADDY_CONFIG_RELPATH,
    _safe_log_fragment,
    build_process_specs,
    caddy_port,
    caddy_skip_reason,
    front_door_reason,
    http_healthy,
)

HOP_HEADER = "X-Omni-Trusted-Hop"
PRINCIPAL_HEADER = "Tailscale-User-Login"


# --------------------------------------------------------------------------
# The injector: the Caddyfile
# --------------------------------------------------------------------------


def _caddyfile_text() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


def test_caddyfile_injects_the_hop_secret_at_request_time() -> None:
    body = "\n".join(
        line for line in _caddyfile_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert f"header_up {HOP_HEADER} {{env.OMNIAGENTOS_TRUSTED_HOP_SECRET}}" in body
    # A REQUEST-time placeholder, not a parse-time `{$VAR}` substitution: the
    # secret must never be baked into the adapted config that `caddy adapt`
    # would print.
    assert "{$OMNIAGENTOS_TRUSTED_HOP_SECRET" not in body


def test_caddyfile_never_deletes_the_header_it_injects() -> None:
    """The strip is the Set. A `-Field` line would delete the injected value.

    Caddy applies Delete AFTER Set inside one handler, so the intuitive
    "strip then inject" pair silently produces NO header at all and 403s every
    request — the exact outage this file exists to fix. Measured against caddy
    v2.11.4; see the comment block in the Caddyfile.
    """
    body = "\n".join(
        line for line in _caddyfile_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert f"header_up -{HOP_HEADER}" not in body
    assert f"header_up -{PRINCIPAL_HEADER}" not in body


def test_caddyfile_takes_ownership_of_the_identity_header() -> None:
    """`api/auth/login` mints a signed credential out of `Tailscale-User-Login`.

    Before this proxy existed nothing carried a valid hop header, so that route
    was unreachable. Now that the proxy vouches for the hop, an un-Set identity
    header would let any caller name themselves an arbitrary principal.
    """
    body = "\n".join(
        line for line in _caddyfile_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert f"header_up {PRINCIPAL_HEADER} {{env.OMNIAGENTOS_DASHBOARD_PRINCIPAL}}" in body


def test_caddyfile_does_not_touch_the_browser_truthful_csrf_signals() -> None:
    """S-B1 reads `Origin`/`Sec-Fetch-Site` precisely because a page cannot
    forge them. Rewriting either here would forge them on its behalf."""
    body = _caddyfile_text()
    assert not re.search(r"^\s*header_up\s+-?(Origin|Sec-Fetch-Site)\b", body, re.MULTILINE)


def test_caddyfile_confines_the_listener_with_bind_not_with_the_site_address() -> None:
    """`bind` is the only thing that confines the listener.

    A site address of `127.0.0.1:PORT` reads like a loopback bind and is not
    one: in Caddy the host part of a site address is a Host-header MATCHER and
    the server still listens on every interface. Measured with lsof — without
    the `bind` line caddy reports `*:PORT` and this whole control plane,
    session-token proxy included, is on the LAN.
    """
    body = "\n".join(
        line for line in _caddyfile_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert re.search(r"^\s*bind\s+127\.0\.0\.1\b", body, re.MULTILINE)


def _caddy_binary() -> str:
    import shutil

    caddy = shutil.which("caddy")
    if caddy is None:
        pytest.skip("caddy not installed")
    return caddy


def test_caddyfile_is_valid_config() -> None:
    result = subprocess.run(
        [_caddy_binary(), "validate", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
        env={**os.environ, "OMNIAGENTOS_CADDY_PORT": "3013", "OMNIAGENTOS_DASH_PORT": "3003"},
    )
    assert result.returncode == 0, result.stderr


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_the_real_caddyfile_replaces_forged_headers_end_to_end() -> None:
    """RUN the committed config; do not read it and hope.

    Two defects in this file survived review by inspection and were caught only
    here: the `header_up -Field` / `header_up Field value` pair (Caddy applies
    Delete AFTER Set, so the "strip then inject" spelling deletes the injected
    value and 403s everything), and the site address that looked like a
    loopback bind. Both produce a config that reads correctly. Only traffic
    tells the truth.
    """
    import http.server
    import json
    import threading
    import urllib.request

    caddy = _caddy_binary()
    backend_port, front_port = _free_port(), _free_port()
    secret, principal = "real-secret-abc123", "operator@localhost"

    class Echo(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps(dict(self.headers.items())).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", backend_port), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    proxy = subprocess.Popen(
        [caddy, "run", "--config", str(CADDYFILE), "--adapter", "caddyfile"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "OMNIAGENTOS_CADDY_PORT": str(front_port),
            "OMNIAGENTOS_DASH_PORT": str(backend_port),
            "OMNIAGENTOS_TRUSTED_HOP_SECRET": secret,
            "OMNIAGENTOS_DASHBOARD_PRINCIPAL": principal,
        },
    )
    try:
        seen: dict[str, str] | None = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{front_port}/api/health",
                    headers={
                        # A caller doing its level best to forge both.
                        HOP_HEADER: "FORGED-HOP",
                        PRINCIPAL_HEADER: "attacker@evil.test",
                        "Sec-Fetch-Site": "cross-site",
                        "Origin": "https://evil.test",
                    },
                )
                with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
                    seen = json.loads(response.read())
                break
            except OSError:
                time.sleep(0.25)
        assert seen is not None, "caddy never became reachable"

        # The forged hop is GONE and the proxy's own secret is what arrives.
        assert seen[HOP_HEADER] == secret
        # The identity header is a signing oracle for api/auth/login; the
        # caller must not get to name themselves.
        assert seen[PRINCIPAL_HEADER] == principal
        # ...and the browser-truthful CSRF signals arrive untouched, or the S-B1
        # layer would be reading this proxy's opinion instead of the browser's.
        assert seen["Sec-Fetch-Site"] == "cross-site"
        assert seen["Origin"] == "https://evil.test"
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------
# The generator/exporter: the launcher
# --------------------------------------------------------------------------


def _launcher_text() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def _run_hop_helper(
    tmp_path: Path, script: str, locale: str = "C"
) -> subprocess.CompletedProcess[str]:
    """Exercise the launcher's hop helpers in isolation.

    The launcher's preamble sources the whole product environment, which a unit
    test must not do; the helpers are self-contained, so extract and run them.
    """
    text = _launcher_text()
    start = text.index('HOP_SECRET_FILE="')
    end = text.index("_dashboard()")
    helpers = text[start:end]
    helpers = helpers.replace('HOP_SECRET_FILE="$ROOT/var/secrets/trusted-hop-secret"', "")
    program = (
        "set -euo pipefail\n"
        f'PYBIN="{sys.executable}"\n'
        f'HOP_SECRET_FILE="{tmp_path}/secrets/trusted-hop-secret"\n'
        f"{helpers}\n{script}\n"
    )
    # LC_ALL is explicit because the checks under test are locale-sensitive:
    # bash bracket ranges and [:space:] both change meaning with collation, and
    # a test that inherits the developer's locale would pass or fail by accident.
    return subprocess.run(
        ["bash", "-c", program],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": locale},
    )


def test_secret_is_generated_once_and_is_owner_only(tmp_path: Path) -> None:
    first = _run_hop_helper(tmp_path, "_hop_secret")
    assert first.returncode == 0, first.stderr
    secret = first.stdout.strip()
    assert len(secret) >= 32

    secret_file = tmp_path / "secrets" / "trusted-hop-secret"
    mode = stat.S_IMODE(secret_file.stat().st_mode)
    assert mode == 0o600, (
        f"secret is mode {mode:o}; a hop secret must never be group/world readable"
    )

    # Stable across calls: if a second read minted a new value, caddy and the
    # dashboard would export different secrets and everything would 403 again.
    second = _run_hop_helper(tmp_path, "_hop_secret")
    assert second.stdout.strip() == secret


def test_concurrent_first_starts_cannot_mint_two_different_secrets(tmp_path: Path) -> None:
    """The dashboard and caddy children race on first boot.

    A loser that clobbered the winner's file would hand the two processes
    different secrets — self-inflicted drift, and the same total outage.
    """
    # One file per racer: `_hop_secret` prints WITHOUT a trailing newline (so
    # the value reaches caddy byte-exact), and eight concurrent writes to the
    # same stream would concatenate into a single line that trivially compares
    # equal to itself — a test that could never fail.
    out = tmp_path / "racers"
    out.mkdir()
    result = _run_hop_helper(
        tmp_path,
        f'for i in 1 2 3 4 5 6 7 8; do (_hop_secret > "{out}/$i") & done; wait',
    )
    assert result.returncode == 0, result.stderr

    values = {path.read_text(encoding="utf-8") for path in out.iterdir()}
    assert len(list(out.iterdir())) == 8
    assert len(values) == 1, f"racing starts produced {len(values)} distinct secrets: drift"
    assert values.pop().strip() != ""


def test_export_refuses_to_start_a_boundary_it_cannot_enforce(tmp_path: Path) -> None:
    """An empty secret must fail loudly, never export an empty string.

    An empty expected value comparing equal to an empty header is precisely the
    favourable-absence bug this boundary cannot afford.
    """
    secret_file = tmp_path / "secrets" / "trusted-hop-secret"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("   \n", encoding="utf-8")

    result = _run_hop_helper(tmp_path, "_export_hop_env; echo LEAKED_THROUGH")
    assert result.returncode != 0
    assert "LEAKED_THROUGH" not in result.stdout
    assert "FATAL(trusted-hop)" in result.stderr


@pytest.mark.parametrize(
    ("contents", "label"),
    [
        (" \n", "U+00A0 only"),
        (" \n", "figure space only"),
        ("dead beef\n", "embedded U+00A0"),
        ("dead beef\n", "embedded ASCII space"),
        ("é-secret\n", "non-ASCII letter"),
        ("dead\tbeef\n", "embedded tab"),
    ],
)
def test_a_secret_the_three_readers_would_disagree_about_is_REFUSED(
    tmp_path: Path, contents: str, label: str
) -> None:
    """The contract is printable ASCII with no whitespace, on all three sides.

    bash, Python and TypeScript do not agree about Unicode: bash's `[:space:]`
    under LC_ALL=C leaves U+00A0 where JavaScript's `.trim()` removes it. A file
    holding only U+00A0 used to export a 2-byte "secret" and report SUCCESS
    while the dashboard read it as empty and 403'd everything — the LS-003
    outage, recreated silently by the repair for it. Narrowing the contract to a
    set where all three are identical by construction is what closes that,
    rather than trying to make three languages agree about whitespace.
    """
    secret_file = tmp_path / "secrets" / "trusted-hop-secret"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text(contents, encoding="utf-8")

    result = _run_hop_helper(tmp_path, "_export_hop_env; echo LEAKED_THROUGH")
    assert result.returncode != 0, f"{label} was exported as a usable secret"
    assert "LEAKED_THROUGH" not in result.stdout
    assert "FATAL(trusted-hop)" in result.stderr


def test_the_ascii_gate_accepts_a_real_secret_in_a_utf8_locale(tmp_path: Path) -> None:
    """The gate must not reject what the generator produces.

    The obvious spelling of this check — `case $v in *[!!-~]*)` — REJECTS a
    valid 64-char hex secret under a UTF-8 locale, because bash bracket ranges
    follow the operator's collation. Measured. The check is forced to byte
    semantics; this test is what stops it drifting back.
    """
    secret_file = tmp_path / "secrets" / "trusted-hop-secret"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("a3f" + "0123456789abcdef" * 3 + "d\n", encoding="utf-8")

    for locale in ("C", "en_US.UTF-8"):
        result = _run_hop_helper(tmp_path, "_export_hop_env; echo OK", locale=locale)
        assert result.returncode == 0, f"LC_ALL={locale}: {result.stderr}"
        assert "OK" in result.stdout


def test_fingerprint_matches_the_dashboard_implementation(tmp_path: Path) -> None:
    """An operator compares the launcher's tag with `expected_fp=` in the log.

    If the two implementations disagree, that comparison silently answers
    "different" for identical secrets and turns every 403 into a false drift
    report — worse than no diagnostic at all.
    """
    secret_file = tmp_path / "secrets" / "trusted-hop-secret"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("caddy-injected-secret\n", encoding="utf-8")

    result = _run_hop_helper(tmp_path, "_hop_fingerprint")
    assert result.returncode == 0, result.stderr

    # The same FNV-1a/32 the TypeScript `hopFingerprint` computes.
    digest = 0x811C9DC5
    for byte in b"caddy-injected-secret":
        digest = ((digest ^ byte) * 0x01000193) & 0xFFFFFFFF
    assert result.stdout.strip() == f"{digest:08x}"


def test_fingerprint_agrees_across_all_three_languages_for_a_non_ASCII_value(
    tmp_path: Path,
) -> None:
    """The fingerprint must stay encoding-independent even where the SECRET
    contract would refuse the value.

    `_hop_secret` now rejects non-ASCII, but `_hop_fingerprint` must still tag
    whatever bytes are in the file — an operator diagnosing a hand-rolled
    deployment the launcher never generated is exactly who needs it. So the
    three implementations have to agree for ANY input, not merely for the
    inputs the generator produces.

    d184d668 is FNV-1a/32 over the UTF-8 bytes of "é-secret"; the pre-fix
    TypeScript hashed UTF-16 low bytes and produced e3ca1f2b, so the one
    feature whose job is deciding whether two sides hold the same secret
    reported a mismatch for two sides that held the same secret.
    """
    secret_file = tmp_path / "secrets" / "trusted-hop-secret"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("é-secret\n", encoding="utf-8")

    result = _run_hop_helper(tmp_path, "_hop_fingerprint")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "d184d668"
    assert result.stdout.strip() != "e3ca1f2b"

    # Both TypeScript copies pin the same golden value in their own suites
    # (middleware.hopDiagnostics.test.ts / serverProxy.hopDiagnostics.test.ts);
    # this asserts they still SAY they do, so deleting one cannot go unnoticed.
    for relative in (
        "dashboard/src/middleware.hopDiagnostics.test.ts",
        "dashboard/src/lib/serverProxy.hopDiagnostics.test.ts",
    ):
        assert "d184d668" in (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_both_dashboard_guards_hash_utf8_bytes_not_utf16_code_units() -> None:
    """Pinned in both copies: they are duplicated by design and must not drift."""
    for relative in ("dashboard/src/middleware.ts", "dashboard/src/lib/serverProxy.ts"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        # CODE only. The comment explaining why `charCodeAt` was wrong contains
        # the word, and a naive substring search over the whole file fails on
        # the documentation of the very defect it is guarding against.
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith(("//", "*", "/*"))
        )
        assert "new TextEncoder().encode(value)" in code, relative
        assert "charCodeAt" not in code, relative


def test_absent_secret_fingerprints_as_unset_not_as_a_value(tmp_path: Path) -> None:
    """Unknown is never a favourable value.

    Neither a missing file nor a blank one may produce a real-looking tag: two
    operators comparing the fingerprint of two ABSENT secrets would otherwise
    read "they match" and conclude there is no drift.
    """
    assert _run_hop_helper(tmp_path, "_hop_fingerprint").stdout.strip() == "unset"

    blank = tmp_path / "secrets" / "trusted-hop-secret"
    blank.parent.mkdir(parents=True)
    blank.write_text("  \n", encoding="utf-8")
    assert _run_hop_helper(tmp_path, "_hop_fingerprint").stdout.strip() == "unset"


def test_a_secret_with_stray_whitespace_reaches_both_sides_identically(tmp_path: Path) -> None:
    """Caddy injects this value VERBATIM; the dashboard trims what it compares
    against. If the launcher emitted the raw file, a stray newline or space
    would make the two sides disagree forever with neither visibly at fault."""
    secret_file = tmp_path / "secrets" / "trusted-hop-secret"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("  deadbeefcafe  \n\n", encoding="utf-8")

    result = _run_hop_helper(tmp_path, "_hop_secret")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "deadbeefcafe"


def test_launcher_exports_the_secret_to_both_sides_from_one_file() -> None:
    text = _launcher_text()
    dashboard = text[text.index("_dashboard() {") : text.index("_caddy() {")]
    caddy = text[text.index("_caddy() {") : text.index("_comms_poller() {")]
    # Both derive from `_export_hop_env`, which reads the single secret file.
    assert "_export_hop_env" in dashboard
    assert "_export_hop_env" in caddy
    assert "caddy) _caddy ;;" in text


def test_the_front_door_port_follows_simulation_mode() -> None:
    """A hardcoded front-door port would collide across fleets.

    `launch-env.sh` gives a `--simulate` campaign its own OMNIAGENTOS_DASH_PORT so a
    sim fleet cannot collide with the live one. A fixed caddy port would have
    put the collision back on the front door alone, and the symptom — caddy
    fails to bind, the sim fleet tears down — points nowhere near the cause.
    """
    text = _launcher_text()
    assert 'OMNIAGENTOS_CADDY_PORT="${OMNIAGENTOS_CADDY_PORT:-$((OMNIAGENTOS_DASH_PORT + 10))}"' in text

    probe = subprocess.run(
        ["bash", "-c", 'OMNIAGENTOS_DASH_PORT=3457; echo "$((OMNIAGENTOS_DASH_PORT + 10))"'],
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == "3467"


def test_the_dashboard_exports_the_secret_BEFORE_it_builds_or_starts() -> None:
    """Ordering, not just presence.

    `next build` pins NODE_ENV=production and `npm run start` serves from that
    bundle, so the export has to precede both. This is asserted here because
    tests/archdocs/test_launcher_hygiene.py extracts `_dashboard` into a harness
    and stubs the helper out — a stub cannot notice the call moving after the
    build, and "the secret is exported, just too late" is exactly the kind of
    silent 403 this lane exists to prevent.
    """
    text = _launcher_text()
    body = text[text.index("_dashboard() {") : text.index("_caddy() {")]
    # CODE only. The comments in this function mention `npm run start` while
    # explaining the export, so a raw source search finds the phrase BEFORE the
    # call it is describing and reports the opposite of the truth.
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert code.index("_export_hop_env") < code.index("npm run build")
    assert code.index("_export_hop_env") < code.index("npm run start")


def test_launcher_widens_the_origin_carriers_to_the_caddy_port() -> None:
    """The browser now loads the dashboard from the caddy origin, so the two
    OTHER carriers of "which origin is the dashboard" have to follow it:
    the S-B1 same-origin allowlist, and the API's CORS list (the browser opens
    EventSource straight to :8485, so a stale CORS list silences every live
    stream while the page still looks fine)."""
    text = _launcher_text()
    assert "OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS" in text
    assert "OMNIAGENTOS_CORS_EXTRA_PORTS" in text
    # Appended, never clobbering an operator-configured value.
    assert '"${OMNIAGENTOS_CORS_EXTRA_PORTS},$OMNIAGENTOS_CADDY_PORT"' in text


# --------------------------------------------------------------------------
# The supervisor
# --------------------------------------------------------------------------


def test_caddy_is_supervised_as_a_core_member_when_it_can_run(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / CADDY_CONFIG_RELPATH.parent).mkdir(parents=True)
    (root / CADDY_CONFIG_RELPATH).write_text("# stub\n", encoding="utf-8")

    if caddy_skip_reason(root, {}) is not None:
        pytest.skip("caddy not installed on this host")

    specs = build_process_specs(
        root, root / "scripts" / "launcher.sh", tmp_path / "runtime", env={}
    )
    caddy = next(spec for spec in specs if spec.name == "caddy")
    assert caddy.command[-1] == "caddy"
    # Fail closed like every other core member: the front door dying is an
    # outage, not a provider hiccup.
    assert caddy.restart_budget == 0


def _force_caddy_binary(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    """Make the caddy binary's presence a property of the test, not of the host.

    `caddy_skip_reason` returns the binary reason BEFORE the Caddyfile reason,
    so on a machine without caddy the Caddyfile branch is unreachable and the
    absence branch is the only answer.  Letting the host pick meant one of the
    two cases below was always asserting the other one's outcome.  Every lookup
    other than `caddy` is delegated to the real `shutil.which`.
    """
    real_which = shutil.which

    def fake_which(cmd: str, *args: Any, **kwargs: Any) -> str | None:
        if cmd == "caddy":
            return "/usr/local/bin/caddy" if present else None
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", fake_which)


def test_a_missing_caddyfile_is_named_never_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """caddy is installed but its config is absent — the skip names the file."""
    _force_caddy_binary(monkeypatch, present=True)
    root = tmp_path / "repo"
    root.mkdir()
    reason = caddy_skip_reason(root, {})
    assert reason is not None and "Caddyfile" in reason

    specs = build_process_specs(
        root, root / "scripts" / "launcher.sh", tmp_path / "runtime", env={}
    )
    assert not any(spec.name == "caddy" for spec in specs)
    err = capsys.readouterr().err
    assert "caddy) skipped" in err
    assert "refused" in err


def test_a_missing_front_door_is_named_never_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence must not read as presence. A machine with no caddy still starts,
    but says the dashboard's API surface will refuse everything.

    The config is present here so that the binary is the ONLY thing missing —
    otherwise this passes for the wrong reason on a host that has neither.
    """
    _force_caddy_binary(monkeypatch, present=False)
    root = tmp_path / "repo"
    (root / CADDY_CONFIG_RELPATH.parent).mkdir(parents=True)
    (root / CADDY_CONFIG_RELPATH).write_text("# stub\n", encoding="utf-8")
    reason = caddy_skip_reason(root, {})
    assert reason is not None and "PATH" in reason
    assert "Caddyfile" not in reason

    specs = build_process_specs(
        root, root / "scripts" / "launcher.sh", tmp_path / "runtime", env={}
    )
    assert not any(spec.name == "caddy" for spec in specs)
    err = capsys.readouterr().err
    assert "caddy) skipped" in err
    assert "refused" in err


def test_caddy_can_be_disabled_explicitly(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / CADDY_CONFIG_RELPATH.parent).mkdir(parents=True)
    (root / CADDY_CONFIG_RELPATH).write_text("# stub\n", encoding="utf-8")
    assert caddy_skip_reason(root, {"OMNIAGENTOS_CADDY_DISABLE": "1"}) == "OMNIAGENTOS_CADDY_DISABLE=1"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", 3013),
        ("3013", 3013),
        ("4100", 4100),
        ("not-a-port", 3013),
        ("0", 3013),
        ("70000", 3013),
    ],
)
def test_caddy_port_resolution_never_yields_a_bogus_port(value: str, expected: int) -> None:
    assert caddy_port({"OMNIAGENTOS_CADDY_PORT": value}) == expected


def _repo_with_caddyfile(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / CADDY_CONFIG_RELPATH.parent).mkdir(parents=True)
    (root / CADDY_CONFIG_RELPATH).write_text("# stub\n", encoding="utf-8")
    return root


def test_the_front_door_is_checked_through_the_whole_hop_chain(tmp_path: Path) -> None:
    """`/api/health` through the caddy port traverses caddy → middleware →
    catch-all → FastAPI, so it is the acceptance check for LS-003 and the
    detector for secret drift."""
    root = _repo_with_caddyfile(tmp_path)
    if caddy_skip_reason(root, {}) is not None:
        pytest.skip("caddy not installed on this host")

    probed: list[str] = []

    def probe(url: str) -> bool:
        probed.append(url)
        return True

    assert front_door_reason(root, {"OMNIAGENTOS_CADDY_PORT": "3013"}, probe=probe) is None
    assert probed == ["http://127.0.0.1:3013/api/health"]


def test_a_broken_front_door_never_gates_the_fleet_start() -> None:
    """THE CONTAINMENT RULE. A dark dashboard is an observability outage while
    sessions still complete and work still lands; a fleet that will not start is
    a total one. The front door must never be able to trade the second for the
    first — so it is NOT in the fleet's health-URL list, whose failure raises
    SupervisionError and tears every process group down.
    """
    source = (REPO_ROOT / "scripts" / "process_supervisor.py").read_text(encoding="utf-8")
    health_urls = source[source.index("supervisor = ProcessSupervisor(") :][:400]
    assert "caddy" not in health_urls
    assert "front_door" not in health_urls

    # It runs only AFTER the fleet is declared healthy, and its result is
    # printed rather than raised.
    healthy_at = source.index('print("OmniAgentOS is healthy')
    checked_at = source.index("front_door = front_door_reason(args.root, timeout_s=")
    assert healthy_at < checked_at

    # Bound the slice at the statement that hands control to the supervise
    # loop, rather than at an arbitrary character count — a wide window sweeps
    # in the unrelated `except (OSError, SupervisionError)` handler and turns
    # this into a test that fails for a reason it is not about.
    block = source[checked_at : source.index("supervisor.supervise()", checked_at)]
    assert "raise" not in block
    assert "SupervisionError" not in block
    assert "stop_all" not in block
    assert "the rest of the fleet is unaffected" in block


def test_a_broken_front_door_is_reported_by_status_so_it_cannot_go_quiet(tmp_path: Path) -> None:
    """Containment must not become silence — that is the defect LS-003 WAS.

    `status` recomputes the verdict live rather than reading a recorded one: a
    stale "serving" after somebody rotated the secret is the same
    favourable-absence shape this whole change exists to remove.
    """
    root = _repo_with_caddyfile(tmp_path)
    if caddy_skip_reason(root, {}) is not None:
        pytest.skip("caddy not installed on this host")

    reason = front_door_reason(root, {"OMNIAGENTOS_CADDY_PORT": "3013"}, probe=lambda _url: False)
    assert reason is not None
    assert "did not answer" in reason
    # An injected probe supplies no HTTP evidence, so this must NOT claim the
    # hop boundary is at fault — which failure it is, is asserted against real
    # traffic in test_a_403_and_a_squatted_port_are_not_reported_as_the_same_fault.
    assert "before suspecting the hop secret" in reason

    source = (REPO_ROOT / "scripts" / "process_supervisor.py").read_text(encoding="utf-8")
    # Bounded at the next statement, not at a character count: a fixed-width
    # window silently stops covering the branch the moment the branch grows,
    # which is how this same assertion broke once already.
    branch_at = source.index('if args.command == "status":')
    status_branch = source[branch_at : source.index("lock_path = ", branch_at)]
    assert "front_door_reason(args.root)" in status_branch
    # ...and it must not turn a healthy fleet's status into a failure exit.
    assert "return 0 if owned else 1" in status_branch


def test_a_probe_that_RAISES_becomes_a_named_reason_not_an_exception(tmp_path: Path) -> None:
    """The containment guarantee, defeated by a path the earlier tests could not see.

    Every test here exercised `probe=False` — a falsy RETURN. A probe that
    RAISES was never covered, and that is how the guarantee actually broke: a
    listener answering with a malformed status line raises `BadStatusLine`,
    which the old `http_healthy` did not catch, so it escaped `front_door_reason`
    into `_run`'s unconditional `finally: stop_all()` and tore the fleet down
    before `supervise()` ever ran. The caller got a raw exception instead of a
    named reason, no evidence survived, and a later-healthy front door could not
    repair it because the fleet was already gone.
    """
    root = _repo_with_caddyfile(tmp_path)
    if caddy_skip_reason(root, {}) is not None:
        pytest.skip("caddy not installed on this host")

    def exploding(_url: str) -> bool:
        raise http.client.BadStatusLine("THIS IS NOT HTTP")

    reason = front_door_reason(root, {"OMNIAGENTOS_CADDY_PORT": "3013"}, probe=exploding)
    assert isinstance(reason, str) and reason
    assert "BadStatusLine" in reason


@pytest.mark.parametrize(
    "exception",
    [
        http.client.BadStatusLine("THIS IS NOT HTTP"),
        http.client.HTTPException("protocol failure"),
        RuntimeError("something nobody predicted"),
        KeyError("not even an OSError"),
    ],
)
def test_no_probe_failure_mode_can_escape_the_front_door_check(
    tmp_path: Path, exception: Exception
) -> None:
    """Enumerating exception types is what failed the first time.

    The old code caught a TUPLE (OSError/URLError/ValueError) and a sibling
    walked straight through it. The contract is now shape-based, not
    type-based: NOTHING escapes, whatever it is.
    """
    root = _repo_with_caddyfile(tmp_path)
    if caddy_skip_reason(root, {}) is not None:
        pytest.skip("caddy not installed on this host")

    def exploding(_url: str) -> bool:
        raise exception

    reason = front_door_reason(root, {"OMNIAGENTOS_CADDY_PORT": "3013"}, probe=exploding)
    assert isinstance(reason, str) and type(exception).__name__ in reason


def _serve_once(payload: bytes) -> int:
    """A one-shot loopback listener that replies with exactly ``payload``."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(payload)
        except OSError:
            pass
        finally:
            listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def test_a_403_and_a_squatted_port_are_not_reported_as_the_same_fault(tmp_path: Path) -> None:
    """The two faults need OPPOSITE actions, so one message cannot serve both.

    A 403 is the hop failure — go compare fingerprints. A port squatted by some
    process that does not speak HTTP means the dashboard may be perfectly
    healthy and caddy simply is not there; sending that operator to grep for
    `trusted-hop DENIED` lines that will never exist is the same
    misattribution-of-an-unknown this lane exists to remove.
    """
    root = _repo_with_caddyfile(tmp_path)
    if caddy_skip_reason(root, {}) is not None:
        pytest.skip("caddy not installed on this host")

    forbidden = _serve_once(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
    hop = front_door_reason(root, {"OMNIAGENTOS_CADDY_PORT": str(forbidden)})
    assert hop is not None
    assert "HTTP 403" in hop
    assert "trusted-hop DENIED" in hop

    squatted = _serve_once(b"THIS IS NOT HTTP\r\n\r\n")
    wrong_process = front_door_reason(root, {"OMNIAGENTOS_CADDY_PORT": str(squatted)})
    assert wrong_process is not None
    assert "nothing is serving HTTP" in wrong_process
    assert "before suspecting the hop secret" in wrong_process
    # It must NOT blame the boundary it has no evidence about.
    assert "trusted-hop DENIED" not in wrong_process


@pytest.mark.parametrize(
    ("kind", "payload_or_status", "expect_present", "expect_absent"),
    [
        (
            "http",
            403,
            ("HTTP 403", "trusted-hop DENIED"),
            (),
        ),
        (
            "http",
            500,
            ("HTTP 500",),
            ("trusted-hop DENIED",),
        ),
        (
            "http",
            502,
            ("HTTP 502",),
            ("trusted-hop DENIED",),
        ),
        (
            "non_http",
            b"THIS IS NOT HTTP\r\n\r\n",
            ("nothing is serving HTTP", "before suspecting the hop secret"),
            ("trusted-hop DENIED",),
        ),
    ],
)
def test_front_door_reason_is_ternary_not_binary(
    tmp_path: Path,
    kind: str,
    payload_or_status: object,
    expect_present: tuple[str, ...],
    expect_absent: tuple[str, ...],
) -> None:
    """THREE distinct faults, not two, need three distinct verdicts.

    403 is the hop-secret comparison in `middleware.ts` failing — go compare
    fingerprints, and the `grep 'trusted-hop DENIED'` pointer is correct there.
    Any OTHER HTTP status (a 500, a 502, ...) is an application/downstream
    fault — the hop comparison never ran, so that grep pointer would send the
    operator hunting for a log line that will never exist. Never having spoken
    HTTP at all is the third, pre-existing case: a squatted port, nothing to
    do with the dashboard's health.

    A binary classification collapses the middle case into either of the
    other two — this test is the regression guard for that misattribution,
    which is how it survived a first repair pass that only enumerated 403 and
    "never spoke HTTP".
    """
    root = _repo_with_caddyfile(tmp_path)
    if caddy_skip_reason(root, {}) is not None:
        pytest.skip("caddy not installed on this host")

    if kind == "http":
        status = int(payload_or_status)
        reasons = {200: "OK", 403: "Forbidden", 500: "Internal Server Error", 502: "Bad Gateway"}
        payload = f"HTTP/1.1 {status} {reasons[status]}\r\nContent-Length: 0\r\n\r\n".encode()
    else:
        payload = payload_or_status  # type: ignore[assignment]

    port = _serve_once(payload)
    reason = front_door_reason(root, {"OMNIAGENTOS_CADDY_PORT": str(port)})
    assert reason is not None
    for needle in expect_present:
        assert needle in reason, f"expected {needle!r} in {reason!r}"
    for needle in expect_absent:
        assert needle not in reason, f"unexpected {needle!r} in {reason!r}"


def test_http_healthy_reads_a_non_http_listener_as_unhealthy() -> None:
    """A wrong process squatting the port is an ordinary deployment mistake.

    It must read as "not healthy" — never as a crash, because `http_healthy`
    also backs `wait_until_healthy`, where an exception is a fleet teardown.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"THIS IS NOT HTTP\r\n\r\n")
        except OSError:
            pass
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert http_healthy(f"http://127.0.0.1:{port}/api/health") is False
    finally:
        thread.join(timeout=5)


def test_untrusted_probe_detail_cannot_forge_log_lines() -> None:
    """The detail comes off a socket, so it is attacker-chosen bytes."""
    fragment = _safe_log_fragment("bad\r\nWARNING: fleet is fine\x1b[31m")
    assert "\n" not in fragment and "\r" not in fragment and "\x1b" not in fragment
    assert _safe_log_fragment("") == "no detail"


def test_an_unsupervised_front_door_reports_the_consequence_not_just_the_cause(
    tmp_path: Path,
) -> None:
    """A missing caddy is not "fine": it means every /api/** request is refused.
    Absence must never read as presence."""
    root = tmp_path / "repo"
    root.mkdir()
    reason = front_door_reason(root, {}, probe=lambda _url: True)
    assert reason is not None
    assert "not supervised" in reason
    assert "refuse every /api/** request" in reason


def test_the_front_door_probe_waits_before_declaring_failure(tmp_path: Path) -> None:
    """Caddy binds instantly but the dashboard behind it compiles. A single
    impatient probe would report a healthy boundary as broken."""
    root = _repo_with_caddyfile(tmp_path)
    if caddy_skip_reason(root, {}) is not None:
        pytest.skip("caddy not installed on this host")

    attempts = {"n": 0}
    clock = {"t": 0.0}

    def probe(_url: str) -> bool:
        attempts["n"] += 1
        return attempts["n"] >= 4

    assert (
        front_door_reason(
            root,
            {"OMNIAGENTOS_CADDY_PORT": "3013"},
            probe=probe,
            timeout_s=20.0,
            monotonic=lambda: clock["t"],
            sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
        )
        is None
    )
    assert attempts["n"] == 4
