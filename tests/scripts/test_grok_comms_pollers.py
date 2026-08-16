"""Receive-only comms poller coverage for the canonical process supervisor."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.process_supervisor import (
    POLLER_RESTART_BUDGET,
    ProcessSpec,
    ProcessSupervisor,
    SupervisionError,
    _should_start_poller,
    build_process_specs,
)

ROOT = Path(__file__).resolve().parents[2]


def test_should_start_poller_checks_required_names_without_reading_values() -> None:
    present = object()
    assert _should_start_poller("telegram", {"TELEGRAM_BOT_TOKEN": present})
    assert _should_start_poller("slack", {"SLACK_BOT_TOKEN": present})
    assert _should_start_poller("slack", {"SLACK_BOT_TOKEN": present, "SLACK_TEAM_ID": object()})

    assert not _should_start_poller("telegram", {})
    assert not _should_start_poller("slack", {"SLACK_TEAM_ID": present})
    assert not _should_start_poller("imap", {"IMAP_HOST_INBOX": present})
    # Names-only matches connectors.doctor: an intentionally blank name remains
    # present.  The poller itself remains responsible for its pending_setup row.
    assert _should_start_poller("telegram", {"TELEGRAM_BOT_TOKEN": ""})


def test_specs_start_only_credentialed_receive_only_pollers(
    capsys, tmp_path: Path
) -> None:
    launcher = tmp_path / "launcher.sh"
    specs = build_process_specs(
        tmp_path / "repo",
        launcher,
        tmp_path / "runtime",
        env={"TELEGRAM_BOT_TOKEN": object()},
    )

    names = {spec.name for spec in specs}
    assert names == {"api", "runner", "sessions", "dashboard", "comms-poll-telegram"}
    poller_specs = [spec for spec in specs if spec.name.startswith("comms-poll-")]
    assert [spec.log_path.name for spec in poller_specs] == ["comms-poll-telegram.log"]
    # Supervision is REAL: the poller is restartable by the supervisor within a
    # bounded budget, and the core fleet keeps its immediate fail-closed exit.
    assert all(spec.restart_budget == POLLER_RESTART_BUDGET for spec in poller_specs)
    assert all(spec.restart_window_s > 0 for spec in poller_specs)
    assert all(
        spec.restart_budget == 0 for spec in specs if not spec.name.startswith("comms-poll-")
    )
    # Structural counterfeit: the only command surface that can be started is
    # the receive-only poll CLI; no send-capable module or command is armed.
    assert all(spec.command[-1] == "comms-poll-telegram" for spec in poller_specs)
    assert not any(
        blocked in " ".join(spec.command)
        for spec in poller_specs
        for blocked in ("slack.post_internal", "gmail.send", "comms.send")
    )

    assert "comms poller slack skipped (missing credential name(s): SLACK_BOT_TOKEN)" in capsys.readouterr().err


def test_specs_skip_all_missing_pollers_without_blocking_core_fleet(capsys, tmp_path: Path) -> None:
    specs = build_process_specs(
        tmp_path / "repo",
        tmp_path / "launcher.sh",
        tmp_path / "runtime",
        env={},
    )

    assert [spec.name for spec in specs] == ["api", "runner", "sessions", "dashboard"]
    logs = capsys.readouterr().err
    assert "comms poller telegram skipped (missing credential name(s): TELEGRAM_BOT_TOKEN)" in logs
    assert "comms poller slack skipped (missing credential name(s): SLACK_BOT_TOKEN)" in logs


def test_canonical_launcher_exports_vault_names_to_child_processes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    connections = home / ".config" / "omni" / "connections.env"
    connections.parent.mkdir(parents=True)
    # Deliberately blank: this verifies shell export propagation without putting
    # a credential-shaped value in a fixture or test output.
    connections.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    python = tmp_path / "python"
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "uvicorn" ]; then\n'
        '  test "${TELEGRAM_BOT_TOKEN+x}" = x\n'
        "  exit $?\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "OMNIAGENTOS_PYTHON": str(python),
            "OMNIAGENTOS_VAR_DIR": str(tmp_path / "runtime"),
        }
    )
    for name in ("TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_TEAM_ID"):
        env.pop(name, None)

    completed = subprocess.run(
        ["bash", str(ROOT / "scripts" / "launch-supervised.sh"), "api"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr


# ---------------------------------------------------------------------------
# MAJOR 6 — the pollers are supervised, not merely wrapped.
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


def _supervisor(
    specs: list[ProcessSpec], spawned: list[_FakeProcess], clock: list[float]
) -> ProcessSupervisor:
    def popen(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        process = _FakeProcess(100 + len(spawned))
        spawned.append(process)
        return process

    return ProcessSupervisor(
        specs,
        [],
        health_timeout=5,
        stop_timeout=1,
        popen=popen,
        health_probe=lambda _url: True,
        kill_group=lambda _pid, _sig: None,
        group_alive=lambda _pid: False,
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )


def test_launcher_poller_execs_instead_of_looping_forever() -> None:
    """A `while true` wrapper is supervision in name only; it must not come back.

    The wrapper never exits, so the supervisor's exit observation never fires
    and a poller failing 100% of its attempts looks exactly like a healthy one.
    """
    script = (ROOT / "scripts" / "launch-supervised.sh").read_text(encoding="utf-8")
    body = script[script.index("_comms_poller()") : script.index("_supervisor_args=(")]
    # Judge the CODE, not the comment that explains why the loop is gone.
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "while" not in code
    assert "sleep" not in code
    assert 'exec "$PYBIN" -m omniagentos.comms.poll --source "$source"' in code


def test_a_poller_that_exits_is_observed_and_restarted_within_budget() -> None:
    clock = [0.0]
    spawned: list[_FakeProcess] = []
    spec = ProcessSpec(
        "comms-poll-telegram",
        ("poll",),
        ROOT,
        ROOT / "var" / "unused.log",
        restart_budget=2,
        restart_window_s=300.0,
    )
    supervisor = _supervisor([spec], spawned, clock)
    supervisor._log_handles["comms-poll-telegram"] = None  # type: ignore[assignment]
    supervisor.start()
    assert len(spawned) == 1

    spawned[0].returncode = 1
    supervisor._reconcile_children()

    assert len(spawned) == 2, "the supervisor must respawn the poller, not ignore its exit"
    assert supervisor.restart_log == [("comms-poll-telegram", 1)]
    assert supervisor.processes[0][1] is spawned[1]


def test_a_crash_looping_poller_exhausts_its_budget_and_fails_closed() -> None:
    clock = [0.0]
    spawned: list[_FakeProcess] = []
    spec = ProcessSpec(
        "comms-poll-slack",
        ("poll",),
        ROOT,
        ROOT / "var" / "unused.log",
        restart_budget=2,
        restart_window_s=300.0,
    )
    supervisor = _supervisor([spec], spawned, clock)
    supervisor._log_handles["comms-poll-slack"] = None  # type: ignore[assignment]
    supervisor.start()

    for _ in range(2):
        supervisor.processes[0][1].returncode = 3  # type: ignore[union-attr]
        supervisor._reconcile_children()
    assert len(spawned) == 3

    supervisor.processes[0][1].returncode = 3  # type: ignore[union-attr]
    with pytest.raises(SupervisionError) as failure:
        supervisor._reconcile_children()
    assert "restart budget" in str(failure.value)
    assert len(spawned) == 3, "a poller past its budget must not be respawned again"


def test_a_restart_refreshes_the_durable_child_roster() -> None:
    """A dead pid left in the pid file is the runtime record saying something untrue."""
    clock = [0.0]
    spawned: list[_FakeProcess] = []
    spec = ProcessSpec(
        "comms-poll-telegram",
        ("poll",),
        ROOT,
        ROOT / "var" / "unused.log",
        restart_budget=2,
    )
    supervisor = _supervisor([spec], spawned, clock)
    supervisor._log_handles["comms-poll-telegram"] = None  # type: ignore[assignment]
    supervisor.start()
    rosters: list[list[int]] = []
    supervisor.on_roster_change = lambda: rosters.append(
        [process.pid for _spec, process in supervisor.processes]
    )

    spawned[0].returncode = 1
    supervisor._reconcile_children()

    assert rosters == [[spawned[1].pid]]


def test_restarts_outside_the_window_do_not_accumulate() -> None:
    clock = [0.0]
    spawned: list[_FakeProcess] = []
    spec = ProcessSpec(
        "comms-poll-telegram",
        ("poll",),
        ROOT,
        ROOT / "var" / "unused.log",
        restart_budget=1,
        restart_window_s=60.0,
    )
    supervisor = _supervisor([spec], spawned, clock)
    supervisor._log_handles["comms-poll-telegram"] = None  # type: ignore[assignment]
    supervisor.start()

    supervisor.processes[0][1].returncode = 1  # type: ignore[union-attr]
    supervisor._reconcile_children()
    clock[0] += 3600.0
    supervisor.processes[0][1].returncode = 1  # type: ignore[union-attr]
    supervisor._reconcile_children()

    assert len(spawned) == 3


def test_a_core_fleet_member_still_fails_closed_immediately() -> None:
    clock = [0.0]
    spawned: list[_FakeProcess] = []
    spec = ProcessSpec("api", ("api",), ROOT, ROOT / "var" / "unused.log")
    supervisor = _supervisor([spec], spawned, clock)
    supervisor._log_handles["api"] = None  # type: ignore[assignment]
    supervisor.start()

    supervisor.processes[0][1].returncode = 7  # type: ignore[union-attr]
    with pytest.raises(SupervisionError) as failure:
        supervisor._reconcile_children()
    assert "exited unexpectedly with status 7" in str(failure.value)
    assert len(spawned) == 1
