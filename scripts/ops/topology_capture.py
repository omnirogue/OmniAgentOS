#!/usr/bin/env python3
"""Read-only live-topology snapshot: launchctl, plists, ports, environment.

Emits a machine-generated snapshot (JSON + markdown) of the live system state:
  * launchctl list rows for omniagentos/omniagentos labels
  * plist inventory in ~/Library/LaunchAgents with Program/EnvironmentVariables
  * listening ports (lsof -nP -iTCP -sTCP:LISTEN)
  * resolved OMNIAGENTOS_DB path

Usage:
  python scripts/ops/topology_capture.py [--output-dir var/topology] [--output-base snapshot]

This is the anti-stale-docs artifact: every other package's acceptance is checked
against this machine-generated source of truth. Writes ONLY to its output path;
mutates NOTHING.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LaunchctlEntry:
    """One launchctl list row."""

    pid: int | None
    last_exit_status: int | None
    label: str


@dataclass(frozen=True)
class PlistEntry:
    """One plist from LaunchAgents."""

    label: str
    path: str
    program: str | None
    environment_variables: dict[str, str]


@dataclass(frozen=True)
class ListeningPort:
    """One listening TCP port."""

    protocol: str  # TCP, TCP6, etc.
    local_address: str  # 127.0.0.1, *, etc.
    local_port: int
    pid: int | None
    command: str | None


def _parse_launchctl_list(runner: Any = subprocess.run) -> list[LaunchctlEntry]:
    """Parse `launchctl list` output, filtering to omniagentos/omniagentos labels."""
    try:
        result = runner(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    entries: list[LaunchctlEntry] = []
    for line in result.stdout.splitlines()[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) < 3:
            continue

        pid_str, status_str, label = parts[0], parts[1], parts[2]

        # Filter to our labels
        if not (label.startswith("com.omniagentos.") or label.startswith("com.omniagentos.")):
            continue

        # Parse PID (- if not running)
        try:
            pid: int | None = int(pid_str) if pid_str != "-" else None
        except ValueError:
            pid = None

        # Parse last exit status (- if running or never exited)
        try:
            last_exit: int | None = int(status_str)
        except ValueError:
            last_exit = None

        entries.append(LaunchctlEntry(pid=pid, last_exit_status=last_exit, label=label))

    return entries


def _scan_plists(launchd_dir: Path) -> list[PlistEntry]:
    """Scan ~/Library/LaunchAgents for our plists, extract Program and EnvironmentVariables."""
    entries: list[PlistEntry] = []

    if not launchd_dir.is_dir():
        return entries

    for plist_path in sorted(launchd_dir.glob("*.plist")):
        name = plist_path.name

        # Filter to our plists
        if not (name.startswith("com.omniagentos.") or name.startswith("com.omniagentos.")):
            continue

        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            # Malformed plist; skip
            continue

        label = data.get("Label", plist_path.stem)
        program = data.get("Program")
        env_vars = data.get("EnvironmentVariables", {})

        # Ensure env_vars is a dict (defensive against malformed plists)
        if not isinstance(env_vars, dict):
            env_vars = {}

        entries.append(
            PlistEntry(
                label=str(label) if label else plist_path.stem,
                path=str(plist_path),
                program=str(program) if program else None,
                environment_variables=env_vars,
            )
        )

    return entries


def _get_listening_ports(runner: Any = subprocess.run) -> list[ListeningPort]:
    """Run `lsof -nP -iTCP -sTCP:LISTEN` and parse listening TCP ports."""
    try:
        result = runner(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except Exception:
        return []

    if result.returncode != 0:
        return []

    ports: list[ListeningPort] = []

    for line in result.stdout.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 9:
            continue

        command = parts[0]
        pid_str = parts[1]
        name = parts[8]  # NAME column has address:port

        # Parse PID
        try:
            pid: int | None = int(pid_str)
        except ValueError:
            pid = None

        # Parse address:port
        # Format can be "127.0.0.1:8485" or "*:3003" or "[::1]:8485" etc.
        if ":" in name:
            try:
                # Handle IPv6 with brackets
                if name.startswith("["):
                    host, port_str = name.rsplit("]:", 1)
                    host = host[1:]  # remove leading [
                else:
                    host, port_str = name.rsplit(":", 1)

                port = int(port_str)
                ports.append(
                    ListeningPort(
                        protocol="TCP",
                        local_address=host,
                        local_port=port,
                        pid=pid,
                        command=command,
                    )
                )
            except (ValueError, IndexError):
                # Skip malformed entries
                pass

    return ports


def _resolve_db_path(runner: Any = subprocess.run) -> str | None:
    """Resolve the OMNIAGENTOS_DB environment variable / path resolution."""
    # Try environment variable first
    import os

    db_env = os.environ.get("OMNIAGENTOS_DB")
    if db_env:
        return db_env

    # Try to resolve via launch-env.sh if available (read-only, no mutations)
    try:
        result = runner(
            ["bash", "-c", "set -a && source scripts/launch-env.sh 2>/dev/null && echo $OMNIAGENTOS_DB"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            cwd=Path.cwd(),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    # Default fallback (never changes production state)
    return str(Path.home() / "OmniAgentOS" / "var" / "runtime" / "state.sqlite3")


def capture_topology() -> dict[str, Any]:
    """Capture complete live topology snapshot."""
    now = datetime.now(UTC)

    launchctl_entries = _parse_launchctl_list()
    plist_entries = _scan_plists(Path.home() / "Library" / "LaunchAgents")
    listening_ports = _get_listening_ports()
    db_path = _resolve_db_path()

    return {
        "timestamp": now.isoformat(),
        "launchctl": [asdict(e) for e in launchctl_entries],
        "plists": [asdict(e) for e in plist_entries],
        "listening_ports": [asdict(e) for e in listening_ports],
        "omniagentos_db": db_path,
    }


def _format_markdown(data: dict[str, Any]) -> str:
    """Format topology snapshot as markdown."""
    lines: list[str] = []

    lines.append("# Live System Topology Snapshot")
    lines.append("")
    lines.append(f"**Captured:** {data['timestamp']}")
    lines.append("")

    # OMNIAGENTOS_DB
    lines.append("## Environment")
    lines.append("")
    lines.append(f"- **OMNIAGENTOS_DB:** `{data['omniagentos_db']}`")
    lines.append("")

    # launchctl list
    lines.append("## Launchd Jobs (omniagentos/omniagentos)")
    lines.append("")
    if data["launchctl"]:
        lines.append("| Label | PID | Last Exit Status |")
        lines.append("|-------|-----|------------------|")
        for entry in data["launchctl"]:
            pid = entry["pid"] if entry["pid"] is not None else "—"
            status = entry["last_exit_status"] if entry["last_exit_status"] is not None else "—"
            lines.append(f"| {entry['label']} | {pid} | {status} |")
    else:
        lines.append("*(No omniagentos/omniagentos jobs found in launchctl)*")
    lines.append("")

    # Installed plists
    lines.append("## Installed Plists (~Library/LaunchAgents)")
    lines.append("")
    if data["plists"]:
        lines.append("| Label | Path | Program |")
        lines.append("|-------|------|---------|")
        for entry in data["plists"]:
            program = entry["program"] or "—"
            lines.append(f"| {entry['label']} | `{entry['path']}` | `{program}` |")
        lines.append("")

        # Environment variables (detailed section)
        lines.append("### Environment Variables")
        lines.append("")
        has_env = False
        for entry in data["plists"]:
            if entry["environment_variables"]:
                has_env = True
                lines.append(f"**{entry['label']}:**")
                lines.append("```")
                for key, value in sorted(entry["environment_variables"].items()):
                    lines.append(f"{key}={value}")
                lines.append("```")
                lines.append("")
        if not has_env:
            lines.append("*(No environment variables defined)*")
    else:
        lines.append("*(No omniagentos/omniagentos plists found)*")
    lines.append("")

    # Listening ports
    lines.append("## Listening TCP Ports")
    lines.append("")
    if data["listening_ports"]:
        lines.append("| Address | Port | PID | Command |")
        lines.append("|---------|------|-----|---------|")
        for entry in data["listening_ports"]:
            pid = entry["pid"] if entry["pid"] is not None else "—"
            cmd = entry["command"] or "—"
            lines.append(f"| {entry['local_address']} | {entry['local_port']} | {pid} | {cmd} |")
    else:
        lines.append("*(No listening TCP ports detected)*")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Capture live system topology snapshot (JSON + markdown)."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: var/topology)",
    )
    parser.add_argument(
        "--output-base",
        type=str,
        default="snapshot",
        help="Output file base name without extension (default: snapshot)",
    )

    args = parser.parse_args()

    # Determine output directory
    output_dir = args.output_dir
    if output_dir is None:
        # Default to var/topology relative to repo root (detect via launch-env.sh or fallback)
        repo_root = Path.cwd()
        if not (repo_root / "scripts").exists():
            # Try parent
            repo_root = repo_root.parent
        output_dir = repo_root / "var" / "topology"

    output_dir.mkdir(parents=True, exist_ok=True)

    # Capture data
    try:
        data = capture_topology()
    except Exception as e:
        print(f"Error capturing topology: {e}", file=sys.stderr)
        return 1

    # Write JSON
    json_path = output_dir / f"{args.output_base}.json"
    try:
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Wrote {json_path}")
    except Exception as e:
        print(f"Error writing JSON: {e}", file=sys.stderr)
        return 1

    # Write Markdown
    md_path = output_dir / f"{args.output_base}.md"
    try:
        markdown = _format_markdown(data)
        md_path.write_text(markdown, encoding="utf-8")
        print(f"Wrote {md_path}")
    except Exception as e:
        print(f"Error writing markdown: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
