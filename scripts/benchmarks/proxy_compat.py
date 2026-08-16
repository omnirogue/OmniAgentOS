"""Benchmark script to test if provider CLIs honor HTTP/HTTPS proxies.

The autonomy lease's net policy 'proxy' denies direct egress and re-opens exactly
one ephemeral port for a parent-side CONNECT proxy. That only works if the sub-CLI
honours the HTTP_PROXY / HTTPS_PROXY environment variables.

This script answers "does CLI X honour HTTPS_PROXY?" WITHOUT standing up the lease
or a real proxy. It points HTTPS_PROXY at a closed local port and makes one trivial
network-touching call:
  - A CLI that HONOURS the proxy tries to CONNECT to a dead port and fails fast with a
    connection error.
  - A CLI that IGNORES the proxy dials the provider directly and SUCCEEDS.
  - Anything else (timeout, unexpected error) is INCONCLUSIVE and is reported as such,
    never guessed.

Note the inversion: a SUCCESSFUL call here is BAD NEWS -- it proves the CLI bypassed
the proxy, which is exactly why proxy egress mode cannot contain it.

Usage:
    uv run python -m scripts.benchmarks.proxy_compat --clis grok
    uv run python -m scripts.benchmarks.proxy_compat --format json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HONORS = "HONORS"
IGNORES = "IGNORES"
INCONCLUSIVE = "INCONCLUSIVE"
PROBE_PROMPT = "Reply with exactly: OK"
DEFAULT_TIMEOUT_S = 90

# Substrings that mean "the CLI tried to reach the proxy and could not".
PROXY_ERROR_MARKERS = (
    "econnrefused",
    "connection refused",
    "proxy",
    "tunnel",
    "cannot connect",
    "unable to connect",
    "connect etimedout",
)

# One trivial, network-touching invocation per provider CLI. --version is
# deliberately NOT used: it never opens a socket, so it cannot detect proxy
# behaviour. Flags mirror each adapter's own _command in omniagentos/adapters/.
PROBE_COMMANDS: dict[str, list[str]] = {
    "grok": ["grok", "-p", PROBE_PROMPT, "--output-format", "json", "--model", "grok-4.5"],
    "claude": ["claude", "-p", PROBE_PROMPT, "--output-format", "json", "--model", "sonnet"],
    "codex": ["codex", "exec", "--json", PROBE_PROMPT],
    "gemini": ["gemini", "-p", PROBE_PROMPT, "-o", "json", "-m", "gemini-3.6-flash"],
    "kimi": ["kimi", "-p", PROBE_PROMPT, "--output-format", "stream-json"],
}


@dataclass
class ProbeResult:
    """Dataclass holding the result of a single CLI proxy compatibility probe."""

    cli: str
    verdict: str
    returncode: int | None
    duration_s: float
    detail: str
    command: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Convert result to a sorted dictionary."""
        return {
            "cli": self.cli,
            "verdict": self.verdict,
            "returncode": self.returncode,
            "duration_s": self.duration_s,
            "detail": self.detail,
            "command": self.command,
        }


def closed_port() -> int:
    """Bind a socket to ("127.0.0.1", 0), read the assigned port, close it, return the port.

    This is a best-effort dead port -- nothing can guarantee the OS does not hand
    it to another process a moment later, and a re-bound port would show up as
    INCONCLUSIVE rather than a wrong verdict.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def probe_env(port: int, base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy base (default os.environ), set proxy vars to 127.0.0.1:port, clear NO_PROXY.

    Does not mutate the base dictionary.
    """
    env = dict(base if base is not None else os.environ)
    proxy_val = f"http://127.0.0.1:{port}"
    env["HTTP_PROXY"] = proxy_val
    env["HTTPS_PROXY"] = proxy_val
    env["http_proxy"] = proxy_val
    env["https_proxy"] = proxy_val
    env["NO_PROXY"] = ""
    env["no_proxy"] = ""
    return env


def classify(returncode: int | None, output: str, *, timed_out: bool) -> tuple[str, str]:
    """Returns (verdict, detail)."""
    if timed_out:
        return (
            INCONCLUSIVE,
            "timed out after the probe budget; a hang matches BOTH a CLI blocking "
            "on a dead proxy and one blocking on denied direct egress",
        )
    if returncode == 0:
        return (
            IGNORES,
            "call succeeded while HTTPS_PROXY pointed at a closed port, "
            "so the CLI dialled the provider directly",
        )

    output_lower = output.lower()
    for marker in PROXY_ERROR_MARKERS:
        if marker in output_lower:
            return (
                HONORS,
                "failed with a proxy/connection error, so the CLI routed through HTTPS_PROXY",
            )

    collapsed_tail = output[-200:].replace("\n", " ")
    return (
        INCONCLUSIVE,
        f"exited {returncode} with no proxy-related error; output tail: {collapsed_tail}",
    )


def probe_cli(
    cli: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    port: int | None = None,
    runner: Callable[..., Any] | None = None,
) -> ProbeResult:
    """Probe a single provider CLI to test if it honors proxies."""
    command = PROBE_COMMANDS.get(cli)
    if command is None:
        return ProbeResult(
            cli=cli,
            verdict=INCONCLUSIVE,
            returncode=None,
            duration_s=0.0,
            detail=f"no probe command defined for {cli!r}",
            command=[],
        )

    if runner is None and shutil.which(command[0]) is None:
        return ProbeResult(
            cli=cli,
            verdict=INCONCLUSIVE,
            returncode=None,
            duration_s=0.0,
            detail=f"{cli} CLI is not installed on this host",
            command=command,
        )

    port = port or closed_port()
    env = probe_env(port)
    run = runner or subprocess.run

    start_time = time.monotonic()
    timed_out = False
    returncode = None
    output = ""

    try:
        res = run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
        returncode = res.returncode
        output = (res.stdout or "") + (res.stderr or "")
    except subprocess.TimeoutExpired:
        timed_out = True

    duration = round(time.monotonic() - start_time, 2)
    verdict, detail = classify(returncode, output, timed_out=timed_out)

    return ProbeResult(
        cli=cli,
        verdict=verdict,
        returncode=returncode,
        duration_s=duration,
        detail=detail,
        command=command,
    )


def probe_all(
    clis: list[str],
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    runner: Callable[..., Any] | None = None,
) -> list[ProbeResult]:
    """Probe each CLI in the given order and return the results."""
    return [probe_cli(cli, timeout_s=timeout_s, runner=runner) for cli in clis]


def render_markdown(results: list[ProbeResult]) -> str:
    """Render a deterministic markdown table representing probe results."""
    lines = [
        "# Per-CLI proxy compatibility",
        "",
        "If a CLI ignores the proxy, it means proxy egress mode cannot contain that CLI.",
        "",
        "| CLI | Verdict | rc | Seconds | Detail |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for r in results:
        rc_str = str(r.returncode) if r.returncode is not None else "-"
        detail_collapsed = r.detail.replace("\n", " ").replace("\r", "")
        lines.append(f"| {r.cli} | {r.verdict} | {rc_str} | {r.duration_s} | {detail_collapsed} |")

    lines.extend(
        [
            "",
            f"- {HONORS}: The CLI routed through HTTPS_PROXY and failed with a proxy/connection error.",
            f"- {IGNORES}: The CLI bypassed HTTPS_PROXY and succeeded directly.",
            f"- {INCONCLUSIVE}: The probe was inconclusive due to timeout or other non-proxy errors.",
        ]
    )

    content = "\n".join(lines)
    return content.rstrip("\r\n") + "\n"


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(prog="python -m scripts.benchmarks.proxy_compat")
    parser.add_argument(
        "--clis",
        default="grok",
        help="Comma-separated list of CLIs to probe.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        help="Timeout in seconds for each CLI probe.",
    )
    parser.add_argument(
        "--format",
        choices=["md", "json"],
        default="md",
        help="Output format (md or json).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Path to write the output to.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Probe all available CLIs in sorted order, ignoring --clis.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.all:
        clis = sorted(PROBE_COMMANDS.keys())
    else:
        clis = [cli.strip() for cli in args.clis.split(",") if cli.strip()]

    results = probe_all(clis, timeout_s=args.timeout)

    if args.format == "json":
        output_str = json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True) + "\n"
    else:
        output_str = render_markdown(results)

    if args.out:
        try:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(output_str, encoding="utf-8")
        except Exception as e:
            sys.stderr.write(f"Error: failed to write output to {args.out}: {e}\n")
            return 2
    else:
        sys.stdout.write(output_str)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
