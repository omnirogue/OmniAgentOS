from __future__ import annotations

from omniagentos.adapters.common import CliAdapter, Invocation, ParsedResponse
from omniagentos.contracts import AgentInput, AgentUsage


class _Adapter(CliAdapter):
    name = "kimi"

    def _command(self, *args: object, **kwargs: object) -> list[str]:
        return ["kimi"]

    def _parse(self, stdout: str):  # pragma: no cover - _log-only fixture
        raise AssertionError("not used")

    def _usage(self, input: AgentInput, parsed: ParsedResponse, wall_ms: int) -> AgentUsage:
        return AgentUsage(wall_ms=wall_ms)


def test_invocation_log_retains_spawn_exception(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    path = _Adapter()._log("run-spawn", [Invocation(exception="[Errno 2] kimi: not found")])

    assert "[Errno 2] kimi: not found" in open(path, encoding="utf-8").read()
