from __future__ import annotations

import subprocess

import pytest

from omniagentos.agentless.verify import run_tests


def test_operator_test_command_executes_as_argv_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("omniagentos.agentless.verify.subprocess.run", fake_run)

    returncode, tail, _seconds = run_tests(
        "/tmp/project",
        "python -m pytest -q tests/agentless",
        30,
    )

    assert returncode == 0
    assert "ok" in tail
    assert calls == [
        (
            ["python", "-m", "pytest", "-q", "tests/agentless"],
            {
                "cwd": "/tmp/project",
                "capture_output": True,
                "text": True,
                "timeout": 30,
            },
        )
    ]
