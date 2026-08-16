"""Tests for scripts/mcp-reaper.py.

The reaper kills processes, so the tests that matter are the ones asserting it
does NOT kill things. Every case below is drawn from a real command line
observed on this machine on 2026-08-13, including the near-miss that would have
SIGKILLed a live production loop.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REAPER_PATH = REPO_ROOT / "scripts" / "mcp-reaper.py"


def _load_reaper():
    """Import the hyphenated script as a module.

    It must be registered in ``sys.modules`` before ``exec_module``: the module
    body defines ``@dataclass`` types, and dataclasses resolves annotations via
    ``sys.modules[cls.__module__]``, which raises AttributeError if absent.
    """
    spec = importlib.util.spec_from_file_location("mcp_reaper", REAPER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcp_reaper"] = module
    spec.loader.exec_module(module)
    return module


reaper = _load_reaper()


# --- Classification: the safety-critical half -----------------------------

LIVE_LOOP_CLAUDE = (
    "/Users/youruser/.local/bin/claude --model claude-opus-5 "
    "--dangerously-skip-permissions --strict-mcp-config -p prompt"
)
SHELL_MENTIONING_CLAUDE = 'bash -lc _n="$(grep X ~/.claude-account-3/foo)"; run'


def test_agent_running_with_strict_mcp_config_is_not_an_mcp_server():
    """The live near-miss: a claude launched with --strict-mcp-config.

    It matched MCP_MARKERS on its own flags and, being the top of its chain, had
    no agent ancestor -- so it classified as a 516 MB orphan. With --force that
    was a SIGKILL of the ThreeLoops planning loop.
    """
    assert reaper._is_agent(LIVE_LOOP_CLAUDE) is True
    assert reaper._is_mcp(LIVE_LOOP_CLAUDE) is False


def test_structural_guard_holds_for_unknown_flag_spellings():
    """The guard must not depend on enumerating known flags."""
    invented = "/Users/youruser/.local/bin/claude --mcp-profile browser --mcp-whatever"
    assert reaper._is_mcp(invented) is False


def test_shell_whose_arguments_mention_claude_is_not_an_agent():
    """A false-OWNED silently protects a genuine orphan forever."""
    assert reaper._is_agent(SHELL_MENTIONING_CLAUDE) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "node /Users/x/.npm/_npx/abc/node_modules/.bin/mcp-server-memory",
        "npm exec @playwright/mcp@latest",
        "/Users/x/.local/bin/uv tool uvx --python 3.12 --with mcp<2 mcp-server-fetch",
    ],
)
def test_real_servers_are_detected(cmd):
    assert reaper._is_mcp(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 scripts/mcp-reaper.py --force",
        "bash scripts/gates/mech_gate.sh --check-mcp-roster",
        "grep -r mcp .",
    ],
)
def test_self_and_tooling_are_never_candidates(cmd):
    assert reaper._is_mcp(cmd) is False


# --- Ancestor chain -------------------------------------------------------


def _proc(pid, ppid, cmd, rss=1000):
    return reaper.Proc(pid=pid, ppid=ppid, rss_kb=rss, etime="01:00", cmd=cmd)


def test_server_with_live_agent_ancestor_is_owned_not_orphaned():
    procs = {
        100: _proc(100, 1, "/bin/zsh"),
        200: _proc(200, 100, "/Users/youruser/.local/bin/claude"),
        300: _proc(300, 200, "npm exec @modelcontextprotocol/server-memory"),
    }
    scan = reaper.classify(procs)
    assert scan.orphans == []
    assert 200 in scan.owned and len(scan.owned[200]) == 1


def test_server_without_agent_ancestor_is_an_orphan():
    """The recycle case: the agent died, the shell above it survived."""
    procs = {
        100: _proc(100, 1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
        300: _proc(300, 100, "npm exec @modelcontextprotocol/server-memory", rss=110_000),
    }
    scan = reaper.classify(procs)
    assert [p.pid for p in scan.orphans] == [300]


def test_rooted_at_terminal_alone_does_not_make_an_orphan():
    """The refuted heuristic: live sessions root at Terminal too."""
    procs = {
        100: _proc(100, 1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
        200: _proc(200, 100, "/Users/youruser/.local/bin/claude"),
        300: _proc(300, 200, "npm exec @modelcontextprotocol/server-memory"),
    }
    scan = reaper.classify(procs)
    assert scan.orphans == []


def test_ancestor_chain_survives_a_cycle():
    procs = {10: _proc(10, 11, "a"), 11: _proc(11, 10, "b")}
    chain = reaper.ancestor_chain(10, procs)
    assert len(chain) == 2


# --- Fail-closed instrument behaviour -------------------------------------


def test_implausibly_small_process_table_is_an_instrument_error(monkeypatch):
    """An unreadable ps is never evidence of orphanhood."""

    class FakeCompleted:
        returncode = 0
        stdout = "1 0 100 01:00 /sbin/launchd\n"
        stderr = ""

    monkeypatch.setattr(reaper.subprocess, "run", lambda *a, **k: FakeCompleted())
    with pytest.raises(reaper.InstrumentError):
        reaper.read_process_table()


def test_ps_failure_is_an_instrument_error(monkeypatch):
    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(reaper.subprocess, "run", lambda *a, **k: FakeCompleted())
    with pytest.raises(reaper.InstrumentError):
        reaper.read_process_table()


def test_main_exits_2_and_kills_nothing_on_instrument_error(monkeypatch, capsys):
    def boom():
        raise reaper.InstrumentError("table unreadable")

    killed = []
    monkeypatch.setattr(reaper, "read_process_table", boom)
    monkeypatch.setattr(reaper, "reap", lambda *a, **k: killed.append(a) or ([], []))
    assert reaper.main(["--force"]) == 2
    assert killed == []


def test_dry_run_is_the_default_and_signals_nothing(monkeypatch):
    procs = {
        100: _proc(100, 1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
        300: _proc(300, 100, "npm exec @modelcontextprotocol/server-memory"),
    }
    monkeypatch.setattr(reaper, "read_process_table", lambda: procs)
    called = []
    monkeypatch.setattr(reaper, "reap", lambda *a, **k: called.append(a) or ([], []))
    assert reaper.main([]) == 0
    assert called == [], "dry run must never call reap()"


# --- Desktop MCP hosts: the third bug --------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "/Applications/Claude.app/Contents/MacOS/Claude",
        "/Applications/Cursor.app/Contents/MacOS/Cursor",
        "/Applications/Windsurf.app/Contents/MacOS/Electron",
        "/Applications/Visual Studio Code.app/Contents/MacOS/Electron",
    ],
)
def test_desktop_mcp_hosts_own_their_servers(host):
    """Claude Desktop is installed and running on this box.

    The first version matched "/claude" case-SENSITIVELY against argv[0], so
    /Applications/Claude.app/.../Claude missed, its servers had no agent
    ancestor, and every one classified as an orphan. With --force that killed
    the desktop app's tooling.
    """
    procs = {
        100: _proc(100, 1, host),
        300: _proc(300, 100, "npm exec @modelcontextprotocol/server-memory"),
    }
    scan = reaper.classify(procs)
    assert scan.orphans == [], f"{host} servers must never be reaped"
    assert 100 in scan.owned


def test_unknown_app_bundle_is_treated_as_a_host():
    """Desktop MCP hosts are a moving target; enumerating them is a losing game."""
    procs = {
        100: _proc(100, 1, "/Applications/SomeFutureEditor.app/Contents/MacOS/Thing"),
        300: _proc(300, 100, "npm exec @modelcontextprotocol/server-memory"),
    }
    assert reaper.classify(procs).orphans == []


def test_case_insensitivity_does_not_swallow_the_shell_wrapper_case():
    """The M-2 fix must survive the case-insensitivity fix."""
    assert reaper._is_agent(SHELL_MENTIONING_CLAUDE) is False


@pytest.mark.parametrize(
    "term",
    [
        "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal",
        "/Applications/iTerm.app/Contents/MacOS/iTerm2",
        "tmux new-session -d -s loop-planning",
    ],
)
def test_terminals_are_never_owners(term):
    """Orphans reparent to terminals. Crediting them makes the reaper a no-op."""
    assert reaper._is_agent(term) is False
    procs = {
        100: _proc(100, 1, term),
        300: _proc(300, 100, "npm exec @modelcontextprotocol/server-memory"),
    }
    assert [p.pid for p in reaper.classify(procs).orphans] == [300]


# --- Interpreter- and bundle-hosted agents: kill-direction false positives ---


def test_node_hosted_claude_cli_is_an_agent():
    """npm-installed Claude Code runs as `node .../claude-code/cli.js`.

    argv[0] is "node", so no AGENT_MARKER matched and every one of its MCP
    servers classified as an orphan. This is how the CLI is installed on most
    machines, so it is the most dangerous false positive found -- not an edge
    case.
    """
    cmd = "node /Users/s/.npm/lib/node_modules/@anthropic-ai/claude-code/cli.js"
    assert reaper._is_agent(cmd) is True
    procs = {
        100: _proc(100, 1, cmd),
        300: _proc(300, 100, "npm exec @modelcontextprotocol/server-memory"),
    }
    assert reaper.classify(procs).orphans == []


@pytest.mark.parametrize(
    "host",
    [
        "/Users/youruser/Applications/SomeEditor.app/Contents/MacOS/SomeEditor",
        "/opt/homebrew/Caskroom/thing/Editor.app/Contents/MacOS/Editor",
    ],
)
def test_app_bundles_outside_slash_applications_are_hosts(host):
    """The catch-all was anchored at /Applications/, so bundles installed in
    ~/Applications or a Homebrew Caskroom fell through and were reaped."""
    procs = {
        100: _proc(100, 1, host),
        300: _proc(300, 100, "npm exec @modelcontextprotocol/server-memory"),
    }
    assert reaper.classify(procs).orphans == []


def test_interpreter_rule_does_not_resurrect_the_shell_wrapper_case():
    """Shells are absent from INTERPRETERS on purpose: `bash -lc '<script>'` has
    argv[1] == "-lc", so judging argv[1] cannot re-open the M-2 false-OWNED."""
    assert reaper._is_agent(SHELL_MENTIONING_CLAUDE) is False
    assert reaper._is_agent("bash -lc 'run /Users/x/.local/bin/claude now'") is False


# --- _is_mcp identity: the kill-anything blocker ----------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "vim .mcp.json",
        "git diff .mcp.json",
        "less configs/toolbroker/mcp-profiles/base.json",
        "cat /Users/s/mcp-notes.txt",
        "code .mcp.json",
        "emacs ~/mcp.json",
        "tail -f var/logs/mcp-reaper.log",
        "python3 -m pytest tests/scripts/test_mcp_reaper.py",
        "python3 scripts/mcp-reaper.py --force",
    ],
)
def test_ordinary_commands_mentioning_mcp_are_never_mcp_servers(cmd):
    """_is_mcp used to substring-match the WHOLE command line.

    Every command here classified as an MCP server, and in a bare terminal with
    no agent ancestor, as an orphan to SIGKILL. An operator with `vim .mcp.json`
    open -- which is exactly what this lane is -- would have lost unsaved work to
    a --force run. The docstring's "never kills a non-MCP process" was false.
    """
    assert reaper._is_mcp(cmd) is False
    procs = {
        100: _proc(100, 1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
        200: _proc(200, 100, cmd),
    }
    assert reaper.classify(procs).orphans == [], f"{cmd!r} must never be reapable"


@pytest.mark.parametrize(
    "cmd",
    [
        "node /Users/x/.npm/_npx/a/node_modules/.bin/mcp-server-memory",
        "npm exec @playwright/mcp@latest",
        "npm exec @modelcontextprotocol/server-memory",
        "/Users/youruser/.local/bin/uv tool uvx --python 3.12 --with mcp<2 mcp-server-fetch",
        "/Users/x/.cache/uv/archive-v0/q/bin/python /Users/x/bin/mcp-server-git",
    ],
)
def test_real_launcher_started_servers_are_still_detected(cmd):
    """The tightening must not blind the reaper to actual servers."""
    assert reaper._is_mcp(cmd) is True


# --- Startup-race guard -----------------------------------------------------


@pytest.mark.parametrize(
    "etime,seconds",
    [("05:30", 330), ("01:02:03", 3723), ("2-03:04:05", 183845), ("00:07", 7)],
)
def test_parse_etime(etime, seconds):
    assert reaper.parse_etime(etime) == seconds


def test_unparseable_age_is_treated_as_too_young():
    """Unknown age is not evidence of abandonment."""
    p = _proc(300, 100, "npm exec @modelcontextprotocol/server-memory")
    p.etime = "not-a-time"
    assert reaper.parse_etime(p.etime) is None
    assert reaper.too_young(p, 300.0) is True


def test_young_orphans_are_held_not_reaped(monkeypatch):
    """A session that is still BOOTING looks identical to one that went down."""
    young = _proc(300, 100, "npm exec @modelcontextprotocol/server-memory")
    young.etime = "00:04"
    procs = {
        100: _proc(100, 1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
        300: young,
    }
    monkeypatch.setattr(reaper, "read_process_table", lambda: procs)
    called = []
    monkeypatch.setattr(reaper, "reap", lambda *a, **k: called.append(a) or ([], []))
    assert reaper.main(["--force"]) == 0
    assert called == [], "a 4-second-old orphan must be held, not reaped"


def test_old_orphans_are_still_reaped(monkeypatch):
    """The guard must not disable the program."""
    old = _proc(300, 100, "npm exec @modelcontextprotocol/server-memory")
    old.etime = "02:00:00"
    procs = {
        100: _proc(100, 1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
        300: old,
    }
    monkeypatch.setattr(reaper, "read_process_table", lambda: procs)
    called = []
    monkeypatch.setattr(reaper, "reap", lambda *a, **k: called.append(a) or ([300], []))
    assert reaper.main(["--force"]) == 0
    assert called and [p.pid for p in called[0][0]] == [300]


# --- _is_mcp: server-shaped names, both failure directions ------------------


@pytest.mark.parametrize(
    "cmd",
    [
        # trailing -mcp: the class a too-tight marker set silently stopped seeing
        "node /x/.bin/playwright-mcp",
        "/u/.local/bin/uv tool uvx markitdown-mcp",
        "npm exec tavily-mcp@latest",
        "node /x/.bin/tavily-mcp",
        "/u/.local/bin/uv tool uvx duckduckgo-mcp-server",
    ],
)
def test_trailing_dash_mcp_packages_are_detected(cmd):
    """A reaper that cannot SEE a server is not safe, it is useless.

    That failure is invisible in the worst way: fewer orphans reported looks
    exactly like fewer orphans existing.
    """
    assert reaper._is_mcp(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 tools/validate_mcp_roster.py",
        "node build.js --out .mcp.json",
        "node build.js --out my-mcp.json",
        "uv run scripts/report.py --input mcp-metrics.csv",
    ],
)
def test_launcher_running_a_script_that_merely_mentions_mcp_is_not_a_server(cmd):
    """The residual kill-direction class after the launcher gate went in.

    argv[0] is a real launcher, so the launcher check passes; only judging the
    ARGUMENT BASENAME rather than the whole line separates these from a server.
    """
    assert reaper._is_mcp(cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 -m pip install mcp-server-fetch",
        "uv tool install mcp-server-fetch",
        "uv run --with mcp-server-fetch python x.py",
        "npx @modelcontextprotocol/inspector",
    ],
)
def test_installing_or_depending_on_a_server_is_not_running_one(cmd):
    """Naming a server is not being one.

    `pip install mcp-server-fetch` is an install whose interruption corrupts a
    package; `uv run --with X` names X as a DEPENDENCY while running something
    else; `@modelcontextprotocol/inspector` is the human-run debugging UI.
    """
    assert reaper._is_mcp(cmd) is False


def test_node_hosted_gemini_cli_is_an_agent():
    """npm-installed Gemini has no 'bin/gemini' in its script path."""
    cmd = "node /Users/s/.npm/lib/node_modules/@google/gemini-cli/dist/index.js"
    assert reaper._is_agent(cmd) is True
