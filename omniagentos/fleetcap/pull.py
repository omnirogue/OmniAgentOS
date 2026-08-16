"""Hub-side, conservative rsync-over-SSH transcript puller."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "configs/fleetcap/devices.yaml"
DEFAULT_INGEST = Path("~/.omniagentos/ops/telemetry/ingest").expanduser()
EXCLUDES = (
    "auth.json",
    "config.toml",
    "config.json",
    ".env*",
    "credentials*",
    "*.key",
    "*.pem",
    "oauth*",
    "settings.json",
)


def _safe_root(cli: str, path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or parts[0] != "/" or ".." in parts:
        return False
    if cli == "claude":
        if len(parts) < 3 or parts[-1] != "projects":
            return False
        profile = parts[-2]
        return (
            profile == ".claude"
            or profile.startswith(".claude-account-")
            or profile.startswith(".claude-")
            or (len(parts) >= 4 and parts[-3] == ".claude-accounts")
        )
    if cli == "codex":
        return (
            len(parts) >= 3
            and parts[-1] == "sessions"
            and (parts[-2] == ".codex" or parts[-2].startswith(".codex-"))
        )
    suffixes = {
        "kimi": ((".kimi-code", "sessions"),),
        "grok": ((".grok", "sessions"), (".grok", "memtrace")),
        "gemini": ((".gemini", "history"),),
        "spool": (("Work", "Ops", "telemetry", "spool"),),
    }
    return any(tuple(parts[-len(suffix) :]) == suffix for suffix in suffixes.get(cli, ()))


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        raise ValueError("devices config must contain a devices list")
    return data


def _binary(name: str) -> str:
    found = shutil.which(name)
    if not found or not Path(found).is_absolute():
        raise RuntimeError(f"fleetcap pull: required executable not found: {name}")
    return found


def commands(config: dict[str, Any], ingest: Path) -> list[tuple[str, list[str], int]]:
    ssh = _binary("ssh")
    rsync = _binary("rsync")
    raw_defaults = config.get("defaults")
    defaults: dict[str, Any] = raw_defaults if isinstance(raw_defaults, dict) else {}
    result: list[tuple[str, list[str], int]] = []
    for device in config["devices"]:
        if not isinstance(device, dict):
            continue
        if device.get("mode") != "pull":
            continue
        name = str(device["device"])
        target = f"{device['user']}@{device['host']}"
        timeout = int(device.get("timeout", defaults.get("timeout", 30)))
        result.append((name, [ssh, "-o", "BatchMode=yes", target, "true"], timeout))
        for root in device.get("roots", []):
            cli = str(root["cli"])
            remote_path = str(root["path"])
            if not remote_path.startswith("/") or not _safe_root(cli, remote_path):
                raise ValueError(f"unsafe {cli} root for {name}: {remote_path}")
            destination = (ingest / name / str(root["cli"]) / str(root["account"])).resolve()
            rsync_command = [
                rsync,
                "-a",
                "--update",
                f"--timeout={timeout}",
                f"--bwlimit={int(device.get('bwlimit', defaults.get('bwlimit', 8192)))}",
                f"--max-size={device.get('max_size', defaults.get('max_size', '512m'))}",
                *(flag for pattern in EXCLUDES for flag in ("--exclude", pattern)),
                f"{target}:{remote_path.rstrip('/')}/",
                f"{destination}/",
            ]
            result.append((name, rsync_command, timeout + 10))
    return result


def run(config: dict[str, Any], ingest: Path, *, dry_run: bool = False) -> int:
    ingest = ingest.expanduser().resolve()
    if not dry_run:
        ingest.mkdir(mode=0o700, parents=True, exist_ok=True)
        ingest.chmod(0o700)
    unavailable: set[str] = set()
    attempted: set[str] = set()
    reachable: set[str] = set()
    failed_transfers = 0
    transfer_attempts: dict[str, int] = {}
    transfer_successes: dict[str, int] = {}
    for device, command, timeout in commands(config, ingest):
        rendered = " ".join(command)
        if dry_run:
            print(rendered)
            continue
        if device in unavailable:
            continue
        is_preflight = command[-1] == "true" and Path(command[0]).name == "ssh"
        if is_preflight:
            attempted.add(device)
        if not is_preflight:
            transfer_attempts[device] = transfer_attempts.get(device, 0) + 1
            Path(command[-1]).mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            completed = subprocess.run(command, check=False, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"fleetcap pull: {device}: unavailable ({exc}); skipped", file=sys.stderr)
            unavailable.add(device)
            continue
        if completed.returncode and is_preflight:
            print(f"fleetcap pull: {device}: preflight failed; skipped", file=sys.stderr)
            unavailable.add(device)
        elif is_preflight:
            reachable.add(device)
        elif completed.returncode:
            print(f"fleetcap pull: {device}: rsync exited {completed.returncode}", file=sys.stderr)
            failed_transfers += 1
        else:
            transfer_successes[device] = transfer_successes.get(device, 0) + 1
    zero_transfer_devices = sorted(
        device
        for device, count in transfer_attempts.items()
        if count and transfer_successes.get(device, 0) == 0
    )
    if not dry_run:
        print(
            f"fleetcap pull: summary devices={len(attempted)} reachable={len(reachable)} "
            f"unreachable={len(unavailable)} transfer_failures={failed_transfers} "
            f"zero_transfer_devices={','.join(zero_transfer_devices) or 'none'}",
            file=sys.stderr,
        )
    return 1 if (attempted and not reachable) or zero_transfer_devices else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--ingest-root", type=Path, default=DEFAULT_INGEST)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--extract", action="store_true", help="run the v2 extractor after the pull sweep"
    )
    args = parser.parse_args(argv)
    result = run(load_config(args.config), args.ingest_root, dry_run=args.dry_run)
    if result != 0:
        return result
    if args.extract and not args.dry_run:
        from omniagentos.fleetcap.extract import main as extract_main

        return extract_main(["--ingest-root", str(args.ingest_root), "--config", str(args.config)])
    return result


if __name__ == "__main__":
    raise SystemExit(main())
