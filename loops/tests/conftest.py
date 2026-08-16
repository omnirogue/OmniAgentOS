"""Fixtures for the loops suite.

Runs in the LOOPS venv only (``loops/bin/loop-tests``). Everything binds to a
temporary control-plane database created by the REAL migrations — the bridge's
claims are about the production ``approvals``/``events``/``idempotency`` tables,
so testing against a hand-rolled schema would prove nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omniagentos_loops import bootstrap_repo_path  # noqa: E402

bootstrap_repo_path()

from omniagentos_loops.runtime import LoopContext  # noqa: E402
from omniagentos_loops.tools import LoopTool, ToolRegistry  # noqa: E402

from omniagentos.db.migrate import migrate  # noqa: E402
from omniagentos.db.store import SqliteStore  # noqa: E402
from omniagentos.policy import load_policy  # noqa: E402


class RecordingNotifier:
    """Stand-in for ``steward.notify.send_slack`` — records instead of posting."""

    def __init__(self) -> None:
        self.pages: list[str] = []

    def __call__(self, text: str) -> Any:
        self.pages.append(text)
        return type("NotifyResult", (), {"ok": True, "detail": "recorded"})()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "control.sqlite3")
    migrate(path)
    return path


@pytest.fixture
def store(db_path: str) -> Any:
    handle = SqliteStore(db_path)
    yield handle
    handle.close()


@pytest.fixture
def loops_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "loops"
    root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_LOOPS_ROOT", str(root))
    return root


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def make_ctx(store: Any, loops_root: Path, notifier: RecordingNotifier) -> Any:
    def factory(
        *,
        instance_id: str = "test_loop",
        template: str = "draft_approve_send",
        params: dict[str, Any] | None = None,
        tools: ToolRegistry | None = None,
    ) -> LoopContext:
        return LoopContext(
            instance_id=instance_id,
            template=template,
            params=params or {},
            store=store,
            policy_cfg=load_policy(),
            tools=tools or ToolRegistry(),
            notifier=notifier,
        )

    return factory


class Counter:
    """A callable that counts its executions — the ground truth for 'did it run'."""

    def __init__(self, result: Any = "done") -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result

    @property
    def count(self) -> int:
        return len(self.calls)


def tool(
    name: str,
    tier: Any,
    call: Any,
    *,
    key: Any = None,
    replay_on_unknown: bool = False,
) -> LoopTool:
    return LoopTool(
        name=name,
        tier=tier,
        idempotency_key=key or (lambda args: name),
        call=call,
        description=f"test tool {name}",
        replay_on_unknown=replay_on_unknown,
    )
