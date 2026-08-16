"""G4: router-decided effort reaches the claude bridge argv, guarded by a CLI probe.

The probe (`_claude_cli_supports_effort`) runs ``claude --help`` once per
process; an unsupported/failed probe must degrade to "no flag", never a spawn
failure. ``effort=None`` must keep the argv byte-identical to before G4.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omniagentos.sessions import supervisor as supervisor_mod
from omniagentos.sessions.supervisor import (
    SessionSupervisor,
    _claude_cli_supports_effort,
    _maybe_append_effort,
)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    _claude_cli_supports_effort.cache_clear()
    yield
    _claude_cli_supports_effort.cache_clear()


@pytest.fixture
def sup(monkeypatch) -> SessionSupervisor:
    # Builders touch only self.claude_binary + module helpers; bypass __init__
    # so these stay hermetic unit tests of the argv construction.
    monkeypatch.setattr(
        supervisor_mod, "bridge_settings_path", lambda: "/tmp/bridge-settings.json", raising=False
    )
    instance = SessionSupervisor.__new__(SessionSupervisor)
    instance.claude_binary = "/fake/claude"
    return instance


def _help_run(text: str):
    def runner(argv, **kwargs):
        assert argv == ["/fake/claude", "--help"]
        return SimpleNamespace(stdout=text, stderr="", returncode=0)

    return runner


# -- probe ------------------------------------------------------------------


def test_probe_true_when_help_advertises_flag(monkeypatch):
    monkeypatch.setattr(supervisor_mod.subprocess, "run", _help_run("... --effort <level> ..."))
    assert _claude_cli_supports_effort("/fake/claude") is True


def test_probe_false_when_flag_absent(monkeypatch):
    monkeypatch.setattr(supervisor_mod.subprocess, "run", _help_run("no effort here"))
    assert _claude_cli_supports_effort("/fake/claude") is False


def test_probe_failure_reads_as_unsupported(monkeypatch):
    def boom(argv, **kwargs):
        raise OSError("missing binary")

    monkeypatch.setattr(supervisor_mod.subprocess, "run", boom)
    assert _claude_cli_supports_effort("/fake/claude") is False


def test_probe_runs_once_per_binary(monkeypatch):
    calls: list[list[str]] = []

    def counting(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(stdout="--effort", stderr="", returncode=0)

    monkeypatch.setattr(supervisor_mod.subprocess, "run", counting)
    assert _claude_cli_supports_effort("/fake/claude")
    assert _claude_cli_supports_effort("/fake/claude")
    assert len(calls) == 1


# -- append helper ----------------------------------------------------------


def test_maybe_append_skips_none_and_blank_without_probing(monkeypatch):
    def forbidden(_binary):  # probe must not even be consulted
        raise AssertionError("probe consulted for empty effort")

    monkeypatch.setattr(supervisor_mod, "_claude_cli_supports_effort", forbidden)
    assert _maybe_append_effort(["/fake/claude"], None) == ["/fake/claude"]
    assert _maybe_append_effort(["/fake/claude"], "   ") == ["/fake/claude"]


def test_maybe_append_skips_when_probe_false(monkeypatch):
    monkeypatch.setattr(supervisor_mod, "_claude_cli_supports_effort", lambda _b: False)
    assert _maybe_append_effort(["/fake/claude"], "high") == ["/fake/claude"]


# -- launch argv -------------------------------------------------------------


def test_launch_argv_appends_effort_when_supported(monkeypatch, sup):
    monkeypatch.setattr(supervisor_mod, "_claude_cli_supports_effort", lambda _b: True)
    argv = sup._bridge_launch_argv("ref-1", "claude-opus-4-8", "do it", None, None, effort="xhigh")
    assert argv[-2:] == ["--effort", "xhigh"]
    assert argv[0] == "/fake/claude"
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"


def test_launch_argv_byte_identical_when_effort_none(monkeypatch, sup):
    monkeypatch.setattr(supervisor_mod, "_claude_cli_supports_effort", lambda _b: True)
    with_none = sup._bridge_launch_argv("r", "m", "p", 5.0, "t", effort=None)
    default = sup._bridge_launch_argv("r", "m", "p", 5.0, "t")
    assert with_none == default
    assert "--effort" not in with_none


def test_launch_argv_unchanged_when_probe_false(monkeypatch, sup):
    monkeypatch.setattr(supervisor_mod, "_claude_cli_supports_effort", lambda _b: False)
    with_effort = sup._bridge_launch_argv("r", "m", "p", None, None, effort="high")
    without = sup._bridge_launch_argv("r", "m", "p", None, None)
    assert with_effort == without


# -- resume argv --------------------------------------------------------------


def test_resume_argv_appends_effort_when_supported(monkeypatch, sup):
    monkeypatch.setattr(supervisor_mod, "_claude_cli_supports_effort", lambda _b: True)
    argv = sup._bridge_resume_argv("ref-9", "continue", None, None, effort="medium")
    assert argv[-2:] == ["--effort", "medium"]
    assert "--resume" in argv


def test_resume_argv_byte_identical_when_effort_none(monkeypatch, sup):
    monkeypatch.setattr(supervisor_mod, "_claude_cli_supports_effort", lambda _b: True)
    assert "--effort" not in sup._bridge_resume_argv("ref-9", "continue", None, None)


# -- end-to-end: router-decided effort reaches supervisor.spawn ---------------


def test_spawn_claude_threads_router_effort_to_supervisor(tmp_path):
    """H-05 pre-pin-vs-CBM: router xhigh pre-pin loses to CBM on the claude path.

    Prior contract asserted pre-pin passthrough. Under the new precedence the
    adapter still receives a concrete effort — the CBM-selected one — and it
    must not equal the stale pre-pin when CBM allocates a different value.
    """
    from tests.swarm.test_spawn import make_request, make_spawner

    spawner, supervisor, *_ = make_spawner(tmp_path)
    spawner.spawn(make_request(effort="xhigh"))
    (call,) = supervisor.calls
    effort = call.get("effort")
    assert effort in {"minimal", "low", "medium", "high", "xhigh"}
    # Default make_spawner task is standard/medium → CBM fast-first "low".
    assert effort == "low"
    assert effort != "xhigh"
    prompt = str(call.get("prompt") or "")
    assert "[cbm allocation" in prompt
    assert "source=cbm" in prompt or f"effort={effort}" in prompt


def test_spawn_claude_effort_absent_receives_cbm_allocation(tmp_path):
    """When the router leaves effort unset, CBM fills it from the allocation.

    Reversal of the prior "stays None" claim (HANDOFF Phase 1.1): discarding the
    allocation was the dark-code failure mode. Unset effort must become the CBM
    recommended reasoning_effort (typically ``low`` for fast-first rung 1).
    Tightened: assert the concrete CBM value, not a permissive set membership.
    """
    from tests.swarm.test_spawn import make_request, make_spawner

    spawner, supervisor, *_ = make_spawner(tmp_path)
    spawner.spawn(make_request())
    (call,) = supervisor.calls
    effort = call.get("effort")
    # CBM fast-first default effort for a vanilla task.
    assert effort == "low"
    prompt = str(call.get("prompt") or "")
    assert "[cbm allocation" in prompt
    assert f"effort={effort}" in prompt
