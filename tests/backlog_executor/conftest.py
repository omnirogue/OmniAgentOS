"""Fixtures for the backlog-executor suite.

`scripts/backlog-executor/` is a hyphenated script tree (not an importable
package), so the module is loaded once from its file path -- the same reality
the launchd job runs it under (`python scripts/backlog-executor/executor.py`).
Every test that exercises side-effecting paths runs inside the `sandbox`
fixture, which repoints all module-level paths at a tmp tree so a test can
never touch the live repo's var/, devtasks/, or improvement log.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "backlog-executor" / "executor.py"
_spec = importlib.util.spec_from_file_location("backlog_executor_module", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("backlog_executor_module", _mod)
_spec.loader.exec_module(_mod)


@pytest.fixture()
def executor():
    return _mod


@pytest.fixture()
def sandbox(executor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    live = tmp_path / "live"
    live.mkdir()
    paths = {
        "root": live,
        "backlog": tmp_path / "var" / "backlog",
        "log": tmp_path / "var" / "log" / "backlog-executor.log",
        "improvement": tmp_path / "var" / "improvement-log.jsonl",
        "todo": tmp_path / "devtasks" / "SWARM-EXECUTION-TODO.md",
        "playbook": tmp_path / "vault" / "playbook.md",
        "reports": tmp_path / "curator-reports",
        "prompt": tmp_path / "prompt.md",
    }
    monkeypatch.setattr(executor, "ROOT", paths["root"])
    monkeypatch.setattr(executor, "BACKLOG_DIR", paths["backlog"])
    monkeypatch.setattr(executor, "LOG_PATH", paths["log"])
    monkeypatch.setattr(executor, "IMPROVEMENT_LOG", paths["improvement"])
    monkeypatch.setattr(executor, "TODO_PATH", paths["todo"])
    monkeypatch.setattr(executor, "PLAYBOOK_PATH", paths["playbook"])
    monkeypatch.setattr(executor, "REPORTS_DIR", paths["reports"])
    monkeypatch.setattr(executor, "PROMPT_PATH", paths["prompt"])
    return paths


class FakeGit:
    """Scripted `run_git` stand-in: prefix-matched overrides, sane defaults."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self._rules: list[tuple[tuple[str, ...], Any]] = []

    def on(self, *prefix: str, result: Any = (0, "")) -> FakeGit:
        self._rules.append((prefix, result))
        return self

    def calls_for(self, subcommand: str) -> list[tuple[list[str], Path]]:
        return [(args, cwd) for args, cwd in self.calls if args and args[0] == subcommand]

    def __call__(self, args: list[str], cwd: Path, timeout: int = 600) -> tuple[int, str]:
        self.calls.append((list(args), Path(str(cwd))))
        for prefix, result in self._rules:
            if tuple(args[: len(prefix)]) == prefix:
                return result(args, cwd) if callable(result) else result
        if args[0] == "clone":
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return 0, ""
        if args[0] == "rev-parse":
            if "--abbrev-ref" in args:
                return 0, "main"
            return 0, "basesha000"
        return 0, ""


class FakeApi:
    """Scripted swarm API: records dispatches, returns a terminal run."""

    def __init__(
        self,
        status: str = "completed",
        risk_classes: tuple[str, ...] = ("none",),
        attempts_per_task: int = 1,
    ) -> None:
        self.status = status
        self.risk_classes = risk_classes
        self.attempts_per_task = attempts_per_task
        self.dispatched: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    def dispatch(self, brief: str, working_dir: str) -> str:
        self.dispatched.append((brief, working_dir))
        return "swr_test"

    def get_run(self, run_id: str) -> dict[str, Any]:
        plan = {
            "tasks": [{"id": f"t{i}", "risk_class": rc} for i, rc in enumerate(self.risk_classes)]
        }
        return {
            "run": {"status": self.status, "plan_json": json.dumps(plan)},
            "attempts": {
                f"t{i}": [{} for _ in range(self.attempts_per_task)]
                for i in range(len(self.risk_classes))
            },
        }

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class FakeNotify:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, kind: str, title: str, body: str = "", payload: Any = None) -> None:
        self.calls.append((kind, title))


@pytest.fixture()
def fake_git() -> FakeGit:
    return FakeGit()


@pytest.fixture()
def fake_api() -> FakeApi:
    return FakeApi()


@pytest.fixture()
def fake_notify() -> FakeNotify:
    return FakeNotify()


def make_runtime(
    executor,
    *,
    api: Any,
    git: Any,
    suite: Any,
    notify: Any,
    now: datetime | None = None,
) -> Any:
    return executor.Runtime(
        api=api,
        git=git,
        suite=suite,
        notify=notify,
        now_fn=(lambda: now) if now is not None else datetime.now,
        poll_seconds=0,
        item_timeout_min=1,
        suite_timeout_min=1,
    )
