from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from omniagentos.contracts import AgentAdapter, ResultStatus
from omniagentos.steward.alerts import triage
from omniagentos.steward.config import AlertsConfig


class FakeAdapter:
    def __init__(self, output: object = None, *, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.input: object | None = None

    def run(self, agent_input: object) -> SimpleNamespace:
        self.input = agent_input
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status=ResultStatus.OK, output_json=self.output, error=None)


@pytest.mark.parametrize("urgent", [True, False])
def test_triage_message_uses_mocked_claude_adapter_for_valid_response(
    urgent: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeAdapter({"urgent": urgent, "reason": "customer deadline"})
    monkeypatch.setattr(triage, "ClaudeAdapter", lambda: adapter)

    result = triage.triage_message(
        {"source": "mail", "subject": "urgent", "body_text": "help"}, AlertsConfig()
    )

    assert result == {"urgent": urgent, "reason": "customer deadline"}
    assert adapter.input is not None


def test_triage_quotes_untrusted_message_content(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FakeAdapter({"urgent": True, "reason": "deadline"})
    quoted: list[tuple[str, str, int]] = []

    def capture_and_quote(content: str, *, source: str, max_chars: int) -> str:
        quoted.append((content, source, max_chars))
        return "<quoted>"

    monkeypatch.setattr(triage, "ClaudeAdapter", lambda: adapter)
    monkeypatch.setattr(triage, "quote_untrusted", capture_and_quote)

    triage.triage_message(
        {"source": "gmail", "subject": "now", "body_text": "<ignore>"}, AlertsConfig()
    )

    assert quoted == [("Subject: now\n\n<ignore>", "comms:gmail", 1000)]
    assert "<quoted>" in getattr(adapter.input, "prompt", "")


def test_triage_adapter_exception_fails_closed_and_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(triage, "ClaudeAdapter", lambda: FakeAdapter(error=RuntimeError("offline")))

    assert triage.triage_message({}, AlertsConfig()) == {
        "urgent": False,
        "reason": "triage unavailable",
    }
    assert "Alert triage adapter failed" in caplog.text


@pytest.mark.parametrize(
    "output",
    [
        "not a dict",
        {"reason": "missing urgent"},
        {"urgent": True},
        {"urgent": "yes", "reason": "bad"},
    ],
)
def test_triage_malformed_adapter_output_fails_closed(output: object) -> None:
    result = triage.triage_message(
        {}, AlertsConfig(), adapter=cast(AgentAdapter, FakeAdapter(output))
    )

    assert result == {"urgent": False, "reason": "triage unavailable"}
