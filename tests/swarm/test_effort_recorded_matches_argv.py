"""O-8: swarm_attempts.effort must match the effort actually present in argv.

The router intent written at open_attempt can diverge from the value that
reaches the CLI after CBM override. provider_exec records the argv-derived
value after launch so the attempt/session rows match what was dispatched.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import omniagentos.runner.sandbox as runner_sandbox
from omniagentos.adapters.codex import CodexAdapter
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.provider_exec import (
    EFFORT_DIVERGENCE_WARNING,
    ProviderSessionRunner,
    argv_reasoning_effort,
    provider_has_effort_knob,
)
from tests.support.db_template import migrated_db


@pytest.fixture(autouse=True)
def _sandbox_wrap_available_for_fake_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model an available outer sandbox; every process in this module is fake."""
    monkeypatch.setattr(runner_sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(runner_sandbox, "wrap_available", lambda _argv, _workspace: True)


class _QueueStream:
    def __init__(self, lines: list[str] | None = None) -> None:
        self._lines = list(lines or [])
        self._i = 0

    def __iter__(self) -> _QueueStream:
        return self

    def __next__(self) -> str:
        if self._i >= len(self._lines):
            raise StopIteration
        line = self._lines[self._i]
        self._i += 1
        return line


class _FakeProcess:
    next_pid = 11000

    def __init__(self, *, lines: list[str] | None = None, returncode: int = 0) -> None:
        self.pid = _FakeProcess.next_pid
        _FakeProcess.next_pid += 1
        self.stdout = _QueueStream(lines or [])
        self.stdin = io.StringIO()
        self.returncode: int | None = returncode

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode if self.returncode is not None else 0


class _UnwrappedCodexAdapter(CodexAdapter):
    """Real codex argv construction; no Seatbelt wrap so the factory sees CLI tokens."""

    def _sandboxed_launch(
        self,
        command: list[str],
        working_dir: str,
        extra_write_roots: list[str] | None = None,
        *,
        provider: str | None = None,
        provider_config_dir: str | None = None,
    ) -> list[str]:
        del working_dir, extra_write_roots, provider, provider_config_dir
        return command

    def _parse(self, stdout: str) -> Any:
        del stdout
        return SimpleNamespace(text="ok", session_ref=None)


class _HardcodedHighCodexAdapter(_UnwrappedCodexAdapter):
    """Simulates CBM override one layer up: argv ignores requested effort."""

    def _command(self, input: Any, prompt: str, session_ref: str | None) -> list[str]:
        del input, prompt, session_ref
        return [
            "codex",
            "exec",
            "--json",
            "-m",
            "gpt-5.6-luna",
            "-c",
            'model_reasoning_effort="high"',
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-C",
            "/tmp",
            "-",
        ]


class _PlainAdapter:
    def __init__(self, provider: str) -> None:
        self.name = provider
        self.cli = provider

    def _command(self, input: Any, prompt: str, session_ref: str | None) -> list[str]:
        del input, session_ref
        return [self.cli, "--prompt", prompt]

    def _sandboxed_launch(
        self,
        command: list[str],
        working_dir: str,
        extra_write_roots: list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        del working_dir, extra_write_roots, kwargs
        return command

    def _parse(self, stdout: str) -> Any:
        del stdout
        return SimpleNamespace(text="ok", session_ref=None)


def _open_swarm_task(
    db: str, *, provider: str = "codex", effort: str | None
) -> tuple[str, str, str]:
    """Create a run + board task + live attempt; return (run_id, task_id, attempt_id)."""
    swarm = SwarmDal(db)
    try:
        run_id = str(swarm.create_run(working_dir="/tmp/ws", goal="effort o-8")["id"])
        now = utc_now_iso()
        task_id = new_id("btk")
        swarm._connection.execute(
            "INSERT INTO board_tasks "
            "(id, title, description, required_expertise_json, priority, status, "
            "claim_version, created_at, updated_at, swarm_run_id, swarm_json) "
            "VALUES (?, 't', '', '[]', 'normal', 'open', 0, ?, ?, ?, '{}')",
            (task_id, now, now, run_id),
        )
        attempt = swarm.open_attempt(
            run_id,
            task_id,
            provider=provider,
            model="model-test",
            tier="complex",
            effort=effort,
        )
        return run_id, task_id, str(attempt["id"])
    finally:
        swarm.close()


def _make_runner(
    db: str,
    *,
    adapter: Any,
    captures: list[dict[str, Any]] | None = None,
) -> ProviderSessionRunner:
    captures = captures if captures is not None else []
    dal = SessionsDal(db)

    def process_factory(command: list[str], **kwargs: Any) -> _FakeProcess:
        captures.append({"command": list(command), **kwargs})
        return _FakeProcess(lines=[], returncode=0)

    return ProviderSessionRunner(
        dal,
        db_path=db,
        process_factory=process_factory,
        adapter_resolver=lambda provider: adapter,
        poll_interval=0.01,
        kill_grace=0.01,
        getpgid=lambda pid: pid,
        killpg=lambda pgid, sig: None,
        pgid_liveness=lambda pid: False,
        command_reader=lambda pid: "",
        reserve_account=lambda *args, **kwargs: None,
        convert_reservation=lambda *args, **kwargs: True,
        report_outcome=lambda *args, **kwargs: None,
    )


def _effort_from_codex_argv(command: list[str]) -> str | None:
    """Independent parse of codex effort tokens — must not call argv_reasoning_effort."""
    pattern = re.compile(r'model_reasoning_effort\s*=\s*"?([^"\s]+)"?')
    found: str | None = None
    for i, token in enumerate(command):
        if token in ("-c", "--config") and i + 1 < len(command):
            match = pattern.search(command[i + 1])
            if match:
                found = match.group(1)
        elif token.startswith("--config=") or token.startswith("-c="):
            match = pattern.search(token.split("=", 1)[1] if False else token)
            # Search the whole token (includes model_reasoning_effort=...).
            match = pattern.search(token)
            if match:
                found = match.group(1)
    return found


# ---------------------------------------------------------------------------
# 1. Pure-function table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "command", "expected"),
    [
        (
            "codex",
            ["codex", "-c", 'model_reasoning_effort="high"'],
            "high",
        ),
        (
            "codex",
            ["codex", "--config=model_reasoning_effort=medium"],
            "medium",
        ),
        (
            "CODEX",
            ["codex", "-c", "model_reasoning_effort='xhigh'"],
            "xhigh",
        ),
        (
            "grok",
            ["grok", "--reasoning-effort", "high"],
            "high",
        ),
        (
            "grok",
            ["grok", "--reasoning-effort=low"],
            "low",
        ),
        (
            "grok",
            ["grok", "--effort", "medium"],
            "medium",
        ),
        (
            "grok",
            ["grok", "--effort=xhigh"],
            "xhigh",
        ),
        (
            "claude",
            ["claude", "--effort", "high"],
            "high",
        ),
        (
            "claude",
            ["claude", "--effort=low"],
            "low",
        ),
        ("unknown", ["cli", "--effort", "high"], None),
        ("gemini", ["gemini", "--prompt", "x"], None),
        (
            "codex",
            [
                "codex",
                "-c",
                'model_reasoning_effort="low"',
                "-c",
                'model_reasoning_effort="high"',
            ],
            "high",
        ),
        (
            "grok",
            ["grok", "--reasoning-effort", "low", "--reasoning-effort", "xhigh"],
            "xhigh",
        ),
        ("codex", ["codex", "-c"], None),
        ("grok", ["grok", "--reasoning-effort"], None),
        ("claude", ["claude", "--effort"], None),
    ],
)
def test_argv_reasoning_effort_table(
    provider: str, command: list[str], expected: str | None
) -> None:
    assert argv_reasoning_effort(command, provider) == expected


def test_provider_has_effort_knob_matches_extractors() -> None:
    assert provider_has_effort_knob("codex") is True
    assert provider_has_effort_knob("Grok") is True
    assert provider_has_effort_knob("claude") is True
    assert provider_has_effort_knob("gemini") is False
    assert provider_has_effort_knob("kimi") is False
    assert provider_has_effort_knob("qwen") is False
    assert provider_has_effort_knob("nope") is False


# ---------------------------------------------------------------------------
# 2. Decisive: recorded == argv (parametrised efforts)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["xhigh", "medium", "low"])
def test_recorded_effort_matches_argv(tmp_path: Path, effort: str) -> None:
    db = str(tmp_path / "effort-match.db")
    db = migrated_db(CollabStore, db)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run_id, task_id, attempt_id = _open_swarm_task(db, effort=effort)
    captures: list[dict[str, Any]] = []
    runner = _make_runner(db, adapter=_UnwrappedCodexAdapter(), captures=captures)

    session_id = runner.spawn(
        provider="codex",
        model="gpt-5.6-luna",
        prompt="do the task",
        working_dir=str(workspace),
        board_task_id=task_id,
        swarm_run_id=run_id,
        budget_usd_max=None,
        idle_minutes=5,
        risk_class="none",
        effort=effort,
    )
    assert session_id
    assert captures, "process_factory must see a launch"
    argv_effort = _effort_from_codex_argv(captures[0]["command"])
    assert argv_effort == effort

    swarm = SwarmDal(db)
    try:
        row = swarm._connection.execute(
            "SELECT effort FROM swarm_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        assert row is not None
        assert row["effort"] == argv_effort == effort
    finally:
        swarm.close()

    session = SessionsDal(db).get_session(session_id)
    assert session is not None
    assert session.get("effort") == effort


# ---------------------------------------------------------------------------
# 3. O-8 reproduction: argv wins over open_attempt intent
# ---------------------------------------------------------------------------


def test_o8_argv_overrides_open_attempt_intent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = str(tmp_path / "effort-o8.db")
    db = migrated_db(CollabStore, db)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run_id, task_id, attempt_id = _open_swarm_task(db, effort="xhigh")
    runner = _make_runner(db, adapter=_HardcodedHighCodexAdapter())

    with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.provider_exec"):
        session_id = runner.spawn(
            provider="codex",
            model="gpt-5.6-luna",
            prompt="do the task",
            working_dir=str(workspace),
            board_task_id=task_id,
            swarm_run_id=run_id,
            budget_usd_max=None,
            idle_minutes=5,
            risk_class="none",
            effort="xhigh",
        )

    assert session_id
    swarm = SwarmDal(db)
    try:
        row = swarm._connection.execute(
            "SELECT effort FROM swarm_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        assert row is not None
        assert row["effort"] == "high"
    finally:
        swarm.close()

    warning_text = " ".join(record.getMessage() for record in caplog.records)
    assert EFFORT_DIVERGENCE_WARNING in warning_text
    assert "xhigh" in warning_text
    assert "high" in warning_text


@pytest.mark.parametrize(
    ("intent", "dispatched"),
    [("xhigh", "medium"), ("low", "xhigh"), ("medium", "high")],
)
def test_recorded_effort_follows_argv_when_intent_diverges(
    tmp_path: Path, intent: str, dispatched: str
) -> None:
    """Open-attempt intent diverges from spawn effort; argv and rows follow dispatched.

    Mirrors CBM override: scheduler open_attempt(effort=intent), then
    spawn.py replaces request.effort with the CBM value before provider_exec
    builds argv. Uses the real CodexAdapter._command path.
    """
    db = str(tmp_path / "effort-diverge.db")
    db = migrated_db(CollabStore, db)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run_id, task_id, attempt_id = _open_swarm_task(db, effort=intent)
    captures: list[dict[str, Any]] = []
    runner = _make_runner(db, adapter=_UnwrappedCodexAdapter(), captures=captures)

    session_id = runner.spawn(
        provider="codex",
        model="gpt-5.6-luna",
        prompt="do the task",
        working_dir=str(workspace),
        board_task_id=task_id,
        swarm_run_id=run_id,
        budget_usd_max=None,
        idle_minutes=5,
        risk_class="none",
        effort=dispatched,
    )
    assert session_id
    assert captures, "process_factory must see a launch"
    argv_effort = _effort_from_codex_argv(captures[0]["command"])
    assert argv_effort == dispatched, (
        f"argv effort {argv_effort!r} must equal dispatched {dispatched!r}"
    )
    assert argv_effort != intent, f"argv effort {argv_effort!r} must diverge from intent {intent!r}"

    swarm = SwarmDal(db)
    try:
        row = swarm._connection.execute(
            "SELECT effort FROM swarm_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        assert row is not None
        assert row["effort"] == argv_effort, (
            f"swarm_attempts.effort {row['effort']!r} must match argv "
            f"{argv_effort!r} (dispatched={dispatched!r})"
        )
        assert row["effort"] != intent, (
            f"swarm_attempts.effort must not stay at open_attempt intent "
            f"{intent!r}; got {row['effort']!r}"
        )
    finally:
        swarm.close()

    session = SessionsDal(db).get_session(session_id)
    assert session is not None
    assert session.get("effort") == dispatched, (
        f"sessions.effort {session.get('effort')!r} must equal dispatched {dispatched!r}"
    )


# ---------------------------------------------------------------------------
# 4. Knobless provider leaves scheduler effort unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["gemini", "kimi"])
def test_knobless_provider_leaves_attempt_effort_unchanged(tmp_path: Path, provider: str) -> None:
    db = str(tmp_path / f"effort-knobless-{provider}.db")
    db = migrated_db(CollabStore, db)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run_id, task_id, attempt_id = _open_swarm_task(db, provider=provider, effort="xhigh")
    runner = _make_runner(db, adapter=_PlainAdapter(provider))

    session_id = runner.spawn(
        provider=provider,
        model="model-test",
        prompt="do the task",
        working_dir=str(workspace),
        board_task_id=task_id,
        swarm_run_id=run_id,
        budget_usd_max=None,
        idle_minutes=5,
        risk_class="none",
        effort="xhigh",
    )
    assert session_id

    swarm = SwarmDal(db)
    try:
        row = swarm._connection.execute(
            "SELECT effort FROM swarm_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        assert row is not None
        assert row["effort"] == "xhigh"
    finally:
        swarm.close()


# ---------------------------------------------------------------------------
# 5. Telemetry must never fail spawn
# ---------------------------------------------------------------------------


def test_telemetry_failure_does_not_fail_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "effort-telemetry.db")
    db = migrated_db(CollabStore, db)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run_id, task_id, _attempt_id = _open_swarm_task(db, effort="high")

    def boom(self: Any, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("forced telemetry failure")

    monkeypatch.setattr(SwarmDal, "record_attempt_usage", boom)
    runner = _make_runner(db, adapter=_UnwrappedCodexAdapter())

    session_id = runner.spawn(
        provider="codex",
        model="gpt-5.6-luna",
        prompt="do the task",
        working_dir=str(workspace),
        board_task_id=task_id,
        swarm_run_id=run_id,
        budget_usd_max=None,
        idle_minutes=5,
        risk_class="none",
        effort="high",
    )
    assert isinstance(session_id, str)
    assert session_id.startswith("ses_")
