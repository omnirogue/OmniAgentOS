"""Unattended provider launch fails closed when the OS sandbox is unproven.

``ProviderSessionRunner.spawn`` consults ``runner.sandbox.wrap_available``
(the same seam tests/swarm/test_provider_exec.py pins True at lines 27-30)
immediately before process creation.  With the probe unproven the launch must
REFUSE with ``provider_exec_no_sandbox_refused`` and never call the process
factory — provider CLIs are only ever unattended inside the outer Seatbelt
wrap.

``sandbox_available`` is ``lru_cache``d, so the cache is cleared around the
pin to guarantee no earlier test's positive probe leaks into this one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import omniagentos.runner.sandbox as runner_sandbox
from omniagentos.db.migrate import migrate
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.provider_exec import ProviderExecSpawnError, ProviderSessionRunner


class _FakeAdapter:
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


def test_unproven_sandbox_refuses_before_any_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "sandbox-gate.db")
    migrate(db)
    dal = SessionsDal(db)

    factory_calls: list[list[str]] = []

    def counting_factory(command: list[str], **kwargs: Any) -> Any:
        factory_calls.append(command)
        raise AssertionError("process factory must never run without a proven sandbox")

    runner = ProviderSessionRunner(
        dal,
        db_path=db,
        process_factory=counting_factory,
        adapter_resolver=lambda provider: _FakeAdapter(provider),
        poll_interval=0.01,
        kill_grace=0.01,
        reserve_account=lambda *args, **kwargs: None,
        convert_reservation=lambda *args, **kwargs: True,
        report_outcome=lambda *args, **kwargs: None,
    )

    # Pin the probe UNPROVEN at the exact seam spawn consults; sandbox_available
    # is lru_cached, so drop any earlier positive probe before and after.
    runner_sandbox.sandbox_available.cache_clear()
    monkeypatch.setattr(runner_sandbox, "sandbox_available", lambda: False)
    monkeypatch.setattr(
        runner_sandbox, "wrap_available", lambda _argv, _workspace_dir: False
    )
    try:
        with pytest.raises(ProviderExecSpawnError) as excinfo:
            runner.spawn(
                "codex",
                "model-test",
                "do the task",
                str(tmp_path),
                "btk_sandbox_gate",
                "swr_sandbox_gate",
                1.0,
                7,
                "none",
                "acct_test",
            )
    finally:
        monkeypatch.undo()
        runner_sandbox.sandbox_available.cache_clear()

    assert "provider_exec_no_sandbox_refused" in str(excinfo.value)
    # ZERO process creations — refusal happened before the factory.
    assert factory_calls == []

    # The durable session row remains inspectable and is terminal FAILED.
    session = dal.get_session(excinfo.value.session_id)
    assert session is not None
    assert session["state"] == "failed"
    assert session["pid"] is None
