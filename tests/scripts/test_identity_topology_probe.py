"""Counterfeit coverage for the P0 identity-topology probe.

Two failure modes are the point of this file, because both of them are green:

* An interrupted run must not leave an artifact. Deleting the scratch file from
  a signal handler and then *continuing* rebuilds a partial matrix that still
  looks like a baseline -- a fiction frozen as the expectation, reported as
  success.
* A baseline whose cells are all SKIP compares nothing to nothing. Capturing one
  on a host where the fleet is down makes the gate permanently, vacuously green.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import threading
import time
from http import server as http_server
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "scripts" / "gates" / "identity-topology-probe.sh"

CREDENTIALS = ("no-creds", "forged-owner", "forged-non-owner", "machine-token")
ROUTES = ("public-get", "gated-get", "mutation", "autonomy-route")
CELLS_PER_PORT = len(CREDENTIALS) * len(ROUTES)
FAKE_REVISION = "0" * 40


class _Handler(http_server.BaseHTTPRequestHandler):
    received_headers: list[dict[str, str]] = []
    delay_seconds = 0.0

    def _reply(self) -> None:
        if type(self).delay_seconds:
            time.sleep(type(self).delay_seconds)
        type(self).received_headers.append(dict(self.headers))
        self.send_response(200)
        self.end_headers()

    do_GET = _reply
    do_POST = _reply
    do_PUT = _reply

    def log_message(self, format: str, *args: object) -> None:
        return


class _SlowHandler(_Handler):
    """Keeps a run alive long enough to signal it mid-matrix."""

    received_headers: list[dict[str, str]] = []
    delay_seconds = 0.2


def _server(
    handler: type[_Handler] = _Handler,
) -> tuple[http_server.ThreadingHTTPServer, threading.Thread]:
    server = http_server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _closed_port() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def _run(*args: str, script: Path | None = None, tmpdir: Path | None = None):
    env = None
    if tmpdir is not None:
        env = {**os.environ, "TMPDIR": str(tmpdir)}
    return subprocess.run(
        ["sh", str(script or PROBE), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _matrix_is_in_flight(tmpdir: Path) -> bool:
    """True once the probe has recorded a completed cell.

    `$TMP.rows` is created immediately after the signal traps are installed and
    appended to once per cell, so a non-empty one proves BOTH preconditions the
    callers need: the traps are armed, and the run is still inside the matrix.
    """
    return any(rows.stat().st_size > 0 for rows in tmpdir.glob("*probe.*.rows"))


def _run_and_signal(
    *args: str,
    script: Path | None = None,
    tmpdir: Path,
    startup_budget_s: float = 60.0,
    sig: int = signal.SIGTERM,
    inspect=None,
) -> subprocess.CompletedProcess[str]:
    """Start the probe, optionally look at its scratch state, then signal it.

    2026-08-10: this used to sleep a flat 0.8s and then signal, which made the
    test's own precondition — "the signal lands mid-matrix, after the traps are
    installed" — a bet on how fast a contended box can start `sh`, `mktemp` and
    the first `curl`. Lose that bet and the signal arrives during startup: the
    run dies untrapped, and a suite reads it as the trap regression these two
    nodes exist to detect. Wait for the OBSERVABLE state instead, on a budget
    generous enough that only a genuinely stuck probe can exhaust it (and the
    wait ends the moment the state appears, so a healthy run is FASTER than the
    old flat sleep, not slower).
    """
    env = {**os.environ, "TMPDIR": str(tmpdir)}
    process = subprocess.Popen(
        ["sh", str(script or PROBE), *args],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + startup_budget_s
        while not _matrix_is_in_flight(tmpdir) and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.02)
        if not _matrix_is_in_flight(tmpdir):
            process.kill()
            out, err = process.communicate()
            raise AssertionError(
                "the probe never began its matrix, so there was no live run to "
                f"signal (rc={process.returncode})\n--- stdout ---\n{out}\n"
                f"--- stderr ---\n{err}"
            )
        if inspect is not None:
            inspect()
        process.send_signal(sig)
        stdout, stderr = process.communicate(timeout=60)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def _probe_temp_files(tmpdir: Path) -> list[Path]:
    return sorted(tmpdir.glob("identity-topology-probe*"))


def _all_skip_baseline(port: str) -> str:
    rows = [
        f"{credential}|{port}|{route}|SKIP:unreachable"
        for credential in CREDENTIALS
        for route in ROUTES
    ]
    header = [
        "# identity-topology-probe baseline",
        f"# revision={FAKE_REVISION}",
        f"# skipped_cells={len(rows)}",
        "credential|port|route|status",
    ]
    return "\n".join(header + rows) + "\n"


def test_probe_captures_complete_machine_token_matrix_and_matches_baseline(tmp_path: Path) -> None:
    server, thread = _server()
    try:
        _Handler.received_headers.clear()
        port = str(server.server_port)
        token = tmp_path / "machine-token"
        token.write_text("machine-secret-value\n", encoding="utf-8")
        baseline = tmp_path / "baseline.txt"
        args = ("--ports", port, "--machine-token-file", str(token), "--baseline", str(baseline))

        captured = _run(*args, "--write-baseline")
        assert captured.returncode == 0, captured.stderr
        written = baseline.read_text(encoding="utf-8")
        assert "# revision=" in written
        assert "# skipped_cells=0" in written
        rows = [
            line
            for line in captured.stdout.splitlines()
            if line.startswith(("no-creds|", "forged-", "machine-token|"))
        ]
        assert len(rows) == CELLS_PER_PORT
        assert any(row.startswith("machine-token|") for row in rows)
        assert "machine-secret-value" not in captured.stdout + captured.stderr
        assert any(
            headers.get("X-Session-Token") == "machine-secret-value"
            for headers in _Handler.received_headers
        )

        checked = _run(*args)
        assert checked.returncode == 0, checked.stderr
        assert "PASS: matrix matches revision-stamped baseline" in checked.stderr
    finally:
        server.shutdown()
        thread.join()


def test_counterfeit_topology_change_refuses_against_the_baseline(tmp_path: Path) -> None:
    """A forged baseline status is the negative control: the probe must go red."""
    server, thread = _server()
    try:
        port = str(server.server_port)
        token = tmp_path / "machine-token"
        token.write_text("machine-secret-value\n", encoding="utf-8")
        baseline = tmp_path / "baseline.txt"
        args = ("--ports", port, "--machine-token-file", str(token), "--baseline", str(baseline))
        assert _run(*args, "--write-baseline").returncode == 0

        baseline.write_text(
            baseline.read_text(encoding="utf-8").replace("no-creds|", "counterfeit|", 1),
            encoding="utf-8",
        )
        counterfeit = _run(*args)

        assert counterfeit.returncode == 1
        assert "REFUSED: identity topology differs from baseline" in counterfeit.stderr
    finally:
        server.shutdown()
        thread.join()


def test_unreachable_port_is_explicitly_skipped_not_silently_green(tmp_path: Path) -> None:
    """An all-SKIP capture is refused as a baseline instead of freezing a vacuous gate."""
    port = _closed_port()
    baseline = tmp_path / "baseline.txt"
    args = ("--ports", port, "--baseline", str(baseline))

    captured = _run(*args, "--write-baseline")

    # The cells are still recorded explicitly -- the refusal is about persisting them.
    assert captured.stdout.count("SKIP:unreachable") == CELLS_PER_PORT
    assert captured.returncode == 2, captured.stderr
    assert "REFUSED" in captured.stderr
    assert not baseline.exists()

    # --allow-skips records a partial gap; it never blesses a matrix of nothing.
    forced = _run(*args, "--write-baseline", "--allow-skips")
    assert forced.returncode == 2, forced.stderr
    assert "measure no identity boundary" in forced.stderr
    assert not baseline.exists()


def test_out_of_range_port_is_refused_as_invalid_input(tmp_path: Path) -> None:
    """A value outside TCP's port range must not be recorded as an outage."""
    baseline = tmp_path / "baseline.txt"

    captured = _run(
        "--ports",
        "65536",
        "--baseline",
        str(baseline),
        "--write-baseline",
    )

    assert captured.returncode == 2
    assert "port must be between 1 and 65535" in captured.stderr
    assert "SKIP:unreachable" not in captured.stdout
    assert not baseline.exists()


def test_allow_skips_records_the_skip_count_in_the_baseline_header(tmp_path: Path) -> None:
    server, thread = _server()
    try:
        live_port = str(server.server_port)
        dead_port = _closed_port()
        token = tmp_path / "machine-token"
        token.write_text("machine-secret-value\n", encoding="utf-8")
        baseline = tmp_path / "baseline.txt"
        args = (
            "--ports",
            f"{live_port},{dead_port}",
            "--machine-token-file",
            str(token),
            "--baseline",
            str(baseline),
        )

        refused = _run(*args, "--write-baseline")
        assert refused.returncode == 2, refused.stderr
        assert "SKIP:unreachable" in refused.stderr
        assert "--allow-skips" in refused.stderr
        assert not baseline.exists()

        allowed = _run(*args, "--write-baseline", "--allow-skips")
        assert allowed.returncode == 0, allowed.stderr
        written = baseline.read_text(encoding="utf-8")
        assert f"# skipped_cells={CELLS_PER_PORT}" in written
        assert written.count("SKIP:unreachable") == CELLS_PER_PORT

        checked = _run(*args)
        assert checked.returncode == 0, checked.stderr
        assert "PASS: matrix matches revision-stamped baseline" in checked.stderr
    finally:
        server.shutdown()
        thread.join()


def test_all_skip_baseline_is_refused_on_compare(tmp_path: Path) -> None:
    """Even a hand-written all-SKIP baseline cannot make the gate vacuously green."""
    port = _closed_port()
    baseline = tmp_path / "baseline.txt"
    baseline.write_text(_all_skip_baseline(port), encoding="utf-8")

    compared = _run("--ports", port, "--baseline", str(baseline))

    assert compared.returncode == 2, compared.stderr
    assert "vacuously green" in compared.stderr


def test_comment_only_baseline_refuses_clearly_instead_of_dying_silently(tmp_path: Path) -> None:
    """`grep -v '^#'` exits 1 on a comment-only file; that must not look like 'differs'."""
    server, thread = _server()
    try:
        port = str(server.server_port)
        baseline = tmp_path / "baseline.txt"
        baseline.write_text(
            f"# identity-topology-probe baseline\n# revision={FAKE_REVISION}\n",
            encoding="utf-8",
        )

        compared = _run("--ports", port, "--baseline", str(baseline))

        assert compared.returncode == 2, compared.stderr
        assert "REFUSED: baseline contains no matrix rows" in compared.stderr
    finally:
        server.shutdown()
        thread.join()


def test_machine_token_file_that_is_a_directory_degrades_to_skip(tmp_path: Path) -> None:
    """An unreadable token path is a documented SKIP, not a silent `set -e` death."""
    server, thread = _server()
    try:
        port = str(server.server_port)
        token_dir = tmp_path / "not-a-token"
        token_dir.mkdir()
        baseline = tmp_path / "baseline.txt"

        captured = _run(
            "--ports",
            port,
            "--machine-token-file",
            str(token_dir),
            "--baseline",
            str(baseline),
            "--write-baseline",
        )

        assert captured.returncode == 0, captured.stderr
        assert captured.stdout.count("SKIP:machine-token-unavailable") == len(ROUTES)
        assert "# skipped_cells=4" in baseline.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        thread.join()


def test_token_containing_a_quote_is_refused_not_silently_mis_sent(tmp_path: Path) -> None:
    """An unescaped token would change the header curl sends and mislead the matrix."""
    port = _closed_port()
    baseline = tmp_path / "baseline.txt"
    token = tmp_path / "machine-token"
    token.write_text('bad"token\n', encoding="utf-8")

    captured = _run(
        "--ports", port, "--machine-token-file", str(token), "--baseline", str(baseline)
    )

    assert captured.returncode == 2, captured.stderr
    assert "quote or backslash" in captured.stderr
    assert not baseline.exists()

    token.write_text("back\\slash\n", encoding="utf-8")
    backslashed = _run(
        "--ports", port, "--machine-token-file", str(token), "--baseline", str(baseline)
    )
    assert backslashed.returncode == 2, backslashed.stderr
    assert "quote or backslash" in backslashed.stderr


def test_signalled_run_exits_and_never_persists_a_partial_baseline(tmp_path: Path) -> None:
    """SIGTERM must terminate the run, not resume it with its scratch file deleted."""
    server, thread = _server(_SlowHandler)
    try:
        port = str(server.server_port)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        baseline = tmp_path / "baseline.txt"

        signalled = _run_and_signal(
            "--ports",
            port,
            "--baseline",
            str(baseline),
            "--write-baseline",
            tmpdir=scratch,
        )

        assert signalled.returncode == 130, (signalled.returncode, signalled.stderr)
        assert "wrote revision-stamped baseline" not in signalled.stderr
        assert not baseline.exists()
        # The traps clean up on the signal path too: nothing is left holding a token.
        assert _probe_temp_files(scratch) == []
    finally:
        server.shutdown()
        thread.join()


def test_completeness_assertion_refuses_a_partial_matrix_if_the_signal_trap_regresses(
    tmp_path: Path,
) -> None:
    """Second line of defence, pinned independently of the trap that normally exits.

    The mutated copy reproduces the original bug exactly: the handler deletes the
    scratch files and lets the run continue, so the matrix is rebuilt short.
    """
    original = PROBE.read_text(encoding="utf-8")
    regressed_source = original.replace(
        "trap 'cleanup; exit 130' HUP INT TERM",
        "trap 'cleanup' HUP INT TERM",
    )
    assert regressed_source != original, "signal trap line not found; update this mutation"

    server, thread = _server(_SlowHandler)
    try:
        port = str(server.server_port)
        script = tmp_path / "regressed-probe.sh"
        script.write_text(regressed_source, encoding="utf-8")
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        baseline = tmp_path / "baseline.txt"

        signalled = _run_and_signal(
            "--ports",
            port,
            "--baseline",
            str(baseline),
            "--write-baseline",
            script=script,
            tmpdir=scratch,
        )

        assert signalled.returncode == 2, (signalled.returncode, signalled.stderr)
        assert "REFUSED: incomplete capture" in signalled.stderr
        assert "wrote revision-stamped baseline" not in signalled.stderr
        assert not baseline.exists()
    finally:
        server.shutdown()
        thread.join()


def test_curl_config_holding_the_token_is_unpredictable_and_owner_only(tmp_path: Path) -> None:
    server, thread = _server(_SlowHandler)
    try:
        port = str(server.server_port)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        baseline = tmp_path / "baseline.txt"
        token = tmp_path / "machine-token"
        token.write_text("machine-secret-value\n", encoding="utf-8")
        seen: list[tuple[str, int]] = []

        def inspect() -> None:
            # Stat while the run is live: the traps remove the file on the way out.
            seen.extend(
                (path.name, path.stat().st_mode & 0o777)
                for path in scratch.glob("identity-topology-probe-curl.*")
            )

        signalled = _run_and_signal(
            "--ports",
            port,
            "--machine-token-file",
            str(token),
            "--baseline",
            str(baseline),
            "--write-baseline",
            tmpdir=scratch,
            inspect=inspect,
        )

        assert len(seen) == 1, seen
        name, mode = seen[0]
        assert re.fullmatch(r"identity-topology-probe-curl\.[A-Za-z0-9]{6,}", name), name
        assert mode == 0o600, oct(mode)
        assert signalled.returncode == 130, signalled.stderr
        assert _probe_temp_files(scratch) == []
    finally:
        server.shutdown()
        thread.join()
