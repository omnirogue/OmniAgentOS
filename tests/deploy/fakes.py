"""Fake runners for the deploy executor tests. No process, socket, or file touched."""

from __future__ import annotations

from omniagentos.deploy.contracts import RunResult


class FakeRunner:
    """Records every command and returns canned results.

    ``failures`` maps a substring of a command to the RunResult to return for it;
    everything else succeeds.
    """

    def __init__(
        self,
        failures: dict[str, RunResult] | None = None,
        default: RunResult | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.failures = failures or {}
        self.default = default or RunResult(exit_code=0, stdout="ok")

    def __call__(self, command: str) -> RunResult:
        self.commands.append(command)
        for needle, result in self.failures.items():
            if needle in command:
                return result
        return self.default


class RaisingRunner:
    """A runner whose transport blows up on a given command substring."""

    def __init__(self, boom_on: str, exc: Exception | None = None) -> None:
        self.commands: list[str] = []
        self.boom_on = boom_on
        self.exc = exc or ConnectionResetError("ssh channel closed")

    def __call__(self, command: str) -> RunResult:
        self.commands.append(command)
        if self.boom_on in command:
            raise self.exc
        return RunResult(exit_code=0)
