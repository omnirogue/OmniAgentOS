"""Your switch for this machine's work telemetry: off / on / status / show-payloads.

SELF-CONTAINED BY DESIGN: stdlib only, no omniagentos imports, so the file can
be copied to any teammate's laptop (``cp`` it into ``~/bin/``) and run with a
bare system ``python3`` — the same deployment story as
``session_collector.py`` and ``transcript_uploader.py``, which is why the
marker constant and ``_iso`` are DELIBERATELY DUPLICATED here.

This is the employee-owned kill switch for the two (and only two) telemetry
feeds this team runs on a dev machine:

- session heartbeat (``session_collector.py``): which AI/terminal sessions
  were recently active, one line each, plus Claude account balance.
- transcript upload (``transcript_uploader.py``): redacted copies of AI
  session transcripts into the team archive repo.

Commands:
  off [--note TEXT]  stop BOTH feeds now. Writes ``~/.ai-telemetry-off``; both
                     scripts check it first and scan/send nothing while it
                     exists. Dashboards show "opted out since <time>" (so you
                     don't just look offline). No approval needed; nothing can
                     re-enable it except you running ``on``.
  on                 remove the marker; collection resumes on the next tick.
  status             ON/OFF, since when, and where the marker lives.
  show-payloads --employee emp_you
                     dry-run both feeds and print EXACTLY what they would
                     send, so you can audit the payloads yourself.

Privacy contract (what is and is not collected, retention, scope):
docs/operations/team-telemetry-privacy.md in the OmniAgentOS repo.
"""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = 1
# Duplicated in session_collector.py / transcript_uploader.py (standalone-copy contract).
OPT_OUT_MARKER = ".ai-telemetry-off"
_FEED_SCRIPTS = ("session_collector.py", "transcript_uploader.py")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _marker(home: Path) -> Path:
    return home / OPT_OUT_MARKER


def _read_marker(home: Path) -> dict | None:
    """Marker contents if telemetry is off, else None. Any file counts as OFF.

    An EXISTING marker that cannot be read (permissions, I/O error) still
    counts as OFF — an unreadable off-switch is still an off-switch; only a
    genuinely absent marker means ON (cross-lineage review finding 1).
    """
    path = _marker(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return {"schema": SCHEMA, "off_since": "unknown (marker unreadable)"}
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("off_since"):
            return data
    except ValueError:
        pass
    try:
        since = _iso(path.stat().st_mtime)
    except OSError:
        since = _iso(time.time())
    return {"schema": SCHEMA, "off_since": since}


def cmd_off(home: Path, note: str | None) -> int:
    existing = _read_marker(home)
    if existing:
        print(f"telemetry: already OFF since {existing.get('off_since')}")
        return 0
    payload = {"schema": SCHEMA, "off_since": _iso(time.time()), "by": getpass.getuser()}
    if note:
        payload["note"] = note
    _marker(home).write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "telemetry: OFF. Both feeds (heartbeat + transcript upload) now scan and "
        "send NOTHING from this machine. Dashboards will show 'opted out since "
        f"{payload['off_since']}'. Run `telemetry_ctl.py on` whenever you choose."
    )
    return 0


def cmd_on(home: Path) -> int:
    path = _marker(home)
    existing = _read_marker(home)
    if existing is None:
        print("telemetry: already ON")
        return 0
    try:
        path.unlink()
    except OSError as exc:
        print(f"telemetry: could not remove {path}: {exc}", file=sys.stderr)
        return 1
    print("telemetry: ON. Collection resumes on the next scheduled tick (hourly).")
    return 0


def cmd_status(home: Path) -> int:
    existing = _read_marker(home)
    if existing:
        print(f"telemetry: OFF since {existing.get('off_since')} (marker: {_marker(home)})")
        if existing.get("note"):
            print(f"  note: {existing['note']}")
    else:
        print(f"telemetry: ON (no {_marker(home)} marker)")
    print(
        "scope: terminal/AI session activity + heartbeat only — no screen, no\n"
        "keystrokes, no browser, no files outside AI session transcripts.\n"
        "audit what would be sent: telemetry_ctl.py show-payloads --employee emp_you"
    )
    return 0


def _find_feed_script(name: str) -> Path | None:
    """The feed script installed next to this file or in ~/bin, if any."""
    for candidate in (Path(__file__).resolve().parent / name, Path.home() / "bin" / name):
        if candidate.is_file():
            return candidate
    return None


def cmd_show_payloads(home: Path, employee: str) -> int:
    """Run both feeds in their dry-run mode so the owner sees the exact payloads."""
    del home  # feeds read their own HOME; the arg keeps the command signature uniform
    failures = 0
    for name in _FEED_SCRIPTS:
        script = _find_feed_script(name)
        print(f"\n===== {name} — exact payload it would send =====")
        if script is None:
            # A feed we cannot show is an INCOMPLETE audit, not a clean one —
            # exit non-zero so absence never reads as "nothing to see"
            # (cross-lineage review finding 5).
            print(f"(not installed on this machine: no {name} beside telemetry_ctl or in ~/bin)")
            failures += 1
            continue
        # --print-text on the uploader includes each file's full redacted body:
        # the exact bytes a push would carry, not just the file list.
        extra = ["--print-text"] if name == "transcript_uploader.py" else []
        result = subprocess.run(
            [sys.executable, str(script), "--employee", employee, "--print", *extra],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        if result.returncode:
            failures += 1
            print(f"({name} exited {result.returncode})", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    off = sub.add_parser("off", help="stop all telemetry from this machine now")
    off.add_argument("--note", help="optional reason recorded in the local marker only")
    sub.add_parser("on", help="resume telemetry")
    sub.add_parser("status", help="show whether telemetry is on or off")
    show = sub.add_parser("show-payloads", help="print exactly what each feed would send")
    show.add_argument("--employee", required=True, help="employee id, e.g. emp_alice")
    args = parser.parse_args(argv)

    home = Path.home()
    if args.command == "off":
        return cmd_off(home, args.note)
    if args.command == "on":
        return cmd_on(home)
    if args.command == "status":
        return cmd_status(home)
    return cmd_show_payloads(home, args.employee)


if __name__ == "__main__":
    raise SystemExit(main())
