#!/usr/bin/env python3
"""Reap MCP server processes whose owning agent session is gone.

WHY THIS EXISTS
---------------
Every agent session (``claude``, ``codex``, ``kimi``, ``grok``, ``gemini``)
eagerly boots its whole MCP roster at startup -- measured 2026-08-13 at ~19
servers and ~1.5 GB per session, for an estate-wide tool-call rate of ~44
calls/week. When an agent turn is killed (the hang-recycler, a crashed CLI, a
closed terminal), its servers are NOT reaped by the parent: they are inherited
and keep running. The loops team measured **51 orphans estate-wide** on
2026-08-09.

Orphaning is triggered by RECYCLE EVENTS, not by elapsed time, so a snapshot
taken between recycles reads zero and proves nothing. (A snapshot on 2026-08-13
did read zero, and that was briefly mistaken for "the leak is not real".)

THE DETECTION RULE, AND WHY IT IS THIS ONE
------------------------------------------
An MCP process is an ORPHAN iff **no process in its ancestor chain is a live
agent CLI**.

Two weaker rules were tried and rejected against the live process table:

* "rooted at ``Terminal.app``" -- REFUTED. Live sessions root at Terminal too,
  so this would have killed every server on the box.
* "older than N hours" -- REFUTED. Legitimate sessions run for days; the oldest
  live session measured was 3d02h with 12 healthy servers.

The ancestor-chain rule was verified 2026-08-13 against 125 live MCP processes
across 8 agent sessions: it attributed every one to its owner, with **0 false
orphans**.

SAFETY POSTURE
--------------
This program kills processes, so it is built to refuse rather than guess:

* **Dry-run is the default.** ``--force`` is required to signal anything.
* **Fail-closed on instrument error.** If the process table cannot be read or
  parsed, it exits non-zero having killed nothing. An unreadable ``ps`` is not
  evidence of orphanhood.
* **Never kills a process with a live agent ancestor**, regardless of age.
* **Never kills a non-MCP process**, regardless of ancestry.
* SIGTERM first, then SIGKILL only after ``--grace`` seconds, and only for pids
  that were already classified as orphans in the SAME scan -- a pid that has
  since been recycled by the OS is not re-targeted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

# Processes whose presence in an ancestor chain means "this server has an owner".
# Matched case-insensitively against argv[0] only (see _is_agent).
#
# The asymmetry that governs every choice here: a false OWNED costs some leaked
# RAM, while a false ORPHAN kills a live session's tooling. So this list errs
# generously.
AGENT_MARKERS = (
    "/claude",
    "bin/codex",
    "bin/kimi",
    "kimi-code",
    "/grok",
    "bin/gemini",
    # npm-installed Gemini runs as `node …/@google/gemini-cli/dist/index.js`,
    # where no "bin/gemini" appears in the script path. The header names gemini
    # as a first-class agent, so its servers must not classify as orphans.
    "gemini-cli",
)

# GUI applications that HOST MCP servers. These were missed entirely in the first
# version, which is the third bug this program's own review turned up:
# `AGENT_MARKERS` held "/claude" and matching was case-SENSITIVE, so
# /Applications/Claude.app/Contents/MacOS/Claude did not match, its servers had
# no agent ancestor, and every one classified as an orphan. Claude Desktop is
# installed and running on this box -- with --force that was a kill of the
# desktop app's tooling.
#
# The general guard is the second rule in _is_agent: an MCP server whose chain
# reaches ANY /Applications/*.app bundle is treated as owned, whether or not the
# host is named here. Desktop MCP hosts are a moving target (Cursor, Windsurf,
# Zed, VS Code, whatever ships next); enumerating them is a losing game, and
# guessing wrong kills a user's editor.
DESKTOP_HOST_MARKERS = (
    "claude.app",
    "cursor.app",
    "windsurf.app",
    "code.app",
    "zed.app",
)

# Interpreters that can HOST an agent CLI. For these, argv[1] (the script) is
# what identifies the agent, not argv[0]. Shells are deliberately absent: the
# whole point of judging argv[1] is that `bash -lc '<script mentioning claude>'`
# has argv[1] == "-lc" and must keep reading as NOT an agent.
INTERPRETERS = ("node", "bun", "deno", "python", "python3")

# Terminal emulators and multiplexers, which must NEVER count as owners.
#
# These are what an orphaned server REPARENTS TO when its agent dies, so
# crediting them as hosts makes every orphan look owned and quietly turns this
# program into a no-op. A first attempt at the desktop-host guard used a bare
# "/applications/ + .app/" catch-all, which matched
# /System/Applications/Utilities/Terminal.app and did exactly that -- caught by
# the existing orphan test, which started failing the moment the guard went in.
TERMINAL_MARKERS = (
    "terminal.app",
    "iterm.app",
    "iterm2.app",
    "ghostty.app",
    "alacritty.app",
    "kitty.app",
    "warp.app",
    "wezterm.app",
    "hyper.app",
    "/tmux",
    "tmux:",
)

# Matched against each ARGUMENT'S BASENAME, never as a substring of the whole
# command line. Package/executable naming is the only reliable signal that an
# argument names a SERVER rather than merely mentioning one.
#
# Two failed attempts are worth recording, because they failed in opposite
# directions and the second was found only by checking against the live table:
#
#   ("mcp", "uvx", "modelcontextprotocol")  -- too loose. Any launcher running a
#       script that merely mentioned mcp matched, so `python3 tools/validate_mcp_roster.py`
#       and `node build.js --out .mcp.json` were reapable.
#   ("mcp-server", "modelcontextprotocol", "/mcp@", "mcp-remote")  -- too tight.
#       It silently stopped detecting playwright-mcp, markitdown-mcp and
#       tavily-mcp, which name themselves with a TRAILING -mcp. A reaper that
#       cannot see a server is not safe, it is just useless, and the failure is
#       invisible: fewer orphans reported looks identical to fewer orphans.
_MCP_NAME_RE = re.compile(
    r"""
    (^mcp-server)          # mcp-server-fetch, mcp-server-git, mcp-server-memory
  | (mcp-server)           # duckduckgo-mcp-server
  | (-mcp$)                # playwright-mcp, markitdown-mcp, tavily-mcp
  | (^mcp$)                # @playwright/mcp@latest  -> basename "mcp"
  | (^mcp-remote$)
    """,
    re.VERBOSE,
)
# Package specs are matched on the whole token instead of a basename.
MCP_PACKAGE_MARKERS = ("@modelcontextprotocol/",)

# Subcommands that INSTALL or MANAGE a server rather than run one. `pip install
# mcp-server-fetch` names a server and is not one; killing it corrupts an install.
# Checked in the first few tokens, where a subcommand always appears.
MANAGEMENT_SUBCOMMANDS = frozenset(
    {"install", "uninstall", "add", "remove", "update", "upgrade", "sync", "lock", "pip", "search"}
)

# Flags whose VALUE is a dependency, not the program being run:
#   uv run --with mcp-server-fetch python x.py
# names a server as a dependency while actually running x.py.
DEPENDENCY_FLAGS = frozenset({"--with", "--from", "-r", "--requirement", "--with-requirements"})

# Server-adjacent tools that are not servers. `@modelcontextprotocol/inspector`
# is the human-run debugging UI; it satisfies the package marker and would
# otherwise be reaped out from under whoever is using it.
NON_SERVER_PACKAGES = ("inspector",)

# Package launchers. A real MCP server is always started by one of these, which
# is what lets _is_mcp judge identity on argv[0] instead of substring-matching
# the whole command line. Observed live on this box: `node …/.bin/mcp-server-*`,
# `npm exec @modelcontextprotocol/server-*`, `npm exec @playwright/mcp@latest`,
# `…/uv tool uvx … mcp-server-fetch`, `…/bin/python …/bin/mcp-server-git`.
LAUNCHERS = frozenset({"node", "npm", "npx", "uv", "uvx", "python", "python3", "bun", "deno"})

# Things that mention "mcp" but are not MCP servers.
#
# ``--strict-mcp-config`` / ``--mcp-config`` are in this list because of a live
# near-miss: the ThreeLoops planning loop launches
# ``claude --model claude-opus-5 ... --strict-mcp-config``, so the AGENT ITSELF
# matched MCP_MARKERS, had no agent ancestor (it is the top of its own chain),
# and was classified as a 516 MB orphan. With ``--force`` that would have
# SIGKILLed a live production loop. Found on the first real dry run, 2026-08-13.
MCP_EXCLUDE = (
    "mcp-reaper",
    "grep",
    "ps -Ao",
    "rg ",
    "--check-mcp-roster",
    "mech_gate",
    "--strict-mcp-config",
    "--mcp-config",
    "pytest",
    "mcp_reaper",
    "mcp-trim",
)


class InstrumentError(RuntimeError):
    """The process table could not be read or trusted. Never a finding."""


@dataclass
class Proc:
    pid: int
    ppid: int
    rss_kb: int
    etime: str
    cmd: str


@dataclass
class Scan:
    procs: dict[int, Proc]
    orphans: list[Proc] = field(default_factory=list)
    owned: dict[int, list[Proc]] = field(default_factory=dict)


def read_process_table() -> dict[int, Proc]:
    """Snapshot the process table, or raise InstrumentError."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,rss=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstrumentError(f"could not run ps: {exc}") from exc
    if out.returncode != 0:
        raise InstrumentError(f"ps exited {out.returncode}: {out.stderr.strip()[:200]}")

    procs: dict[int, Proc] = {}
    for line in out.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        procs[pid] = Proc(pid=pid, ppid=ppid, rss_kb=rss, etime=parts[3], cmd=parts[4])

    # A machine always has more than a handful of processes. A table this small
    # means ps was truncated or filtered, and acting on it could kill anything.
    if len(procs) < 20:
        raise InstrumentError(f"process table implausibly small ({len(procs)} rows)")
    return procs


def _is_mcp(cmd: str) -> bool:
    """True iff this process is an MCP SERVER.

    IDENTITY IS argv[0]. This function used to substring-match MCP_MARKERS over
    the WHOLE command line, which made every one of these an "MCP server" and,
    in a bare terminal with no agent ancestor, an orphan to be SIGKILLed:

        vim .mcp.json
        git diff .mcp.json
        less configs/toolbroker/mcp-profiles/base.json
        cat ~/mcp-notes.txt

    An operator editing .mcp.json -- which is precisely what this lane is --
    would have lost unsaved work to a ``--force`` run. The module docstring
    claimed "Never kills a non-MCP process, regardless of ancestry"; that
    invariant was false.

    It is the same mistake the agent side already fixed and documented
    ("arguments are not identity"), left un-fixed on this side. A real MCP server
    is always started by a package launcher, so require BOTH:

      * argv[0] is a launcher (node/npm/npx/uv/uvx/python/bun/deno), and
      * a marker appears in the ARGUMENTS.

    An editor, pager, VCS or test runner fails the first test no matter what
    filename it was handed.
    """
    if _is_agent(cmd):
        return False
    low = cmd.lower()
    if any(x in low for x in MCP_EXCLUDE):
        return False

    parts = cmd.split()
    if not parts:
        return False
    argv0 = parts[0].lower().rsplit("/", 1)[-1]
    if argv0 not in LAUNCHERS:
        return False

    # An install/manage invocation NAMES a server without being one.
    if any(tok.lower() in MANAGEMENT_SUBCOMMANDS for tok in parts[1:4]):
        return False

    skip_next = False
    for token in parts[1:]:
        low_token = token.lower()
        if skip_next:
            skip_next = False
            continue
        if low_token in DEPENDENCY_FLAGS:
            skip_next = True  # its value is a dependency, not the program
            continue
        if any(ns in low_token for ns in NON_SERVER_PACKAGES):
            return False
        if any(pkg in low_token for pkg in MCP_PACKAGE_MARKERS):
            return True
        # Strip a path and an npm @version suffix so that
        # ".../node_modules/.bin/tavily-mcp" and "tavily-mcp@latest" both reduce
        # to the package name the author chose.
        base = low_token.rsplit("/", 1)[-1]
        if "@" in base:
            base = base.split("@", 1)[0] or base
        if _MCP_NAME_RE.search(base):
            return True
    return False


def _is_agent(cmd: str) -> bool:
    """True iff this process IS an agent CLI -- judged on argv[0] only.

    Matching the whole command line was tried first and is wrong: the loops run
    ``bash -lc '<script mentioning ~/.claude-account-N ...>'`` and a shell whose
    ARGUMENTS mention claude then reads as an agent. That is a false-OWNED, and
    a false-OWNED silently protects a genuine orphan forever -- the leak this
    program exists to catch hides behind the wrapper that spawned it.

    Judging argv[0] is both tighter and still correct for the chain walk: MCP
    servers are direct children of the agent process itself, so the agent is
    always reachable without crediting the shell above it.

    Matching is case-INSENSITIVE. Case sensitivity is exactly what hid
    /Applications/Claude.app/Contents/MacOS/Claude from the marker "/claude".
    """
    if not cmd.strip():
        return False
    low = cmd.lower()
    argv0 = cmd.split(None, 1)[0].lower()

    # CLI agents: argv[0] only, which is what keeps a shell whose ARGUMENTS
    # mention claude from reading as an agent.
    if any(m in argv0 for m in AGENT_MARKERS):
        return True

    # Terminals are never owners -- they are what orphans reparent to. Checked
    # BEFORE the desktop-host rules, which would otherwise match them.
    if any(m in low for m in TERMINAL_MARKERS):
        return False

    # Interpreter-hosted agents. An npm-installed Claude Code runs as
    #   node /…/node_modules/@anthropic-ai/claude-code/cli.js
    # so argv[0] is "node" and NO agent marker matches -- every one of its MCP
    # servers then classified as an orphan. That is the single most dangerous
    # false positive found, because it is how the CLI is installed on most
    # machines rather than an edge case.
    #
    # The test is applied to argv[1], the SCRIPT being run, which is what keeps
    # the earlier shell-wrapper fix intact: `bash -lc '<script mentioning
    # claude>'` has argv[1] == "-lc" and still does not match.
    if any(argv0.endswith(i) or f"/{i}" in argv0 for i in INTERPRETERS):
        parts = cmd.split()
        if len(parts) > 1:
            argv1 = parts[1].lower()
            if any(m in argv1 for m in AGENT_MARKERS):
                return True

    # Desktop hosts are matched against the WHOLE command line, not argv[0]:
    # macOS app paths contain spaces ("/Applications/Visual Studio Code.app/..."),
    # so splitting on whitespace yields "/Applications/Visual" and the match is
    # lost. Widening to the full line is safe here because the terminal exclusion
    # above already removed the dangerous false-positive class.
    if any(m in low for m in DESKTOP_HOST_MARKERS):
        return True

    # Catch-all for GUI MCP hosts not enumerated above -- they are a moving
    # target, and guessing wrong kills a user's editor.
    #
    # This was anchored at "/applications/" until an app bundle installed
    # anywhere else -- ~/Applications, a Homebrew Caskroom path -- was shown to
    # fall straight through and have its servers reaped. Any .app bundle counts
    # now, wherever it lives. Terminal.app cannot reach this line: terminals are
    # excluded above, before any host rule runs.
    return ".app/contents/" in low


def ancestor_chain(pid: int, procs: dict[int, Proc]) -> list[int]:
    """Walk pid -> ppid to init. Cycle-safe."""
    chain: list[int] = []
    seen: set[int] = set()
    cur = pid
    while cur in procs and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = procs[cur].ppid
        if cur <= 1:
            break
    return chain


def classify(procs: dict[int, Proc]) -> Scan:
    scan = Scan(procs=procs)
    for pid, proc in procs.items():
        if not _is_mcp(proc.cmd):
            continue
        owner = None
        for anc in ancestor_chain(pid, procs):
            if anc != pid and _is_agent(procs[anc].cmd):
                owner = anc
                break
        if owner is None:
            scan.orphans.append(proc)
        else:
            scan.owned.setdefault(owner, []).append(proc)
    return scan


def _still_matches(pid: int, expected_cmd: str) -> bool:
    """Guard against pid reuse between classification and the kill."""
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    return out.stdout.strip()[:60] == expected_cmd.strip()[:60]


def parse_etime(etime: str) -> int | None:
    """ps elapsed time -> seconds. ``None`` when it cannot be parsed.

    Formats: ``MM:SS``, ``HH:MM:SS``, ``D-HH:MM:SS``. An unparseable value
    returns None and is treated by callers as "too young to judge", because the
    safe reading of "I do not know how old this is" is to leave it alone.
    """
    try:
        days = 0
        rest = etime.strip()
        if "-" in rest:
            d, rest = rest.split("-", 1)
            days = int(d)
        parts = [int(x) for x in rest.split(":")]
        if len(parts) == 2:
            h, m, s = 0, parts[0], parts[1]
        elif len(parts) == 3:
            h, m, s = parts
        else:
            return None
        return days * 86400 + h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return None


def too_young(proc: Proc, min_age_seconds: float) -> bool:
    """Guard the startup race.

    A session that is still booting can have its servers visible in ``ps`` in an
    order that makes the owner hard to attribute for a moment. Reaping inside
    that window would kill the tooling of a session that is coming UP, which is
    indistinguishable in the snapshot from one that has gone down. Anything whose
    age cannot be parsed is also treated as too young -- unknown age is not
    evidence of abandonment.
    """
    age = parse_etime(proc.etime)
    return age is None or age < min_age_seconds


def reap(orphans: list[Proc], grace: float) -> tuple[list[int], list[int]]:
    """SIGTERM, wait, then SIGKILL survivors. Returns (termed, killed)."""
    termed: list[int] = []
    for proc in orphans:
        if not _still_matches(proc.pid, proc.cmd):
            continue  # pid reused or already gone
        try:
            os.kill(proc.pid, signal.SIGTERM)
            termed.append(proc.pid)
        except (ProcessLookupError, PermissionError):
            continue

    if not termed:
        return [], []

    time.sleep(grace)

    killed: list[int] = []
    by_pid = {p.pid: p for p in orphans}
    for pid in termed:
        if not _still_matches(pid, by_pid[pid].cmd):
            continue  # exited cleanly on SIGTERM
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except (ProcessLookupError, PermissionError):
            continue
    return termed, killed


def render_human(scan: Scan) -> str:
    lines = []
    total_owned = sum(len(v) for v in scan.owned.values())
    owned_mb = sum(p.rss_kb for v in scan.owned.values() for p in v) / 1024
    orphan_mb = sum(p.rss_kb for p in scan.orphans) / 1024

    lines.append(
        f"owned   : {total_owned:4d} procs  {owned_mb:9.1f} MB  across {len(scan.owned)} live session(s)"
    )
    lines.append(f"orphaned: {len(scan.orphans):4d} procs  {orphan_mb:9.1f} MB")

    if scan.owned:
        lines.append("")
        lines.append("live sessions (never touched):")
        for owner, kids in sorted(scan.owned.items(), key=lambda kv: -sum(p.rss_kb for p in kv[1])):
            o = scan.procs[owner]
            mb = sum(p.rss_kb for p in kids) / 1024
            lines.append(
                f"  pid {owner:<8} age {o.etime:>13}  {len(kids):3d} servers  {mb:8.1f} MB  {o.cmd[:44]}"
            )

    if scan.orphans:
        lines.append("")
        lines.append("orphans:")
        for p in sorted(scan.orphans, key=lambda x: -x.rss_kb):
            lines.append(
                f"  pid {p.pid:<8} age {p.etime:>13}  {p.rss_kb / 1024:8.1f} MB  {p.cmd[:60]}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mcp-reaper",
        description="Reap MCP servers whose owning agent session is gone. Dry-run unless --force.",
    )
    ap.add_argument(
        "--force", action="store_true", help="actually signal orphans (default: report only)"
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--min-age",
        type=float,
        default=300.0,
        help="never reap a process younger than this many seconds (default 300)",
    )
    ap.add_argument(
        "--grace", type=float, default=5.0, help="seconds between SIGTERM and SIGKILL (default 5)"
    )
    args = ap.parse_args(argv)

    try:
        procs = read_process_table()
    except InstrumentError as exc:
        # Fail closed: an instrument error is never reported as a finding, and
        # never authorizes a kill.
        print(f"mcp-reaper: INSTRUMENT ERROR, nothing killed: {exc}", file=sys.stderr)
        return 2

    scan = classify(procs)

    # Startup-race guard: young orphans are held, not reaped.
    reapable = [p for p in scan.orphans if not too_young(p, args.min_age)]
    held = [p for p in scan.orphans if too_young(p, args.min_age)]

    termed: list[int] = []
    killed: list[int] = []
    if args.force and reapable:
        termed, killed = reap(reapable, args.grace)

    if args.json:
        print(
            json.dumps(
                {
                    "orphans": [
                        {"pid": p.pid, "rss_kb": p.rss_kb, "etime": p.etime, "cmd": p.cmd}
                        for p in scan.orphans
                    ],
                    "owned_sessions": {
                        str(o): {"servers": len(k), "rss_kb": sum(x.rss_kb for x in k)}
                        for o, k in scan.owned.items()
                    },
                    "held_too_young": [{"pid": p.pid, "etime": p.etime} for p in held],
                    "forced": args.force,
                    "sigtermed": termed,
                    "sigkilled": killed,
                },
                indent=2,
            )
        )
    else:
        print(render_human(scan))
        if held:
            print(f"\nheld (younger than {args.min_age:.0f}s, startup-race guard): {len(held)}")
        if reapable and not args.force:
            print(f"\ndry run -- re-run with --force to reap {len(reapable)} orphan(s)")
        elif args.force:
            print(f"\nreaped: {len(termed)} SIGTERM, {len(killed)} SIGKILL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
